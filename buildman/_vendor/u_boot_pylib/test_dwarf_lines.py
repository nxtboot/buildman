# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the u_boot_pylib.dwarf_lines module"""

import os
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import dwarf_lines


class TestLinesToRanges(unittest.TestCase):
    """Tests for lines_to_ranges()"""

    def test_empty(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([]), [])

    def test_single(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([5]), [(5, 5)])

    def test_contiguous(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([1, 2, 3, 4]), [(1, 4)])

    def test_gaps(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([1, 2, 4, 5, 6]),
                         [(1, 2), (4, 6)])

    def test_unsorted_and_duplicates(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([6, 1, 2, 2, 5, 4]),
                         [(1, 2), (4, 6)])

    def test_alternating(self):
        self.assertEqual(dwarf_lines.lines_to_ranges([1, 3, 5]),
                         [(1, 1), (3, 3), (5, 5)])


class TestRangeFormat(unittest.TestCase):
    """Tests for format_ranges() and parse_ranges()"""

    def test_format_empty(self):
        self.assertEqual(dwarf_lines.format_ranges([]), '')

    def test_format(self):
        self.assertEqual(
            dwarf_lines.format_ranges([(1, 1), (3, 5), (9, 9)]), '1,3-5,9')

    def test_parse_empty(self):
        self.assertEqual(dwarf_lines.parse_ranges(''), set())
        self.assertEqual(dwarf_lines.parse_ranges('  '), set())

    def test_parse(self):
        self.assertEqual(dwarf_lines.parse_ranges('1,3-5,9'),
                         {1, 3, 4, 5, 9})

    def test_round_trip(self):
        lines = {1, 2, 3, 7, 20, 21, 22, 50}
        text = dwarf_lines.format_ranges(dwarf_lines.lines_to_ranges(lines))
        self.assertEqual(dwarf_lines.parse_ranges(text), lines)


class TestGetReadelf(unittest.TestCase):
    """Tests for _get_readelf()"""

    def setUp(self):
        dwarf_lines._get_readelf.cache_clear()

    def tearDown(self):
        dwarf_lines._get_readelf.cache_clear()

    def test_absolute_path(self):
        """readelf should resolve to an absolute path when on the PATH"""
        with mock.patch.object(dwarf_lines.shutil, 'which',
                               return_value='/usr/bin/readelf') as which:
            self.assertEqual(dwarf_lines._get_readelf(), '/usr/bin/readelf')
            # A second call must not search the PATH again
            self.assertEqual(dwarf_lines._get_readelf(), '/usr/bin/readelf')
            which.assert_called_once()

    def test_fallback(self):
        """Fall back to plain 'readelf' when it is not on the PATH"""
        with mock.patch.object(dwarf_lines.shutil, 'which', return_value=None):
            self.assertEqual(dwarf_lines._get_readelf(), 'readelf')


class TestResolveHeader(unittest.TestCase):
    """Tests for _resolve_header()"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = self.tmp.name
        # srcdir/foo.c and srcdir/sub/bar.c exist; missing.h does not
        with open(os.path.join(self.src, 'foo.c'), 'w'):
            pass
        os.mkdir(os.path.join(self.src, 'sub'))
        with open(os.path.join(self.src, 'sub', 'bar.c'), 'w'):
            pass

    def tearDown(self):
        self.tmp.cleanup()

    def test_absolute(self):
        """An absolute header path is resolved directly"""
        path = os.path.join(self.src, 'foo.c')
        self.assertEqual(dwarf_lines._resolve_header(path, 'sub', self.src, {}),
                         os.path.realpath(path))

    def test_relative_via_obj_dir(self):
        """A relative path resolves against srcdir/obj_dir first"""
        self.assertEqual(
            dwarf_lines._resolve_header('bar.c', 'sub', self.src, {}),
            os.path.realpath(os.path.join(self.src, 'sub', 'bar.c')))

    def test_relative_via_xpl_dir(self):
        """An xPL object's source resolves without the phase directory"""
        self.assertEqual(
            dwarf_lines._resolve_header('bar.c', 'spl/sub', self.src, {}),
            os.path.realpath(os.path.join(self.src, 'sub', 'bar.c')))

    def test_relative_via_srcdir(self):
        """A relative path falls back to srcdir when obj_dir misses"""
        self.assertEqual(
            dwarf_lines._resolve_header('foo.c', 'sub', self.src, {}),
            os.path.realpath(os.path.join(self.src, 'foo.c')))

    def test_unresolved(self):
        """A path that does not exist resolves to None"""
        self.assertIsNone(
            dwarf_lines._resolve_header('missing.h', 'sub', self.src, {}))

    def test_cache_populated_and_reused(self):
        """Results, including None, are cached and reused without re-statting"""
        cache = {}
        dwarf_lines._resolve_header('foo.c', 'sub', self.src, cache)
        dwarf_lines._resolve_header('missing.h', 'sub', self.src, cache)
        self.assertEqual(
            cache[('foo.c', 'sub')],
            os.path.realpath(os.path.join(self.src, 'foo.c')))
        self.assertIsNone(cache[('missing.h', 'sub')])

        # A cached entry is returned verbatim, even if the filesystem changed
        cache[('foo.c', 'sub')] = '/cached/answer'
        self.assertEqual(
            dwarf_lines._resolve_header('foo.c', 'sub', self.src, cache),
            '/cached/answer')


class TestWorker(unittest.TestCase):
    """Tests for worker()"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = self.tmp.name
        with open(os.path.join(self.src, 'foo.c'), 'w'):
            pass
        os.mkdir(os.path.join(self.src, 'sub'))
        with open(os.path.join(self.src, 'sub', 'bar.c'), 'w'):
            pass

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, stdout, returncode=0):
        """Run worker() with readelf mocked to return the given output"""
        obj_path = os.path.join(self.src, 'sub', 'obj.o')
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr='err')
        with mock.patch.object(dwarf_lines.subprocess, 'run',
                               return_value=result):
            return dwarf_lines.worker((obj_path, self.src, self.src, {}))

    def test_parse_and_resolve(self):
        """Lines are attributed to resolved source files"""
        # foo.c resolves via srcdir fallback, bar.c via obj_dir
        stdout = (
            '\nContents of the .debug_line section:\n\n'
            'foo.c:\n'
            'File name            Line number    Starting address\n'
            'foo.c                         10               0\n'
            'foo.c                         11             0x4\n'
            '\n'
            'bar.c:\n'
            'bar.c                         20               0\n')
        lines, err = self._run(stdout)
        self.assertIsNone(err)
        self.assertEqual(dict(lines), {
            os.path.realpath(os.path.join(self.src, 'foo.c')): {10, 11},
            os.path.realpath(os.path.join(self.src, 'sub', 'bar.c')): {20},
        })

    def test_unresolved_header_keeps_previous_file(self):
        """An unresolvable header leaves the current file unchanged"""
        stdout = (
            'bar.c:\n'
            'bar.c                         20               0\n'
            'missing.h:\n'
            'missing.h                     99               0\n')
        lines, err = self._run(stdout)
        self.assertIsNone(err)
        # Line 99 is attributed to bar.c, since missing.h did not resolve
        self.assertEqual(dict(lines), {
            os.path.realpath(os.path.join(self.src, 'sub', 'bar.c')): {20, 99},
        })

    def test_readelf_failure(self):
        """A non-zero readelf exit returns an error and no lines"""
        lines, err = self._run('', returncode=1)
        self.assertFalse(lines)
        self.assertIn('readelf failed', err)


if __name__ == '__main__':
    unittest.main()
