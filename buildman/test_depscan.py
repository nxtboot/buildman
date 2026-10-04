# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Google LLC
#
"""Tests for the depscan module"""

import os
import shutil
import tempfile
import unittest

from buildman import depscan


class TestScanDeps(unittest.TestCase):
    """Tests for scanning .cmd dependency files"""
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.src_dir = os.path.join(self.base_dir, 'src')
        self.out_dir = os.path.join(self.base_dir, 'out')
        os.makedirs(os.path.join(self.out_dir, 'common'))
        os.makedirs(self.src_dir)

    def tearDown(self):
        shutil.rmtree(self.base_dir)

    def write_cmd(self, relpath, text):
        """Write a .cmd file into the output directory"""
        fname = os.path.join(self.out_dir, relpath)
        with open(fname, 'w', encoding='utf-8') as outf:
            outf.write(text)

    def test_scan(self):
        """Test collecting dependencies from a .cmd file"""
        src = os.path.realpath(self.src_dir)
        cmd = f'cmd_common/main.o := cc -c -o common/main.o {src}/common/main.c'
        self.write_cmd('common/.main.o.cmd', f'''{cmd}

source_common/main.o := {src}/common/main.c

deps_common/main.o := \\
    $(wildcard include/config/sys/cbsize.h) \\
  {src}/include/linux/kconfig.h \\
  include/generated/autoconf.h \\
  {src}/include/linux/../../scripts/kconfig.h \\

common/main.o: $(deps_common/main.o)
''')
        deps = depscan.scan_deps(self.out_dir, self.src_dir)
        self.assertEqual(
            {'common/main.c', 'include/linux/kconfig.h', 'scripts/kconfig.h'},
            deps)

    def test_scan_ignores(self):
        """Test that non-cmd files and out-of-tree paths are ignored"""
        src = os.path.realpath(self.src_dir)
        self.write_cmd('common/.main.o.cmd',
                       f'deps := /other/place/file.h {src}/a.c')
        self.write_cmd('common/notdot.cmd', f'deps := {src}/b.c')
        self.write_cmd('common/.main.o.d', f'deps := {src}/c.c')
        deps = depscan.scan_deps(self.out_dir, self.src_dir)
        self.assertEqual({'a.c'}, deps)

    def test_scan_unreadable(self):
        """Test that a .cmd file which cannot be read is skipped"""
        src = os.path.realpath(self.src_dir)
        self.write_cmd('common/.main.o.cmd', f'deps := {src}/a.c')
        os.symlink('missing', os.path.join(self.out_dir, 'common',
                                           '.other.o.cmd'))
        deps = depscan.scan_deps(self.out_dir, self.src_dir)
        self.assertEqual({'a.c'}, deps)


class TestCanSkip(unittest.TestCase):
    """Tests for deciding whether changes can affect a board"""
    def setUp(self):
        self.deps = {'common/main.c', 'include/command.h',
                     'arch/arm/dts/rk3399-u-boot.dtsi'}
        self.defconfigs = {'snow_defconfig'}

    def check(self, fname):
        """Check a single file against the standard dependencies"""
        return depscan.can_skip([fname], self.deps, self.defconfigs)

    def test_dep_files(self):
        """Test files which are in the dependency list"""
        self.assertFalse(self.check('common/main.c'))
        self.assertFalse(self.check('include/command.h'))
        self.assertFalse(self.check('arch/arm/dts/rk3399-u-boot.dtsi'))

    def test_unused_source(self):
        """Test source files which the board does not use"""
        self.assertTrue(self.check('drivers/video/simplefb.c'))
        self.assertTrue(self.check('include/other.h'))
        self.assertTrue(self.check('arch/m68k/lib/traps.S'))
        self.assertTrue(self.check('arch/arm/dts/other-board.dts'))

    def test_safe_files(self):
        """Test files which can never affect the build"""
        self.assertTrue(self.check('doc/build/gcc.rst'))
        self.assertTrue(self.check('README.md'))
        self.assertTrue(self.check('drivers/video/MAINTAINERS'))
        self.assertTrue(self.check('.mailmap'))
        self.assertTrue(self.check('tools/buildman/builder.py'))
        self.assertTrue(self.check('tools/patman/patman.rst'))
        self.assertTrue(self.check('test/py/tests/test_ut.py'))

    def test_risky_files(self):
        """Test files which must always cause a rebuild"""
        self.assertFalse(self.check('Makefile'))
        self.assertFalse(self.check('common/Makefile'))
        self.assertFalse(self.check('common/Kconfig'))
        self.assertFalse(self.check('scripts/Makefile.lib'))
        self.assertFalse(self.check('scripts/dtc/dtc-parser.y'))
        self.assertFalse(self.check('scripts/kconfig/conf.c'))
        self.assertFalse(self.check('tools/binman/ftest.py'))
        self.assertFalse(self.check('board/sandbox/README.sandbox'))
        self.assertFalse(self.check('include/env/common.env'))
        self.assertFalse(self.check('board/samsung/snow/snow.cfg'))

    def test_tools_source(self):
        """Test that tools sources rely on the dependency list"""
        # mkimage is built by every board, so it appears in the dependency
        # list and must force a rebuild; a tool source which is not built
        # for this board cannot affect it
        self.deps.add('tools/mkimage.c')
        self.assertFalse(self.check('tools/mkimage.c'))
        self.assertTrue(self.check('tools/mxsimage.c'))

    def test_ci_files(self):
        """Test that CI configuration cannot affect the build"""
        self.assertTrue(self.check('.gitlab-ci.yml'))
        self.assertTrue(self.check('.azure-pipelines.yml'))

    def test_defconfigs(self):
        """Test that only the board's own defconfig forces a rebuild"""
        self.assertFalse(self.check('configs/snow_defconfig'))
        self.assertTrue(self.check('configs/sandbox_defconfig'))

    def test_multiple(self):
        """Test a set of files, all of which must be safe to skip"""
        self.assertTrue(depscan.can_skip(
            ['drivers/video/simplefb.c', 'doc/README.rst'],
            self.deps, self.defconfigs))
        self.assertFalse(depscan.can_skip(
            ['drivers/video/simplefb.c', 'common/main.c'],
            self.deps, self.defconfigs))

    def test_no_deps(self):
        """Test that an empty dependency set prevents skipping"""
        self.assertFalse(depscan.can_skip(['doc/README.rst'], set(),
                                          self.defconfigs))


if __name__ == '__main__':
    unittest.main()
