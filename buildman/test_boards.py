# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2024 Google, Inc
#

"""Tests for boards.py"""

import errno
import multiprocessing
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from buildman import board
from buildman import boards
from buildman.boards import Extended
from u_boot_pylib import terminal
from u_boot_pylib import tools


BOARDS = [
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 0', 'board0',  ''],
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 1', 'board1', ''],
    ['Active', 'powerpc', 'powerpc', '', 'Tester', 'PowerPC board 1', 'board2', ''],
    ['Active', 'sandbox', 'sandbox', '', 'Tester', 'Sandbox board', 'board4', ''],
]


class TestBoards(unittest.TestCase):
    """Test boards.py functionality"""

    def setUp(self):
        self._base_dir = tempfile.mkdtemp()
        self._output_dir = tempfile.mkdtemp()
        self._git_dir = os.path.join(self._base_dir, 'src')
        self._buildman_dir = os.path.dirname(os.path.realpath(__file__))
        self._test_dir = os.path.join(self._buildman_dir, 'test')

        # Set up some fake source files
        shutil.copytree(self._test_dir, self._git_dir)

        # Avoid sending any output and clear all terminal output
        terminal.set_print_test_mode()
        terminal.get_print_test_lines()

        self._boards = boards.Boards()
        for brd in BOARDS:
            self._boards.add_board(board.Board(*brd))

    def tearDown(self):
        shutil.rmtree(self._base_dir)
        shutil.rmtree(self._output_dir)

    def test_try_remove(self):
        """Test try_remove() function"""
        # Test removing a file that doesn't exist - should not raise
        boards.try_remove('/nonexistent/path/to/file')

        # Test removing a file that does exist
        fname = os.path.join(self._base_dir, 'test_remove')
        tools.write_file(fname, b'test')
        self.assertTrue(os.path.exists(fname))
        boards.try_remove(fname)
        self.assertFalse(os.path.exists(fname))

    def test_read_boards(self):
        """Test Boards.read_boards() with various field counts"""
        # Test normal boards.cfg file
        boards_cfg = os.path.join(self._base_dir, 'boards.cfg')
        content = '''# Comment line
Active  arm      armv7   -       Tester   ARM_Board_0  board0  config0  maint@test.com
Active  powerpc  ppc     mpc85xx Tester   PPC_Board_1  board2  config2  maint2@test.com

'''
        tools.write_file(boards_cfg, content.encode('utf-8'))

        brds = boards.Boards()
        brds.read_boards(boards_cfg)
        board_list = brds.get_list()
        self.assertEqual(2, len(board_list))
        self.assertEqual('board0', board_list[0].target)
        self.assertEqual('arm', board_list[0].arch)
        self.assertEqual('', board_list[0].soc)  # '-' converted to ''
        self.assertEqual('mpc85xx', board_list[1].soc)

        # Test with fewer than 8 fields
        boards_cfg = os.path.join(self._base_dir, 'boards_short.cfg')
        content = '''Active  arm  armv7  -  Tester  Board  target  config
'''
        tools.write_file(boards_cfg, content.encode('utf-8'))
        brds = boards.Boards()
        brds.read_boards(boards_cfg)
        self.assertEqual(1, len(brds.get_list()))

        # Test with more than 8 fields (extra fields ignored)
        boards_cfg = os.path.join(self._base_dir, 'boards_extra.cfg')
        content = '''Active  arm  armv7  soc  Tester  Board  target  config  maint  extra
'''
        tools.write_file(boards_cfg, content.encode('utf-8'))
        brds = boards.Boards()
        brds.read_boards(boards_cfg)
        self.assertEqual('config', brds.get_list()[0].cfg_name)

    def test_boards_methods(self):
        """Test Boards helper methods: get_dict, get_selected_names, find_by_target"""
        brds = boards.Boards()
        for brd in BOARDS:
            brds.add_board(board.Board(*brd))

        # Test get_dict()
        board_dict = brds.get_dict()
        self.assertEqual(4, len(board_dict))
        self.assertEqual('arm', board_dict['board0'].arch)
        self.assertEqual('sandbox', board_dict['board4'].arch)

        # Test get_selected_names()
        brds.select_boards(['arm'])
        self.assertEqual(['board0', 'board1'], brds.get_selected_names())

        # Test select_boards warning for missing board
        brds2 = boards.Boards()
        for brd in BOARDS:
            brds2.add_board(board.Board(*brd))
        result, warnings = brds2.select_boards([], brds=['nonexistent', 'board0'])
        self.assertEqual(1, len(warnings))
        self.assertIn('nonexistent', warnings[0])

        # Test find_by_target()
        found = brds.find_by_target('board0')
        self.assertEqual('arm', found.arch)

        with terminal.capture() as (stdout, stderr):
            with self.assertRaises(ValueError) as exc:
                brds.find_by_target('nonexistent')
        self.assertIn('nonexistent', str(exc.exception))

    def test_kconfig_riscv(self):
        """Test KconfigScanner riscv architecture detection"""
        src = self._git_dir
        kc_file = os.path.join(src, 'Kconfig')
        orig_kc_data = tools.read_file(kc_file)

        riscv_kconfig = orig_kc_data + b'''

config RISCV
\tbool

config ARCH_RV32I
\tbool

config TARGET_RISCV_BOARD
\tbool "RISC-V Board"
\tselect RISCV
\tdefault n

if TARGET_RISCV_BOARD
config SYS_ARCH
\tdefault "riscv"

config SYS_CPU
\tdefault "generic"

config SYS_VENDOR
\tdefault "RiscVendor"

config SYS_BOARD
\tdefault "RISC-V Board"

config SYS_CONFIG_NAME
\tdefault "riscv_config"
endif
'''
        tools.write_file(kc_file, riscv_kconfig)

        try:
            scanner = boards.KconfigScanner(src)
            defconfig = os.path.join(src, 'riscv64_defconfig')
            tools.write_file(defconfig, 'CONFIG_TARGET_RISCV_BOARD=y\n', False)

            # Test riscv64 (no RV32I)
            res, warnings = scanner.scan(defconfig, False)
            self.assertEqual('riscv64', res['arch'])

            # Test riscv32 (with RV32I)
            riscv32_kconfig = riscv_kconfig + b'''
config ARCH_RV32I
\tdefault y if TARGET_RISCV_BOARD
'''
            tools.write_file(kc_file, riscv32_kconfig)
            scanner = boards.KconfigScanner(src)
            res, warnings = scanner.scan(defconfig, False)
            self.assertEqual('riscv32', res['arch'])
        finally:
            tools.write_file(kc_file, orig_kc_data)

    def test_maintainers_commented(self):
        """Test MaintainersDatabase with commented maintainer lines"""
        src = self._git_dir
        main = os.path.join(src, 'boards', 'board0', 'MAINTAINERS')
        config_dir = os.path.join(src, 'configs')
        orig_data = tools.read_file(main, binary=False)

        new_data = '#M: Commented Maintainer <comment@test.com>\n' + orig_data
        tools.write_file(main, new_data, binary=False)

        try:
            params_list, warnings = self._boards.build_board_list(config_dir, src)
            self.assertEqual(2, len(params_list))
        finally:
            tools.write_file(main, orig_data, binary=False)

    def test_ensure_board_list_options(self):
        """Test ensure_board_list() with force and quiet flags"""
        outfile = os.path.join(self._output_dir, 'test-boards-opts.cfg')
        brds = boards.Boards()

        # Test force=False, quiet=False (normal generation)
        with terminal.capture() as (stdout, stderr):
            brds.ensure_board_list(outfile, jobs=1, force=False, quiet=False)
        self.assertTrue(os.path.exists(outfile))

        # Test force=True (regenerate even if current)
        with terminal.capture() as (stdout, stderr):
            brds.ensure_board_list(outfile, jobs=1, force=True, quiet=False)
        self.assertTrue(os.path.exists(outfile))

        # Test quiet=True (minimal output)
        with terminal.capture() as (stdout, stderr):
            brds.ensure_board_list(outfile, jobs=1, force=False, quiet=True)
        self.assertNotIn('Checking', stdout.getvalue())

        # Test quiet=True when up to date (no output)
        with terminal.capture() as (stdout, stderr):
            result = brds.ensure_board_list(outfile, jobs=1, force=False,
                                            quiet=True)
        self.assertTrue(result)
        self.assertEqual('', stdout.getvalue())

    def test_output_is_new_old_format(self):
        """Test output_is_new() with old format containing Options"""
        src = self._git_dir
        config_dir = os.path.join(src, 'configs')
        boards_cfg = os.path.join(self._base_dir, 'boards_old.cfg')

        content = b'''#
# List of boards
#
# Status, Arch, CPU, SoC, Vendor, Board, Target, Options, Maintainers

Active  arm  armv7  -  Tester  Board  board0  options  maint
'''
        tools.write_file(boards_cfg, content)
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))

    def test_maintainers_status(self):
        """Test MaintainersDatabase.get_status() with various statuses"""
        database = boards.MaintainersDatabase()

        # Test missing target
        self.assertEqual('-', database.get_status('missing'))
        self.assertIn("no status info for 'missing'", database.warnings[-1])

        # Test 'Supported' maps to Active
        database.database['test1'] = ('Supported', ['maint@test.com'])
        self.assertEqual('Active', database.get_status('test1'))

        # Test 'Orphan' status
        database.database['orphan'] = ('Orphan', ['maint@test.com'])
        self.assertEqual('Orphan', database.get_status('orphan'))

        # Test unknown status
        database.database['test2'] = ('Unknown Status', ['maint@test.com'])
        self.assertEqual('-', database.get_status('test2'))
        self.assertIn("unknown status for 'test2'", database.warnings[-1])

    def test_expr_term_str(self):
        """Test Expr and Term __str__() methods"""
        expr = boards.Expr('arm.*')
        self.assertEqual('arm.*', str(expr))

        term = boards.Term()
        term.add_expr('arm')
        term.add_expr('cortex')
        self.assertEqual('arm&cortex', str(term))

    def test_kconfig_scanner_warnings(self):
        """Test KconfigScanner.scan() TARGET_xxx warnings"""
        src = self._git_dir
        kc_file = os.path.join(src, 'Kconfig')
        orig_kc_data = tools.read_file(kc_file)

        # Test missing TARGET_xxx warning
        defconfig = os.path.join(src, 'configs', 'no_target_defconfig')
        tools.write_file(defconfig, 'CONFIG_SYS_ARCH="arm"\n', False)
        try:
            scanner = boards.KconfigScanner(src)
            res, warnings = scanner.scan(defconfig, warn_targets=True)
            self.assertEqual(1, len(warnings))
            self.assertIn('No TARGET_NO_TARGET enabled', warnings[0])
        finally:
            if os.path.exists(defconfig):
                os.remove(defconfig)

        # Test duplicate TARGET_xxx warning
        extra = b'''
config TARGET_BOARD0_DUP
\tbool "Duplicate target"
\tdefault y if TARGET_BOARD0
'''
        tools.write_file(kc_file, orig_kc_data + extra)
        try:
            scanner = boards.KconfigScanner(src)
            defconfig = os.path.join(src, 'configs', 'board0_defconfig')
            res, warnings = scanner.scan(defconfig, warn_targets=True)
            self.assertEqual(1, len(warnings))
            self.assertIn('Duplicate TARGET_xxx', warnings[0])
        finally:
            tools.write_file(kc_file, orig_kc_data)

    def test_scan_extended(self):
        """Test scan_extended() for finding boards matching extended criteria"""
        brds = boards.Boards()

        # Test with CONFIG-based selection (value=y)
        ext = Extended(
            name='test_ext',
            desc='Test extended board',
            fragments=['test_frag'],
            targets=[['CONFIG_ARM', 'y']])

        with mock.patch('qconfig.find_config') as mock_find, \
             mock.patch.object(tools, 'read_file', return_value='CONFIG_TEST=y'):
            mock_find.return_value = {'board0', 'board1'}
            result = brds.scan_extended(None, ext)
            self.assertEqual({'board0', 'board1'}, result)
            mock_find.assert_called_once_with(None, ['CONFIG_ARM'])

        # Test with CONFIG-based selection (value=n)
        ext = Extended(
            name='test_ext2',
            desc='Test extended board 2',
            fragments=['test_frag'],
            targets=[['CONFIG_DEBUG', 'n']])

        with mock.patch('qconfig.find_config') as mock_find, \
             mock.patch.object(tools, 'read_file', return_value=''):
            mock_find.return_value = {'board2'}
            result = brds.scan_extended(None, ext)
            self.assertEqual({'board2'}, result)
            mock_find.assert_called_once_with(None, ['~CONFIG_DEBUG'])

        # Test with CONFIG-based selection (specific value)
        ext = Extended(
            name='test_ext3',
            desc='Test extended board 3',
            fragments=['test_frag'],
            targets=[['CONFIG_SYS_SOC', '"k3"']])

        with mock.patch('qconfig.find_config') as mock_find, \
             mock.patch.object(tools, 'read_file', return_value=''):
            mock_find.return_value = {'board4'}
            result = brds.scan_extended(None, ext)
            self.assertEqual({'board4'}, result)
            mock_find.assert_called_once_with(None, ['CONFIG_SYS_SOC="k3"'])

        # Test with regex pattern - intersection of glob and find_config
        ext = Extended(
            name='test_ext4',
            desc='Test extended board 4',
            fragments=['test_frag'],
            targets=[['regex', 'configs/board*_defconfig']])

        with mock.patch('qconfig.find_config') as mock_find, \
             mock.patch.object(tools, 'read_file', return_value=''), \
             mock.patch('glob.glob') as mock_glob:
            mock_glob.return_value = ['configs/board0_defconfig',
                                      'configs/board2_defconfig']
            mock_find.return_value = {'board0', 'board1', 'board2'}
            result = brds.scan_extended(None, ext)
            # Should be intersection: {board0, board2} & {board0, board1, board2}
            self.assertEqual({'board0', 'board2'}, result)

    def test_parse_extended(self):
        """Test parse_extended() for creating extended board entries"""
        brds = boards.Boards()
        for brd in BOARDS:
            brds.add_board(board.Board(*brd))

        # Create a .buildman file
        buildman_file = os.path.join(self._base_dir, 'test.buildman')
        content = '''name: test_acpi
desc: Test ACPI boards
fragment: acpi
targets:
  CONFIG_ARM=y
'''
        tools.write_file(buildman_file, content, binary=False)

        # Mock scan_extended to return specific boards
        with mock.patch.object(brds, 'scan_extended') as mock_scan:
            mock_scan.return_value = {'board0', 'board1'}
            brds.parse_extended(None, buildman_file)

        # Check that new extended boards were added
        board_list = brds.get_list()
        # Original 4 boards + 2 extended boards
        self.assertEqual(6, len(board_list))

        # Find the extended boards
        ext_boards = [b for b in board_list if ',' in b.target]
        self.assertEqual(2, len(ext_boards))

        # Check the extended board properties
        ext_board = next(b for b in ext_boards if 'board0' in b.target)
        self.assertEqual('test_acpi,board0', ext_board.target)
        self.assertEqual('arm', ext_board.arch)
        self.assertEqual('board0', ext_board.orig_target)
        self.assertIsNotNone(ext_board.extended)
        self.assertEqual('test_acpi', ext_board.extended.name)

    def test_try_remove_other_error(self):
        """Test try_remove() re-raises non-ENOENT errors"""
        with mock.patch('os.remove') as mock_remove:
            # Simulate a permission error (not ENOENT)
            err = OSError(errno.EACCES, 'Permission denied')
            mock_remove.side_effect = err
            with self.assertRaises(OSError) as exc:
                boards.try_remove('/some/file')
            self.assertEqual(errno.EACCES, exc.exception.errno)

    def test_output_is_new_other_error(self):
        """Test output_is_new() re-raises non-ENOENT errors"""
        with mock.patch('os.path.getctime') as mock_ctime:
            err = OSError(errno.EACCES, 'Permission denied')
            mock_ctime.side_effect = err
            with self.assertRaises(OSError) as exc:
                boards.output_is_new('/some/file', 'configs', '.')
            self.assertEqual(errno.EACCES, exc.exception.errno)

    def test_output_is_new_hidden_files(self):
        """Test output_is_new() skips hidden defconfig files"""
        base = self._base_dir
        src = self._git_dir
        config_dir = os.path.join(src, 'configs')

        # Create boards.cfg
        boards_cfg = os.path.join(base, 'boards_hidden.cfg')
        content = b'''#
# List of boards
#   Automatically generated by buildman/boards.py: don't edit
#
# Status, Arch, CPU, SoC, Vendor, Board, Target, Config, Maintainers

Active  arm  armv7  -  Tester  Board  board0  config0  maint
'''
        tools.write_file(boards_cfg, content)

        # Create a hidden defconfig file (should be skipped)
        hidden = os.path.join(config_dir, '.hidden_defconfig')
        tools.write_file(hidden, b'# hidden')

        try:
            # Touch boards.cfg to make it newer
            time.sleep(0.02)
            Path(boards_cfg).touch()
            # Should return True (hidden file skipped)
            self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))
        finally:
            os.remove(hidden)

    def test_kconfig_scanner_destructor(self):
        """Test KconfigScanner.__del__() cleans up leftover temp file"""
        src = self._git_dir
        scanner = boards.KconfigScanner(src)

        # Simulate a leftover temp file
        tmpfile = os.path.join(self._base_dir, 'leftover.tmp')
        tools.write_file(tmpfile, b'temp')
        scanner._tmpfile = tmpfile

        # Delete the scanner - should clean up the temp file
        del scanner
        self.assertFalse(os.path.exists(tmpfile))

    def test_kconfig_scanner_aarch64(self):
        """Test KconfigScanner.scan() aarch64 fix-up"""
        src = self._git_dir
        kc_file = os.path.join(src, 'Kconfig')
        orig_kc_data = tools.read_file(kc_file)

        # Add aarch64 board to Kconfig
        aarch64_kconfig = orig_kc_data + b'''

config TARGET_AARCH64_BOARD
\tbool "AArch64 Board"
\tdefault n

if TARGET_AARCH64_BOARD
config SYS_ARCH
\tdefault "arm"

config SYS_CPU
\tdefault "armv8"

config SYS_VENDOR
\tdefault "Test"

config SYS_BOARD
\tdefault "AArch64 Board"

config SYS_CONFIG_NAME
\tdefault "aarch64_config"
endif
'''
        tools.write_file(kc_file, aarch64_kconfig)

        try:
            scanner = boards.KconfigScanner(src)
            defconfig = os.path.join(src, 'aarch64_defconfig')
            tools.write_file(defconfig, 'CONFIG_TARGET_AARCH64_BOARD=y\n', False)
            res, warnings = scanner.scan(defconfig, False)
            # Should be fixed up to aarch64
            self.assertEqual('aarch64', res['arch'])
        finally:
            tools.write_file(kc_file, orig_kc_data)
            if os.path.exists(defconfig):
                os.remove(defconfig)

    def test_read_boards_short_line(self):
        """Test Boards.read_boards() pads short lines to 8 fields"""
        boards_cfg = os.path.join(self._base_dir, 'boards_veryshort.cfg')
        # Create a board with only 7 fields (missing maintainers)
        content = '''Active  arm  armv7  soc  Tester  Board  target
'''
        tools.write_file(boards_cfg, content.encode('utf-8'))

        brds = boards.Boards()
        brds.read_boards(boards_cfg)
        board_list = brds.get_list()
        self.assertEqual(1, len(board_list))
        # cfg_name should be empty string (padded)
        self.assertEqual('', board_list[0].cfg_name)

    def test_ensure_board_list_up_to_date_message(self):
        """Test ensure_board_list() shows up-to-date message"""
        outfile = os.path.join(self._output_dir, 'test-boards-uptodate.cfg')
        brds = boards.Boards()

        # First generate the file
        with terminal.capture() as (stdout, stderr):
            brds.ensure_board_list(outfile, jobs=1, force=False, quiet=False)

        # Run again - should say "up to date"
        with terminal.capture() as (stdout, stderr):
            result = brds.ensure_board_list(outfile, jobs=1, force=False,
                                            quiet=False)
        self.assertTrue(result)
        self.assertIn('up to date', stdout.getvalue())

    def test_ensure_board_list_warnings(self):
        """Test ensure_board_list() prints warnings to stderr"""
        outfile = os.path.join(self._output_dir, 'test-boards-warn.cfg')
        brds = boards.Boards()

        # Mock build_board_list to return warnings
        with mock.patch.object(brds, 'build_board_list') as mock_build:
            mock_build.return_value = ([], ['WARNING: test warning'])
            with terminal.capture() as (stdout, stderr):
                result = brds.ensure_board_list(outfile, jobs=1, force=True,
                                                quiet=False)
            self.assertFalse(result)
            self.assertIn('WARNING: test warning', stderr.getvalue())

    def test_parse_all_extended(self):
        """Test parse_all_extended() finds and parses .buildman files"""
        brds = boards.Boards()
        for brd in BOARDS:
            brds.add_board(board.Board(*brd))

        # Mock glob to return a .buildman file and parse_extended
        with mock.patch('glob.glob') as mock_glob, \
             mock.patch.object(brds, 'parse_extended') as mock_parse:
            mock_glob.return_value = ['configs/test.buildman']
            brds.parse_all_extended(None)
            mock_glob.assert_called_once_with('configs/*.buildman')
            mock_parse.assert_called_once_with(None, 'configs/test.buildman')

    def test_scan_extended_no_match_warning(self):
        """Test scan_extended() warns when no configs match regex"""
        brds = boards.Boards()

        ext = Extended(
            name='test_ext',
            desc='Test extended board',
            fragments=['test_frag'],
            targets=[['regex', 'nonexistent*_defconfig']])

        with mock.patch('qconfig.find_config') as mock_find, \
             mock.patch.object(tools, 'read_file', return_value=''), \
             mock.patch('glob.glob') as mock_glob, \
             terminal.capture() as (stdout, stderr):
            mock_glob.return_value = []  # No matches
            mock_find.return_value = set()
            result = brds.scan_extended(None, ext)
            self.assertEqual(set(), result)
            # Warning should be printed
            self.assertIn('Warning', stdout.getvalue())

    def test_kconfig_scanner_riscv_no_rv32i(self):
        """Test KconfigScanner.scan() when ARCH_RV32I symbol doesn't exist"""
        src = self._git_dir
        kc_file = os.path.join(src, 'Kconfig')
        orig_kc_data = tools.read_file(kc_file)

        # Add RISCV board WITHOUT defining ARCH_RV32I symbol
        # This will cause syms.get('ARCH_RV32I') to return None,
        # and accessing .str_value on None raises AttributeError
        riscv_kconfig = orig_kc_data + b'''

config RISCV
\tbool

config TARGET_RISCV_TEST
\tbool "RISC-V Test Board"
\tdefault n

if TARGET_RISCV_TEST
config SYS_ARCH
\tdefault "riscv"

config SYS_CPU
\tdefault "generic"

config SYS_VENDOR
\tdefault "Test"

config SYS_BOARD
\tdefault "RISCV Test"

config SYS_CONFIG_NAME
\tdefault "riscv_test"
endif
'''
        tools.write_file(kc_file, riscv_kconfig)
        defconfig = os.path.join(src, 'riscv_test_defconfig')

        try:
            # Create defconfig that enables the riscv board
            tools.write_file(defconfig, 'CONFIG_TARGET_RISCV_TEST=y\n', False)

            scanner = boards.KconfigScanner(src)
            res, warnings = scanner.scan(defconfig, False)

            # Should default to riscv64 when ARCH_RV32I lookup fails
            self.assertEqual('riscv64', res['arch'])
        finally:
            tools.write_file(kc_file, orig_kc_data)
            if os.path.exists(defconfig):
                os.remove(defconfig)

    def test_scan_defconfigs_for_multiprocess(self):
        """Test scan_defconfigs_for_multiprocess() function directly"""
        src = self._git_dir
        config_dir = os.path.join(src, 'configs')

        # Get a list of defconfigs
        defconfigs = [os.path.join(config_dir, 'board0_defconfig')]

        # Create a queue and call the function
        queue = multiprocessing.Queue()
        boards.Boards.scan_defconfigs_for_multiprocess(src, queue, defconfigs,
                                                       False)

        # Get the result from the queue
        result = queue.get(timeout=5)
        params, warnings = result
        self.assertEqual('board0', params['target'])
        self.assertEqual('arm', params['arch'])

    def test_scan_defconfigs_hidden_files(self):
        """Test scan_defconfigs() skips hidden defconfig files"""
        src = self._git_dir
        config_dir = os.path.join(src, 'configs')

        # Create a hidden defconfig
        hidden = os.path.join(config_dir, '.hidden_defconfig')
        tools.write_file(hidden, b'CONFIG_TARGET_BOARD0=y')

        try:
            brds = boards.Boards()
            params_list, warnings = brds.scan_defconfigs(config_dir, src, 1)

            # Hidden file should not be in results
            targets = [p['target'] for p in params_list]
            self.assertNotIn('.hidden', targets)
            # But regular boards should be there
            self.assertIn('board0', targets)
        finally:
            os.remove(hidden)

    def test_maintainers_n_tag_non_configs_path(self):
        """Test MaintainersDatabase N: tag skips non-configs paths"""
        src = self._git_dir

        # Create a MAINTAINERS file with N: tag
        maintainers_file = os.path.join(src, 'MAINTAINERS_TEST')
        maintainers_content = '''BOARD0
M: Test <test@test.com>
S: Active
N: .*
'''
        tools.write_file(maintainers_file, maintainers_content, binary=False)

        # Mock os.walk to return a path that doesn't start with 'configs/'
        # when walking the configs directory. This tests line 443.
        def mock_walk(path):
            # Return paths with 'configs/' prefix (normal) and without (edge case)
            yield (os.path.join(src, 'configs'), [], ['board0_defconfig'])
            # This path will have 'other/' prefix after srcdir removal
            yield (os.path.join(src, 'other'), [], ['fred_defconfig'])

        try:
            database = boards.MaintainersDatabase()
            with mock.patch('os.walk', mock_walk):
                database.parse_file(src, maintainers_file)

            # board0 should be found (path starts with configs/)
            # fred should be skipped (path starts with other/, not configs/)
            self.assertIn('board0', database.database)
            self.assertNotIn('fred', database.database)
        finally:
            os.remove(maintainers_file)


if __name__ == '__main__':
    unittest.main()
