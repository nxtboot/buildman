# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Worker mode for distributed builds

A worker runs on a remote machine and receives build commands over stdin from a
boss. Commands and responses use a JSON-lines protocol:

Commands (boss -> worker, on stdin):
    {"cmd": "setup", "work_dir": "/path"}
    {"cmd": "configure", "settings": {"no_lto": true, ...}}
    {"cmd": "build_boards",
     "boards": [{"board": "sandbox", "arch": "sandbox"}],
     "commits": ["<hash>", ...]}
    {"cmd": "build_prepare", "commits": ["<hash>", ...]}
    {"cmd": "build_board", "board": "sandbox", "arch": "sandbox"}
    {"cmd": "build_done"}
    {"cmd": "quit"}

Responses (worker -> boss, on stdout):
    Each line is prefixed with 'BM> ' followed by a JSON object:
    BM> {"resp": "ready", "nthreads": 8, "slots": 2}
    BM> {"resp": "setup_done", "work_dir": "/path", "git_dir": "/path/.git"}
    BM> {"resp": "configure_done"}
    BM> {"resp": "build_prepare_done"}
    BM> {"resp": "build_result", "board": "sandbox", "commit_upto": 0,
         "return_code": 0, "stderr": "", "sizes": {...}}
    BM> {"resp": "build_done", "exceptions": 0}
    BM> {"resp": "error", "msg": "..."}
    BM> {"resp": "quit_ack"}

The 'BM> ' prefix allows the boss to distinguish protocol messages from
any stray output on the SSH connection (e.g. login banners, warnings).

The worker uses Builder and BuilderThread from the local build path,
with a custom BuilderThread subclass that sends results over SSH
instead of writing them to disk.  This means the worker inherits the
same board-first scheduling, per-thread worktrees, incremental builds
and retry logic as local builds.

Typical flow (batch mode):
    1. Boss starts worker: ssh host buildman --worker
    2. Worker sends 'ready' with nthreads
    3. Boss sends 'setup' to create work directory with a git repo
    4. Worker sends 'setup_done' with git_dir path
    5. Boss pushes source: git push ssh://host/<git_dir> HEAD:refs/heads/work
    6. Boss sends 'build_boards' with all boards and commits
    7. Worker creates a Builder which sets up per-thread worktrees
       and runs BuilderThread instances that pick boards from a queue,
       build all commits for each, and stream 'build_result' responses
    8. Boss sends 'quit' when done

Demand-driven flow:
    Steps 1-5 same as above, then:
    6. Boss sends 'build_prepare' with commits
    7. Worker creates Builder and worktrees, sends 'build_prepare_done'
    8. Boss sends 'build_board' commands one at a time from a shared
       pool, sending more as results arrive to keep threads busy
    9. Boss sends 'build_done' when no more boards
   10. Worker drains queue, sends 'build_done', boss sends 'quit'
