# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Google LLC
# Written by Simon Glass <sjg@chromium.org>

"""Unit tests for builder.py"""

from datetime import datetime
import os
import shutil
import unittest
from unittest import mock

from buildman import builder
from buildman import builderthread
from buildman.outcome import (OUTCOME_OK, OUTCOME_WARNING, OUTCOME_ERROR,
                              OUTCOME_UNKNOWN)
from u_boot_pylib import gitutil
from u_boot_pylib import terminal


class TestPrintFuncSizeDetail(unittest.TestCase):
    """Tests for Builder.print_func_size_detail()"""

    def setUp(self):
        """Set up test fixtures"""
        # Create a minimal Builder for testing
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp', git_dir=None, num_threads=0,
            num_jobs=1)
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    def test_no_change(self):
        """Test with no size changes"""
        old = {'func_a': 100, 'func_b': 200}
        new = {'func_a': 100, 'func_b': 200}

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        # No output when there are no changes
        self.assertEqual(len(lines), 0)

    def test_function_grows(self):
        """Test when a function grows in size"""
        old = {'func_a': 100}
        new = {'func_a': 150}

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        text = '\n'.join(line.text for line in lines)
        self.assertIn('u-boot', text)
        self.assertIn('func_a', text)
        self.assertIn('grow:', text)
        # Check old, new and delta values appear
        self.assertIn('100', text)
        self.assertIn('150', text)
        self.assertIn('+50', text)

    def test_function_shrinks(self):
        """Test when a function shrinks in size"""
        old = {'func_a': 200}
        new = {'func_a': 150}

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        text = '\n'.join(line.text for line in lines)
        self.assertIn('func_a', text)
        self.assertIn('-50', text)

    def test_function_added(self):
        """Test when a new function is added"""
        old = {'func_a': 100}
        new = {'func_a': 100, 'func_b': 200}

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        text = '\n'.join(line.text for line in lines)
        self.assertIn('func_b', text)
        self.assertIn('add:', text)
        # New function shows '-' for old value
        self.assertIn('-', text)
        self.assertIn('200', text)

    def test_function_removed(self):
        """Test when a function is removed"""
        old = {'func_a': 100, 'func_b': 200}
        new = {'func_a': 100}

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        text = '\n'.join(line.text for line in lines)
        self.assertIn('func_b', text)
        # Removed function shows '-' for new value
        self.assertIn('-200', text)

    def test_mixed_changes(self):
        """Test with a mix of added, removed, grown and shrunk functions"""
        old = {
            'unchanged': 100,
            'grown': 200,
            'shrunk': 300,
            'removed': 400,
        }
        new = {
            'unchanged': 100,
            'grown': 250,
            'shrunk': 250,
            'added': 150,
        }

        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', old, new)
        lines = terminal.get_print_test_lines()

        text = '\n'.join(line.text for line in lines)
        # Check all changed functions appear
        self.assertIn('grown', text)
        self.assertIn('shrunk', text)
        self.assertIn('removed', text)
        self.assertIn('added', text)
        # unchanged should not appear (no delta)
        # Check the header line appears
        self.assertIn('function', text)
        self.assertIn('old', text)
        self.assertIn('new', text)
        self.assertIn('delta', text)

    def test_empty_dicts(self):
        """Test with empty dictionaries"""
        terminal.get_print_test_lines()  # Clear
        self.builder.print_func_size_detail('u-boot', {}, {})
        lines = terminal.get_print_test_lines()

        # No output when both dicts are empty
        self.assertEqual(len(lines), 0)


