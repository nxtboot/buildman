# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the worker module"""

# pylint: disable=W0212,C0302

import io
import json
import os
import queue
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from u_boot_pylib.command import CommandExc, CommandResult

from buildman import worker


def _parse(line):
    """Parse a protocol line into a dict"""
    return json.loads(line[len(worker.RESPONSE_PREFIX):])


class _ProtoTestBase(unittest.TestCase):
    """Base class for tests that use the worker protocol

    Provides capture/parse helpers and resets _protocol_out on tearDown.
    """

    def setUp(self):
        self.buf = io.StringIO()
        worker._protocol_out = self.buf

    def tearDown(self):
        worker._protocol_out = None

    def get_resp(self):
        """Parse the last response written to the capture buffer"""
        return _parse(self.buf.getvalue())

    def get_all_resp(self):
        """Parse all responses from the capture buffer"""
        return [_parse(line) for line in self.buf.getvalue().strip().split('\n')
                if line]

    def assert_resp(self, key, value):
        """Assert a key in the last response equals value"""
        self.assertEqual(self.get_resp()[key], value)

    def assert_in_output(self, text):
        """Assert text appears in raw protocol output"""
        self.assertIn(text, self.buf.getvalue())


class _RunWorkerBase(_ProtoTestBase):
    """Base class for tests that run the full worker loop"""

    def _run(self, stdin_text):
        """Run the worker with given stdin, return (result, output lines)"""
        buf = io.StringIO()
        with mock.patch('buildman.worker.toolchain_mod.Toolchains'), \
             mock.patch('sys.stdin', io.StringIO(stdin_text)), \
             mock.patch('sys.stdout', buf):
            result = worker.run_worker()
        lines = [line for line in buf.getvalue().strip().split('\n')
                 if line]
        return result, lines


class TestProtocol(_ProtoTestBase):
    """Test _send(), _send_error() and _send_build_result()"""

    def test_send(self):
        """Test sending a response"""
        worker._send({'resp': 'ready', 'nthreads': 4})
        self.assertTrue(self.buf.getvalue().startswith(
            worker.RESPONSE_PREFIX))
        self.assert_resp('resp', 'ready')
        self.assert_resp('nthreads', 4)

    def test_send_error(self):
        """Test sending an error response"""
        worker._send_error('something broke')
        self.assert_resp('resp', 'error')
        self.assert_resp('msg', 'something broke')

    def test_send_build_result_with_sizes(self):
        """Test sending result with sizes"""
        worker._send_build_result(
            'sandbox', 0, 0,
            sizes={'text': 1000, 'data': 200})
        self.assertEqual(
            self.get_resp()['sizes'], {'text': 1000, 'data': 200})

    def test_send_build_result_without_sizes(self):
        """Test sending result without sizes"""
        worker._send_build_result('sandbox', 0, 0)
        self.assertNotIn('sizes', self.get_resp())


