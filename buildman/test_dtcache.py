# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Google LLC
#
"""Tests for the dtcache module"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import command

from buildman import dtcache


class TestDtcCache(unittest.TestCase):
    """Tests for the shared dtc/pylibfdt build cache"""
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.src_dir = os.path.join(self.base_dir, 'src')
        self.dtc_dir = os.path.join(self.src_dir, dtcache.DTC_SUBPATH)
        os.makedirs(self.dtc_dir)
        self.cache_dir = os.path.join(self.base_dir, 'cache')

    def tearDown(self):
        shutil.rmtree(self.base_dir)

    def write_src(self, fname, data):
        """Write a source file into the dtc directory"""
        with open(os.path.join(self.dtc_dir, fname), 'w',
                  encoding='utf-8') as outf:
            outf.write(data)

    def test_hash(self):
        """Test that the hash changes only when the source changes"""
        self.write_src('dtc.c', 'int main(void) { return 0; }')
        first = dtcache.hash_src_tree(self.src_dir)
        self.assertEqual(first, dtcache.hash_src_tree(self.src_dir))

        self.write_src('dtc.c', 'int main(void) { return 1; }')
        second = dtcache.hash_src_tree(self.src_dir)
        self.assertNotEqual(first, second)

        self.write_src('extra.c', '')
        self.assertNotEqual(second, dtcache.hash_src_tree(self.src_dir))

    def test_hash_missing(self):
        """Test hashing a tree with no dtc directory"""
        self.assertIsNone(dtcache.hash_src_tree(self.base_dir))

    def test_hash_unreadable(self):
        """Test that a file which cannot be read is left out of the hash"""
        self.write_src('dtc.c', 'hello')
        expect = dtcache.hash_src_tree(self.src_dir)
        os.symlink('missing', os.path.join(self.dtc_dir, 'broken.c'))
        self.assertEqual(expect, dtcache.hash_src_tree(self.src_dir))

    def test_obtain_no_dtc(self):
        """Test that there is no shared build without a dtc directory"""
        cache = dtcache.DtcCache(self.cache_dir, num_jobs=1)
        with mock.patch.object(command, 'run_one') as run_one:
            self.assertIsNone(cache.obtain(self.base_dir))
        run_one.assert_not_called()

    def _setup_result(self, key):
        """Create a plausible cache entry for the given key"""
        out_dir = os.path.join(self.cache_dir, key[:dtcache.HASH_LEN],
                               dtcache.DTC_SUBPATH)
        pylibfdt = os.path.join(out_dir, 'pylibfdt')
        os.makedirs(pylibfdt)
        with open(os.path.join(out_dir, 'dtc'), 'w', encoding='utf-8'):
            pass
        with open(os.path.join(pylibfdt, '_libfdt.so'), 'w',
                  encoding='utf-8'):
            pass

    def test_reuse(self):
        """Test that an existing cache entry is used without building"""
        self.write_src('dtc.c', 'hello')
        key = dtcache.hash_src_tree(self.src_dir)
        self._setup_result(key)

        cache = dtcache.DtcCache(self.cache_dir, num_jobs=1)
        with mock.patch.object(command, 'run_one') as run_one:
            result = cache.obtain(self.src_dir)
        run_one.assert_not_called()
        self.assertIsNotNone(result)
        dtc, pylibfdt = result
        self.assertTrue(os.path.exists(dtc))
        self.assertTrue(os.path.isdir(pylibfdt))

        # A second call should used the cached result
        with mock.patch.object(command, 'run_one') as run_one:
            self.assertEqual(result, cache.obtain(self.src_dir))
        run_one.assert_not_called()

    def test_build(self):
        """Test that a missing cache entry triggers a build"""
        self.write_src('dtc.c', 'hello')
        key = dtcache.hash_src_tree(self.src_dir)

        def fake_make(*cmdline, **_kwargs):
            result = command.CommandResult(return_code=0)
            if 'scripts_dtc' in cmdline:
                self._setup_result(key)
            return result

        cache = dtcache.DtcCache(self.cache_dir, num_jobs=1)
        with mock.patch.object(command, 'run_one',
                               side_effect=fake_make) as run_one:
            result = cache.obtain(self.src_dir, gnu_make='mymake')
        self.assertIsNotNone(result)
        self.assertEqual(2, run_one.call_count)
        self.assertEqual('mymake', run_one.call_args_list[0].args[0])
        self.assertIn('sandbox_defconfig', run_one.call_args_list[0].args)
        self.assertIn('scripts_dtc', run_one.call_args_list[1].args)

    def test_build_failure(self):
        """Test that a failed build returns None and warns only once"""
        self.write_src('dtc.c', 'hello')

        def fake_make(*_cmdline, **_kwargs):
            return command.CommandResult(return_code=1, stderr='swig missing')

        cache = dtcache.DtcCache(self.cache_dir, num_jobs=1)
        with mock.patch.object(command, 'run_one', side_effect=fake_make):
            with mock.patch('builtins.print') as prnt:
                self.assertIsNone(cache.obtain(self.src_dir))
        prnt.assert_called_once()
        self.assertIn('swig missing', prnt.call_args.args[0])

        # The failure is cached, so no new build attempt or warning
        with mock.patch.object(command, 'run_one') as run_one:
            with mock.patch('builtins.print') as prnt:
                self.assertIsNone(cache.obtain(self.src_dir))
        run_one.assert_not_called()
        prnt.assert_not_called()

    def test_build_no_output(self):
        """Test a build which succeeds but does not produce dtc"""
        self.write_src('dtc.c', 'hello')

        def fake_make(*_cmdline, **_kwargs):
            return command.CommandResult(return_code=0)

        cache = dtcache.DtcCache(self.cache_dir, num_jobs=1)
        with mock.patch.object(command, 'run_one', side_effect=fake_make):
            with mock.patch('builtins.print') as prnt:
                self.assertIsNone(cache.obtain(self.src_dir))
        prnt.assert_called_once()
        self.assertIn('did not produce dtc/pylibfdt', prnt.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