class TestPrepareThread(unittest.TestCase):
    """Tests for Builder._prepare_thread()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    @mock.patch.object(builderthread, 'mkdir')
    def test_no_setup_git(self, mock_mkdir):
        """Test with setup_git=None (no git setup needed)"""
        self.builder._prepare_thread(0, None)
        mock_mkdir.assert_called_once()

    @mock.patch.object(gitutil, 'fetch')
    @mock.patch.object(os.path, 'isdir', return_value=True)
    @mock.patch.object(builderthread, 'mkdir')
    def test_existing_clone(self, mock_mkdir, mock_isdir, mock_fetch):
        """Test with existing git clone (fetches updates)"""
        terminal.get_print_test_lines()  # Clear
        self.builder._prepare_thread(0, 'clone')

        mock_fetch.assert_called_once()
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('Fetching repo', lines[0].text)

    @mock.patch.object(os.path, 'isfile', return_value=True)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_existing_worktree(self, mock_mkdir, mock_isdir, mock_isfile):
        """Test with existing worktree (no action needed)"""
        terminal.get_print_test_lines()  # Clear
        self.builder._prepare_thread(0, 'worktree')

        # No git operations should be called
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 0)

    @mock.patch.object(os.path, 'exists', return_value=True)
    @mock.patch.object(os.path, 'isfile', return_value=False)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_invalid_git_dir(self, mock_mkdir, mock_isdir, mock_isfile,
                             mock_exists):
        """Test with git_dir that exists but is neither file nor directory"""
        with self.assertRaises(ValueError) as ctx:
            self.builder._prepare_thread(0, 'clone')
        self.assertIn('exists, but is not a file or a directory',
                      str(ctx.exception))

    @mock.patch.object(gitutil, 'add_worktree')
    @mock.patch.object(os.path, 'exists', return_value=False)
    @mock.patch.object(os.path, 'isfile', return_value=False)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_create_worktree(self, mock_mkdir, mock_isdir, mock_isfile,
                             mock_exists, mock_add_worktree):
        """Test creating a new worktree"""
        terminal.get_print_test_lines()  # Clear
        self.builder._prepare_thread(0, 'worktree')

        mock_add_worktree.assert_called_once()
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('Checking out worktree', lines[0].text)

    @mock.patch.object(gitutil, 'clone')
    @mock.patch.object(os.path, 'exists', return_value=False)
    @mock.patch.object(os.path, 'isfile', return_value=False)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_create_clone(self, mock_mkdir, mock_isdir, mock_isfile,
                          mock_exists, mock_clone):
        """Test creating a new clone"""
        terminal.get_print_test_lines()  # Clear
        self.builder._prepare_thread(0, 'clone')

        mock_clone.assert_called_once()
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('Cloning repo', lines[0].text)

    @mock.patch.object(gitutil, 'clone')
    @mock.patch.object(os.path, 'exists', return_value=False)
    @mock.patch.object(os.path, 'isfile', return_value=False)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_create_clone_with_true(self, mock_mkdir, mock_isdir, mock_isfile,
                                    mock_exists, mock_clone):
        """Test creating a clone when setup_git=True"""
        terminal.get_print_test_lines()  # Clear
        self.builder._prepare_thread(0, True)

        mock_clone.assert_called_once()

    @mock.patch.object(os.path, 'exists', return_value=False)
    @mock.patch.object(os.path, 'isfile', return_value=False)
    @mock.patch.object(os.path, 'isdir', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_invalid_setup_git(self, mock_mkdir, mock_isdir, mock_isfile,
                               mock_exists):
        """Test with invalid setup_git value"""
        with self.assertRaises(ValueError) as ctx:
            self.builder._prepare_thread(0, 'invalid')
        self.assertIn("Can't setup git repo", str(ctx.exception))


class TestPrepareWorkingSpace(unittest.TestCase):
    """Tests for Builder._prepare_working_space()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    @mock.patch.object(builder.Builder, '_prepare_thread')
    @mock.patch.object(builderthread, 'mkdir')
    def test_no_setup_git(self, mock_mkdir, mock_prepare_thread):
        """Test with setup_git=False"""
        self.builder._prepare_working_space(2, False)

        mock_mkdir.assert_called_once()
        # Should prepare 2 threads with setup_git=False
        self.assertEqual(mock_prepare_thread.call_count, 2)
        mock_prepare_thread.assert_any_call(0, False)
        mock_prepare_thread.assert_any_call(1, False)

    @mock.patch.object(builder.Builder, '_prepare_thread')
    @mock.patch.object(gitutil, 'prune_worktrees')
    @mock.patch.object(gitutil, 'check_worktree_is_available', return_value=True)
    @mock.patch.object(builderthread, 'mkdir')
    def test_worktree_available(self, mock_mkdir, mock_check_worktree,
                                mock_prune, mock_prepare_thread):
        """Test when worktree is available"""
        self.builder._prepare_working_space(3, True)

        mock_check_worktree.assert_called_once()
        mock_prune.assert_called_once()
        # Should prepare 3 threads with setup_git='worktree'
        self.assertEqual(mock_prepare_thread.call_count, 3)
        mock_prepare_thread.assert_any_call(0, 'worktree')
        mock_prepare_thread.assert_any_call(1, 'worktree')
        mock_prepare_thread.assert_any_call(2, 'worktree')

    @mock.patch.object(builder.Builder, '_prepare_thread')
    @mock.patch.object(gitutil, 'check_worktree_is_available', return_value=False)
    @mock.patch.object(builderthread, 'mkdir')
    def test_worktree_not_available(self, mock_mkdir, mock_check_worktree,
                                    mock_prepare_thread):
        """Test when worktree is not available (falls back to clone)"""
        self.builder._prepare_working_space(2, True)

        mock_check_worktree.assert_called_once()
        # Should prepare 2 threads with setup_git='clone'
        self.assertEqual(mock_prepare_thread.call_count, 2)
        mock_prepare_thread.assert_any_call(0, 'clone')
        mock_prepare_thread.assert_any_call(1, 'clone')

    @mock.patch.object(builder.Builder, '_prepare_thread')
    @mock.patch.object(builderthread, 'mkdir')
    def test_zero_threads(self, mock_mkdir, mock_prepare_thread):
        """Test with max_threads=0 (should still prepare 1 thread)"""
        self.builder._prepare_working_space(0, False)

        # Should prepare at least 1 thread
        self.assertEqual(mock_prepare_thread.call_count, 1)
        mock_prepare_thread.assert_called_with(0, False)

    @mock.patch.object(builder.Builder, '_prepare_thread')
    @mock.patch.object(builderthread, 'mkdir')
    def test_no_git_dir(self, mock_mkdir, mock_prepare_thread):
        """Test with no git_dir set"""
        self.builder.git_dir = None
        self.builder._prepare_working_space(2, True)

        # setup_git should remain True but git operations skipped
        self.assertEqual(mock_prepare_thread.call_count, 2)
        mock_prepare_thread.assert_any_call(0, True)
        mock_prepare_thread.assert_any_call(1, True)


