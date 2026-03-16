# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the boss module"""

# pylint: disable=C0302,E1101,W0212,W0612

import io
import json
import os
import queue
import random
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tools

from buildman import boss
from buildman import bsettings
from buildman import machine
from buildman import worker as worker_mod


def _make_response(obj):
    """Create a BM>-prefixed response line as bytes"""
    return (worker_mod.RESPONSE_PREFIX + json.dumps(obj) + '\n').encode()


class FakeProc:
    """Fake subprocess.Popen for testing"""

    def __init__(self, responses=None):
        self.stdin = io.BytesIO()
        self._responses = responses or []
        self._resp_idx = 0
        self.stdout = self
        self.stderr = io.BytesIO(b'')
        self._returncode = None

    def readline(self):
        """Return the next canned response line"""
        if self._resp_idx < len(self._responses):
            line = self._responses[self._resp_idx]
            self._resp_idx += 1
            return line
        return b''

    def poll(self):
        """Return the process return code"""
        return self._returncode

    def terminate(self):
        """Simulate SIGTERM"""
        self._returncode = -15

    def kill(self):
        """Simulate SIGKILL"""
        self._returncode = -9

    def wait(self, timeout=None):
        """Wait for the process (no-op)"""


class TestRunSsh(unittest.TestCase):
    """Test _run_ssh()"""

    @mock.patch('buildman.boss.command.run_pipe')
    def test_success(self, mock_pipe):
        """Test successful one-shot SSH command"""
        mock_pipe.return_value = mock.Mock(
            stdout='/tmp/bm-worker-abc\n')
        result = boss._run_ssh('host1', 'echo hello')
        self.assertEqual(result, '/tmp/bm-worker-abc')

    @mock.patch('buildman.boss.command.run_pipe')
    def test_failure(self, mock_pipe):
        """Test SSH command failure"""
        mock_pipe.side_effect = command.CommandExc(
            'connection refused', command.CommandResult())
        with self.assertRaises(boss.BossError) as ctx:
            boss._run_ssh('host1', 'echo hello')
        self.assertIn('SSH command failed', str(ctx.exception))


class TestKillWorkers(unittest.TestCase):
    """Test kill_workers()"""

    @mock.patch('buildman.boss._run_ssh')
    def test_kill_workers(self, mock_ssh):
        """Test killing workers on multiple machines"""
        mock_ssh.side_effect = ['killed 1234', 'no workers']
        with terminal.capture():
            result = boss.kill_workers(['host1', 'host2'])
        self.assertEqual(result, 0)
        self.assertEqual(mock_ssh.call_count, 2)

    @mock.patch('buildman.boss._run_ssh')
    def test_kill_workers_ssh_failure(self, mock_ssh):
        """Test that SSH failures are reported but do not abort"""
        mock_ssh.side_effect = boss.BossError('connection refused')
        with terminal.capture():
            result = boss.kill_workers(['host1'])
        self.assertEqual(result, 0)


class TestRemoteWorkerInitGit(unittest.TestCase):
    """Test RemoteWorker.init_git()"""

    @mock.patch('buildman.boss._run_ssh')
    def test_init_git(self, mock_ssh):
        """Test successful git init"""
        mock_ssh.return_value = '/tmp/bm-worker-abc'
        w = boss.RemoteWorker('host1')
        w.init_git()
        self.assertEqual(w.work_dir, '/tmp/bm-worker-abc')
        self.assertEqual(w.git_dir, '/tmp/bm-worker-abc/.git')

    @mock.patch('buildman.boss._run_ssh')
    def test_init_git_busy(self, mock_ssh):
        """Test init_git when machine is locked"""
        mock_ssh.return_value = 'BUSY'
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.WorkerBusy) as ctx:
            w.init_git()
        self.assertIn('busy', str(ctx.exception))

    @mock.patch('buildman.boss._run_ssh')
    def test_init_git_empty_output(self, mock_ssh):
        """Test init_git with empty output"""
        mock_ssh.return_value = ''
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.BossError) as ctx:
            w.init_git()
        self.assertIn('returned no work directory', str(ctx.exception))

    @mock.patch('buildman.boss._run_ssh')
    def test_init_git_ssh_failure(self, mock_ssh):
        """Test init_git when SSH fails"""
        mock_ssh.side_effect = boss.BossError('connection refused')
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.BossError):
            w.init_git()


def _make_result(board, commit_upto=0, return_code=0,
                  stderr='', stdout=''):
    """Create a build_result response dict"""
    return {
        'resp': 'build_result',
        'board': board,
        'commit_upto': commit_upto,
        'return_code': return_code,
        'stderr': stderr,
        'stdout': stdout,
    }


def _make_builder(tmpdir, force_build=True):
    """Create a mock Builder with base_dir set"""
    builder = mock.Mock()
    builder.force_build = force_build
    builder.base_dir = tmpdir
    builder.count = 0
    builder.get_build_dir.side_effect = (
        lambda c, b: os.path.join(tmpdir, b))
    return builder


def _start_worker(hostname, mock_popen, proc):
    """Helper to create a worker with work_dir set and start it"""
    mock_popen.return_value = proc
    wrk = boss.RemoteWorker(hostname)
    wrk.work_dir = '/tmp/bm-worker-123'
    wrk.git_dir = '/tmp/bm-worker-123/.git'
    wrk.start()
    return wrk


class TestRemoteWorkerStart(unittest.TestCase):
    """Test RemoteWorker.start()"""

    @mock.patch('subprocess.Popen')
    def test_start_success(self, mock_popen):
        """Test successful worker start"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 8}),
        ])
        w = _start_worker('myhost', mock_popen, proc)
        self.assertEqual(w.nthreads, 8)

        # Check SSH command runs from pushed tree
        cmd = mock_popen.call_args[0][0]
        self.assertIn('ssh', cmd)
        self.assertIn('myhost', cmd)
        self.assertIn('--worker', cmd[-1])

    @mock.patch('subprocess.Popen')
    def test_start_not_ready(self, mock_popen):
        """Test start when worker sends unexpected response"""
        proc = FakeProc([
            _make_response({'resp': 'error', 'msg': 'broken'}),
        ])
        mock_popen.return_value = proc

        w = boss.RemoteWorker('badhost')
        w.work_dir = '/tmp/bm-test'
        with self.assertRaises(boss.BossError) as ctx:
            w.start()
        self.assertIn('did not send ready', str(ctx.exception))

    @mock.patch('subprocess.Popen')
    def test_start_ssh_failure(self, mock_popen):
        """Test start when SSH fails to launch"""
        mock_popen.side_effect = OSError('No such file')

        w = boss.RemoteWorker('badhost')
        w.work_dir = '/tmp/bm-test'
        with self.assertRaises(boss.BossError) as ctx:
            w.start()
        self.assertIn('Failed to start SSH', str(ctx.exception))

    @mock.patch('subprocess.Popen')
    def test_start_connection_closed(self, mock_popen):
        """Test start when connection closes immediately"""
        proc = FakeProc([])  # No responses
        mock_popen.return_value = proc

        w = boss.RemoteWorker('deadhost')
        w.work_dir = '/tmp/bm-test'
        with self.assertRaises(boss.BossError) as ctx:
            w.start()
        self.assertIn('closed connection', str(ctx.exception))

    def test_start_no_work_dir(self):
        """Test start without init_git raises error"""
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.BossError) as ctx:
            w.start()
        self.assertIn('call init_git', str(ctx.exception))

    @mock.patch('subprocess.Popen')
    def test_max_boards_defaults_to_nthreads(self, mock_popen):
        """Test max_boards defaults to nthreads when not configured"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 64}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertEqual(w.max_boards, 64)

    @mock.patch('subprocess.Popen')
    def test_max_boards_preserved_when_set(self, mock_popen):
        """Test max_boards keeps its configured value after start"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 256}),
        ])
        mock_popen.return_value = proc
        w = boss.RemoteWorker('host1')
        w.work_dir = '/tmp/bm-test'
        w.max_boards = 64
        w.start()
        self.assertEqual(w.nthreads, 256)
        self.assertEqual(w.max_boards, 64)


class TestRemoteWorkerPush(unittest.TestCase):
    """Test RemoteWorker.push_source()"""

    def test_push_no_init(self):
        """Test push before init_git raises error"""
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.BossError) as ctx:
            w.push_source('/tmp/repo', 'HEAD:refs/heads/work')
        self.assertIn('call init_git first', str(ctx.exception))

    @mock.patch('buildman.boss.command.run_pipe')
    def test_push_success(self, mock_pipe):
        """Test successful git push"""
        mock_pipe.return_value = mock.Mock(return_code=0)
        w = boss.RemoteWorker('host1')
        w.git_dir = '/tmp/bm-worker-123/.git'

        w.push_source('/home/user/u-boot', 'HEAD:refs/heads/work')
        cmd = mock_pipe.call_args[0][0][0]
        self.assertIn('git', cmd)
        self.assertIn('push', cmd)
        self.assertIn('host1:/tmp/bm-worker-123/.git', cmd)
        self.assertIn('HEAD:refs/heads/work', cmd)

    @mock.patch('buildman.boss.command.run_pipe')
    def test_push_failure(self, mock_pipe):
        """Test git push failure"""
        mock_pipe.side_effect = command.CommandExc(
            'push failed', command.CommandResult())
        w = boss.RemoteWorker('host1')
        w.git_dir = '/tmp/bm/.git'

        with self.assertRaises(boss.BossError) as ctx:
            w.push_source('/tmp/repo', 'HEAD:refs/heads/work')
        self.assertIn('git push', str(ctx.exception))


class TestRemoteWorkerBuildBoards(unittest.TestCase):
    """Test RemoteWorker.build_boards() and recv()"""

    @mock.patch('subprocess.Popen')
    def test_build_boards_and_recv(self, mock_popen):
        """Test sending build_boards and receiving results"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 4}),
            _make_response({
                'resp': 'build_result', 'board': 'sandbox',
                'commit_upto': 0, 'return_code': 0,
                'stderr': '', 'stdout': '',
            }),
        ])
        w = _start_worker('host1', mock_popen, proc)
        boards = [{'board': 'sandbox', 'defconfig': 'sandbox_defconfig',
                    'env': {}}]
        w.build_boards(boards, ['abc123'])

        # Check the command was sent
        sent = proc.stdin.getvalue().decode()
        obj = json.loads(sent)
        self.assertEqual(obj['cmd'], 'build_boards')
        self.assertEqual(len(obj['boards']), 1)
        self.assertEqual(obj['boards'][0]['board'], 'sandbox')
        self.assertEqual(obj['commits'], ['abc123'])

        # Receive result
        resp = w.recv()
        self.assertEqual(resp['resp'], 'build_result')
        self.assertEqual(resp['board'], 'sandbox')


