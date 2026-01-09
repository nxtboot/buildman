# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Google LLC
# Written by Simon Glass <sjg@chromium.org>

"""Unit tests for builder.py"""

import os
import shutil
import unittest
from unittest import mock

from buildman import builder
from buildman import builderthread
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


if __name__ == '__main__':
    unittest.main()
