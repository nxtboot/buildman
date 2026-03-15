# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>
# pylint: disable=C0302

"""Boss side of the distributed build protocol

Manages SSH connections to remote workers and communicates using the JSON-lines
protocol defined in worker.py. Each RemoteWorker wraps a persistent SSH process
whose stdin/stdout carry the protocol messages.

Typical usage:
    w = RemoteWorker('myhost', buildman_path='buildman')
    w.start()                      # launches ssh, waits for 'ready'
    w.setup()                      # worker creates git repo
    w.push_source(git_dir, ref)    # git push to the worker's repo
    w.build('sandbox', commit='abc123', commit_upto=0)
    result = w.recv()              # {'resp': 'build_result', ...}
    w.quit()
"""

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time

from buildman import builderthread
from buildman import worker as worker_mod
from u_boot_pylib import command
from u_boot_pylib import tools
from u_boot_pylib import tout

# SSH options shared with machine.py
SSH_OPTS = [
    '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=accept-new',
]

# Per-build timeout in seconds. If a worker doesn't respond within this
# time, the boss assumes the worker is dead or hung and stops using it.
BUILD_TIMEOUT = 300

# Interval in seconds between status summaries in the boss log
STATUS_INTERVAL = 60


class BossError(Exception):
    """Error communicating with a remote worker"""


class WorkerBusy(BossError):
    """Worker machine is already in use by another boss"""


def _run_ssh(hostname, remote_cmd, timeout=10):
    """Run a one-shot SSH command on a remote host

    Args:
        hostname (str): SSH hostname
        remote_cmd (str): Shell command to run on the remote host
        timeout (int): SSH connect timeout in seconds

    Returns:
        str: stdout from the command

    Raises:
        BossError: if the command fails
    """
    ssh_cmd = [
        'ssh',
        '-o', f'ConnectTimeout={timeout}',
    ] + SSH_OPTS + [hostname, '--', remote_cmd]
    try:
        result = command.run_pipe(
            [ssh_cmd], capture=True, capture_stderr=True,
            raise_on_error=True)
        return result.stdout.strip() if result.stdout else ''
    except command.CommandExc as exc:
        raise BossError(f'SSH command failed on {hostname}: {exc}') from exc


def kill_workers(machines):
    """Kill stale worker processes and remove lock files on remote machines

    Connects to each machine via SSH, kills any running worker processes
    and removes the lock file. Useful for cleaning up after a failed or
    interrupted distributed build.

    Args:
        machines (list of str): SSH hostnames to clean up

    Returns:
        int: 0 on success
    """

    results = {}
    lock = threading.Lock()

    def _kill_one(hostname):
        kill_script = ('pids=$(pgrep -f "[p]ython3.*--worker" 2>/dev/null); '
            'if [ -n "$pids" ]; then '
            '  kill $pids 2>/dev/null; '
            '  echo "killed $pids"; '
            'else '
            '  echo "no workers"; '
            'fi; '
            'rm -f ~/dev/.bm-worker/.lock'
        )
        try:
            output = _run_ssh(hostname, kill_script)
            with lock:
                results[hostname] = output
        except BossError as exc:
            with lock:
                results[hostname] = f'FAILED: {exc}'

    threads = []
    for hostname in machines:
        thr = threading.Thread(target=_kill_one, args=(hostname,))
        thr.start()
        threads.append(thr)
    for thr in threads:
        thr.join()

    for hostname, output in sorted(results.items()):
        print(f'  {hostname}: {output}')
    return 0