class TestRemoteWorkerQuit(unittest.TestCase):
    """Test RemoteWorker.quit() and close()"""

    @mock.patch('buildman.boss._run_ssh')
    @mock.patch('subprocess.Popen')
    def test_quit(self, mock_popen, mock_ssh):
        """Test clean quit removes the lock"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 4}),
            _make_response({'resp': 'quit_ack'}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        resp = w.quit()
        self.assertEqual(resp.get('resp'), 'quit_ack')
        self.assertIsNone(w._proc)
        # Lock removal SSH should have been called
        mock_ssh.assert_called_once()
        self.assertIn('rm -f', mock_ssh.call_args[0][1])

    @mock.patch('subprocess.Popen')
    def test_close_without_quit(self, mock_popen):
        """Test close without sending quit"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 4}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertIsNotNone(w._proc)
        w.close()
        self.assertIsNone(w._proc)

    def test_close_when_not_started(self):
        """Test close on a worker that was never started"""
        w = boss.RemoteWorker('host1')
        w.close()  # Should not raise


class TestRemoteWorkerRecv(unittest.TestCase):
    """Test response parsing"""

    @mock.patch('subprocess.Popen')
    def test_skip_non_protocol_lines(self, mock_popen):
        """Test that non-BM> lines are skipped"""
        proc = FakeProc([
            b'Welcome to myhost\n',
            b'Last login: Mon Jan 1\n',
            _make_response({'resp': 'ready', 'nthreads': 2}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertEqual(w.nthreads, 2)

    @mock.patch('subprocess.Popen')
    def test_bad_json(self, mock_popen):
        """Test bad JSON in protocol line"""
        proc = FakeProc([
            (worker_mod.RESPONSE_PREFIX + 'not json\n').encode(),
        ])
        mock_popen.return_value = proc

        w = boss.RemoteWorker('host1')
        w._proc = proc
        with self.assertRaises(boss.BossError) as ctx:
            w._recv()
        self.assertIn('Bad JSON', str(ctx.exception))


class TestRemoteWorkerSend(unittest.TestCase):
    """Test _send()"""

    def test_send_when_not_running(self):
        """Test sending to a stopped worker"""
        w = boss.RemoteWorker('host1')
        with self.assertRaises(boss.BossError) as ctx:
            w._send({'cmd': 'quit'})
        self.assertIn('not running', str(ctx.exception))


class TestRemoteWorkerRepr(unittest.TestCase):
    """Test __repr__"""

    def test_repr_stopped(self):
        """Test repr when stopped"""
        w = boss.RemoteWorker('host1')
        self.assertIn('host1', repr(w))
        self.assertIn('stopped', repr(w))

    @mock.patch('subprocess.Popen')
    def test_repr_running(self, mock_popen):
        """Test repr when running"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 8}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertIn('running', repr(w))
        self.assertIn('nthreads=8', repr(w))
        w.close()


class FakeBoard:  # pylint: disable=R0903
    """Fake board for testing split_boards()"""

    def __init__(self, target, arch):
        self.target = target
        self.arch = arch


class TestSplitBoards(unittest.TestCase):
    """Test split_boards()"""

    def test_all_local(self):
        """Test when no remote toolchains match"""
        boards = {
            'sandbox': FakeBoard('sandbox', 'sandbox'),
            'rpi': FakeBoard('rpi', 'arm'),
        }
        local, remote = boss.split_boards(boards, {'x86': '/usr/bin/gcc'})
        self.assertEqual(len(local), 2)
        self.assertEqual(len(remote), 0)

    def test_all_remote(self):
        """Test when all boards have remote toolchains"""
        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
        }
        local, remote = boss.split_boards(boards, {'arm': '/usr/bin/gcc'})
        self.assertEqual(len(local), 0)
        self.assertEqual(len(remote), 2)

    def test_mixed(self):
        """Test split with some local, some remote"""
        boards = {
            'sandbox': FakeBoard('sandbox', 'sandbox'),
            'rpi': FakeBoard('rpi', 'arm'),
            'qemu': FakeBoard('qemu', 'riscv'),
        }
        local, remote = boss.split_boards(
            boards, {'arm': '/usr/bin/gcc', 'riscv': '/usr/bin/gcc'})
        self.assertEqual(len(local), 1)
        self.assertIn('sandbox', local)
        self.assertEqual(len(remote), 2)
        self.assertIn('rpi', remote)
        self.assertIn('qemu', remote)

    def test_empty_toolchains(self):
        """Test with no remote toolchains"""
        boards = {'sandbox': FakeBoard('sandbox', 'sandbox')}
        local, remote = boss.split_boards(boards, {})
        self.assertEqual(len(local), 1)
        self.assertEqual(len(remote), 0)

    def test_none_toolchains(self):
        """Test with None toolchains"""
        boards = {'sandbox': FakeBoard('sandbox', 'sandbox')}
        local, remote = boss.split_boards(boards, None)
        self.assertEqual(len(local), 1)
        self.assertEqual(len(remote), 0)


class TestWriteRemoteResult(unittest.TestCase):
    """Test _write_remote_result()"""

    def test_success(self):
        """Test writing a successful build result"""
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, 'sandbox')
            builder = mock.Mock()
            builder.get_build_dir.return_value = build_dir

            resp = {
                'resp': 'build_result',
                'board': 'sandbox',
                'commit_upto': 0,
                'return_code': 0,
                'stderr': '',
                'stdout': 'build output',
            }
            boss._write_remote_result(builder, resp, {}, 'host1')

            build_dir = builder.get_build_dir.return_value
            self.assertTrue(os.path.isdir(build_dir))

            self.assertEqual(
                tools.read_file(os.path.join(build_dir, 'done'),
                                binary=False), '0\n')
            self.assertEqual(
                tools.read_file(os.path.join(build_dir, 'log'),
                                binary=False), 'build output')
            self.assertFalse(os.path.exists(
                os.path.join(build_dir, 'err')))

    def test_failure_with_stderr(self):
        """Test writing a failed build result with stderr"""
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, 'rpi')
            builder = mock.Mock()
            builder.get_build_dir.return_value = build_dir

            resp = {
                'resp': 'build_result',
                'board': 'rpi',
                'commit_upto': 1,
                'return_code': 2,
                'stderr': 'error: undefined reference',
                'stdout': '',
            }
            boss._write_remote_result(builder, resp, {}, 'host1')

            build_dir = builder.get_build_dir.return_value
            self.assertEqual(
                tools.read_file(os.path.join(build_dir, 'done'),
                                binary=False), '2\n')
            self.assertEqual(
                tools.read_file(os.path.join(build_dir, 'err'),
                                binary=False),
                'error: undefined reference')

    def test_with_sizes(self):
        """Test writing a result with size information"""
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, 'sandbox')
            builder = mock.Mock()
            builder.get_build_dir.return_value = build_dir

            sizes_raw = ('   text    data     bss     dec     hex\n'
                         '  12345    1234     567   14146    374a\n')
            resp = {
                'resp': 'build_result',
                'board': 'sandbox',
                'commit_upto': 0,
                'return_code': 0,
                'stderr': '',
                'stdout': '',
                'sizes': {'raw': sizes_raw},
            }
            boss._write_remote_result(builder, resp, {}, 'host1')

            # Boss strips the header line from sizes
            build_dir = builder.get_build_dir.return_value
            self.assertEqual(
                tools.read_file(os.path.join(build_dir, 'sizes'),
                                binary=False),
                '  12345    1234     567   14146    374a')


class FakeMachineInfo:  # pylint: disable=R0903
    """Fake machine info for testing"""
    bogomips = 5000.0


class FakeMachine:  # pylint: disable=R0903
    """Fake machine for testing WorkerPool"""

    def __init__(self, hostname):
        self.hostname = hostname
        self.name = hostname
        self.toolchains = {'arm': '/usr/bin/arm-linux-gnueabihf-gcc'}
        self.info = FakeMachineInfo()
        self.max_boards = 0

def _make_worker():
    """Create a RemoteWorker with mocked subprocess for testing

    Uses __new__ to avoid calling __init__ which requires real SSH.
    Sets all attributes to safe defaults.
    """
    wrk = boss.RemoteWorker.__new__(boss.RemoteWorker)
    wrk.hostname = 'host1'
    wrk.name = 'host1'
    wrk.nthreads = 4
    wrk.bogomips = 5000.0
    wrk.max_boards = 0
    wrk.slots = 2
    wrk.toolchains = {}
    wrk._proc = mock.Mock()
    wrk._proc.poll.return_value = None  # process is running
    wrk._log = None
    wrk._closed = False
    wrk._closing = False
    wrk._stderr_buf = []
    wrk._stderr_thread = None
    wrk._stderr_lines = []
    wrk._ready = queue.Queue()
    wrk._lock_file = None
    wrk._work_dir = ''
    wrk._git_dir = ''
    wrk.work_dir = ''
    wrk.timeout = 10
    wrk.bytes_sent = 0
    wrk.bytes_recv = 0
    wrk.closing = False
    return wrk


def _make_ctx(board_selected=None):
    """Create a _DispatchContext with temp directory for testing

    Returns:
        tuple: (ctx, wrk, tmpdir) — caller must call ctx.close() and
            shutil.rmtree(tmpdir)
    """
    wrk = mock.Mock(nthreads=4, closing=False, max_boards=0,
                    slots=2, toolchains={'arm': '/gcc'})
    wrk.name = 'host1'
    builder = mock.Mock()
    tmpdir = tempfile.mkdtemp()
    builder.base_dir = tmpdir
    ctx = boss._DispatchContext(
        workers=[wrk], builder=builder,
        board_selected=board_selected or {},
        boss_log=mock.Mock())
    return ctx, wrk, tmpdir


class TestWorkerPool(unittest.TestCase):
    """Test WorkerPool"""

    @mock.patch('subprocess.Popen')
    @mock.patch('buildman.boss._run_ssh')
    @mock.patch('buildman.boss.command.run_pipe')
    def test_start_all(self, mock_pipe, mock_ssh, mock_popen):
        """Test starting workers on multiple machines"""
        mock_ssh.return_value = '/tmp/bm-1'
        mock_pipe.return_value = mock.Mock(return_code=0)
        proc1 = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 4}),
        ])
        proc2 = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 8}),
        ])
        mock_popen.side_effect = [proc1, proc2]

        machines = [FakeMachine('host1'), FakeMachine('host2')]
        pool = boss.WorkerPool(machines)
        with terminal.capture():
            workers = pool.start_all('/tmp/repo', 'HEAD:refs/heads/work')
        self.assertEqual(len(workers), 2)

    @mock.patch('subprocess.Popen')
    @mock.patch('buildman.boss._run_ssh')
    @mock.patch('buildman.boss.command.run_pipe')
    def test_start_all_with_settings(self, mock_pipe, mock_ssh, mock_popen):
        """Test that start_all sends settings via configure"""
        mock_ssh.return_value = '/tmp/bm-1'
        mock_pipe.return_value = mock.Mock(return_code=0)
        proc1 = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 4}),
            _make_response({'resp': 'configure_done'}),
        ])
        proc2 = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 8}),
            _make_response({'resp': 'configure_done'}),
        ])
        mock_popen.side_effect = [proc1, proc2]

        machines = [FakeMachine('host1'), FakeMachine('host2')]
        pool = boss.WorkerPool(machines)
        settings = {'no_lto': True, 'allow_missing': True}
        with terminal.capture():
            workers = pool.start_all('/tmp/repo', 'HEAD:refs/heads/work',
                                     settings=settings)
        self.assertEqual(len(workers), 2)
        # Check that configure was sent (2nd response consumed)
        self.assertEqual(proc1._resp_idx, 2)
        self.assertEqual(proc2._resp_idx, 2)

    @mock.patch('buildman.boss._run_ssh')
    def test_start_all_init_failure(self, mock_ssh):
        """Test start_all when init_git fails on one machine"""
        call_count = [0]

        def _side_effect(_hostname, _cmd, **_kwargs):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise boss.BossError('connection refused')
            return '/tmp/bm-1'

        mock_ssh.side_effect = _side_effect

        machines = [FakeMachine('good'), FakeMachine('bad')]
        pool = boss.WorkerPool(machines)
        with terminal.capture():
            workers = pool.start_all('/tmp/repo', 'HEAD:refs/heads/work')
        # Only 'good' survives init phase; push and start not reached
        # for 'bad'
        self.assertLessEqual(len(workers), 1)

    def test_quit_all(self):
        """Test quitting all workers"""
        pool = boss.WorkerPool([])
        w1 = mock.Mock(spec=boss.RemoteWorker)
        w2 = mock.Mock(spec=boss.RemoteWorker)
        pool.workers = [w1, w2]
        with terminal.capture():
            pool.quit_all()
        w1.quit.assert_called_once()
        w2.quit.assert_called_once()
        self.assertEqual(len(pool.workers), 0)

    def test_quit_all_with_error(self):
        """Test quit_all when a worker raises BossError"""
        pool = boss.WorkerPool([])
        w1 = mock.Mock(spec=boss.RemoteWorker)
        w1.quit.side_effect = boss.BossError('connection lost')
        pool.workers = [w1]
        with terminal.capture():
            pool.quit_all()
        w1.close.assert_called_once()
        self.assertEqual(len(pool.workers), 0)

    def test_build_boards_empty(self):
        """Test build_boards with no workers or boards"""
        pool = boss.WorkerPool([])
        with terminal.capture():
            pool.build_boards({}, None, mock.Mock())  # Should not raise


class TestBuildBoards(unittest.TestCase):
    """Test WorkerPool.build_boards() end-to-end"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _make_worker(hostname, toolchains, responses):
        """Create a mock RemoteWorker with canned responses

        Args:
            hostname (str): Hostname for the worker
            toolchains (dict): arch -> gcc path
            responses (list of dict): Responses to return from recv()

        Returns:
            Mock: Mock RemoteWorker
        """
        wrk = mock.Mock(spec=boss.RemoteWorker)
        wrk.hostname = hostname
        wrk.name = hostname
        wrk.toolchains = toolchains
        wrk.nthreads = 4
        wrk.max_boards = 4
        wrk.bogomips = 5000.0
        wrk.slots = 4
        wrk.recv.side_effect = list(responses)
        return wrk

    def _demand_responses(self, *results):
        """Build recv responses for the demand-driven protocol

        Returns a list starting with build_prepare_done, followed by
        the given results, and ending with build_done.
        """
        return ([{'resp': 'build_prepare_done'}] + list(results)
                + [{'resp': 'build_done'}])

    def test_build_boards_success(self):
        """Test building boards across workers with correct results"""
        wrk1 = self._make_worker('host1', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(_make_result('rpi')))
        wrk2 = self._make_worker('host2',
            {'riscv': '/usr/bin/riscv64-gcc'},
            self._demand_responses(_make_result('odroid')))

        pool = boss.WorkerPool([])
        pool.workers = [wrk1, wrk2]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'riscv'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # Both workers should have received build_prepare
        wrk1.build_prepare.assert_called_once()
        wrk2.build_prepare.assert_called_once()

        # Verify done files were written
        for board in ['rpi', 'odroid']:
            done_path = os.path.join(self._tmpdir, board, 'done')
            self.assertTrue(os.path.exists(done_path))

    def test_board_stays_on_same_worker(self):
        """Test that all commits for a board go to the same worker"""
        commits = [types.SimpleNamespace(hash=f'abc{i}') for i in range(3)]

        # Two workers with different archs, each gets one board
        wrk1 = self._make_worker('host1', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                *[_make_result('rpi', commit_upto=i)
                  for i in range(3)]))
        wrk2 = self._make_worker('host2',
            {'riscv': '/usr/bin/riscv64-gcc'},
            self._demand_responses(
                *[_make_result('odroid', commit_upto=i)
                  for i in range(3)]))

        pool = boss.WorkerPool([])
        pool.workers = [wrk1, wrk2]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'riscv'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, commits, builder)

        # Each worker gets one board via build_board
        wrk1.build_board.assert_called_once()
        wrk2.build_board.assert_called_once()

        # Check each worker got the right board
        self.assertEqual(wrk1.build_board.call_args[0][0], 'rpi')
        self.assertEqual(wrk2.build_board.call_args[0][0], 'odroid')

    def test_arch_passed(self):
        """Test that the board's arch is sent to the worker"""
        wrk = self._make_worker(
            'host1',
            {'arm': '/usr/bin/arm-linux-gnueabihf-gcc'},
            self._demand_responses(_make_result('rpi')))

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'rpi': FakeBoard('rpi', 'arm')}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        wrk.build_board.assert_called_once()
        # build_board(board, arch)
        self.assertEqual(wrk.build_board.call_args[0][1], 'arm')

    def test_worker_error_response(self):
        """Test that error responses are caught and stop the worker"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'}, [
                {'resp': 'error', 'msg': 'no work directory set up'},
            ])

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # build_prepare sent, error on first recv stops the worker
        self.assertTrue(wrk.build_prepare.called)

    def test_boss_error_stops_worker(self):
        """Test that BossError from recv() stops the worker"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'}, [])
        wrk.recv.side_effect = boss.BossError('connection lost')

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # build_prepare sent, but only one recv attempted before error
        self.assertTrue(wrk.build_prepare.called)
        self.assertEqual(wrk.recv.call_count, 1)

    def test_toolchain_matching(self):
        """Test boards only go to workers with the right toolchain"""
        wrk_arm = self._make_worker(
            'arm-host', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                _make_result('rpi'),
                _make_result('odroid'),
            ))
        wrk_riscv = self._make_worker(
            'rv-host', {'riscv': '/usr/bin/riscv64-gcc'},
            self._demand_responses(
                _make_result('qemu_rv'),
            ))

        pool = boss.WorkerPool([])
        pool.workers = [wrk_arm, wrk_riscv]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
            'qemu_rv': FakeBoard('qemu_rv', 'riscv'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # arm boards go to wrk_arm, riscv to wrk_riscv
        arm_boards = {call[0][0]
                      for call in wrk_arm.build_board.call_args_list}
        rv_boards = {call[0][0]
                     for call in wrk_riscv.build_board.call_args_list}
        self.assertEqual(arm_boards, {'rpi', 'odroid'})
        self.assertEqual(rv_boards, {'qemu_rv'})

    def test_sandbox_any_worker(self):
        """Test that sandbox boards can go to any worker"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                _make_result('sandbox'),
            ))

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'sandbox': FakeBoard('sandbox', 'sandbox')}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # sandbox should be sent to the worker even though it has
        # no 'sandbox' toolchain — sandbox uses the host compiler
        wrk.build_board.assert_called_once()
        self.assertEqual(wrk.build_board.call_args[0][1], 'sandbox')

    def test_skip_done_boards(self):
        """Test that already-done boards are skipped without force"""
        # Create a done file for 'rpi'
        done_path = os.path.join(self._tmpdir, 'rpi_done')
        tools.write_file(done_path, '0\n', binary=False)

        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                _make_result('odroid'),
            ))

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
        }
        builder = _make_builder(self._tmpdir, force_build=False)
        builder.get_done_file.side_effect = (
            lambda c, b: os.path.join(self._tmpdir, f'{b}_done'))

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # Only odroid should be built (rpi has done file)
        wrk.build_board.assert_called_once()
        self.assertEqual(wrk.build_board.call_args[0][0], 'odroid')

    def test_no_capable_worker(self):
        """Test boards with no capable worker are silently skipped"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'},
            [{'resp': 'build_prepare_done'}])

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'qemu_rv': FakeBoard('qemu_rv', 'riscv')}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # No build_board should be sent (no riscv worker)
        wrk.build_board.assert_not_called()

    def test_progress_updated(self):
        """Test that process_result is called for each build result"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                _make_result('rpi'),
                _make_result('odroid'),
            ))

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        brd_rpi = FakeBoard('rpi', 'arm')
        brd_odroid = FakeBoard('odroid', 'arm')
        boards = {'rpi': brd_rpi, 'odroid': brd_odroid}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # process_result should be called for each board
        self.assertEqual(builder.process_result.call_count, 2)
        # Each result should have remote set to hostname
        for call in builder.process_result.call_args_list:
            result = call[0][0]
            self.assertEqual(result.remote, 'host1')

    def test_log_files_created(self):
        """Test that worker log files are created in the output dir"""
        wrk = self._make_worker(
            'myhost', {'arm': '/usr/bin/arm-gcc'},
            self._demand_responses(
                _make_result('rpi'),
            ))

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'rpi': FakeBoard('rpi', 'arm')}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        log_path = os.path.join(self._tmpdir, 'worker-myhost.log')
        self.assertTrue(os.path.exists(log_path))
        content = tools.read_file(log_path, binary=False)
        self.assertIn('>> 1 boards', content)
        self.assertIn('<< ', content)
        self.assertIn('build_result', content)

    def test_heartbeat_resets_timeout(self):
        """Test that heartbeat messages are accepted without error"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'}, [
                {'resp': 'build_prepare_done'},
                {'resp': 'heartbeat', 'board': 'rpi', 'thread': 0},
                _make_result('rpi'),
                {'resp': 'build_done'},
            ])

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'rpi': FakeBoard('rpi', 'arm')}
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # The heartbeat should be silently consumed, result processed
        self.assertEqual(builder.process_result.call_count, 1)

    def test_build_done_stops_worker(self):
        """Test that build_done ends collection without timeout"""
        wrk = self._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'}, [
                {'resp': 'build_prepare_done'},
                _make_result('rpi'),
                # Worker had 2 boards but only 1 result, then
                # build_done
                {'resp': 'build_done', 'exceptions': 1},
                # Final build_done response after boss sends
                # build_done
                {'resp': 'build_done'},
            ])

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {
            'rpi': FakeBoard('rpi', 'arm'),
            'odroid': FakeBoard('odroid', 'arm'),
        }
        builder = _make_builder(self._tmpdir)

        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # Only 1 result processed (odroid was lost to a thread
        # exception)
        self.assertEqual(builder.process_result.call_count, 1)