class TestShowNotBuilt(unittest.TestCase):
    """Tests for Builder._show_not_built()"""

    def setUp(self):
        """Set up test fixtures"""
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    def _make_outcome(self, rc, err_lines=None):
        """Create a mock outcome with a given return code"""
        outcome = mock.Mock()
        outcome.rc = rc
        outcome.err_lines = err_lines if err_lines else []
        return outcome

    def _show_not_built(self, board_selected, board_dict):
        """Helper to call Builder._show_not_built"""
        builder.Builder._show_not_built(board_selected, board_dict)

    def test_all_boards_built(self):
        """Test when all selected boards were built successfully"""
        board_selected = {'board1': None, 'board2': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_OK),
            'board2': self._make_outcome(OUTCOME_OK),
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        # No output when all boards were built
        self.assertEqual(len(lines), 0)

    def test_some_boards_unknown(self):
        """Test when some boards have OUTCOME_UNKNOWN (e.g. missing toolchain)"""
        board_selected = {'board1': None, 'board2': None, 'board3': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_OK),
            'board2': self._make_outcome(OUTCOME_UNKNOWN),
            'board3': self._make_outcome(OUTCOME_UNKNOWN),
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        self.assertEqual(len(lines), 1)
        self.assertIn('Boards not built', lines[0].text)
        self.assertIn('2', lines[0].text)  # Count of not-built boards
        self.assertIn('board2', lines[0].text)
        self.assertIn('board3', lines[0].text)

    def test_all_boards_unknown(self):
        """Test when all boards have OUTCOME_UNKNOWN"""
        board_selected = {'board1': None, 'board2': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_UNKNOWN),
            'board2': self._make_outcome(OUTCOME_UNKNOWN),
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        self.assertEqual(len(lines), 1)
        self.assertIn('Boards not built', lines[0].text)
        self.assertIn('board1', lines[0].text)
        self.assertIn('board2', lines[0].text)

    def test_build_error_not_counted(self):
        """Test that build errors (not toolchain) are not counted as 'not built'"""
        board_selected = {'board1': None, 'board2': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_OK),
            'board2': self._make_outcome(OUTCOME_ERROR,
                                         ['error: some build error']),
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        # Build errors are still "built", just with errors
        self.assertEqual(len(lines), 0)

    def test_toolchain_error_counted(self):
        """Test that toolchain errors are counted as 'not built'"""
        board_selected = {'board1': None, 'board2': None, 'board3': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_OK),
            'board2': self._make_outcome(OUTCOME_ERROR,
                                         ['Tool chain error for arm: not found']),
            'board3': self._make_outcome(OUTCOME_ERROR,
                                         ['error: some build error']),
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        # Only toolchain errors count as "not built"
        self.assertEqual(len(lines), 1)
        self.assertIn('Boards not built', lines[0].text)
        self.assertIn('1', lines[0].text)
        self.assertIn('board2', lines[0].text)
        self.assertNotIn('board3', lines[0].text)

    def test_board_not_in_dict(self):
        """Test that boards missing from board_dict are counted as 'not built'"""
        board_selected = {'board1': None, 'board2': None, 'board3': None}
        board_dict = {
            'board1': self._make_outcome(OUTCOME_OK),
            # board2 and board3 are not in board_dict
        }

        terminal.get_print_test_lines()  # Clear
        builder.Builder._show_not_built(board_selected, board_dict)
        lines = terminal.get_print_test_lines()

        self.assertEqual(len(lines), 1)
        self.assertIn('Boards not built', lines[0].text)
        self.assertIn('2', lines[0].text)
        self.assertIn('board2', lines[0].text)
        self.assertIn('board3', lines[0].text)