class TestUtilityFunctions(unittest.TestCase):
    """Test _get_nthreads(), _get_load_avg() and _get_sizes()"""

    def test_nthreads_normal(self):
        """Test getting thread count"""
        self.assertGreater(worker._get_nthreads(), 0)

    @mock.patch('os.cpu_count', return_value=None)
    def test_nthreads_none(self, _mock):
        """Test when cpu_count returns None"""
        self.assertEqual(worker._get_nthreads(), 1)

    @mock.patch('os.cpu_count', side_effect=AttributeError)
    def test_nthreads_attribute_error(self, _mock):
        """Test when cpu_count raises AttributeError"""
        self.assertEqual(worker._get_nthreads(), 1)

    @mock.patch('builtins.open', side_effect=OSError('no file'))
    def test_load_avg_no_proc(self, _mock):
        """Test when /proc/loadavg is not available"""
        self.assertEqual(worker._get_load_avg(), 0.0)

    def test_get_sizes_no_elf(self):
        """Test with no ELF file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(worker._get_sizes(tmpdir), {})

    @mock.patch('buildman.worker.subprocess.Popen')
    def test_get_sizes_with_elf(self, mock_popen):
        """Test with ELF file present"""
        proc = mock.Mock()
        proc.communicate.return_value = (
            b'   text    data     bss     dec     hex filename\n'
            b'  12345    1234     567   14146    374a u-boot\n',
            b'')
        proc.returncode = 0
        mock_popen.return_value = proc
        with tempfile.TemporaryDirectory() as tmpdir:
            elf = os.path.join(tmpdir, 'u-boot')
            with open(elf, 'w', encoding='utf-8') as fout:
                fout.write('fake')
            self.assertIn('raw', worker._get_sizes(tmpdir))

    @mock.patch('buildman.worker.subprocess.Popen',
                side_effect=OSError('no size'))
    def test_get_sizes_popen_fails(self, _mock):
        """Test when size command fails"""
        with tempfile.TemporaryDirectory() as tmpdir:
            elf = os.path.join(tmpdir, 'u-boot')
            with open(elf, 'w', encoding='utf-8') as fout:
                fout.write('fake')
            self.assertEqual(worker._get_sizes(tmpdir), {})


class TestCmdSetup(_ProtoTestBase):
    """Test _cmd_setup()"""

    @mock.patch('buildman.worker.command.run_one')
    def test_auto_work_dir(self, mock_run):
        """Test setup with auto-created work directory"""
        mock_run.return_value = mock.Mock(return_code=0)
        state = {}
        result = worker._cmd_setup({'work_dir': ''}, state)
        self.assertTrue(result)
        self.assertIn('work_dir', state)
        self.assertTrue(state.get('auto_work_dir'))
        mock_run.assert_called_once()
        self.addCleanup(lambda: os.path.isdir(state['work_dir'])
                        and os.rmdir(state['work_dir']))

    @mock.patch('buildman.worker.command.run_one')
    def test_explicit_work_dir(self, mock_run):
        """Test setup with explicit work directory"""
        mock_run.return_value = mock.Mock(return_code=0)
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {}
            work_dir = os.path.join(tmpdir, 'build')
            self.assertTrue(
                worker._cmd_setup({'work_dir': work_dir}, state))
            self.assertEqual(state['work_dir'], work_dir)
            self.assertTrue(os.path.isdir(work_dir))
            self.assertNotIn('auto_work_dir', state)

    @mock.patch('buildman.worker.command.run_one')
    def test_setup_returns_git_dir(self, mock_run):
        """Test setup response includes git_dir"""
        mock_run.return_value = mock.Mock(return_code=0)
        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_setup({'work_dir': tmpdir}, {})
            self.assert_resp('resp', 'setup_done')
            self.assertEqual(
                self.get_resp()['git_dir'],
                os.path.join(tmpdir, '.git'))

    def test_setup_existing_git(self):
        """Test setup skips git init if .git already exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            with mock.patch(
                    'buildman.worker.command.run_one') as mock_run:
                self.assertTrue(
                    worker._cmd_setup({'work_dir': tmpdir}, {}))
            mock_run.assert_not_called()

    @mock.patch('buildman.worker.command.run_one')
    def test_git_init_fails(self, mock_run):
        """Test setup when git init fails"""
        mock_run.side_effect = CommandExc(
            'git init failed', CommandResult())
        with tempfile.TemporaryDirectory() as tmpdir:
            result = worker._cmd_setup(
                {'work_dir': os.path.join(tmpdir, 'new')}, {})
        self.assertFalse(result)
        self.assert_in_output('git init failed')