class RemoteWorker:  # pylint: disable=R0902
    """Manages one SSH connection to a remote buildman worker

    The startup sequence is:
        1. init_git() - create a bare git repo on the remote via one-shot SSH
        2. push_source() - git push the local tree to the remote repo
        3. start() - launch the worker from the pushed tree

    This ensures the worker runs the same version of buildman as the boss.

    Attributes:
        hostname (str): SSH hostname (user@host or just host)
        nthreads (int): Number of build threads the worker reported
        git_dir (str): Path to the worker's git directory
        work_dir (str): Path to the worker's work directory
    """

    def __init__(self, hostname, timeout=10, name=None):
        """Create a new remote worker connection

        Args:
            hostname (str): SSH hostname
            timeout (int): SSH connect timeout in seconds
            name (str or None): Short display name, defaults to hostname
        """
        self.hostname = hostname
        self.name = name or hostname
        self.timeout = timeout
        self.nthreads = 0
        self.slots = 1
        self.max_boards = 0
        self.bogomips = 0.0
        self.git_dir = ''
        self.work_dir = ''
        self.toolchains = {}
        self.closing = False
        self.bytes_sent = 0
        self.bytes_recv = 0
        self._proc = None
        self._stderr_lines = []
        self._stderr_thread = None

    def init_git(self, work_dir='~/dev/.bm-worker'):
        """Ensure a git repo exists on the remote host via one-shot SSH

        Reuses an existing repo if present, so that subsequent pushes
        only transfer the delta. Creates a lock file to prevent two
        bosses from using the same worker simultaneously. A lock is
        considered stale if no worker process is running.

        Args:
            work_dir (str): Fixed path for the work directory

        Raises:
            WorkerBusy: if another boss holds the lock
            BossError: if the SSH command fails
        """
        lock = f'{work_dir}/.lock'
        init_script = (f'mkdir -p {work_dir} && '
            # Check for lock — stale if no worker process is running
            f'if [ -f {lock} ]; then '
            f'  if pgrep -f "[p]ython3.*--worker" >/dev/null 2>&1; then '
            f'    echo BUSY; exit 0; '
            f'  fi; '
            f'  rm -f {lock}; '
            f'fi && '
            # Create lock and init git
            f'date +%s > {lock} && '
            f'(test -d {work_dir}/.git || git init -q {work_dir}) && '
            f'git -C {work_dir} config '
            f'receive.denyCurrentBranch updateInstead && '
            f'echo {work_dir}'
        )
        output = _run_ssh(self.hostname, init_script, self.timeout)
        if not output:
            raise BossError(
                f'init_git on {self.hostname} returned no work directory')
        last_line = output.splitlines()[-1].strip()
        if last_line == 'BUSY':
            raise WorkerBusy(f'{self.hostname} is busy (locked)')
        self.work_dir = last_line
        self.git_dir = os.path.join(self.work_dir, '.git')

    def start(self, debug=False):
        """Start the worker from the pushed source tree

        Launches the worker using the buildman from the pushed git tree.
        The source must already have been pushed via init_git() and
        push_source().

        A background thread forwards the worker's stderr to the boss's
        stderr, prefixed with the machine name, so that debug messages
        and errors are always visible.

        Args:
            debug (bool): True to pass -D to the worker for tracebacks

        Raises:
            BossError: if the SSH connection or worker startup fails
        """
        if not self.work_dir:
            raise BossError(f'No work_dir on {self.hostname} '
                f'(call init_git and push_source first)')
        worker_cmd = 'python3 tools/buildman/main.py --worker'
        if debug:
            worker_cmd += ' -D'
        ssh_cmd = [
            'ssh',
            '-o', f'ConnectTimeout={self.timeout}',
        ] + SSH_OPTS + [
            self.hostname, '--',
            f'cd {self.work_dir} && git checkout -qf work && '
            f'{worker_cmd}',
        ]
        try:
            # pylint: disable=R1732
            self._proc = subprocess.Popen(
                ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except OSError as exc:
            raise BossError(
                f'Failed to start SSH to {self.hostname}: {exc}') from exc

        # Forward worker stderr in a background thread so debug messages
        # and errors are always visible
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(
            target=self._forward_stderr, daemon=True)
        self._stderr_thread.start()

        resp = self._recv()
        if resp.get('resp') != 'ready':
            self.close()
            raise BossError(
                f'Worker on {self.hostname} did not send ready: {resp}')
        self.nthreads = resp.get('nthreads', 1)
        self.slots = resp.get('slots', 1)
        if not self.max_boards:
            self.max_boards = self.nthreads

    def _forward_stderr(self):
        """Forward worker stderr to boss stderr with machine name prefix

        Runs in a background thread. Saves lines for _get_stderr() too.
        """
        try:
            for raw in self._proc.stderr:
                line = raw.decode('utf-8', errors='replace').rstrip('\n')
                if line:
                    self._stderr_lines.append(line)
                    sys.stderr.write(f'[{self.name}] {line}\n')
                    sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def _send(self, obj):
        """Send a JSON command to the worker

        Args:
            obj (dict): Command object to send

        Raises:
            BossError: if the SSH process is not running
        """
        if not self._proc or self._proc.poll() is not None:
            raise BossError(f'Worker on {self.hostname} is not running')
        line = json.dumps(obj) + '\n'
        data = line.encode('utf-8')
        self.bytes_sent += len(data)
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _recv(self):
        """Read the next protocol response from the worker

        Reads lines from stdout, skipping any that don't start with the
        'BM> ' prefix (e.g. SSH banners).

        Returns:
            dict: Parsed JSON response

        Raises:
            BossError: if the worker closes the connection or sends bad data
        """
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                stderr = self._get_stderr()
                raise BossError(f'Worker on {self.hostname} closed connection'
                    f'{": " + stderr if stderr else ""}')
            self.bytes_recv += len(raw)
            line = raw.decode('utf-8', errors='replace').rstrip('\n')
            if line.startswith(worker_mod.RESPONSE_PREFIX):
                payload = line[len(worker_mod.RESPONSE_PREFIX):]
                try:
                    return json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise BossError(
                        f'Bad JSON from {self.hostname}: {exc}') from exc

    def _get_stderr(self):
        """Get the last stderr line from the worker

        Waits briefly for the stderr forwarding thread to finish
        collecting output, then returns the last non-empty line.

        Returns:
            str: Last non-empty line of stderr, or empty string
        """
        if hasattr(self, '_stderr_thread'):
            self._stderr_thread.join(timeout=2)
        for line in reversed(self._stderr_lines):
            if line.strip():
                return line.strip()
        return ''

    def push_source(self, local_git_dir, refspec):
        """Push source code to the worker's git repo

        Uses 'git push' over SSH to send commits to the worker.

        Args:
            local_git_dir (str): Path to local git directory
            refspec (str): Git refspec to push (e.g. 'HEAD:refs/heads/work')

        Raises:
            BossError: if the push fails
        """
        if not self.git_dir:
            raise BossError(
                f'No git_dir on {self.hostname} (call init_git first)')
        push_url = f'{self.hostname}:{self.git_dir}'
        try:
            command.run_pipe([['git', 'push', '--force', push_url, refspec]],
                capture=True, capture_stderr=True,
                raise_on_error=True, cwd=local_git_dir)
        except command.CommandExc as exc:
            raise BossError(
                f'git push to {self.hostname} failed: {exc}') from exc

    def configure(self, settings):
        """Send build settings to the worker

        Sends settings that affect how make is invoked (verbose, no_lto,
        allow_missing, etc.). Must be called after start() and before
        any build commands.

        Args:
            settings (dict): Build settings, e.g.:
                verbose_build (bool): Run make with V=1
                allow_missing (bool): Pass BINMAN_ALLOW_MISSING=1
                no_lto (bool): Pass NO_LTO=1
                reproducible_builds (bool): Pass SOURCE_DATE_EPOCH=0
                warnings_as_errors (bool): Pass KCFLAGS=-Werror
                mrproper (bool): Run make mrproper before config
                fallback_mrproper (bool): Retry with mrproper on failure

        Raises:
            BossError: if the worker rejects the settings
        """
        self._send({'cmd': 'configure', 'settings': settings})
        resp = self._recv()
        if resp.get('resp') != 'configure_done':
            raise BossError(
                f'Worker on {self.hostname} rejected configure: {resp}')

    def build_boards(self, boards, commits):
        """Send a build_boards command to the worker

        Tells the worker to build all boards for each commit. The
        worker handles checkout scheduling, parallelism and -j
        calculation internally.

        Args:
            boards (list of dict): Board info dicts with keys:
                board (str): Board target name
                defconfig (str): Defconfig target
                env (dict): Extra environment variables
            commits (list): Commit hashes in order, or [None] for
                current source
        """
        self._send({
            'cmd': 'build_boards',
            'boards': boards,
            'commits': commits,
        })

    def build_prepare(self, commits):
        """Send a build_prepare command to the worker

        Creates the Builder and worktrees. Follow with build_board()
        calls, then build_done().

        Args:
            commits (list): Commit hashes in order, or [None] for
                current source
        """
        self._send({'cmd': 'build_prepare', 'commits': commits,
                    'max_boards': self.max_boards})

    def build_board(self, board, arch):
        """Send a build_board command to add one board to the worker

        Args:
            board (str): Board target name
            arch (str): Board architecture
        """
        self._send({
            'cmd': 'build_board',
            'board': board,
            'arch': arch,
        })

    def build_done(self):
        """Tell the worker no more boards are coming"""
        self._send({'cmd': 'build_done'})

    def recv(self):
        """Receive the next response from the worker

        Returns:
            dict: Parsed JSON response
        """
        return self._recv()

    def quit(self):
        """Tell the worker to quit, remove the lock and close"""
        try:
            self._send({'cmd': 'quit'})
            resp = self._recv()
        except BossError:
            resp = {}
        self.close()
        self.remove_lock()
        return resp

    def remove_lock(self):
        """Remove the lock file from the remote machine"""
        if self.work_dir:
            try:
                _run_ssh(self.hostname,
                         f'rm -f {self.work_dir}/.lock', self.timeout)
            except BossError:
                pass

    def close(self):
        """Close the SSH connection

        Closes stdin first so SSH can flush any pending data (e.g. a
        quit command) to the remote, then waits briefly for SSH to
        exit on its own before terminating it.
        """
        if self._proc:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def __repr__(self):
        status = 'running' if self._proc else 'stopped'
        return (f'RemoteWorker({self.hostname}, '
                f'nthreads={self.nthreads}, {status})')

    def __del__(self):
        self.close()


def _format_bytes(nbytes):
    """Format a byte count as a human-readable string"""
    if nbytes < 1024:
        return f'{nbytes}B'
    if nbytes < 1024 * 1024:
        return f'{nbytes / 1024:.1f}KB'
    return f'{nbytes / (1024 * 1024):.1f}MB'


class _BossLog:
    """Central boss log for distributed builds

    Logs major events and periodic per-worker status summaries
    to boss.log in the builder output directory.
    """

    def __init__(self, base_dir):
        os.makedirs(base_dir, exist_ok=True)
        path = os.path.join(base_dir, '.buildman.log')
        # pylint: disable=R1732
        self._logf = open(path, 'w', encoding='utf-8')
        self._lock = threading.Lock()
        self._stats = {}
        self._timer = None
        self._closed = False

    def log(self, msg):
        """Write a timestamped log entry"""
        with self._lock:
            if self._logf:
                stamp = datetime.datetime.now().strftime('%H:%M:%S')
                self._logf.write(f'{stamp} {msg}\n')
                self._logf.flush()

    def init_worker(self, wrk):
        """Register a worker for status tracking"""
        with self._lock:
            self._stats[wrk.name] = {
                'sent': 0,
                'recv': 0,
                'load_avg': 0.0,
                'nthreads': wrk.nthreads,
            }

    def record_sent(self, wrk_name, count=1):
        """Record boards sent to a worker"""
        with self._lock:
            if wrk_name in self._stats:
                self._stats[wrk_name]['sent'] += count

    def record_recv(self, wrk_name, load_avg=0.0):
        """Record a reply received from a worker"""
        with self._lock:
            if wrk_name in self._stats:
                self._stats[wrk_name]['recv'] += 1
                self._stats[wrk_name]['load_avg'] = load_avg

    def log_status(self):
        """Log a status summary for all workers"""
        with self._lock:
            parts = []
            total_load = 0.0
            total_threads = 0
            total_done = 0
            total_sent = 0
            for name, st in self._stats.items():
                nthreads = st['nthreads']
                cpu_pct = (st['load_avg'] / nthreads * 100 if nthreads else 0)
                parts.append(f'{name}:done={st["recv"]}/{st["sent"]}'
                    f' cpu={cpu_pct:.0f}%')
                total_load += st['load_avg']
                total_threads += nthreads
                total_done += st['recv']
                total_sent += st['sent']
            total_cpu = (total_load / total_threads * 100
                         if total_threads else 0)
            parts.append(f'TOTAL:done={total_done}/{total_sent}'
                         f' cpu={total_cpu:.0f}%')
            if self._logf:
                stamp = datetime.datetime.now().strftime('%H:%M:%S')
                self._logf.write(f'{stamp} STATUS {", ".join(parts)}\n')
                self._logf.flush()

    def start_timer(self):
        """Start the periodic status timer"""
        def _tick():
            if not self._closed:
                self.log_status()
                self._timer = threading.Timer(STATUS_INTERVAL, _tick)
                self._timer.daemon = True
                self._timer.start()
        self._timer = threading.Timer(STATUS_INTERVAL, _tick)
        self._timer.daemon = True
        self._timer.start()

    def close(self):
        """Stop the timer and close the log file"""
        self._closed = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        with self._lock:
            if self._logf:
                self._logf.close()
                self._logf = None


def split_boards(board_selected, toolchains):
    """Split boards between local and remote machines

    Boards whose architecture has a toolchain on at least one remote machine
    are assigned to remote workers. The rest stay local.

    Args:
        board_selected (dict): target_name -> Board for all selected boards
        toolchains (dict): Architecture -> gcc path on remote machines.
            Combined from all machines.

    Returns:
        tuple:
            dict: target_name -> Board for local builds
            dict: target_name -> Board for remote builds
    """
    remote_archs = set(toolchains.keys()) if toolchains else set()
    local = {}
    remote = {}
    for name, brd in board_selected.items():
        if brd.arch in remote_archs:
            remote[name] = brd
        else:
            local[name] = brd
    return local, remote


def _write_remote_result(builder, resp, board_selected, hostname):
    """Write a remote build result and update builder progress

    Creates the same directory structure and files that BuilderThread would
    create for a local build, then calls builder.process_result() to
    update the progress display.

    Args:
        builder (Builder): Builder object
        resp (dict): build_result response from a worker
        board_selected (dict): target_name -> Board, for looking up
            the Board object
        hostname (str): Remote machine that built this board
    """
    board = resp.get('board', '')
    commit_upto = resp.get('commit_upto', 0)
    return_code = resp.get('return_code', 1)
    stderr = resp.get('stderr', '')

    build_dir = builder.get_build_dir(commit_upto, board)
    builderthread.mkdir(build_dir, parents=True)

    tools.write_file(os.path.join(build_dir, 'done'),
        f'{return_code}\n', binary=False)

    err_path = os.path.join(build_dir, 'err')
    if stderr:
        tools.write_file(err_path, stderr, binary=False)
    elif os.path.exists(err_path):
        os.remove(err_path)

    tools.write_file(os.path.join(build_dir, 'log'),
        resp.get('stdout', ''), binary=False)

    sizes = resp.get('sizes', {})
    if sizes.get('raw'):
        # Strip any header line (starts with 'text') in case the worker
        # sends raw size output including the header
        raw = sizes['raw']
        lines = raw.splitlines()
        if lines and lines[0].lstrip().startswith('text'):
            raw = '\n'.join(lines[1:])
        if raw.strip():
            tools.write_file(os.path.join(build_dir, 'sizes'),
                raw, binary=False)

    # Update the builder's progress display
    brd = board_selected.get(board)
    if brd:
        result = command.CommandResult(stderr=stderr, return_code=return_code)
        result.brd = brd
        result.commit_upto = commit_upto
        result.already_done = False
        result.kconfig_reconfig = False
        result.remote = hostname
        builder.process_result(result)


class DemandState:  # pylint: disable=R0903
    """Mutable state for a demand-driven worker build

    Tracks how many boards have been sent, received and are in-flight
    for a single worker during demand-driven dispatch.

    Attributes:
        sent: Total boards sent to the worker
        in_flight: Boards currently being built (sent - completed)
        expected: Total results expected (sent * ncommits)
        received: Results received so far
        board_results: Per-board result count (target -> int)
        ncommits: Number of commits being built
        grab_func: Callable(wrk, count) -> list of Board to get more
            boards from the shared pool
    """

    def __init__(self, sent, ncommits, grab_func):
        self.sent = sent
        self.in_flight = sent
        self.expected = sent * ncommits
        self.received = 0
        self.board_results = {}
        self.ncommits = ncommits
        self.grab_func = grab_func


class _DispatchContext:
    """Shared infrastructure for dispatching builds to workers

    Manages per-worker log files, worktree progress tracking, reader
    threads, and result processing. Used by both _dispatch_jobs() and
    _dispatch_demand() to avoid duplicating this infrastructure.
    """

    def __init__(self, workers, builder, board_selected, boss_log):
        self.builder = builder
        self.board_selected = board_selected
        self.boss_log = boss_log
        self._worktree_counts = {}
        self._worktree_lock = threading.Lock()

        # Open a log file per worker
        os.makedirs(builder.base_dir, exist_ok=True)
        self.log_files = {}
        for wrk in workers:
            path = os.path.join(builder.base_dir, f'worker-{wrk.name}.log')
            self.log_files[wrk] = open(  # pylint: disable=R1732
                path, 'w', encoding='utf-8')

    def log(self, wrk, direction, msg):
        """Write a timestamped entry to a worker's log file"""
        logf = self.log_files.get(wrk)
        if logf:
            stamp = datetime.datetime.now().strftime('%H:%M:%S')
            logf.write(f'{stamp} {direction} {msg}\n')
            logf.flush()

    def update_progress(self, resp, wrk):
        """Handle worktree progress messages from a worker

        Args:
            resp (dict): Response from the worker
            wrk (RemoteWorker): Worker that sent the response

        Returns:
            bool: True if the response was a progress message
        """
        resp_type = resp.get('resp')
        if resp_type == 'build_started':
            with self._worktree_lock:
                num = resp.get('num_threads', wrk.nthreads)
                self._worktree_counts[wrk.name] = (self._worktree_counts.get(
                        wrk.name, (0, num))[0], num)
            return True
        if resp_type == 'worktree_created':
            with self._worktree_lock:
                done, total = self._worktree_counts.get(
                    wrk.name, (0, wrk.nthreads))
                self._worktree_counts[wrk.name] = (done + 1, total)
            self._refresh_progress()
            return True
        return False

    def _refresh_progress(self):
        """Update the builder's progress string from worktree counts"""
        with self._worktree_lock:
            parts = []
            for name, (done, total) in sorted(self._worktree_counts.items()):
                if done < total:
                    parts.append(f'{name} {done}/{total}')
            self.builder.progress = ', '.join(parts)
            if self.builder.progress:
                self.builder.process_result(None)

    def start_reader(self, wrk):
        """Start a background reader thread for a worker

        Returns:
            queue.Queue: Queue that receives (status, value) tuples
        """
        recv_q = queue.Queue()

        def _reader():
            while True:
                try:
                    resp = wrk.recv()
                    recv_q.put(('ok', resp))
                except BossError as exc:
                    recv_q.put(('error', exc))
                    break
                except Exception:  # pylint: disable=W0718
                    recv_q.put(('error', BossError(
                        f'Worker on {wrk.name} connection lost')))
                    break

        threading.Thread(target=_reader, daemon=True).start()
        return recv_q

    def recv(self, wrk, recv_q):
        """Get next response from queue with timeout

        Returns:
            dict or None: Response, or None on error
        """
        try:
            status, val = recv_q.get(timeout=BUILD_TIMEOUT)
        except queue.Empty:
            self.log(wrk, '!!', f'Worker timed out after {BUILD_TIMEOUT}s')
            if not wrk.closing:
                print(f'\n  Error from {wrk.name}: timed out')
            return None
        if status == 'error':
            self.log(wrk, '!!', str(val))
            if not wrk.closing:
                print(f'\n  Error from {wrk.name}: {val}')
            return None
        resp = val
        self.log(wrk, '<<', json.dumps(resp, separators=(',', ':')))
        if resp.get('resp') == 'error':
            if not wrk.closing:
                print(f'\n  Worker error on {wrk.name}: '
                      f'{resp.get("msg", "unknown")}')
            return None
        return resp

    def write_result(self, wrk, resp):
        """Write a build result and update progress

        Returns:
            bool: True on success, False on error
        """
        if self.boss_log:
            self.boss_log.record_recv(wrk.name, resp.get('load_avg', 0.0))
        try:
            _write_remote_result(
                self.builder, resp, self.board_selected, wrk.name)
        except Exception as exc:  # pylint: disable=W0718
            self.log(wrk, '!!', f'unexpected: {exc}')
            print(f'\n  Unexpected error on {wrk.name}: {exc}')
            return False
        return True

    def wait_for_prepare(self, wrk, recv_q):
        """Wait for build_prepare_done, handling progress messages

        Returns:
            bool: True if prepare succeeded
        """
        while True:
            resp = self.recv(wrk, recv_q)
            if resp is None:
                return False
            if self.update_progress(resp, wrk):
                continue
            resp_type = resp.get('resp')
            if resp_type == 'build_prepare_done':
                return True
            if resp_type == 'heartbeat':
                continue
            self.log(wrk, '!!', f'unexpected during prepare: {resp_type}')
            return False

    @staticmethod
    def send_batch(wrk, boards):
        """Send a batch of boards to a worker

        Returns:
            int: Number of boards sent, or -1 on error
        """
        for brd in boards:
            try:
                wrk.build_board(brd.target, brd.arch)
            except BossError:
                return -1
        return len(boards)

    def collect_results(self, wrk, recv_q, state):
        """Collect results and send more boards as threads free up

        Args:
            wrk (RemoteWorker): Worker to collect from
            recv_q (queue.Queue): Response queue from start_reader()
            state (DemandState): Mutable build state for this worker
        """
        while state.received < state.expected:
            resp = self.recv(wrk, recv_q)
            if resp is None:
                return False
            resp_type = resp.get('resp')
            if resp_type == 'heartbeat':
                continue
            if resp_type == 'build_done':
                return True
            if resp_type != 'build_result':
                continue

            if not self.write_result(wrk, resp):
                return False
            state.received += 1

            target = resp.get('board')
            results = state.board_results
            results[target] = results.get(target, 0) + 1
            if results[target] == state.ncommits:
                state.in_flight -= 1
                if state.in_flight < wrk.max_boards:
                    more = state.grab_func(wrk, 1)
                    if more and self.send_batch(wrk, more) > 0:
                        state.sent += 1
                        state.in_flight += 1
                        state.expected += state.ncommits
                        if self.boss_log:
                            self.boss_log.record_sent(
                                wrk.name, state.ncommits)
        return True

    def recv_one(self, wrk, recv_q):
        """Receive one build result, skipping progress messages

        Returns:
            bool: True to continue, False to stop this worker
        """
        while True:
            resp = self.recv(wrk, recv_q)
            if resp is None:
                return False
            if self.update_progress(resp, wrk):
                continue
            resp_type = resp.get('resp')
            if resp_type == 'heartbeat':
                continue
            if resp_type == 'build_done':
                nexc = resp.get('exceptions', 0)
                if nexc:
                    self.log(wrk, '!!', f'worker finished with {nexc} '
                             f'thread exception(s)')
                return False
            if resp_type == 'build_result':
                return self.write_result(wrk, resp)
            return True

    def close(self):
        """Close all log files and the boss log"""
        for logf in self.log_files.values():
            logf.close()
        if self.boss_log:
            self.boss_log.log_status()
            self.boss_log.log('dispatch: end')
            self.boss_log.close()


class WorkerPool:
    """Manages a pool of remote workers for distributed builds

    Handles starting workers, pushing source, distributing build jobs
    and collecting results.

    Attributes:
        workers (list of RemoteWorker): Active workers
    """

    def __init__(self, machines):
        """Create a worker pool from available machines

        Args:
            machines (list of Machine): Available machines from MachinePool
        """
        self.workers = []
        self._machines = machines
        self._boss_log = None

    def start_all(self, git_dir, refspec, debug=False, settings=None):
        """Start workers on all machines

        Uses a three-phase approach so that each worker runs the same
        version of buildman as the boss:
            1. Create git repos on all machines (parallel)
            2. Push source to all repos (parallel)
            3. Start workers from pushed source (parallel)
            4. Send build settings to all workers (parallel)

        Args:
            git_dir (str): Local git directory to push
            refspec (str): Git refspec to push
            debug (bool): True to pass -D to workers for tracebacks
            settings (dict or None): Build settings to send to workers

        Returns:
            list of RemoteWorker: Workers that started successfully
        """
        # Phase 1: init git repos
        ready = self._run_parallel(
            'Preparing', self._machines, self._init_one)

        # Phase 2: push source
        ready = self._run_parallel('Pushing source to', ready,
            lambda wrk: wrk.push_source(git_dir, refspec))

        # Phase 3: start workers
        self.workers = self._run_parallel('Starting', ready,
            lambda wrk: self._start_one(wrk, debug))

        # Phase 4: send build settings
        if settings and self.workers:
            self._run_parallel('Configuring', self.workers,
                lambda wrk: wrk.configure(settings))

        return self.workers

    def _init_one(self, mach):
        """Create a RemoteWorker and initialise its git repo

        Args:
            mach: Machine object with hostname attribute

        Returns:
            RemoteWorker: Initialised worker
        """
        wrk = RemoteWorker(mach.hostname, name=mach.name)
        wrk.toolchains = dict(mach.toolchains)
        wrk.bogomips = mach.info.bogomips if mach.info else 0.0
        wrk.max_boards = mach.max_boards
        wrk.init_git()
        return wrk

    @staticmethod
    def _start_one(wrk, debug=False):
        """Start the worker process from the pushed tree

        Args:
            wrk (RemoteWorker): Worker to start
            debug (bool): True to pass -D to the worker
        """
        wrk.start(debug=debug)

    def _run_parallel(self, label, items, func):
        """Run a function on items in parallel, collecting successes

        Args:
            label (str): Progress label (e.g. 'Pushing source to')
            items (list): Items to process
            func (callable): Function to call on each item. May return
                a replacement item; if None is returned, the original
                item is kept.

        Returns:
            list: Items that succeeded (possibly replaced by func)
        """
        lock = threading.Lock()
        results = []
        done = []

        def _run_one(item):
            name = getattr(item, 'name', getattr(item, 'hostname', str(item)))
            try:
                replacement = func(item)
                with lock:
                    results.append(replacement if replacement else item)
                    done.append(name)
                    tout.progress(f'{label} workers {len(done)}/'
                        f'{len(items)}: {", ".join(done)}')
            except WorkerBusy:
                with lock:
                    done.append(f'{name} (BUSY)')
                    tout.progress(f'{label} workers {len(done)}/'
                        f'{len(items)}: {", ".join(done)}')
            except BossError as exc:
                # Clean up lock if the worker was initialised
                if hasattr(item, 'remove_lock'):
                    item.remove_lock()
                with lock:
                    done.append(f'{name} (FAILED)')
                    tout.progress(f'{label} workers {len(done)}/'
                        f'{len(items)}: {", ".join(done)}')
                print(f'\n  Worker failed on {name}: {exc}')

        tout.progress(f'{label} workers on {len(items)} machines')
        threads = []
        for item in items:
            thr = threading.Thread(target=_run_one, args=(item,))
            thr.start()
            threads.append(thr)
        for thr in threads:
            thr.join()
        tout.clear_progress()
        return results

    @staticmethod
    def _get_capacity(wrk):
        """Get a worker's build capacity score

        Uses nthreads * bogomips as the capacity metric. Falls back to
        nthreads alone if bogomips is not available.

        Args:
            wrk (RemoteWorker): Worker to score

        Returns:
            float: Capacity score (higher is faster)
        """
        bogo = wrk.bogomips if wrk.bogomips else 1.0
        return wrk.nthreads * bogo

    def _get_worker_for_arch(self, arch, assigned):
        """Pick the next worker that supports a given architecture

        Distributes boards proportionally to each worker's capacity
        (nthreads * bogomips). Picks the capable worker whose current
        assignment is most below its fair share.

        Args:
            arch (str): Board architecture (e.g. 'arm', 'aarch64')
            assigned (dict): worker -> int count of boards assigned so far

        Returns:
            RemoteWorker or None: A worker with the right toolchain
        """
        if arch == 'sandbox':
            capable = list(self.workers)
        else:
            capable = [w for w in self.workers if arch in w.toolchains]
        if not capable:
            return None

        total_cap = sum(self._get_capacity(w) for w in capable)
        if not total_cap:
            total_cap = len(capable)

        # Pick the worker with the lowest assigned / capacity ratio
        best = min(capable, key=lambda w: (assigned.get(w, 0) /
                                           (self._get_capacity(w) or 1)))
        assigned[best] = assigned.get(best, 0) + 1
        return best

    def build_boards(self, board_selected, commits, builder, local_count=0):
        """Build boards on remote workers and write results locally

        Uses demand-driven dispatch: boards are fed to workers from a
        shared pool. Each worker gets one board per thread initially,
        then one more each time a board completes. Faster workers
        naturally get more boards.

        Args:
            board_selected (dict): target_name -> Board to build remotely
            commits (list of Commit or None): Commits to build
            builder (Builder): Builder object for result processing
            local_count (int): Number of boards being built locally
        """
        if not self.workers or not board_selected:
            return

        ncommits = max(1, len(commits)) if commits else 1

        # Build a pool of boards that have work remaining
        pool = list(board_selected.values())
        if not builder.force_build:
            commit_range = range(len(commits)) if commits else range(1)
            pool = [b for b in pool
                    if any(not os.path.exists(
                               builder.get_done_file(cu, b.target))
                           for cu in commit_range)]

        # Filter boards that no worker can handle
        capable_archs = set()
        for wrk in self.workers:
            capable_archs.update(wrk.toolchains.keys())
        capable_archs.add('sandbox')
        skipped = [b for b in pool if b.arch not in capable_archs]
        pool = [b for b in pool if b.arch in capable_archs]
        if skipped:
            builder.count -= len(skipped) * ncommits

        if not pool:
            print('No remote jobs to dispatch')
            return

        total_jobs = len(pool) * ncommits
        nmach = len(self.workers)
        if local_count:
            nmach += 1
        parts = [f'{len(pool)} boards', f'{ncommits} commits {nmach} machines']
        print(f'Building {" x ".join(parts)} (demand-driven)')

        self._boss_log = _BossLog(builder.base_dir)
        self._boss_log.log(f'dispatch: {len(self.workers)} workers, '
            f'{total_jobs} total jobs')
        for wrk in self.workers:
            self._boss_log.init_worker(wrk)

        self._dispatch_demand(pool, commits, builder, board_selected)

    @staticmethod
    def _grab_boards(pool, pool_lock, wrk, count):
        """Take up to count boards from pool that wrk can build

        Args:
            pool (list of Board): Shared board pool (modified in place)
            pool_lock (threading.Lock): Lock protecting the pool
            wrk (RemoteWorker): Worker to match toolchains against
            count (int): Maximum number of boards to take

        Returns:
            list of Board: Boards taken from the pool
        """
        with pool_lock:
            batch = []
            remaining = []
            for brd in pool:
                if len(batch) >= count:
                    remaining.append(brd)
                elif (brd.arch == 'sandbox'
                      or brd.arch in wrk.toolchains):
                    batch.append(brd)
                else:
                    remaining.append(brd)
            pool[:] = remaining
            return batch

    def _dispatch_jobs(self, worker_jobs, builder, board_selected):
        """Send build jobs to workers and collect results

        Opens a log file per worker, then runs each worker's jobs
        in a separate thread.

        Args:
            worker_jobs (dict): worker -> list of (board, commit_upto,
                commit) tuples
            builder (Builder): Builder for result processing
            board_selected (dict): target_name -> Board mapping
        """
        ctx = _DispatchContext(worker_jobs.keys(), builder, board_selected,
            self._boss_log)

        if ctx.boss_log:
            ctx.boss_log.start_timer()

        threads = []
        for wrk, wjobs in worker_jobs.items():
            thr = threading.Thread(
                target=self._run_batch_worker, args=(wrk, wjobs, ctx),
                daemon=True)
            thr.start()
            threads.append(thr)
        for thr in threads:
            thr.join()

        ctx.close()
        self._boss_log = None

    @staticmethod
    def _run_batch_worker(wrk, wjobs, ctx):
        """Send build commands to one worker and collect results

        Args:
            wrk (RemoteWorker): Worker to run
            wjobs (list): List of (board, commit_upto, commit) tuples
            ctx (_DispatchContext): Shared dispatch infrastructure
        """
        recv_q = ctx.start_reader(wrk)

        board_infos = {}
        commit_list = []
        commit_set = set()
        for brd, _, commit in wjobs:
            target = brd.target
            if target not in board_infos:
                board_infos[target] = {
                    'board': target, 'arch': brd.arch}
            commit_hash = commit.hash if commit else None
            if commit_hash not in commit_set:
                commit_set.add(commit_hash)
                commit_list.append(commit_hash)

        boards_list = list(board_infos.values())
        total = len(boards_list) * len(commit_list)

        ctx.log(wrk, '>>', f'{len(boards_list)} boards x '
                f'{len(commit_list)} commits')
        if ctx.boss_log:
            ctx.boss_log.log(f'{wrk.name}: {len(boards_list)} boards'
                f' x {len(commit_list)} commits')

        try:
            wrk.build_boards(boards_list, commit_list)
        except BossError as exc:
            ctx.log(wrk, '!!', str(exc))
            if not wrk.closing:
                print(f'\n  Error from {wrk.name}: {exc}')
            return
        if ctx.boss_log:
            ctx.boss_log.record_sent(wrk.name, total)

        for _ in range(total):
            if not ctx.recv_one(wrk, recv_q):
                return

    def _start_demand_worker(  # pylint: disable=R0913
            self, wrk, ctx, commit_list, ncommits, pool, pool_lock):
        """Prepare a worker and send the initial batch of boards

        Args:
            wrk (RemoteWorker): Worker to run
            ctx (_DispatchContext): Shared dispatch infrastructure
            commit_list (list of str): Commit hashes to build
            ncommits (int): Number of commits
            pool (list of Board): Shared board pool
            pool_lock (threading.Lock): Lock protecting the pool

        Returns:
            tuple: (recv_q, state) on success, or (None, None) if the
                worker failed during prepare or had no boards to build
        """
        recv_q = ctx.start_reader(wrk)

        try:
            wrk.build_prepare(commit_list)
        except BossError as exc:
            ctx.log(wrk, '!!', str(exc))
            if not wrk.closing:
                print(f'\n  Error from {wrk.name}: {exc}')
            return None, None

        if not ctx.wait_for_prepare(wrk, recv_q):
            return None, None

        initial = self._grab_boards(pool, pool_lock, wrk, wrk.max_boards)
        if not initial:
            try:
                wrk.build_done()
            except BossError:
                pass
            return None, None

        count = ctx.send_batch(wrk, initial)
        if count < 0:
            return None, None
        if ctx.boss_log:
            ctx.boss_log.record_sent(wrk.name, count * ncommits)
        ctx.log(wrk, '>>', f'{count} boards (initial,'
                f' max_boards={wrk.max_boards})')

        def _grab(w, n):
            return self._grab_boards(pool, pool_lock, w, n)

        state = DemandState(count, ncommits, _grab)
        return recv_q, state

    @staticmethod
    def _finish_demand_worker(wrk, ctx, recv_q, state):
        """Collect results and finish the demand-driven protocol

        Args:
            wrk (RemoteWorker): Worker to collect from
            ctx (_DispatchContext): Shared dispatch infrastructure
            recv_q (queue.Queue): Response queue from start_reader()
            state (DemandState): Build state from _start_demand_worker()
        """
        ctx.collect_results(wrk, recv_q, state)

        ctx.log(wrk, '>>', f'{state.sent} boards total')
        try:
            wrk.build_done()
        except BossError as exc:
            ctx.log(wrk, '!!', str(exc))
            return

        # Wait for worker's build_done
        while True:
            resp = ctx.recv(wrk, recv_q)
            if resp is None:
                return
            if resp.get('resp') == 'build_done':
                break

    def _dispatch_demand(self, pool, commits, builder, board_selected):
        """Dispatch boards on demand from a shared pool

        Each worker gets boards from the pool as it finishes previous
        ones, so faster workers naturally get more work.

        Args:
            pool (list of Board): Boards available to build
            commits (list of Commit or None): Commits to build
            builder (Builder): Builder for result processing
            board_selected (dict): target_name -> Board mapping
        """
        commit_list = [c.hash if c else None for c in (commits or [None])]
        ncommits = len(commit_list)
        pool_lock = threading.Lock()

        ctx = _DispatchContext(
            self.workers, builder, board_selected, self._boss_log)

        if ctx.boss_log:
            ctx.boss_log.start_timer()

        def _run_worker(wrk):
            recv_q, state = self._start_demand_worker(
                wrk, ctx, commit_list, ncommits, pool, pool_lock)
            if recv_q is not None:
                self._finish_demand_worker(wrk, ctx, recv_q, state)

        threads = []
        for wrk in self.workers:
            thr = threading.Thread(target=_run_worker, args=(wrk,),
                daemon=True)
            thr.start()
            threads.append(thr)
        for thr in threads:
            thr.join()

        ctx.close()
        self._boss_log = None

    def quit_all(self):
        """Quit all workers gracefully"""
        self.print_transfer_summary()
        if self._boss_log:
            self._boss_log.log('quit: shutting down')
            self._boss_log.log_status()
            self._boss_log.close()
            self._boss_log = None
        for wrk in self.workers:
            try:
                wrk.quit()
            except BossError:
                wrk.close()
        self.workers = []

    def print_transfer_summary(self):
        """Print data transfer summary for all workers"""
        if not self.workers:
            return
        total_sent = 0
        total_recv = 0
        parts = []
        for wrk in self.workers:
            sent = getattr(wrk, 'bytes_sent', 0)
            recv = getattr(wrk, 'bytes_recv', 0)
            total_sent += sent
            total_recv += recv
            name = getattr(wrk, 'name', '?')
            parts.append(f'{name}:'
                         f'{_format_bytes(sent)}/'
                         f'{_format_bytes(recv)}')
        sys.stderr.write(f'\nTransfer (sent/recv): {", ".join(parts)}'
            f' total:{_format_bytes(total_sent)}/'
            f'{_format_bytes(total_recv)}\n')
        sys.stderr.flush()

    def close_all(self):
        """Stop all workers immediately

        Use this on Ctrl-C. Sends a quit command to all workers first,
        then waits briefly for the commands to travel through SSH
        before closing the connections. This two-phase approach avoids
        a race where closing SSH kills the connection before the quit
        command is forwarded to the remote worker.
        """
        self.print_transfer_summary()
        if self._boss_log:
            self._boss_log.log('interrupted: Ctrl-C')
            self._boss_log.log_status()
            self._boss_log.close()
            self._boss_log = None

        # Suppress error messages from reader threads
        for wrk in self.workers:
            wrk.closing = True

        # Phase 1: send quit to all workers
        for wrk in self.workers:
            try:
                wrk._send({'cmd': 'quit'})  # pylint: disable=W0212
            except BossError:
                pass

        # Brief pause so SSH can forward the quit commands to the
        # remote workers before we tear down the connections
        time.sleep(0.5)

        # Phase 2: close all connections
        for wrk in self.workers:
            wrk.close()
            wrk.remove_lock()
        self.workers = []