class TestPrepareOutputSpace(unittest.TestCase):
    """Tests for Builder._prepare_output_space() and _get_output_space_removals()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    def test_get_removals_no_commits(self):
        """Test _get_output_space_removals with no commits"""
        self.builder.commits = None
        result = self.builder._get_output_space_removals()
        self.assertEqual(result, [])

    @mock.patch.object(builder.Builder, 'get_output_dir')
    @mock.patch('glob.glob')
    def test_get_removals_no_old_dirs(self, mock_glob, mock_get_output_dir):
        """Test _get_output_space_removals with no old directories"""
        self.builder.commits = [mock.Mock()]  # Non-empty to trigger logic
        self.builder.commit_count = 1
        mock_get_output_dir.return_value = '/tmp/test/01_gabcdef1_test'
        mock_glob.return_value = []

        result = self.builder._get_output_space_removals()
        self.assertEqual(result, [])

    @mock.patch.object(builder.Builder, 'get_output_dir')
    @mock.patch('glob.glob')
    def test_get_removals_with_old_dirs(self, mock_glob, mock_get_output_dir):
        """Test _get_output_space_removals identifies old directories"""
        self.builder.commits = [mock.Mock()]  # Non-empty to trigger logic
        self.builder.commit_count = 1
        mock_get_output_dir.return_value = '/tmp/test/01_gabcdef1_current'
        # Simulate old directories with buildman naming pattern
        mock_glob.return_value = [
            '/tmp/test/01_gabcdef1_current',  # Current - should not remove
            '/tmp/test/02_g1234567_old',      # Old - should remove
            '/tmp/test/random_dir',           # Not matching pattern - keep
        ]

        result = self.builder._get_output_space_removals()
        self.assertEqual(result, ['/tmp/test/02_g1234567_old'])

    @mock.patch.object(builder.Builder, '_get_output_space_removals')
    def test_prepare_output_space_nothing_to_remove(self, mock_get_removals):
        """Test _prepare_output_space with nothing to remove"""
        mock_get_removals.return_value = []
        terminal.get_print_test_lines()  # Clear

        self.builder._prepare_output_space()

        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 0)

    @mock.patch.object(shutil, 'rmtree')
    @mock.patch.object(builder.Builder, '_get_output_space_removals')
    def test_prepare_output_space_removes_dirs(self, mock_get_removals,
                                               mock_rmtree):
        """Test _prepare_output_space removes old directories"""
        mock_get_removals.return_value = ['/tmp/test/old1', '/tmp/test/old2']
        terminal.get_print_test_lines()  # Clear

        self.builder._prepare_output_space()

        # Check rmtree was called for each directory
        self.assertEqual(mock_rmtree.call_count, 2)
        mock_rmtree.assert_any_call('/tmp/test/old1')
        mock_rmtree.assert_any_call('/tmp/test/old2')

        # Check 'Removing' message was printed
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('Removing 2 old build directories', lines[0].text)
        # Check newline=False was used (message should be overwritten)
        self.assertFalse(lines[0].newline)


class TestCheckOutputForLoop(unittest.TestCase):
    """Tests for Builder._check_output_for_loop()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)
        # Reset state before each test
        self.builder._restarting_config = False
        self.builder._terminated = False

    def test_no_restart_message(self):
        """Test that normal output does not trigger termination"""
        result = self.builder._check_output_for_loop(b'Building target...')

        self.assertFalse(result)
        self.assertFalse(self.builder._restarting_config)
        self.assertFalse(self.builder._terminated)

    def test_restart_message_sets_flag(self):
        """Test that 'Restart config' sets the restarting flag"""
        result = self.builder._check_output_for_loop(b'Restart config...')

        self.assertFalse(result)  # No loop detected yet
        self.assertTrue(self.builder._restarting_config)
        self.assertFalse(self.builder._terminated)

    def test_single_new_item_no_loop(self):
        """Test that a single NEW item after restart is not a loop"""
        self.builder._restarting_config = True

        result = self.builder._check_output_for_loop(
            b'(CONFIG_ITEM) [] (NEW)')

        self.assertFalse(result)
        self.assertFalse(self.builder._terminated)

    def test_different_new_items_no_loop(self):
        """Test that different NEW items do not trigger a loop"""
        self.builder._restarting_config = True

        result = self.builder._check_output_for_loop(
            b'(CONFIG_A) [] (NEW)\n(CONFIG_B) [] (NEW)')

        self.assertFalse(result)
        self.assertFalse(self.builder._terminated)

    def test_duplicate_items_triggers_loop(self):
        """Test that duplicate NEW items trigger loop detection"""
        self.builder._restarting_config = True

        result = self.builder._check_output_for_loop(
            b'(CONFIG_ITEM) [] (NEW)\n(CONFIG_ITEM) [] (NEW)')

        self.assertTrue(result)
        self.assertTrue(self.builder._terminated)

    def test_no_loop_without_restart(self):
        """Test that duplicates without restart flag do not trigger loop"""
        # _restarting_config is False by default

        result = self.builder._check_output_for_loop(
            b'(CONFIG_ITEM) [] (NEW)\n(CONFIG_ITEM) [] (NEW)')

        self.assertFalse(result)
        self.assertFalse(self.builder._terminated)

    def test_multiple_items_one_duplicate(self):
        """Test loop detection with multiple items, one duplicated"""
        self.builder._restarting_config = True

        result = self.builder._check_output_for_loop(
            b'(CONFIG_A) [] (NEW)\n(CONFIG_B) [] (NEW)\n(CONFIG_A) [] (NEW)')

        self.assertTrue(result)
        self.assertTrue(self.builder._terminated)


