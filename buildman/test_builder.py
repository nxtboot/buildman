# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Google LLC
# Written by Simon Glass <sjg@chromium.org>

"""Unit tests for builder.py"""

import unittest

from buildman import builder
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


if __name__ == '__main__':
    unittest.main()