class TestCmdQuit(_ProtoTestBase):
    """Test _cmd_quit()"""

    def test_quit(self):
        """Test quit command"""
        worker._cmd_quit({})
        self.assert_resp('resp', 'quit_ack')

    def test_quit_cleanup(self):
        """Test quit cleans up auto work directory"""
        tmpdir = tempfile.mkdtemp(prefix='bm-test-')
        worker._cmd_quit({'work_dir': tmpdir, 'auto_work_dir': True})
        self.assertFalse(os.path.exists(tmpdir))

    def test_quit_preserves_explicit_dir(self):
        """Test quit does not remove non-auto work directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_quit({'work_dir': tmpdir})
            self.assertTrue(os.path.isdir(tmpdir))


class TestCmdConfigure(_ProtoTestBase):
    """Test _cmd_configure()"""

    def test_configure(self):
        """Test that configure stores settings in state"""
        state = {}
        settings = {'no_lto': True, 'allow_missing': True}
        self.assertTrue(
            worker._cmd_configure({'settings': settings}, state))
        self.assertEqual(state['settings'], settings)
        self.assert_resp('resp', 'configure_done')

    def test_configure_empty(self):
        """Test configure with empty settings"""
        state = {}
        worker._cmd_configure({'settings': {}}, state)
        self.assertEqual(state['settings'], {})


class TestRunWorker(_RunWorkerBase):
    """Test run_worker()"""

    def test_empty_stdin(self):
        """Test worker with empty stdin (unexpected close)"""
        result, lines = self._run('')
        self.assertEqual(result, 1)
        self.assertEqual(_parse(lines[0])['resp'], 'ready')

    def test_ready_includes_slots(self):
        """Test that the ready response includes slots"""
        _, lines = self._run('{"cmd": "quit"}\n')
        obj = _parse(lines[0])
        self.assertEqual(obj['resp'], 'ready')
        self.assertIn('slots', obj)
        self.assertGreaterEqual(obj['slots'], 1)

    def test_quit_command(self):
        """Test worker with quit command"""
        result, lines = self._run('{"cmd": "quit"}\n')
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 2)  # ready + quit_ack

    def test_invalid_json(self):
        """Test worker with invalid JSON input"""
        _, lines = self._run('not json\n{"cmd": "quit"}\n')
        self.assertEqual(len(lines), 3)  # ready + error + quit_ack
        self.assertIn('invalid JSON', _parse(lines[1])['msg'])

    def test_unknown_command(self):
        """Test worker with unknown command"""
        _, lines = self._run(
            '{"cmd": "dance"}\n{"cmd": "quit"}\n')
        self.assertIn('unknown command', _parse(lines[1])['msg'])

    def test_blank_lines(self):
        """Test worker ignores blank lines"""
        _, lines = self._run('\n\n{"cmd": "quit"}\n\n')
        self.assertEqual(len(lines), 2)  # ready + quit_ack

    def test_configure_command(self):
        """Test configure command in worker loop"""
        result, lines = self._run(
            '{"cmd": "configure", "settings": {"no_lto": true}}\n'
            '{"cmd": "quit"}\n')
        self.assertEqual(result, 0)
        self.assertEqual(_parse(lines[1])['resp'], 'configure_done')


class TestRunWorkerDispatch(_RunWorkerBase):
    """Test run_worker command dispatch"""

    @mock.patch('buildman.worker._cmd_setup', return_value=True)
    def test_setup(self, mock_fn):
        """Test setup command is dispatched"""
        self._run('{"cmd": "setup", "work_dir": "/tmp"}\n'
                  '{"cmd": "quit"}\n')
        mock_fn.assert_called_once()

    @mock.patch('buildman.worker._cmd_build_boards')
    def test_build_boards(self, mock_fn):
        """Test build_boards command is dispatched"""
        self._run('{"cmd": "build_boards", "boards": [], '
                  '"commits": []}\n{"cmd": "quit"}\n')
        mock_fn.assert_called_once()

    @mock.patch('buildman.worker._cmd_build_prepare')
    def test_build_prepare(self, mock_fn):
        """Test build_prepare command is dispatched"""
        self._run('{"cmd": "build_prepare", "commits": []}\n'
                  '{"cmd": "quit"}\n')
        mock_fn.assert_called_once()

    @mock.patch('buildman.worker._cmd_build_board')
    def test_build_board(self, mock_fn):
        """Test build_board command is dispatched"""
        self._run('{"cmd": "build_board", "board": "x"}\n'
                  '{"cmd": "quit"}\n')
        mock_fn.assert_called_once()

    @mock.patch('buildman.worker._cmd_build_done')
    def test_build_done(self, mock_fn):
        """Test build_done command is dispatched"""
        self._run('{"cmd": "build_done"}\n{"cmd": "quit"}\n')
        mock_fn.assert_called_once()

    def test_stdin_eof(self):
        """Test worker handles stdin EOF"""
        result, _lines = self._run('')
        self.assertEqual(result, 1)

    def test_queue_empty_retry(self):
        """Test dispatch retries on queue.Empty"""
        eof = object()
        mock_queue = mock.Mock()
        mock_queue.get.side_effect = [
            queue.Empty(),
            '{"cmd": "quit"}\n',
        ]
        worker._dispatch_commands(mock_queue, eof, {})
        self.assert_in_output('quit_ack')
        self.assertEqual(mock_queue.get.call_count, 2)


class TestWorkerMake(unittest.TestCase):
    """Test _worker_make()"""

    @mock.patch('buildman.worker.subprocess.Popen')
    def test_success(self, mock_popen):
        """Test successful make invocation"""
        proc = mock.Mock()
        proc.communicate.return_value = (b'built ok\n', b'')
        proc.returncode = 0
        mock_popen.return_value = proc

        result = worker._worker_make(
            None, None, None, '/tmp',
            'O=/tmp/out', '-s', '-j', '4', 'sandbox_defconfig',
            env={'PATH': '/usr/bin'})
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.stdout, 'built ok\n')
        self.assertEqual(mock_popen.call_args[0][0][0], 'make')

    @mock.patch('buildman.worker.subprocess.Popen',
                side_effect=FileNotFoundError('no make'))
    def test_make_not_found(self, _mock_popen):
        """Test when make binary is not found"""
        result = worker._worker_make(
            None, None, None, '/tmp', env={})
        self.assertEqual(result.return_code, 1)
        self.assertIn('make failed', result.stderr)


class TestWorkerBuilderThread(_ProtoTestBase):
    """Test _WorkerBuilderThread"""

    def _make_thread(self):
        """Create an uninitialised thread instance for testing

        Uses __new__ to avoid calling __init__ which requires a real
        Builder. Tests must set any attributes they need.
        """
        return worker._WorkerBuilderThread.__new__(
            worker._WorkerBuilderThread)

    def test_write_result_is_noop(self):
        """Test that _write_result does nothing"""
        self._make_thread()._write_result(None, False, False)

    def test_send_result(self):
        """Test that _send_result sends a build_result message"""
        thread = self._make_thread()
        result = mock.Mock(
            brd=mock.Mock(target='sandbox'),
            commit_upto=0, return_code=0,
            stderr='', stdout='', out_dir='/nonexistent')
        thread._send_result(result)
        self.assert_resp('resp', 'build_result')
        self.assert_resp('board', 'sandbox')

    def test_run_job_sends_heartbeat(self):
        """Test run_job sends heartbeat"""
        thread = self._make_thread()
        thread.thread_num = 0
        job = mock.Mock(brd=mock.Mock(target='sandbox'))
        with mock.patch.object(worker._WorkerBuilderThread.__bases__[0],
                               'run_job'):
            thread.run_job(job)
        self.assert_in_output('heartbeat')

    def test_checkout_with_commits(self):
        """Test _checkout with commits"""
        thread = self._make_thread()
        thread.builder = mock.Mock()
        commit = mock.Mock(hash='abc123')
        thread.builder.commits = [commit]
        thread.builder.checkout = True

        with mock.patch('buildman.worker._run_git') as mock_git, \
             mock.patch('buildman.worker._remove_stale_lock'), \
             tempfile.TemporaryDirectory() as tmpdir:
            result = thread._checkout(0, tmpdir)

        self.assertEqual(result, commit)
        mock_git.assert_called_once()

    def test_checkout_no_commits(self):
        """Test _checkout without commits returns 'current'"""
        thread = self._make_thread()
        thread.builder = mock.Mock(commits=None)
        self.assertEqual(thread._checkout(0, '/tmp'), 'current')


class TestCmdBuildBoards(_ProtoTestBase):
    """Test _cmd_build_boards"""

    def _make_state(self, tmpdir, **overrides):
        """Create a standard state dict for build tests"""
        state = {
            'work_dir': tmpdir,
            'toolchains': mock.Mock(),
            'settings': {},
            'nthreads': 4,
        }
        state.update(overrides)
        return state

    def test_no_work_dir_error(self):
        """Test error when no work directory set"""
        worker._cmd_build_boards({
            'boards': [{'board': 'x', 'arch': 'arm'}],
            'commits': ['abc'],
        }, {'work_dir': None})
        self.assert_in_output('no work directory')

    def test_no_boards_error(self):
        """Test error when no boards specified"""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_boards(
                {'boards': [], 'commits': ['abc']},
                self._make_state(tmpdir))
        self.assert_in_output('no boards')

    def test_no_toolchains_error(self):
        """Test error when toolchains not set up"""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_boards({
                'boards': [{'board': 'x', 'arch': 'arm'}],
                'commits': ['abc'],
            }, {'work_dir': tmpdir})
        self.assert_in_output('no toolchains')

    @mock.patch('buildman.worker._setup_worktrees')
    @mock.patch('buildman.worker.builder_mod.Builder')
    @mock.patch('buildman.worker.ResultHandler')
    def test_creates_builder(self, _mock_rh_cls, mock_builder_cls,
                             mock_setup_wt):
        """Test that build_boards creates a Builder correctly"""
        mock_builder = mock.Mock()
        mock_builder.run_build.return_value = (0, 0, [])
        mock_builder_cls.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_boards({
                'boards': [
                    {'board': 'sandbox', 'arch': 'sandbox'},
                    {'board': 'rpi', 'arch': 'arm'},
                ],
                'commits': ['abc123', 'def456'],
            }, self._make_state(tmpdir, nthreads=8,
                                settings={'no_lto': True,
                                          'force_build': True,
                                          'kconfig_check': False}))

        mock_setup_wt.assert_called_once()
        kwargs = mock_builder_cls.call_args[1]
        self.assertEqual(kwargs['thread_class'],
                         worker._WorkerBuilderThread)
        self.assertTrue(kwargs['no_lto'])
        self.assertFalse(kwargs['kconfig_check'])

        call_args = mock_builder.init_build.call_args
        self.assertEqual(len(call_args[0][0]), 2)
        self.assertIn('rpi', call_args[0][1])
        self.assert_in_output('build_done')

    @mock.patch('buildman.worker.builder_mod.Builder')
    @mock.patch('buildman.worker.ResultHandler')
    def test_no_commits(self, _mock_rh_cls, mock_builder_cls):
        """Test build_boards with no commits (current source)"""
        mock_builder = mock.Mock()
        mock_builder.run_build.return_value = (0, 0, [])
        mock_builder_cls.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_boards({
                'boards': [{'board': 'sandbox', 'arch': 'sandbox'}],
                'commits': [None],
            }, self._make_state(tmpdir))

        self.assertIsNone(
            mock_builder.init_build.call_args[0][0])
        self.assertTrue(mock_builder_cls.call_args[1]['kconfig_check'])

    @mock.patch('buildman.worker._setup_worktrees')
    @mock.patch('buildman.worker.builder_mod.Builder')
    @mock.patch('buildman.worker.ResultHandler')
    def test_build_crash(self, _mock_rh, mock_builder_cls, _mock_wt):
        """Test build_boards when run_build crashes"""
        mock_builder = mock.Mock()
        mock_builder.run_build.side_effect = RuntimeError('crash')
        mock_builder_cls.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_boards({
                'boards': [{'board': 'x', 'arch': 'arm'}],
                'commits': ['abc'],
            }, self._make_state(tmpdir, nthreads=2))

        self.assert_in_output('"exceptions": 1')


class TestCmdBuildPrepare(_ProtoTestBase):
    """Test _cmd_build_prepare()"""

    def test_no_work_dir(self):
        """Test error when no work directory"""
        worker._cmd_build_prepare({}, {})
        self.assert_in_output('no work directory')

    def test_no_toolchains(self):
        """Test error when no toolchains"""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker._cmd_build_prepare({}, {'work_dir': tmpdir})
        self.assert_in_output('no toolchains')

    @mock.patch('buildman.worker._setup_worktrees')
    @mock.patch('buildman.worker._create_builder')
    def test_success(self, mock_create, _mock_wt):
        """Test successful build_prepare"""
        mock_bldr = mock.Mock(
            base_dir='/tmp/test', commit_count=1,
            work_in_output=False)
        mock_create.return_value = mock_bldr

        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            state = {
                'work_dir': tmpdir,
                'toolchains': mock.Mock(),
                'nthreads': 2,
                'settings': {},
            }
            worker._cmd_build_prepare(
                {'commits': ['abc123']}, state)

        self.assert_in_output('build_prepare_done')
        self.assertIn('builder', state)


class TestCmdBuildBoard(_ProtoTestBase):
    """Test _cmd_build_board()"""

    def test_no_builder(self):
        """Test error when no builder"""
        worker._cmd_build_board({'board': 'x'}, {})
        self.assert_in_output('no builder')

    def test_queues_job(self):
        """Test that build_board queues a job"""
        mock_bldr = mock.Mock(
            commit_count=1, count=0, work_in_output=False,
            adjust_cfg=None, step=1)
        worker._cmd_build_board(
            {'board': 'sandbox', 'arch': 'sandbox'},
            {'builder': mock_bldr, 'commits': None})
        mock_bldr.queue.put.assert_called_once()


class TestCmdBuildDone(_ProtoTestBase):
    """Test _cmd_build_done()"""

    def test_no_builder(self):
        """Test build_done with no builder"""
        worker._cmd_build_done({})
        self.assert_resp('resp', 'build_done')
        self.assert_resp('exceptions', 0)

    def test_with_builder(self):
        """Test build_done with a builder"""
        mock_bldr = mock.Mock()
        mock_bldr.run_build.return_value = (0, 0, [])
        state = {'builder': mock_bldr, 'commits': ['abc']}
        worker._cmd_build_done(state)
        self.assert_resp('resp', 'build_done')
        self.assertNotIn('builder', state)

    def test_builder_crash(self):
        """Test build_done when run_build crashes"""
        mock_bldr = mock.Mock()
        mock_bldr.run_build.side_effect = RuntimeError('boom')
        state = {'builder': mock_bldr, 'commits': ['abc']}
        worker._cmd_build_done(state)
        self.assert_resp('exceptions', 1)
        self.assertNotIn('builder', state)


class TestSetupWorktrees(_ProtoTestBase):
    """Test _setup_worktrees()"""

    @mock.patch('buildman.worker._run_git')
    def test_creates_worktrees(self, mock_git):
        """Test that worktrees are created for each thread"""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = os.path.join(tmpdir, '.git')
            os.makedirs(git_dir)
            worker._setup_worktrees(tmpdir, git_dir, 3)

        self.assertEqual(mock_git.call_count, 4)  # prune + 3 adds
        self.assertIn('prune', mock_git.call_args_list[0][0])
        resps = self.get_all_resp()
        self.assertEqual(len(resps), 3)
        for resp in resps:
            self.assertEqual(resp['resp'], 'worktree_created')

    @mock.patch('buildman.worker._run_git')
    def test_skips_existing_valid_worktree(self, mock_git):
        """Test that valid existing worktrees are reused"""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = os.path.join(tmpdir, '.git')
            os.makedirs(git_dir)

            thread_dir = os.path.join(tmpdir, '.bm-work', '00')
            os.makedirs(thread_dir)
            real_gitdir = os.path.join(git_dir, 'worktrees', '00')
            os.makedirs(real_gitdir)
            with open(os.path.join(thread_dir, '.git'), 'w',
                       encoding='utf-8') as fout:
                fout.write(f'gitdir: {real_gitdir}\n')

            worker._setup_worktrees(tmpdir, git_dir, 1)

        self.assertEqual(mock_git.call_count, 1)  # prune only

    @mock.patch('buildman.worker._run_git')
    def test_replaces_stale_clone(self, mock_git):
        """Test that a full .git directory (old clone) is replaced"""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = os.path.join(tmpdir, '.git')
            os.makedirs(git_dir)

            thread_dir = os.path.join(tmpdir, '.bm-work', '00')
            clone_git = os.path.join(thread_dir, '.git')
            os.makedirs(os.path.join(clone_git, 'objects'))

            worker._setup_worktrees(tmpdir, git_dir, 1)
            self.assertFalse(os.path.isdir(clone_git))

        self.assertEqual(mock_git.call_count, 2)  # prune + add

    @mock.patch('buildman.worker._run_git')
    def test_stale_dot_git_file(self, mock_git):
        """Test removing stale .git file pointing to non-existent dir"""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = os.path.join(tmpdir, '.git')
            os.makedirs(git_dir)

            thread_dir = os.path.join(tmpdir, '.bm-work', '00')
            os.makedirs(thread_dir)
            dot_git = os.path.join(thread_dir, '.git')
            with open(dot_git, 'w', encoding='utf-8') as fout:
                fout.write('gitdir: /nonexistent\n')

            worker._setup_worktrees(tmpdir, git_dir, 1)
            self.assertFalse(os.path.isfile(dot_git))

        self.assertGreaterEqual(mock_git.call_count, 2)


class TestRunGit(unittest.TestCase):
    """Test _run_git()"""

    @mock.patch('buildman.worker.subprocess.Popen')
    def test_success(self, mock_popen):
        """Test successful git command"""
        proc = mock.Mock()
        proc.communicate.return_value = (b'', b'')
        proc.returncode = 0
        mock_popen.return_value = proc
        worker._run_git('status', cwd='/tmp')

    @mock.patch('buildman.worker.subprocess.Popen')
    def test_failure(self, mock_popen):
        """Test failed git command"""
        proc = mock.Mock()
        proc.communicate.return_value = (b'', b'fatal: bad ref\n')
        proc.returncode = 128
        mock_popen.return_value = proc
        with self.assertRaises(OSError) as ctx:
            worker._run_git('checkout', 'bad', cwd='/tmp')
        self.assertIn('bad ref', str(ctx.exception))

    @mock.patch('buildman.worker.subprocess.Popen')
    def test_timeout(self, mock_popen):
        """Test git command timeout"""
        proc = mock.Mock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired('git', 30),
            (b'', b''),
        ]
        mock_popen.return_value = proc
        with self.assertRaises(OSError) as ctx:
            worker._run_git('fetch', cwd='/tmp', timeout=30)
        self.assertIn('timed out', str(ctx.exception))


class TestResolveGitDir(unittest.TestCase):
    """Test _resolve_git_dir()"""

    def test_directory(self):
        """Test with a regular .git directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = os.path.join(tmpdir, '.git')
            os.makedirs(git_dir)
            self.assertEqual(worker._resolve_git_dir(git_dir), git_dir)

    def test_gitdir_file_absolute(self):
        """Test with a .git file with absolute path"""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_git = os.path.join(tmpdir, 'real.git')
            os.makedirs(real_git)
            dot_git = os.path.join(tmpdir, '.git')
            with open(dot_git, 'w', encoding='utf-8') as fout:
                fout.write(f'gitdir: {real_git}\n')
            self.assertEqual(
                worker._resolve_git_dir(dot_git), real_git)

    def test_gitdir_file_relative(self):
        """Test .git file with relative path"""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_git = os.path.join(tmpdir, 'worktrees', 'wt1')
            os.makedirs(real_git)
            dot_git = os.path.join(tmpdir, '.git')
            with open(dot_git, 'w', encoding='utf-8') as fout:
                fout.write('gitdir: worktrees/wt1\n')
            self.assertEqual(
                worker._resolve_git_dir(dot_git),
                os.path.join(tmpdir, 'worktrees', 'wt1'))