class TestMake(unittest.TestCase):
    """Tests for Builder.make()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)

    @mock.patch('buildman.builder.command.run_one')
    def test_make_basic(self, mock_run_one):
        """Test basic make execution"""
        mock_result = mock.Mock()
        mock_result.stdout = 'build output'
        mock_result.stderr = ''
        mock_result.combined = 'build output'
        mock_run_one.return_value = mock_result

        result = self.builder.make(None, None, None, '/tmp/build', 'all')

        self.assertEqual(result, mock_result)
        mock_run_one.assert_called_once()
        # Check make was called with correct args
        call_args = mock_run_one.call_args
        self.assertEqual(call_args[0][0], 'make')
        self.assertEqual(call_args[0][1], 'all')
        self.assertEqual(call_args[1]['cwd'], '/tmp/build')

    @mock.patch('buildman.builder.command.run_one')
    def test_make_with_loop_detection(self, mock_run_one):
        """Test make adds helpful message when loop is detected"""
        mock_result = mock.Mock()
        mock_result.stdout = ''
        mock_result.stderr = 'config error'
        mock_result.combined = 'config error'
        mock_run_one.return_value = mock_result

        # Simulate loop detection by setting _terminated during the call
        def side_effect(*args, **kwargs):
            # Simulate output_func being called with loop data
            output_func = kwargs.get('output_func')
            if output_func:
                self.builder._restarting_config = True
                output_func(None, b'(CONFIG_X) [] (NEW)\n(CONFIG_X) [] (NEW)')
            return mock_result

        mock_run_one.side_effect = side_effect

        result = self.builder.make(None, None, None, '/tmp/build', 'defconfig')

        # Check helpful message was appended
        self.assertIn('did you define an int/hex Kconfig', result.stderr)

    @mock.patch('buildman.builder.command.run_one')
    def test_make_verbose_build(self, mock_run_one):
        """Test make prepends command in verbose mode"""
        mock_result = mock.Mock()
        mock_result.stdout = 'output'
        mock_result.stderr = ''
        mock_result.combined = 'output'
        mock_run_one.return_value = mock_result

        self.builder.verbose_build = True

        result = self.builder.make(None, None, None, '/tmp/build', 'all', '-j4')

        # Check command was prepended to stdout and combined
        self.assertIn('make all -j4', result.stdout)
        self.assertIn('make all -j4', result.combined)

    @mock.patch('buildman.builder.command.run_one')
    def test_make_resets_state(self, mock_run_one):
        """Test make resets _restarting_config and _terminated flags"""
        mock_result = mock.Mock()
        mock_result.stdout = ''
        mock_result.stderr = ''
        mock_result.combined = ''
        mock_run_one.return_value = mock_result

        # Set flags to non-default values
        self.builder._restarting_config = True
        self.builder._terminated = True

        self.builder.make(None, None, None, '/tmp/build', 'all')

        # Flags should be reset at the start of make()
        # (they may be set again by output_func, but start fresh)
        # Since mock doesn't call output_func, they stay False
        self.assertFalse(self.builder._restarting_config)
        self.assertFalse(self.builder._terminated)


class TestPrintBuildSummary(unittest.TestCase):
    """Tests for Builder._print_build_summary()"""

    def setUp(self):
        """Set up test fixtures"""
        self.builder = builder.Builder(
            toolchains=None, base_dir='/tmp/test', git_dir='/src/repo',
            num_threads=4, num_jobs=1)
        # Set a start time in the past (less than 1 second ago to avoid
        # duration output)
        self.builder._start_time = datetime.now()
        self.builder.thread_exceptions = []
        terminal.set_print_test_mode()

    def tearDown(self):
        """Clean up after tests"""
        terminal.set_print_test_mode(False)

    def test_basic_count(self):
        """Test basic completed message with just count"""
        self.builder.count = 10
        self.builder.already_done = 0
        self.builder.kconfig_reconfig = 0

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        # First line is blank, second is the message
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].text, '')
        self.assertIn('Completed: 10 total built', lines[1].text)
        self.assertNotIn('previously', lines[1].text)

    def test_all_previously_done(self):
        """Test message when all builds were already done"""
        self.builder.count = 5
        self.builder.already_done = 5
        self.builder.kconfig_reconfig = 0

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        self.assertIn('5 previously', lines[1].text)
        self.assertNotIn('newly', lines[1].text)

    def test_some_newly_built(self):
        """Test message with some previously done and some new"""
        self.builder.count = 10
        self.builder.already_done = 6
        self.builder.kconfig_reconfig = 0

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        self.assertIn('6 previously', lines[1].text)
        self.assertIn('4 newly', lines[1].text)

    def test_with_kconfig_reconfig(self):
        """Test message with kconfig reconfigurations"""
        self.builder.count = 8
        self.builder.already_done = 0
        self.builder.kconfig_reconfig = 3

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        self.assertIn('3 reconfig', lines[1].text)

    def test_thread_exceptions(self):
        """Test message with thread exceptions"""
        self.builder.count = 5
        self.builder.already_done = 0
        self.builder.kconfig_reconfig = 0
        self.builder.thread_exceptions = [Exception('err1'), Exception('err2')]

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        self.assertEqual(len(lines), 3)
        self.assertIn('Failed: 2 thread exceptions', lines[2].text)

    @mock.patch('buildman.builder.datetime')
    def test_duration_and_rate(self, mock_datetime):
        """Test message includes duration and rate for long builds"""
        self.builder.count = 100
        self.builder.already_done = 0
        self.builder.kconfig_reconfig = 0

        # Mock datetime to simulate a 10 second build
        start_time = datetime(2024, 1, 1, 12, 0, 0)
        end_time = datetime(2024, 1, 1, 12, 0, 10)
        self.builder._start_time = start_time
        mock_datetime.now.return_value = end_time
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        self.assertIn('duration', lines[1].text)
        self.assertIn('rate', lines[1].text)
        self.assertIn('10.00', lines[1].text)  # 100 boards / 10 seconds

    @mock.patch('buildman.builder.datetime')
    def test_duration_rounds_up(self, mock_datetime):
        """Test duration rounds up when microseconds >= 500000"""
        self.builder.count = 100
        self.builder.already_done = 0
        self.builder.kconfig_reconfig = 0

        # Mock datetime to simulate a 10.6 second build (should round to 11)
        start_time = datetime(2024, 1, 1, 12, 0, 0)
        end_time = datetime(2024, 1, 1, 12, 0, 10, 600000)  # 10.6 seconds
        self.builder._start_time = start_time
        mock_datetime.now.return_value = end_time
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        terminal.get_print_test_lines()  # Clear
        self.builder._print_build_summary()
        lines = terminal.get_print_test_lines()

        # Duration should be rounded up to 11 seconds
        self.assertIn('0:00:11', lines[1].text)


if __name__ == '__main__':
    unittest.main()