"""

import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import tempfile
import traceback
import threading

from buildman.board import Board
from buildman import builderthread
from buildman import builder as builder_mod
from buildman.outcome import DisplayOptions
from buildman.resulthandler import ResultHandler
from buildman import toolchain as toolchain_mod
from u_boot_pylib import command
from u_boot_pylib import terminal

from patman.commit import Commit

# Protocol prefix for all worker responses
RESPONSE_PREFIX = 'BM> '

# Lock to prevent interleaved stdout writes from concurrent build threads
_send_lock = threading.Lock()

# Lock for debug output to stderr
_debug_lock = threading.Lock()

# Whether debug output is enabled (set by run_worker)
_debug = False  # pylint: disable=C0103

# Whether this process is a process group leader (set by do_worker)
_is_group_leader = False  # pylint: disable=C0103

# The real stdout for protocol messages (set by run_worker)
_protocol_out = None  # pylint: disable=C0103


def _kill_group():
    """Kill all processes in our process group

    Sends SIGKILL to our entire process group, which includes this
    process plus all make, cc1, as, ld, etc. spawned by build threads.
    Only works if do_worker() confirmed we are the process group leader.
    Does nothing otherwise, to avoid killing unrelated processes
    (e.g. the test runner).
    """
    if not _is_group_leader:
        _dbg('_kill_group: not leader, skipping')
        return
    _dbg(f'_kill_group: killing pgid {os.getpgrp()}')
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError as exc:
        _dbg(f'_kill_group: killpg failed: {exc}')


def _dbg(msg):
    """Print a debug message to stderr if debug mode is enabled

    Args:
        msg (str): Message to print
    """
    if _debug:
        with _debug_lock:
            try:
                sys.stderr.write(f'W: {msg}\n')
                sys.stderr.flush()
            except OSError:
                pass


def _send(obj):
    """Send a JSON response to the boss

    Thread-safe: uses a lock to prevent interleaved writes from
    concurrent build threads. Writes to _protocol_out (the real
    stdout) rather than sys.stdout which is redirected to stderr.

    Args:
        obj (dict): Response object to send
    """
    out = _protocol_out or sys.stdout
    with _send_lock:
        out.write(RESPONSE_PREFIX + json.dumps(obj) + '\n')
        out.flush()


def _send_error(msg):
    """Send an error response

    Args:
        msg (str): Error message
    """
    _send({'resp': 'error', 'msg': msg})


def _send_build_result(board, commit_upto, return_code, **kwargs):
    """Send a build result response

    Args:
        board (str): Board target name
        commit_upto (int): Commit number
        return_code (int): Build return code
        **kwargs: Optional keys: stderr, stdout, sizes
    """
    result = {
        'resp': 'build_result',
        'board': board,
        'commit_upto': commit_upto,
        'return_code': return_code,
        'stderr': kwargs.get('stderr', ''),
        'stdout': kwargs.get('stdout', ''),
        'load_avg': _get_load_avg(),
    }
    sizes = kwargs.get('sizes')
    if sizes:
        result['sizes'] = sizes
    _send(result)


def _get_nthreads():
    """Get the number of available build threads

    Returns:
        int: Number of threads available for building
    """
    try:
        return os.cpu_count() or 1
    except (AttributeError, NotImplementedError):
        return 1


def _get_load_avg():
    """Get the 1-minute load average

    Returns:
        float: 1-minute load average, or 0.0 if unavailable
    """
    try:
        with open('/proc/loadavg', encoding='utf-8') as inf:
            return float(inf.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _get_sizes(out_dir):
    """Get the image sizes from a build output directory

    Uses subprocess.Popen directly instead of command.run_pipe() to
    avoid the select() FD_SETSIZE limit in cros_subprocess. With many
    threads running builds, pipe file descriptors can exceed 1024,
    causing select() to fail or corrupt memory.

    Args:
        out_dir (str): Build output directory

    Returns:
        dict: Size information, or empty dict if not available
    """
    elf = os.path.join(out_dir, 'u-boot')
    if not os.path.exists(elf):
        return {}
    try:
        proc = subprocess.Popen(  # pylint: disable=R1732
            ['size', elf], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _ = proc.communicate()
        if proc.returncode == 0:
            # Strip the header line from size output, keeping only data lines.
            # This matches the format that local builderthread produces.
            lines = stdout.decode('utf-8', errors='replace').splitlines()
            if len(lines) > 1:
                return {'raw': '\n'.join(lines[1:])}
    except OSError:
        pass
    return {}


def _worker_make(_commit, _brd, _stage, cwd, *args, **kwargs):
    """Run make using subprocess.Popen to avoid select() FD limit

    On workers with many parallel builds, file descriptor numbers can
    exceed FD_SETSIZE (1024), causing the select()-based
    communicate_filter in cros_subprocess to fail. Using
    subprocess.Popen with communicate() avoids this.

    Args:
        _commit: Unused (API compatibility with Builder.make)
        _brd: Unused
        _stage: Unused
        cwd (str): Working directory
        *args: Make arguments
        **kwargs: Must include 'env' dict

    Returns:
        CommandResult: Result of the make command
    """
    env = kwargs.get('env')
    cmd = ['make'] + list(args)
    try:
        proc = subprocess.Popen(  # pylint: disable=R1732
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        result = command.CommandResult()
        result.stdout = stdout.decode('utf-8', errors='replace')
        result.stderr = stderr.decode('utf-8', errors='replace')
        result.combined = result.stdout + result.stderr
        result.return_code = proc.returncode
        return result
    except Exception as exc:  # pylint: disable=W0718
        result = command.CommandResult()
        result.return_code = 1
        result.stderr = f'make failed to start: {exc}'
        result.combined = result.stderr
        return result


def _run_git(*args, cwd=None, timeout=60):
    """Run a git command using subprocess.Popen to avoid select() FD limit

    On workers with many parallel builds, file descriptor numbers can
    exceed FD_SETSIZE (1024), causing the select()-based cros_subprocess
    to fail. Using subprocess.Popen with communicate() avoids this.

    Args:
        *args: Git command arguments (without 'git' prefix)
        cwd (str): Working directory
        timeout (int): Timeout in seconds for the command

    Returns:
        CommandResult: Result of the git command

    Raises:
        OSError: If the git command fails or times out
    """
    cmd = ['git'] + list(args)
    proc = subprocess.Popen(  # pylint: disable=R1732
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise OSError(
            f'git command timed out after {timeout}s: {cmd}') from exc
    if proc.returncode != 0:
        raise OSError(stderr.decode('utf-8', errors='replace').strip())


def _resolve_git_dir(git_dir):
    """Resolve a .git entry to the actual git directory

    For a regular repo, .git is a directory and is returned as-is.
    For a worktree, .git is a file containing 'gitdir: <path>' and
    the referenced directory is returned.

    Args:
        git_dir (str): Path to a .git file or directory

    Returns:
        str: Path to the actual git directory
    """
    if os.path.isfile(git_dir):
        with open(git_dir, encoding='utf-8') as inf:
            line = inf.readline().strip()
        if line.startswith('gitdir: '):
            path = line[8:]
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(git_dir), path)
            return path
    return git_dir


def _remove_stale_lock(git_dir):
    """Remove a stale index.lock left by a previous SIGKILL'd run

    When the worker is killed (e.g. boss timeout or Ctrl-C), any
    in-progress git checkout leaves behind an index.lock. This must
    be cleaned up before the next checkout can proceed.

    Args:
        git_dir (str): Path to a .git file or directory
    """
    real_dir = _resolve_git_dir(git_dir)
    lock = os.path.join(real_dir, 'index.lock')
    try:
        os.remove(lock)
    except FileNotFoundError:
        pass


def _setup_worktrees(work_dir, git_dir, num_threads):
    """Create per-thread worktrees sequentially with progress messages

    Sets up git worktrees for each build thread before the Builder is
    created. This avoids the problems of lazy per-thread creation:
    concurrent threads contending on the index lock, and no progress
    messages reaching the boss while threads are blocked.

    For existing valid worktrees (from a previous run), creation is
    skipped. A 'worktree_created' message is sent after each thread
    so the boss can show setup progress.

    Args:
        work_dir (str): Base work directory (Builder's base_dir)
        git_dir (str): Git directory path (e.g. work_dir/.git)
        num_threads (int): Number of threads to create worktrees for
    """
    bm_work = os.path.join(work_dir, '.bm-work')
    os.makedirs(bm_work, exist_ok=True)
    src_dir = os.path.abspath(git_dir)

    # Clean up stale locks from a previous SIGKILL'd run. Both the
    # main repo and the worktree gitdirs can have stale index.lock
    # files that would make git commands hang indefinitely.
    _remove_stale_lock(git_dir)

    # Prune stale worktree entries before creating new ones
    _run_git('worktree', 'prune', cwd=work_dir)

    for i in range(num_threads):
        thread_dir = os.path.join(bm_work, f'{i:02d}')
        dot_git = os.path.join(thread_dir, '.git')

        need_worktree = not os.path.exists(dot_git)
        if not need_worktree:
            if os.path.isdir(dot_git):
                # This is a full clone from an older buildman version,
                # not a worktree. Remove it so we can create a proper
                # worktree that shares objects with the main repo.
                shutil.rmtree(thread_dir)
                need_worktree = True
            else:
                # Validate existing worktree — it may be stale from a
                # previous killed run whose gitdir was pruned
                real_dir = _resolve_git_dir(dot_git)
                if not os.path.isdir(real_dir):
                    os.remove(dot_git)
                    need_worktree = True

        if need_worktree:
            os.makedirs(thread_dir, exist_ok=True)
            _run_git('--git-dir', src_dir, 'worktree',
                     'add', '.', '--detach', cwd=thread_dir)
        else:
            _remove_stale_lock(dot_git)

        _send({'resp': 'worktree_created', 'thread': i})


class _WorkerBuilderThread(builderthread.BuilderThread):
    """BuilderThread subclass that sends results over SSH

    Overrides _write_result() (no-op, since the worker doesn't write
    build output to a local directory tree), _send_result() (sends
    the result back to the boss as a JSON protocol message instead of
    putting it in the builder's out_queue), and _checkout() (uses
    subprocess.Popen for git checkout to avoid the select() FD_SETSIZE
    limit on machines with many threads).

    Worktrees are created sequentially before the Builder starts
    threads (see _setup_worktrees), so _checkout() only needs to
    do the checkout itself.
    """

    def run_job(self, job):
        """Run a job, sending a heartbeat so the boss knows we're alive"""
        _send({'resp': 'heartbeat', 'board': job.brd.target,
               'thread': self.thread_num})
        super().run_job(job)

    def _write_result(self, result, keep_outputs, work_in_output):
        """Skip disk writes — results are sent over SSH"""

    def _send_result(self, result):
        """Send the build result to the boss over the SSH protocol"""
        sizes = {}
        if result.out_dir and result.return_code == 0:
            sizes = _get_sizes(result.out_dir)
        _send_build_result(
            result.brd.target, result.commit_upto, result.return_code,
            stderr=result.stderr or '', stdout=result.stdout or '',
            sizes=sizes)

    def _checkout(self, commit_upto, work_dir):
        """Check out a commit using subprocess to avoid select() FD limit

        Worktrees are already set up by _setup_worktrees() before
        the Builder starts threads, so this only needs to do the
        checkout itself.
        """
        if self.builder.commits:
            commit = self.builder.commits[commit_upto]
            if self.builder.checkout:
                git_dir = os.path.join(work_dir, '.git')
                _remove_stale_lock(git_dir)
                _run_git('checkout', '-f', commit.hash, cwd=work_dir)
        else:
            commit = 'current'
        return commit


def _cmd_setup(req, state):
    """Handle the 'setup' command

    Creates or re-uses a work directory and initialises a git repo in it.
    The boss can then use 'git push' over SSH to send source code
    to the repo before issuing build commands.

    Also scans for available toolchains so that the worker can select
    the right cross-compiler for each board's architecture.

    Args:
        req (dict): Request with keys:
            work_dir (str): Working directory path (auto-created if empty)
        state (dict): Worker state, updated in place

    Returns:
        bool: True on success
    """
    work_dir = req.get('work_dir')
    if not work_dir:
        work_dir = tempfile.mkdtemp(prefix='bm-worker-')
        state['auto_work_dir'] = True
    os.makedirs(work_dir, exist_ok=True)
    state['work_dir'] = work_dir

    # Initialise a git repo so the boss can push to it
    git_dir = os.path.join(work_dir, '.git')
    if not os.path.isdir(git_dir):
        try:
            command.run_one('git', 'init', cwd=work_dir,
                            capture=True, raise_on_error=True)
        except command.CommandExc as exc:
            _send_error(f'git init failed: {exc}')
            return False

    _send({'resp': 'setup_done', 'work_dir': work_dir,
           'git_dir': git_dir})
    return True


def _cmd_configure(req, state):
    """Handle the 'configure' command

    Stores build settings received from the boss. These settings mirror
    the command-line flags that affect how make is invoked (verbose,
    allow_missing, no_lto, etc.) and are applied to every subsequent
    build.

    Args:
        req (dict): Request with 'settings' dict containing build flags
        state (dict): Worker state, updated in place

    Returns:
        bool: True on success
    """
    settings = req.get('settings', {})
    state['settings'] = settings
    _dbg(f'configure: {settings}')
    _send({'resp': 'configure_done'})
    return True


def _parse_commits(commit_hashes):
    """Convert commit hashes to Commit objects

    Args:
        commit_hashes (list): Commit hashes, or [None] for current source

    Returns:
        list of Commit or None: Commit objects, or None for current source
    """
    if commit_hashes and commit_hashes[0] is not None:
        return [Commit(h) for h in commit_hashes]
    return None


def _parse_boards(board_dicts):
    """Convert board dicts from the boss into Board objects

    Args:
        board_dicts (list of dict): Each with 'board' and 'arch' keys

    Returns:
        dict: target_name -> Board mapping
    """
    board_selected = {}
    for bd in board_dicts:
        target = bd['board']
        brd = Board('Active', bd.get('arch', ''), '', '', '',
                     target, target, target)
        board_selected[target] = brd
    return board_selected


def _run_build(bldr, commits, board_selected):
    """Run a build and send the result over the protocol

    Args:
        bldr (Builder): Configured builder
        commits (list of Commit or None): Commits to build
        board_selected (dict): target_name -> Board mapping
    """
    bldr.init_build(commits, board_selected, keep_outputs=False,
                    verbose=False, fragments=None)
    try:
        _fail, _warned, exceptions = bldr.run_build(delay_summary=True)
    except Exception as exc:  # pylint: disable=W0718
        _dbg(f'run_build crashed: {exc}')
        _dbg(traceback.format_exc())
        _send({'resp': 'build_done', 'exceptions': 1})
        return
    _send({'resp': 'build_done',
           'exceptions': len(exceptions)})


def _cmd_build_boards(req, state):
    """Handle the 'build_boards' command

    Creates a Builder with a _WorkerBuilderThread subclass and runs the
    build. Results are streamed back over the SSH protocol as each
    commit completes.

    Args:
        req (dict): Request with:
            boards (list of dict): Each with 'board' and 'arch'
            commits (list): Commit hashes in order, or [None] for
                current source
        state (dict): Worker state
    """
    work_dir = state.get('work_dir')
    if not work_dir:
        _send_error('no work directory set up')
        return

    board_dicts = req.get('boards', [])
    if not board_dicts:
        _send_error('no boards specified')
        return

    toolchains = state.get('toolchains')
    if not toolchains:
        _send_error('no toolchains available (run setup first)')
        return

    nthreads = state.get('nthreads', _get_nthreads())
    commits = _parse_commits(req.get('commits', [None]))
    board_selected = _parse_boards(board_dicts)

    # Calculate thread/job split: enough threads to keep all CPUs
    # busy, with each thread running make with -j proportionally
    num_threads = min(nthreads, len(board_selected))
    num_jobs = max(1, nthreads // num_threads)

    _dbg(f'build_boards: {len(board_selected)} boards x '
         f'{len(commits) if commits else 1} commits '
         f'threads={num_threads} -j{num_jobs}')

    # Set up worktrees sequentially before creating the Builder.
    # This sends progress messages so the boss can show setup status
    # (e.g. [ruru 3/256]) and avoids the build timeout firing before
    # any build results arrive.
    git_dir = os.path.join(work_dir, '.git')
    if commits is not None:
        _send({'resp': 'build_started', 'num_threads': num_threads})
        _setup_worktrees(work_dir, git_dir, num_threads)

    bldr = _create_builder(state, num_threads, num_jobs)
    _run_build(bldr, commits, board_selected)


def _create_builder(state, num_threads, num_jobs):
    """Create a Builder configured for worker use

    Args:
        state (dict): Worker state with toolchains, work_dir, settings
        num_threads (int): Number of build threads
        num_jobs (int): Make -j value per thread

    Returns:
        Builder: Configured builder with threads started and waiting
    """
    work_dir = state['work_dir']
    git_dir = os.path.join(work_dir, '.git')
    settings = state.get('settings', {})
    toolchains = state['toolchains']

    col = terminal.Color(terminal.COLOR_NEVER)
    opts = DisplayOptions(
        show_errors=False, show_sizes=False, show_detail=False,
        show_bloat=False, show_config=False, show_environment=False,
        show_unknown=False, ide=True, list_error_boards=False)
    result_handler = ResultHandler(col, opts)

    bldr = builder_mod.Builder(
        toolchains, work_dir, git_dir,
        num_threads, num_jobs, col, result_handler,
        thread_class=_WorkerBuilderThread,
        make_func=_worker_make,
        handle_signals=False,
        lazy_thread_setup=True,
        checkout=True,
        per_board_out_dir=False,
        force_build=settings.get('force_build', False),
        force_build_failures=settings.get('force_build', False),
        no_lto=settings.get('no_lto', False),
        allow_missing=settings.get('allow_missing', False),
        verbose_build=settings.get('verbose_build', False),
        warnings_as_errors=settings.get('warnings_as_errors', False),
        mrproper=settings.get('mrproper', False),
        fallback_mrproper=settings.get('fallback_mrproper', False),
        config_only=settings.get('config_only', False),
        reproducible_builds=settings.get('reproducible_builds', False),
        force_config_on_failure=True,
        kconfig_check=settings.get('kconfig_check', True),
    )
    result_handler.set_builder(bldr)
    return bldr


def _cmd_build_prepare(req, state):
    """Handle the 'build_prepare' command

    Creates a Builder with threads waiting for jobs. The boss follows
    this with 'build_board' commands to feed boards one at a time, then
    'build_done' to signal completion.

    Args:
        req (dict): Request with:
            commits (list): Commit hashes in order, or [None] for
                current source
        state (dict): Worker state
    """
    work_dir = state.get('work_dir')
    if not work_dir:
        _send_error('no work directory set up')
        return

    toolchains = state.get('toolchains')
    if not toolchains:
        _send_error('no toolchains available (run setup first)')
        return

    commits = _parse_commits(req.get('commits', [None]))
    nthreads = state.get('nthreads', _get_nthreads())
    max_boards = req.get('max_boards', 0)

    num_threads = nthreads
    num_jobs = None  # dynamic: nthreads / active_boards

    _dbg(f'build_prepare: '
         f'{len(commits) if commits else 1} commits '
         f'threads={num_threads} max_boards={max_boards} -j=dynamic')

    # Set up worktrees before creating the Builder
    git_dir = os.path.join(work_dir, '.git')
    if commits is not None:
        _send({'resp': 'build_started', 'num_threads': num_threads})
        _setup_worktrees(work_dir, git_dir, num_threads)

    bldr = _create_builder(state, num_threads, num_jobs)
    bldr.max_boards = max_boards

    # Minimal init: set commits and prepare directories. Threads are
    # already started by the Builder constructor, waiting on the queue.
    bldr.commit_count = len(commits) if commits else 1
    bldr.commits = commits
    bldr.verbose = False
    builderthread.mkdir(bldr.base_dir, parents=True)
    bldr.prepare_working_space(num_threads, commits is not None)
    bldr.prepare_output_space()
    bldr.start_time = builder_mod.datetime.now()
    bldr.count = 0
    bldr.upto = bldr._warned = bldr.fail = 0
    bldr.timestamps = builder_mod.collections.deque()
    bldr.thread_exceptions = []

    state['builder'] = bldr
    state['commits'] = commits
    _send({'resp': 'build_prepare_done'})


def _cmd_build_board(req, state):
    """Handle the 'build_board' command

    Adds one board to the running Builder's job queue.

    Args:
        req (dict): Request with:
            board (str): Board target name
            arch (str): Board architecture
        state (dict): Worker state with 'builder' from build_prepare
    """
    bldr = state.get('builder')
    if not bldr:
        _send_error('no builder (send build_prepare first)')
        return

    target = req['board']
    arch = req.get('arch', '')
    brd = Board('Active', arch, '', '', '', target, target, target)
    commits = state.get('commits')

    job = builderthread.BuilderJob()
    job.brd = brd
    job.commits = commits
    job.keep_outputs = False
    job.work_in_output = bldr.work_in_output
    job.adjust_cfg = bldr.adjust_cfg
    job.fragments = None
    job.step = bldr.step
    bldr.count += bldr.commit_count
    bldr.queue.put(job)


def _cmd_build_done(state):
    """Handle the 'build_done' command from the boss

    Waits for all queued jobs to finish, then sends build_done.

    Args:
        state (dict): Worker state with 'builder' from build_prepare
    """
    bldr = state.get('builder')
    if not bldr:
        _send({'resp': 'build_done', 'exceptions': 0})
        return

    try:
        _fail, _warned, exceptions = bldr.run_build(delay_summary=True)
    except Exception as exc:  # pylint: disable=W0718
        _dbg(f'run_build crashed: {exc}')
        _dbg(traceback.format_exc())
        _send({'resp': 'build_done', 'exceptions': 1})
        state.pop('builder', None)
        state.pop('commits', None)
        return
    _send({'resp': 'build_done',
           'exceptions': len(exceptions)})
    state.pop('builder', None)
    state.pop('commits', None)


def _cmd_quit(state):
    """Handle the 'quit' command

    Cleans up the work directory if auto-created, sends quit_ack,
    then kills all child processes (make, cc1, etc.) and this process
    via SIGKILL to the process group.

    Args:
        state (dict): Worker state
    """
    work_dir = state.get('work_dir', '')
    if work_dir and state.get('auto_work_dir'):
        shutil.rmtree(work_dir, ignore_errors=True)
    _send({'resp': 'quit_ack'})
    _kill_group()


def run_worker(debug=False):
    """Main worker loop

    Reads JSON commands from stdin and dispatches them. Sends responses
    as 'BM> ' prefixed JSON lines on stdout. Builds run in parallel
    using Builder with a _WorkerBuilderThread subclass.

    Args:
        debug (bool): True to print debug messages to stderr

    Returns:
        int: 0 on success, non-zero on error
    """
    global _debug, _protocol_out  # pylint: disable=W0603

    _debug = debug

    # Save the real stdout for protocol messages, then redirect
    # stdout to stderr so that tprint and other library output
    # doesn't corrupt the JSON protocol on the SSH pipe.
    _protocol_out = sys.stdout
    sys.stdout = sys.stderr

    # Exit immediately on signals, killing all child processes.
    # SIGHUP is sent by sshd when the SSH connection drops.
    # _kill_group() sends SIGKILL to the process group which terminates
    # everything including this process.
    def _exit_handler(_signum, _frame):
        _kill_group()
        os._exit(1)  # pylint: disable=W0212
    signal.signal(signal.SIGTERM, _exit_handler)
    signal.signal(signal.SIGINT, _exit_handler)
    signal.signal(signal.SIGHUP, _exit_handler)

    nthreads = _get_nthreads()

    # Scan for toolchains at startup so we can select the right
    # cross-compiler for each board's architecture. The boss sets up
    # the git repo and pushes source via SSH before starting us, so
    # there is no 'setup' command — we are ready as soon as we start.
    toolchains = toolchain_mod.Toolchains()
    toolchains.get_settings(show_warning=False)
    toolchains.scan(verbose=False, raise_on_error=False)

    _dbg(f'ready: {nthreads} threads')
    _send({'resp': 'ready', 'nthreads': nthreads, 'slots': nthreads})

    stop_event = threading.Event()
    state = {
        'work_dir': os.getcwd(),
        'nthreads': nthreads,
        'toolchains': toolchains,
        'stop': stop_event,
    }

    # Read stdin in a background thread so that EOF (boss
    # disconnected) is detected even while a long-running command
    # like build_boards is executing. When EOF is seen, kill the
    # entire process group so that all child make processes die too.
    cmd_queue = queue.Queue()
    eof_sentinel = object()

    def _stdin_reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                # Boss disconnected — kill everything immediately.
                # _kill_group() handles production (kills the process
                # group); stop_event handles tests (where _kill_group
                # is a no-op) by unblocking build threads.
                _dbg('stdin closed, killing group')
                stop_event.set()
                _kill_group()
                cmd_queue.put(eof_sentinel)
                return
            line = line.strip()
            if line:
                cmd_queue.put(line)

    threading.Thread(target=_stdin_reader, daemon=True).start()

    return _dispatch_commands(cmd_queue, eof_sentinel, state)


def _dispatch_commands(cmd_queue, eof_sentinel, state):
    """Read commands from the queue and dispatch them

    Args:
        cmd_queue (queue.Queue): Queue of JSON command strings
        eof_sentinel (object): Sentinel value indicating stdin closed
        state (dict): Worker state

    Returns:
        int: 0 on clean quit, 1 on unexpected stdin close
    """
    while True:
        try:
            line = cmd_queue.get(timeout=1)
        except queue.Empty:
            continue
        if line is eof_sentinel:
            break

        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _send_error(f'invalid JSON: {exc}')
            continue

        cmd = req.get('cmd', '')

        if cmd == 'setup':
            _cmd_setup(req, state)
        elif cmd == 'configure':
            _cmd_configure(req, state)
        elif cmd == 'build_boards':
            _cmd_build_boards(req, state)
        elif cmd == 'build_prepare':
            _cmd_build_prepare(req, state)
        elif cmd == 'build_board':
            _cmd_build_board(req, state)
        elif cmd == 'build_done':
            _cmd_build_done(state)
        elif cmd == 'quit':
            _cmd_quit(state)
            return 0
        else:
            _send_error(f'unknown command: {cmd}')

    # stdin closed without quit — boss was interrupted
    return 1


def do_worker(debug=False):
    """Entry point for 'buildman --worker'

    Args:
        debug (bool): True to print debug messages to stderr

    Returns:
        int: 0 on success
    """
    global _is_group_leader  # pylint: disable=W0603

    # Ensure we are a process group leader so _kill_group() can kill
    # all child processes (make, cc1, as, ld) on exit. When launched
    # via SSH, sshd already makes us session + group leader (pid ==
    # pgid), so setpgrp() fails with EPERM — that's fine. This is
    # done here rather than in run_worker() so that tests can call
    # run_worker() without becoming a process group leader.
    try:
        os.setpgrp()
    except OSError:
        pass
    _is_group_leader = os.getpid() == os.getpgrp()
    return run_worker(debug)