class TestPipelinedBuilds(unittest.TestCase):
    """Test pipelined builds with multiple slots"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_multiple_boards(self):
        """Test that boss sends build_prepare then build_board for each"""
        wrk = TestBuildBoards._make_worker(
            'host1', {'arm': '/usr/bin/arm-gcc'}, [
                {'resp': 'build_prepare_done'},
                _make_result('b1'),
                _make_result('b2'),
                _make_result('b3'),
                _make_result('b4'),
                {'resp': 'build_done'},
            ])

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {
            'b1': FakeBoard('b1', 'arm'),
            'b2': FakeBoard('b2', 'arm'),
            'b3': FakeBoard('b3', 'arm'),
            'b4': FakeBoard('b4', 'arm'),
        }
        builder = _make_builder(self._tmpdir)
        with terminal.capture():
            pool.build_boards(boards, None, builder)

        # build_prepare called once, build_board called 4 times
        wrk.build_prepare.assert_called_once()
        sent_boards = {call[0][0]
                       for call in wrk.build_board.call_args_list}
        self.assertEqual(sent_boards, {'b1', 'b2', 'b3', 'b4'})
        # All 4 results collected
        self.assertEqual(builder.process_result.call_count, 4)

    @mock.patch('subprocess.Popen')
    def test_slots_from_ready(self, mock_popen):
        """Test that slots is read from the worker's ready response"""
        proc = FakeProc([
            _make_response(
                {'resp': 'ready', 'nthreads': 20, 'slots': 5}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertEqual(w.nthreads, 20)
        self.assertEqual(w.slots, 5)

    @mock.patch('subprocess.Popen')
    def test_slots_default(self, mock_popen):
        """Test that slots defaults to 1 for old workers"""
        proc = FakeProc([
            _make_response({'resp': 'ready', 'nthreads': 8}),
        ])
        w = _start_worker('host1', mock_popen, proc)
        self.assertEqual(w.slots, 1)


class TestBuildTimeout(unittest.TestCase):
    """Test that the build timeout prevents hangs"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_timeout = boss.BUILD_TIMEOUT

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        boss.BUILD_TIMEOUT = self._orig_timeout

    def test_recv_timeout(self):
        """Test that a hung worker times out instead of blocking"""

        # Use a very short timeout so the test runs quickly
        boss.BUILD_TIMEOUT = 0.5

        wrk = TestBuildBoards._make_worker(
            'slowhost', {'arm': '/usr/bin/arm-gcc'}, [])
        wrk.nthreads = 1
        wrk.max_boards = 1
        wrk.slots = 1

        # Simulate a worker that never responds: recv blocks forever
        wrk.recv.side_effect = lambda: time.sleep(60)

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {'rpi': FakeBoard('rpi', 'arm')}
        builder = _make_builder(self._tmpdir)

        start = time.monotonic()
        with terminal.capture():
            pool.build_boards(boards, None, builder)
        elapsed = time.monotonic() - start

        # Should complete quickly (within a few seconds), not hang
        self.assertLess(elapsed, 10)

        # No results should have been processed
        builder.process_result.assert_not_called()


class TestMachineMaxBoards(unittest.TestCase):
    """Test per-machine max_boards config"""

    def test_max_boards_from_config(self):
        """Test [machine:name] section sets max_boards"""
        bsettings.setup('')
        bsettings.settings.read_string("""
[machines]
ruru
weka

[machine:ruru]
max_boards = 64
""")
        pool = machine.MachinePool()
        pool._load_from_config()
        by_name = {m.name: m for m in pool.machines}
        self.assertEqual(by_name['ruru'].max_boards, 64)
        self.assertEqual(by_name['weka'].max_boards, 0)

    def test_max_boards_default(self):
        """Test max_boards is 0 when no per-machine section exists"""
        mach = machine.Machine('host1')
        self.assertEqual(mach.max_boards, 0)


class TestGccVersion(unittest.TestCase):
    """Test gcc_version()"""

    def test_buildman_toolchain(self):
        """Test extracting version from a buildman-fetched toolchain"""
        path = ('/home/sglass/.buildman-toolchains/gcc-13.1.0-nolibc/'
                'aarch64-linux/bin/aarch64-linux-gcc')
        self.assertEqual(machine.gcc_version(path), 'gcc-13.1.0-nolibc')

    def test_different_version(self):
        """Test a different gcc version"""
        path = ('/home/sglass/.buildman-toolchains/gcc-11.1.0-nolibc/'
                'aarch64-linux/bin/aarch64-linux-gcc')
        self.assertEqual(machine.gcc_version(path), 'gcc-11.1.0-nolibc')

    def test_system_gcc(self):
        """Test a system gcc with no version directory"""
        self.assertIsNone(machine.gcc_version('/usr/bin/gcc'))

    def test_empty(self):
        """Test an empty path"""
        self.assertIsNone(machine.gcc_version(''))



class _PipelineWorker:  # pylint: disable=R0902
    """Mock worker that simulates the demand-driven build protocol

    Responds to build_prepare, build_board, and build_done commands
    via the recv() queue, simulating real worker behaviour.
    """

    def __init__(self, nthreads, max_boards=0):
        self.hostname = 'sim-host'
        self.name = 'sim-host'
        self.toolchains = {'arm': '/usr/bin/arm-gcc'}
        self.nthreads = nthreads
        self.max_boards = max_boards or nthreads
        self.slots = nthreads
        self.bogomips = 5000.0
        self.closing = False

        self._ready = queue.Queue()
        self._commits = None

    def build_prepare(self, commits):
        """Accept prepare command and queue ready response"""
        self._commits = commits
        self._ready.put({'resp': 'build_prepare_done'})

    def build_board(self, board, _arch):
        """Queue results for this board across all commits"""

        def _produce():
            for cu in range(len(self._commits)):
                time.sleep(random.uniform(0.001, 0.005))
                self._ready.put({
                    'resp': 'build_result',
                    'board': board,
                    'commit_upto': cu,
                    'return_code': 0,
                    'stderr': '',
                    'stdout': '',
                })

        threading.Thread(target=_produce, daemon=True).start()

    def build_done(self):
        """Queue the build_done response after a short delay"""
        def _respond():
            time.sleep(0.05)
            self._ready.put({'resp': 'build_done'})
        threading.Thread(target=_respond, daemon=True).start()

    def recv(self):
        """Wait for the next result"""
        return self._ready.get()


class TestBuildBoardsUtilisation(unittest.TestCase):
    """Test that build_boards dispatches correctly to workers

    The boss sends one build_boards command per worker.  The mock
    worker simulates board-first scheduling and produces results.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run_build(self, nboards, ncommits, nthreads, max_boards=0):
        """Run a build and return the mock worker"""
        wrk = _PipelineWorker(nthreads, max_boards=max_boards)

        pool = boss.WorkerPool([])
        pool.workers = [wrk]

        boards = {}
        for i in range(nboards):
            target = f'board{i}'
            boards[target] = FakeBoard(target, 'arm')

        commits = [types.SimpleNamespace(hash=f'commit{i}')
                   for i in range(ncommits)]

        builder = mock.Mock()
        builder.force_build = True
        builder.base_dir = self._tmpdir
        builder.count = 0
        builder.get_build_dir.side_effect = (
            lambda c, b: os.path.join(self._tmpdir, b))

        with terminal.capture():
            pool.build_boards(boards, commits, builder)
        return wrk

    def test_all_results_collected(self):
        """Verify boss collects all board x commit results"""
        nboards = 30
        ncommits = 10
        wrk = self._run_build(nboards, ncommits, nthreads=8)

        # The ready queue should be empty (boss drained everything)
        self.assertTrue(wrk._ready.empty())

    def test_large_scale(self):
        """Test at realistic scale: 200 boards x 10 commits"""
        wrk = self._run_build(nboards=200, ncommits=10,
                              nthreads=32)
        self.assertTrue(wrk._ready.empty())

    def test_max_boards_caps_batch(self):
        """Test that max_boards limits initial and in-flight boards"""
        wrk = self._run_build(nboards=30, ncommits=5,
                              nthreads=32, max_boards=8)
        self.assertTrue(wrk._ready.empty())
        # Worker should still receive all boards despite the cap
        self.assertEqual(wrk.max_boards, 8)


class TestFormatBytes(unittest.TestCase):
    """Test _format_bytes()"""

    def test_format_bytes(self):
        """Test bytes, KB and MB ranges"""
        self.assertEqual(boss._format_bytes(0), '0B')
        self.assertEqual(boss._format_bytes(1023), '1023B')
        self.assertEqual(boss._format_bytes(1024), '1.0KB')
        self.assertEqual(boss._format_bytes(1536), '1.5KB')
        self.assertEqual(boss._format_bytes(1024 * 1024), '1.0MB')
        self.assertEqual(boss._format_bytes(5 * 1024 * 1024), '5.0MB')


# Tests merged into TestWriteRemoteResult above

    def test_with_stderr(self):
        """Test writing result with stderr"""
        bldr = mock.Mock()
        bldr.get_build_dir.return_value = tempfile.mkdtemp()
        resp = {
            'board': 'sandbox',
            'commit_upto': 0,
            'return_code': 2,
            'stderr': 'error: missing header\n',
            'stdout': '',
        }
        boss._write_remote_result(bldr, resp, {'sandbox': mock.Mock()},
                                  'host1')
        build_dir = bldr.get_build_dir.return_value
        err_path = os.path.join(build_dir, 'err')
        self.assertIn('error:',
                       tools.read_file(err_path, binary=False))

        shutil.rmtree(build_dir)

    def test_removes_stale_err(self):
        """Test that stale err file is removed on success"""
        bldr = mock.Mock()
        build_dir = tempfile.mkdtemp()
        bldr.get_build_dir.return_value = build_dir
        err_path = os.path.join(build_dir, 'err')
        tools.write_file(err_path, 'old error', binary=False)
        resp = {
            'board': 'sandbox',
            'commit_upto': 0,
            'return_code': 0,
            'stderr': '',
            'stdout': '',
        }
        boss._write_remote_result(bldr, resp, {'sandbox': mock.Mock()},
                                  'host1')
        self.assertFalse(os.path.exists(err_path))

        shutil.rmtree(build_dir)


class TestRemoteWorkerMethods(unittest.TestCase):
    """Test RemoteWorker send/recv/close methods"""

    def test_send(self):
        """Test _send writes JSON to stdin"""
        wrk = _make_worker()
        wrk._proc.stdin = mock.Mock()
        wrk._send({'cmd': 'quit'})
        wrk._proc.stdin.write.assert_called_once()
        data = wrk._proc.stdin.write.call_args[0][0]
        self.assertIn(b'quit', data)

    def test_send_broken_pipe(self):
        """Test _send raises BrokenPipeError on broken pipe"""
        wrk = _make_worker()
        wrk._proc.stdin.write.side_effect = BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            wrk._send({'cmd': 'quit'})

    def test_build_commands(self):
        """Test build_prepare, build_board and build_done commands"""
        wrk = _make_worker()
        wrk._send = mock.Mock()

        wrk.build_prepare(['abc123'])
        self.assertEqual(wrk._send.call_args[0][0]['cmd'],
                         'build_prepare')

        wrk._send.reset_mock()
        wrk.build_board('sandbox', 'sandbox')
        self.assertEqual(wrk._send.call_args[0][0]['cmd'],
                         'build_board')

        wrk._send.reset_mock()
        wrk.build_done()
        self.assertEqual(wrk._send.call_args[0][0]['cmd'],
                         'build_done')

    def test_quit(self):
        """Test quit sends command and closes, handles errors"""
        wrk = _make_worker()
        wrk._send = mock.Mock()
        wrk._recv = mock.Mock(return_value={'resp': 'quit_ack'})
        wrk.close = mock.Mock()
        wrk.quit()
        wrk._send.assert_called_once()
        wrk.close.assert_called_once()

        # Error path: BossError during quit still closes
        wrk2 = _make_worker()
        wrk2._send = mock.Mock(side_effect=boss.BossError('gone'))
        wrk2.close = mock.Mock()
        wrk2.quit()
        wrk2.close.assert_called_once()

    def test_close_idempotent(self):
        """Test close can be called multiple times"""
        wrk = _make_worker()
        wrk.close()
        self.assertIsNone(wrk._proc)
        wrk.close()  # should not raise

    @mock.patch('buildman.boss._run_ssh')
    def test_remove_lock(self, mock_ssh):
        """Test remove_lock: SSH call, no work_dir, SSH failure"""
        wrk = _make_worker()
        wrk.work_dir = '/tmp/bm'
        wrk.remove_lock()
        mock_ssh.assert_called_once()

        # No work_dir: does nothing
        wrk2 = _make_worker()
        wrk2.work_dir = ''
        wrk2.remove_lock()

        # SSH failure: silently ignored
        mock_ssh.side_effect = boss.BossError('gone')
        wrk3 = _make_worker()
        wrk3.work_dir = '/tmp/bm'
        wrk3.remove_lock()





class TestWorkerPoolCapacity(unittest.TestCase):
    """Test WorkerPool capacity and arch assignment"""

    def test_get_capacity(self):
        """Test capacity calculation"""
        wrk = mock.Mock(nthreads=8, bogomips=5000.0)
        self.assertEqual(
            boss.WorkerPool._get_capacity(wrk), 40000.0)

    def test_get_capacity_no_bogomips(self):
        """Test capacity with no bogomips falls back to nthreads"""
        wrk = mock.Mock(nthreads=4, bogomips=0)
        self.assertEqual(boss.WorkerPool._get_capacity(wrk), 4.0)

    def test_get_worker_for_arch(self):
        """Test arch-based worker selection"""
        w1 = mock.Mock(nthreads=8, bogomips=5000.0,
                       toolchains={'arm': '/gcc'})
        w2 = mock.Mock(nthreads=4, bogomips=5000.0,
                       toolchains={'arm': '/gcc'})
        pool = boss.WorkerPool.__new__(boss.WorkerPool)
        pool.workers = [w1, w2]
        assigned = {}

        # First assignment should go to w1 (higher capacity)
        wrk = pool._get_worker_for_arch('arm', assigned)
        self.assertEqual(wrk, w1)

        # Second should go to w2 (w1 already has 1)
        wrk = pool._get_worker_for_arch('arm', assigned)
        self.assertEqual(wrk, w2)

    def test_get_worker_sandbox(self):
        """Test sandbox goes to any worker"""
        w1 = mock.Mock(nthreads=4, bogomips=1000.0, toolchains={})
        pool = boss.WorkerPool.__new__(boss.WorkerPool)
        pool.workers = [w1]
        assigned = {}
        wrk = pool._get_worker_for_arch('sandbox', assigned)
        self.assertEqual(wrk, w1)

    def test_get_worker_no_capable(self):
        """Test returns None when no worker supports arch"""
        w1 = mock.Mock(nthreads=4, bogomips=1000.0,
                       toolchains={'arm': '/gcc'})
        pool = boss.WorkerPool.__new__(boss.WorkerPool)
        pool.workers = [w1]
        self.assertIsNone(
            pool._get_worker_for_arch('mips', {}))


class TestBossLog(unittest.TestCase):
    """Test _BossLog"""

    def test_log_and_close(self):
        """Test logging and closing"""
        with tempfile.TemporaryDirectory() as tmpdir:
            blog = boss._BossLog(tmpdir)
            wrk = mock.Mock(name='host1', nthreads=4)
            wrk.name = 'host1'
            blog.init_worker(wrk)
            blog.log('test message')
            blog.record_sent('host1', 3)
            blog.record_recv('host1', load_avg=2.5)
            blog.close()

            log_path = os.path.join(tmpdir, '.buildman.log')
            content = tools.read_file(log_path, binary=False)
            self.assertIn('test message', content)

    def test_log_status(self):
        """Test log_status writes counts"""
        with tempfile.TemporaryDirectory() as tmpdir:
            blog = boss._BossLog(tmpdir)
            wrk = mock.Mock(name='host1', nthreads=4)
            wrk.name = 'host1'
            blog.init_worker(wrk)
            blog.record_sent('host1', 5)
            blog.record_recv('host1')
            blog.record_recv('host1')
            blog.log_status()
            blog.close()

            content = tools.read_file(
                os.path.join(tmpdir, '.buildman.log'), binary=False)
            self.assertIn('host1', content)

    def test_start_timer(self):
        """Test start_timer and close with elapsed"""
        with tempfile.TemporaryDirectory() as tmpdir:
            blog = boss._BossLog(tmpdir)
            blog.start_timer()
            blog.close()


class TestDispatchContext(unittest.TestCase):
    """Test _DispatchContext"""

    def test_update_progress_build_started(self):
        """Test worktree progress: build_started"""
        wrk = mock.Mock(nthreads=4)
        wrk.name = 'host1'
        builder = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            builder.base_dir = tmpdir
            ctx = boss._DispatchContext(
                workers=[wrk], builder=builder,
                board_selected={}, boss_log=mock.Mock())
            resp = {'resp': 'build_started', 'num_threads': 8}
            self.assertTrue(ctx.update_progress(resp, wrk))
            ctx.close()

    def test_update_progress_worktree_created(self):
        """Test worktree progress: worktree_created"""
        wrk = mock.Mock(nthreads=2)
        wrk.name = 'host1'
        builder = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            builder.base_dir = tmpdir
            ctx = boss._DispatchContext(
                workers=[wrk], builder=builder,
                board_selected={}, boss_log=mock.Mock())
            ctx.update_progress(
                {'resp': 'build_started', 'num_threads': 2}, wrk)
            self.assertTrue(ctx.update_progress(
                {'resp': 'worktree_created'}, wrk))
            ctx.close()

    def test_update_progress_other(self):
        """Test non-progress messages return False"""
        wrk = mock.Mock(nthreads=4)
        wrk.name = 'host1'
        builder = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            builder.base_dir = tmpdir
            ctx = boss._DispatchContext(
                workers=[wrk], builder=builder,
                board_selected={}, boss_log=mock.Mock())
            self.assertFalse(ctx.update_progress(
                {'resp': 'build_result'}, wrk))
            ctx.close()

    def test_log(self):
        """Test per-worker log file"""
        wrk = mock.Mock(nthreads=4)
        wrk.name = 'host1'
        builder = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            builder.base_dir = tmpdir
            ctx = boss._DispatchContext(
                workers=[wrk], builder=builder,
                board_selected={}, boss_log=mock.Mock())
            ctx.log(wrk, '>>>', 'test message')
            ctx.close()
            content = tools.read_file(
                os.path.join(tmpdir, 'worker-host1.log'),
                binary=False)
            self.assertIn('test message', content)


class TestRemoteWorkerClose(unittest.TestCase):
    """Test RemoteWorker.close() error paths"""

    def test_close_error_paths(self):
        """Test close: stdin OSError, terminate timeout, kill timeout"""
        # stdin.close() raises OSError
        wrk = _make_worker()
        wrk._proc.stdin.close.side_effect = OSError('broken')
        wrk.close()
        self.assertIsNone(wrk._proc)

        # wait() times out, terminate succeeds
        wrk2 = _make_worker()
        wrk2._proc.wait.side_effect = [
            subprocess.TimeoutExpired('ssh', 2),
            None,
        ]
        wrk2.close()
        self.assertIsNone(wrk2._proc)

        # wait() times out twice, falls back to kill
        wrk3 = _make_worker()
        wrk3._proc.wait.side_effect = [
            subprocess.TimeoutExpired('ssh', 2),
            subprocess.TimeoutExpired('ssh', 3),
        ]
        wrk3.close()
        self.assertIsNone(wrk3._proc)

    def test_configure_rejected(self):
        """Test configure raises on rejection"""
        wrk = _make_worker()
        wrk._send = mock.Mock()
        wrk._recv = mock.Mock(return_value={'resp': 'error', 'msg': 'bad'})
        with self.assertRaises(boss.BossError):
            wrk.configure({'no_lto': True})

    def test_get_stderr(self):
        """Test _get_stderr returns last non-empty line, or empty"""
        wrk = _make_worker()
        wrk._stderr_thread = mock.Mock()
        wrk._stderr_lines = ['first', '', 'last error', '']
        self.assertEqual(wrk._get_stderr(), 'last error')

        wrk._stderr_lines = []
        self.assertEqual(wrk._get_stderr(), '')


# Tests merged into TestWriteRemoteResult above

    def test_sizes_with_header(self):
        """Test that size output header line is stripped"""
        bldr = mock.Mock()
        build_dir = tempfile.mkdtemp()
        bldr.get_build_dir.return_value = build_dir
        resp = {
            'board': 'sandbox',
            'commit_upto': 0,
            'return_code': 0,
            'stderr': '',
            'stdout': '',
            'sizes': {
                'raw': ('   text    data     bss     dec     hex\n'
                        '  1000     200     100    1300     514\n')},
        }
        boss._write_remote_result(bldr, resp, {'sandbox': mock.Mock()},
                                  'host1')
        sizes_content = tools.read_file(
            os.path.join(build_dir, 'sizes'), binary=False)
        self.assertNotIn('text', sizes_content)
        self.assertIn('1000', sizes_content)

        shutil.rmtree(build_dir)





class TestWorkerPoolEdgeCases(unittest.TestCase):
    """Test WorkerPool edge cases"""

    def test_print_transfer_empty(self):
        """Test print_transfer_summary with no workers"""
        pool = boss.WorkerPool([])
        with terminal.capture():
            pool.print_transfer_summary()

    def test_quit_all_with_boss_log(self):
        """Test quit_all closes boss_log"""
        pool = boss.WorkerPool([])
        blog = mock.Mock()
        pool._boss_log = blog
        with terminal.capture():
            pool.quit_all()
        blog.log.assert_called()
        blog.close.assert_called_once()

    def test_build_boards_with_local_count(self):
        """Test build_boards progress includes local count"""
        pool = boss.WorkerPool([])
        wrk = mock.Mock(
            nthreads=4, bogomips=1000.0, slots=2,
            toolchains={'arm': '/gcc'}, closing=False)
        wrk.name = 'host1'
        wrk.recv.side_effect = [
            {'resp': 'build_prepare_done'},
            {'resp': 'build_result', 'board': 'rpi',
             'commit_upto': 0, 'return_code': 0,
             'stderr': '', 'stdout': ''},
            {'resp': 'build_done'},
        ]
        pool.workers = [wrk]

        builder = mock.Mock()
        builder.force_build = True
        builder.base_dir = tempfile.mkdtemp()
        builder.count = 0
        builder.get_build_dir.return_value = tempfile.mkdtemp()

        boards = {'rpi': mock.Mock(target='rpi', arch='arm')}
        with terminal.capture():
            pool.build_boards(boards, None, builder, local_count=5)

        shutil.rmtree(builder.base_dir)
        shutil.rmtree(builder.get_build_dir.return_value)

    def test_get_capacity_no_bogomips(self):
        """Test _get_worker_for_arch falls back when bogomips is 0"""
        w1 = mock.Mock(nthreads=4, bogomips=0, toolchains={'arm': '/gcc'})
        pool = boss.WorkerPool.__new__(boss.WorkerPool)
        pool.workers = [w1]
        wrk = pool._get_worker_for_arch('arm', {})
        self.assertEqual(wrk, w1)

    def test_close_all(self):
        """Test close_all with boss_log"""
        pool = boss.WorkerPool([])
        wrk = mock.Mock()
        wrk.closing = False
        wrk.bytes_sent = 100
        wrk.bytes_recv = 200
        wrk.name = 'host1'
        pool.workers = [wrk]
        blog = mock.Mock()
        pool._boss_log = blog
        with terminal.capture():
            pool.close_all()
        wrk.close.assert_called()
        wrk.remove_lock.assert_called()
        blog.close.assert_called_once()
        self.assertEqual(len(pool.workers), 0)


class TestDispatchContextRecv(unittest.TestCase):
    """Test _DispatchContext.recv() error paths"""


    @mock.patch.object(boss, 'BUILD_TIMEOUT', 0.01)
    def test_recv_timeout(self):
        """Test recv returns None on timeout"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()  # empty queue
        with terminal.capture():
            result = ctx.recv(wrk, recv_q)
        self.assertIsNone(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_recv_error_response(self):
        """Test recv returns None on error response"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        recv_q.put(('error', 'something broke'))
        with terminal.capture():
            result = ctx.recv(wrk, recv_q)
        self.assertIsNone(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_recv_worker_error(self):
        """Test recv returns None on worker error response"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'error', 'msg': 'oops'}))
        with terminal.capture():
            result = ctx.recv(wrk, recv_q)
        self.assertIsNone(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_write_result_exception(self):
        """Test write_result catches exceptions"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        ctx.builder.get_build_dir.side_effect = RuntimeError('boom')
        resp = {'board': 'sandbox', 'commit_upto': 0,
                'return_code': 0}
        with terminal.capture():
            result = ctx.write_result(wrk, resp)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_wait_for_prepare(self):
        """Test wait_for_prepare with prepare_done"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_prepare_done'}))
        result = ctx.wait_for_prepare(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    @mock.patch.object(boss, 'BUILD_TIMEOUT', 0.01)
    def test_wait_for_prepare_timeout(self):
        """Test wait_for_prepare returns False on timeout"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        with terminal.capture():
            result = ctx.wait_for_prepare(wrk, recv_q)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_send_batch_error(self):
        """Test send_batch returns -1 on BossError"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        wrk.build_board.side_effect = boss.BossError('gone')
        brd = mock.Mock(target='rpi', arch='arm')
        result = ctx.send_batch(wrk, [brd])
        self.assertEqual(result, -1)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_collect_results_heartbeat(self):
        """Test collect_results skips heartbeat"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        ctx.board_selected = {'rpi': mock.Mock(target='rpi', arch='arm')}
        build_dir = tempfile.mkdtemp()
        ctx.builder.get_build_dir.return_value = build_dir
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'heartbeat'}))
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        recv_q.put(('resp', {'resp': 'build_done'}))
        state = boss.DemandState(
            sent=1, ncommits=1, grab_func=lambda w, c: [])
        with terminal.capture():
            result = ctx.collect_results(wrk, recv_q, state)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(build_dir)


class TestForwardStderr(unittest.TestCase):
    """Test RemoteWorker._forward_stderr()"""

    def test_forward_stderr(self):
        """Test stderr collection and OSError handling"""
        wrk = boss.RemoteWorker.__new__(boss.RemoteWorker)
        wrk.name = 'host1'
        wrk._stderr_lines = []
        wrk._proc = mock.Mock()
        wrk._proc.stderr = [b'error line 1\n', b'error line 2\n', b'']
        with terminal.capture():
            wrk._forward_stderr()
        self.assertEqual(wrk._stderr_lines,
                         ['error line 1', 'error line 2'])

        # OSError path: silently handled
        wrk2 = boss.RemoteWorker.__new__(boss.RemoteWorker)
        wrk2.name = 'host1'
        wrk2._stderr_lines = []
        wrk2._proc = mock.Mock()
        wrk2._proc.stderr.__iter__ = mock.Mock(
            side_effect=OSError('closed'))
        wrk2._forward_stderr()


class TestStartDebug(unittest.TestCase):
    """Test RemoteWorker.start() debug flag (line 242)"""

    @mock.patch('subprocess.Popen')
    @mock.patch('buildman.boss._run_ssh')
    def test_debug_flag(self, _mock_ssh, mock_popen):
        """Test start() passes -D when debug=True"""
        proc = mock.Mock()
        proc.stdout.readline.return_value = (
            b'BM> {"resp":"ready","nthreads":4,"slots":2}\n')
        proc.poll.return_value = None
        proc.stderr = []  # empty iterable for _forward_stderr thread
        mock_popen.return_value = proc

        wrk = boss.RemoteWorker.__new__(boss.RemoteWorker)
        wrk.hostname = 'host1'
        wrk.name = 'host1'
        wrk._proc = None
        wrk._closed = False
        wrk._closing = False
        wrk._stderr_lines = []
        wrk._stderr_thread = None
        wrk._ready = queue.Queue()
        wrk._log = None
        wrk.bytes_sent = 0
        wrk.bytes_recv = 0
        wrk.nthreads = 0
        wrk.slots = 0
        wrk.max_boards = 0
        wrk.toolchains = {}
        wrk.closing = False
        wrk._work_dir = '/tmp/bm'
        wrk._git_dir = '/tmp/bm/.git'
        wrk.work_dir = '/tmp/bm'
        wrk.timeout = 10

        wrk.start(debug=True)
        cmd = mock_popen.call_args[0][0]
        self.assertIn('-D', ' '.join(cmd))


class TestBossLogTimer(unittest.TestCase):
    """Test _BossLog.start_timer() (lines 607-612)"""

    def test_timer_ticks(self):
        """Test that the timer fires and logs status"""
        with tempfile.TemporaryDirectory() as tmpdir:
            blog = boss._BossLog(tmpdir)
            wrk = mock.Mock(nthreads=4)
            wrk.name = 'host1'
            blog.init_worker(wrk)
            blog.record_sent('host1', 5)

            # Patch STATUS_INTERVAL to fire quickly
            with mock.patch.object(boss, 'STATUS_INTERVAL', 0.01):
                blog.start_timer()
                time.sleep(0.05)
            blog.close()

            content = tools.read_file(
                os.path.join(tmpdir, '.buildman.log'), binary=False)
            # Timer should have logged at least one status line
            self.assertIn('host1', content)


class TestWaitForPrepareProgress(unittest.TestCase):
    """Test wait_for_prepare with progress and heartbeat messages"""


    def test_heartbeat_during_prepare(self):
        """Test heartbeat messages are skipped during prepare"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'heartbeat'}))
        recv_q.put(('resp', {'resp': 'build_prepare_done'}))
        result = ctx.wait_for_prepare(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_progress_during_prepare(self):
        """Test worktree progress during prepare"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_started', 'num_threads': 2}))
        recv_q.put(('resp', {'resp': 'worktree_created'}))
        recv_q.put(('resp', {'resp': 'build_prepare_done'}))
        result = ctx.wait_for_prepare(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_unexpected_during_prepare(self):
        """Test unexpected response during prepare returns False"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'something_weird'}))
        result = ctx.wait_for_prepare(wrk, recv_q)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)


class TestRecvOne(unittest.TestCase):
    """Test _DispatchContext.recv_one()"""


    @mock.patch.object(boss, 'BUILD_TIMEOUT', 0.01)
    def test_recv_one_timeout(self):
        """Test recv_one returns False on timeout"""
        ctx, wrk, tmpdir = _make_ctx()
        recv_q = queue.Queue()
        with terminal.capture():
            result = ctx.recv_one(wrk, recv_q)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_recv_one_build_done(self):
        """Test recv_one handles build_done with exceptions"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_done', 'exceptions': 2}))
        result = ctx.recv_one(wrk, recv_q)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_recv_one_build_result(self):
        """Test recv_one processes build_result"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        build_dir = tempfile.mkdtemp()
        ctx.builder.get_build_dir.return_value = build_dir
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        with terminal.capture():
            result = ctx.recv_one(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(build_dir)

    def test_recv_one_heartbeat(self):
        """Test recv_one skips heartbeat then gets result"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        build_dir = tempfile.mkdtemp()
        ctx.builder.get_build_dir.return_value = build_dir
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'heartbeat'}))
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        with terminal.capture():
            result = ctx.recv_one(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(build_dir)

    def test_recv_one_other(self):
        """Test recv_one returns True for unknown response"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'something_else'}))
        result = ctx.recv_one(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_recv_one_progress(self):
        """Test recv_one handles progress then result"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        build_dir = tempfile.mkdtemp()
        ctx.builder.get_build_dir.return_value = build_dir
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_started', 'num_threads': 2}))
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        with terminal.capture():
            result = ctx.recv_one(wrk, recv_q)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(build_dir)


class TestCollectResultsExtended(unittest.TestCase):
    """Test collect_results with more board grabbing"""


    def test_collect_grabs_more(self):
        """Test collect_results grabs more boards when in_flight drops"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        wrk.max_boards = 2
        build_dir = tempfile.mkdtemp()
        ctx.builder.get_build_dir.return_value = build_dir
        extra_brd = mock.Mock(target='extra', arch='arm')
        grab_calls = [0]

        def grab(_w, _n):
            grab_calls[0] += 1
            if grab_calls[0] == 1:
                return [extra_brd]
            return []

        state = boss.DemandState(sent=1, ncommits=1, grab_func=grab)

        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        # After rpi completes, in_flight drops to 0 < max_boards=2,
        # grab returns extra_brd, then build_done ends collection
        recv_q.put(('resp', {'resp': 'build_done'}))

        with terminal.capture():
            ctx.collect_results(wrk, recv_q, state)

        self.assertEqual(state.received, 1)
        self.assertGreater(grab_calls[0], 0)
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(ctx.builder.get_build_dir.return_value)

    def test_collect_write_failure(self):
        """Test collect_results stops on write_result failure"""
        ctx, wrk, tmpdir = _make_ctx(
            {'rpi': mock.Mock(target='rpi', arch='arm')})
        ctx.builder.get_build_dir.side_effect = RuntimeError('boom')

        state = boss.DemandState(sent=1, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        with terminal.capture():
            result = ctx.collect_results(wrk, recv_q, state)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)


class TestRunParallelErrors(unittest.TestCase):
    """Test WorkerPool._run_parallel error paths"""

    def test_worker_busy_and_boss_error(self):
        """Test _run_parallel handles WorkerBusy and BossError"""
        pool = boss.WorkerPool([])
        busy_wrk = mock.Mock(name='busy1')
        busy_wrk.name = 'busy1'
        fail_wrk = mock.Mock(name='fail1')
        fail_wrk.name = 'fail1'

        calls = []

        def func(item):
            calls.append(item.name)
            if item.name == 'busy1':
                raise boss.WorkerBusy('too busy')
            raise boss.BossError('failed')

        with terminal.capture():
            pool._run_parallel('Testing', [busy_wrk, fail_wrk], func)
        fail_wrk.remove_lock.assert_called_once()


class TestCloseAll(unittest.TestCase):
    """Test WorkerPool.close_all() (lines 1533-1544)"""

    def test_close_all_sends_quit(self):
        """Test close_all sends quit and closes workers"""
        pool = boss.WorkerPool([])
        wrk = mock.Mock()
        wrk.closing = False
        wrk.bytes_sent = 0
        wrk.bytes_recv = 0
        wrk.name = 'host1'
        pool.workers = [wrk]
        blog = mock.Mock()
        pool._boss_log = blog

        with terminal.capture():
            pool.close_all()

        wrk.close.assert_called()
        wrk.remove_lock.assert_called()
        self.assertEqual(len(pool.workers), 0)


class TestCollectResultsTimeout(unittest.TestCase):
    """Test collect_results recv timeout and non-result responses"""


    @mock.patch.object(boss, 'BUILD_TIMEOUT', 0.01)
    def test_collect_timeout(self):
        """Test collect_results returns False on recv timeout (line 933)"""
        ctx, wrk, tmpdir = _make_ctx()
        state = boss.DemandState(sent=1, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()
        with terminal.capture():
            result = ctx.collect_results(wrk, recv_q, state)
        self.assertFalse(result)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_collect_skips_unknown(self):
        """Test collect_results skips non-build_result responses (line 940)"""
        ctx, wrk, tmpdir = _make_ctx()
        state = boss.DemandState(sent=1, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'configure_done'}))  # unknown
        recv_q.put(('resp', {'resp': 'build_done'}))
        with terminal.capture():
            result = ctx.collect_results(wrk, recv_q, state)
        self.assertTrue(result)
        ctx.close()
        shutil.rmtree(tmpdir)


class TestDispatchJobs(unittest.TestCase):
    """Test WorkerPool._dispatch_jobs() (lines 1290-1309)"""

    def test_dispatch_jobs(self):
        """Test _dispatch_jobs runs batch workers and closes context"""
        pool = boss.WorkerPool([])
        wrk = mock.Mock(nthreads=4, closing=False, slots=2)
        wrk.name = 'host1'

        brd = mock.Mock(target='rpi', arch='arm')
        commit = mock.Mock(hash='abc123')
        wjobs = [(brd, 0, commit)]

        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir
        board_selected = {'rpi': brd}
        blog = mock.Mock()
        pool._boss_log = blog

        # Mock build_boards to avoid actual protocol
        wrk.build_boards = mock.Mock()
        # recv_one will be called once — return False to end
        with mock.patch.object(boss._DispatchContext, 'start_reader',
                               return_value=queue.Queue()), \
             mock.patch.object(boss._DispatchContext, 'recv_one',
                               return_value=False):
            with terminal.capture():
                pool._dispatch_jobs({wrk: wjobs}, builder,
                                    board_selected)

        self.assertIsNone(pool._boss_log)
        shutil.rmtree(tmpdir)


class TestRunBatchWorker(unittest.TestCase):
    """Test WorkerPool._run_batch_worker() (lines 1320-1358)"""

    def test_batch_worker_success(self):
        """Test _run_batch_worker sends build_boards and collects"""
        wrk = mock.Mock(nthreads=4, closing=False, slots=2)
        wrk.name = 'host1'
        brd = mock.Mock(target='rpi', arch='arm')
        commit = mock.Mock(hash='abc123')

        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir

        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={'rpi': brd}, boss_log=mock.Mock())

        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'build_result', 'board': 'rpi',
                             'commit_upto': 0, 'return_code': 0,
                             'stderr': '', 'stdout': ''}))
        build_dir = tempfile.mkdtemp()
        builder.get_build_dir.return_value = build_dir

        with mock.patch.object(ctx, 'start_reader',
                               return_value=recv_q):
            with terminal.capture():
                boss.WorkerPool._run_batch_worker(
                    wrk, [(brd, 0, commit)], ctx)

        wrk.build_boards.assert_called_once()
        ctx.close()
        shutil.rmtree(tmpdir)
        shutil.rmtree(build_dir)

    def test_batch_worker_build_error(self):
        """Test _run_batch_worker handles build_boards BossError"""
        wrk = mock.Mock(nthreads=4, closing=False, slots=2)
        wrk.name = 'host1'
        wrk.build_boards.side_effect = boss.BossError('gone')
        brd = mock.Mock(target='rpi', arch='arm')
        commit = mock.Mock(hash='abc123')

        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir

        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={}, boss_log=mock.Mock())

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()):
            with terminal.capture():
                boss.WorkerPool._run_batch_worker(
                    wrk, [(brd, 0, commit)], ctx)

        ctx.close()
        shutil.rmtree(tmpdir)


class TestStartDemandWorker(unittest.TestCase):
    """Test WorkerPool._start_demand_worker() (lines 1380-1400)"""

    def _make_pool_and_ctx(self):
        pool = boss.WorkerPool([])
        wrk = mock.Mock(nthreads=4, closing=False, max_boards=2,
                        slots=2, toolchains={'arm': '/gcc'})
        wrk.name = 'host1'
        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir
        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={}, boss_log=mock.Mock())
        return pool, wrk, ctx, tmpdir

    def test_prepare_error(self):
        """Test _start_demand_worker handles build_prepare BossError"""
        pool, wrk, ctx, tmpdir = self._make_pool_and_ctx()
        wrk.build_prepare.side_effect = boss.BossError('gone')

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()):
            with terminal.capture():
                recv_q, state = pool._start_demand_worker(
                    wrk, ctx, ['abc'], 1, [], threading.Lock())
        self.assertIsNone(recv_q)
        self.assertIsNone(state)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_prepare_timeout(self):
        """Test _start_demand_worker when wait_for_prepare fails"""
        pool, wrk, ctx, tmpdir = self._make_pool_and_ctx()

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()), \
             mock.patch.object(ctx, 'wait_for_prepare',
                               return_value=False):
            with terminal.capture():
                recv_q, state = pool._start_demand_worker(
                    wrk, ctx, ['abc'], 1, [], threading.Lock())
        self.assertIsNone(recv_q)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_no_boards(self):
        """Test _start_demand_worker when no boards available

        Also covers the BossError path in build_done (lines 1395-1396).
        """
        pool, wrk, ctx, tmpdir = self._make_pool_and_ctx()
        wrk.build_done.side_effect = boss.BossError('gone')

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()), \
             mock.patch.object(ctx, 'wait_for_prepare',
                               return_value=True):
            with terminal.capture():
                recv_q, state = pool._start_demand_worker(
                    wrk, ctx, ['abc'], 1, [], threading.Lock())
        self.assertIsNone(recv_q)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_send_batch_failure(self):
        """Test _start_demand_worker when send_batch fails"""
        pool, wrk, ctx, tmpdir = self._make_pool_and_ctx()
        brd = mock.Mock(target='rpi', arch='arm')
        pool_list = [brd]

        wrk.build_board.side_effect = boss.BossError('gone')

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()), \
             mock.patch.object(ctx, 'wait_for_prepare',
                               return_value=True):
            with terminal.capture():
                recv_q, state = pool._start_demand_worker(
                    wrk, ctx, ['abc'], 1, pool_list,
                    threading.Lock())
        self.assertIsNone(recv_q)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_success(self):
        """Test _start_demand_worker success path"""
        pool, wrk, ctx, tmpdir = self._make_pool_and_ctx()
        brd = mock.Mock(target='rpi', arch='arm')
        pool_list = [brd]

        with mock.patch.object(ctx, 'start_reader',
                               return_value=queue.Queue()), \
             mock.patch.object(ctx, 'wait_for_prepare',
                               return_value=True):
            with terminal.capture():
                recv_q, state = pool._start_demand_worker(
                    wrk, ctx, ['abc'], 1, pool_list,
                    threading.Lock())
        self.assertIsNotNone(recv_q)
        self.assertIsNotNone(state)
        self.assertEqual(state.sent, 1)
        ctx.close()
        shutil.rmtree(tmpdir)


class TestFinishDemandWorker(unittest.TestCase):
    """Test WorkerPool._finish_demand_worker() (lines 1429-1437)"""

    def test_build_done_error(self):
        """Test _finish_demand_worker when build_done raises"""
        wrk = mock.Mock(nthreads=4, closing=False, max_boards=0,
                        slots=2)
        wrk.name = 'host1'
        wrk.build_done.side_effect = boss.BossError('gone')
        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir

        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={}, boss_log=mock.Mock())

        state = boss.DemandState(sent=0, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()

        with mock.patch.object(ctx, 'collect_results'):
            boss.WorkerPool._finish_demand_worker(
                wrk, ctx, recv_q, state)
        ctx.close()
        shutil.rmtree(tmpdir)

    def test_finish_waits_for_done(self):
        """Test _finish_demand_worker waits for build_done response"""
        wrk = mock.Mock(nthreads=4, closing=False, max_boards=0,
                        slots=2)
        wrk.name = 'host1'
        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir

        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={}, boss_log=mock.Mock())

        state = boss.DemandState(sent=0, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()
        recv_q.put(('resp', {'resp': 'heartbeat'}))
        recv_q.put(('resp', {'resp': 'build_done'}))

        with mock.patch.object(ctx, 'collect_results'):
            boss.WorkerPool._finish_demand_worker(
                wrk, ctx, recv_q, state)
        ctx.close()
        shutil.rmtree(tmpdir)

    @mock.patch.object(boss, 'BUILD_TIMEOUT', 0.01)
    def test_finish_recv_timeout(self):
        """Test _finish_demand_worker handles recv timeout"""
        wrk = mock.Mock(nthreads=4, closing=False, max_boards=0,
                        slots=2)
        wrk.name = 'host1'
        builder = mock.Mock()
        tmpdir = tempfile.mkdtemp()
        builder.base_dir = tmpdir

        ctx = boss._DispatchContext(
            workers=[wrk], builder=builder,
            board_selected={}, boss_log=mock.Mock())

        state = boss.DemandState(sent=0, ncommits=1,
                                 grab_func=lambda w, n: [])
        recv_q = queue.Queue()

        with mock.patch.object(ctx, 'collect_results'), \
             terminal.capture():
            boss.WorkerPool._finish_demand_worker(
                wrk, ctx, recv_q, state)
        ctx.close()
        shutil.rmtree(tmpdir)


class TestCloseAllSignal(unittest.TestCase):
    """Test close_all quit/close path (lines 1543-1544)"""

    def test_close_all_quit_error(self):
        """Test close_all handles _send BossError during quit"""
        pool = boss.WorkerPool([])
        wrk = mock.Mock()
        wrk.closing = False
        wrk.bytes_sent = 0
        wrk.bytes_recv = 0
        wrk.name = 'host1'
        wrk._send.side_effect = boss.BossError('gone')
        pool.workers = [wrk]
        pool._boss_log = mock.Mock()

        with terminal.capture():
            pool.close_all()
        wrk.close.assert_called()
        self.assertEqual(len(pool.workers), 0)


class TestZeroCapacity(unittest.TestCase):
    """Test _get_worker_for_arch with zero total capacity (line 1183)"""

    def test_zero_nthreads(self):
        """Test workers with 0 nthreads don't cause division by zero"""
        w1 = mock.Mock(nthreads=0, bogomips=0,
                        toolchains={'arm': '/gcc'})
        pool = boss.WorkerPool.__new__(boss.WorkerPool)
        pool.workers = [w1]
        # Should not raise ZeroDivisionError
        wrk = pool._get_worker_for_arch('arm', {})
        self.assertEqual(wrk, w1)


if __name__ == '__main__':
    unittest.main()