class TestProcessManagement(unittest.TestCase):
    """Test _kill_group(), _dbg() and signal handling"""

    @mock.patch('os.killpg')
    def test_kill_group_not_leader(self, mock_killpg):
        """Test _kill_group when not group leader"""
        old = worker._is_group_leader
        worker._is_group_leader = False
        worker._kill_group()
        mock_killpg.assert_not_called()
        worker._is_group_leader = old

    @mock.patch('os.killpg')
    @mock.patch('os.getpgrp', return_value=12345)
    def test_kill_group_leader(self, _mock_grp, mock_killpg):
        """Test _kill_group when group leader"""
        old = worker._is_group_leader
        worker._is_group_leader = True
        worker._kill_group()
        mock_killpg.assert_called_once()
        worker._is_group_leader = old

    @mock.patch('os.killpg', side_effect=OSError('no perm'))
    @mock.patch('os.getpgrp', return_value=12345)
    def test_kill_group_fails(self, _mock_grp, _mock_killpg):
        """Test _kill_group handles OSError"""
        old = worker._is_group_leader
        worker._is_group_leader = True
        worker._kill_group()  # should not raise
        worker._is_group_leader = old

    def test_dbg_off(self):
        """Test _dbg when debug is off"""
        old = worker._debug
        worker._debug = False
        worker._dbg('test message')  # should not raise
        worker._debug = old

    def test_dbg_on(self):
        """Test _dbg when debug is on"""
        old = worker._debug
        worker._debug = True
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            worker._dbg('hello')
            self.assertIn('hello', err.getvalue())
        worker._debug = old

    def test_dbg_stderr_oserror(self):
        """Test _dbg handles OSError from stderr"""
        old = worker._debug
        worker._debug = True
        err = mock.Mock()
        err.write.side_effect = OSError('broken pipe')
        with mock.patch('sys.stderr', err):
            worker._dbg('will fail')  # should not raise
        worker._debug = old

    @mock.patch('buildman.worker._kill_group')
    @mock.patch('os._exit')
    def test_exit_handler(self, mock_exit, mock_kill):
        """Test signal handler calls _kill_group and os._exit"""
        handlers = {}
        orig_signal = signal.signal

        def capture_signal(signum, handler):
            handlers[signum] = handler
            return orig_signal(signum, signal.SIG_DFL)

        with mock.patch('signal.signal', side_effect=capture_signal), \
             mock.patch('buildman.worker.toolchain_mod.Toolchains'), \
             mock.patch('sys.stdin', io.StringIO('{"cmd":"quit"}\n')), \
             mock.patch('sys.stdout', io.StringIO()):
            worker.run_worker()

        handler = handlers.get(signal.SIGTERM)
        self.assertIsNotNone(handler)
        mock_kill.reset_mock()
        mock_exit.reset_mock()
        handler(signal.SIGTERM, None)
        mock_kill.assert_called()
        mock_exit.assert_called_with(1)


class TestDoWorker(unittest.TestCase):
    """Test do_worker()"""

    @mock.patch('buildman.worker.run_worker', return_value=0)
    @mock.patch('os.setpgrp')
    @mock.patch('os.getpid', return_value=100)
    @mock.patch('os.getpgrp', return_value=100)
    def test_start(self, _grp, _pid, _setpgrp, mock_run):
        """Test do_worker sets process group and runs"""
        self.assertEqual(worker.do_worker(debug=False), 0)
        mock_run.assert_called_once_with(False)

    @mock.patch('buildman.worker.run_worker', return_value=0)
    @mock.patch('os.setpgrp', side_effect=OSError('not allowed'))
    @mock.patch('os.getpid', return_value=100)
    @mock.patch('os.getpgrp', return_value=1)
    def test_setpgrp_fails(self, _grp, _pid, _setpgrp, _mock_run):
        """Test do_worker handles setpgrp failure"""
        self.assertEqual(worker.do_worker(debug=False), 0)


if __name__ == '__main__':
    unittest.main()
