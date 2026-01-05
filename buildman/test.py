# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2012 The Chromium OS Authors.
#

"""Tests for the buildman build tool"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from filelock import FileLock

from buildman import board
from buildman import boards
from buildman import bsettings
from buildman import builder
from buildman import builderthread
from buildman import cfgutil
from buildman import control
from buildman import toolchain
from patman import commit
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tools

use_network = True

settings_data = '''
# Buildman settings file

[toolchain]
main: /usr/sbin

[toolchain-alias]
x86: i386 x86_64
'''

settings_data_wrapper = '''
# Buildman settings file

[toolchain]
main: /usr/sbin

[toolchain-wrapper]
wrapper = ccache
'''

settings_data_homedir = '''
# Buildman settings file

[toolchain]
main = ~/mypath

[toolchain-prefix]
x86 = ~/mypath-x86-
'''

migration = '''===================== WARNING ======================
This board does not use CONFIG_DM. CONFIG_DM will be
compulsory starting with the v2020.01 release.
Failure to update may result in board removal.
See doc/develop/driver-model/migration.rst for more info.
====================================================
'''

errors = [
    '''main.c: In function 'main_loop':
main.c:260:6: warning: unused variable 'joe' [-Wunused-variable]
''',
    '''main.c: In function 'main_loop2':
main.c:295:2: error: 'fred' undeclared (first use in this function)
main.c:295:2: note: each undeclared identifier is reported only once for each function it appears in
make[1]: *** [main.o] Error 1
make: *** [common/libcommon.o] Error 2
Make failed
''',
    '''arch/arm/dts/socfpga_arria10_socdk_sdmmc.dtb: Warning \
(avoid_unnecessary_addr_size): /clocks: unnecessary #address-cells/#size-cells \
without "ranges" or child "reg" property
''',
    '''powerpc-linux-ld: warning: dot moved backwards before `.bss'
powerpc-linux-ld: warning: dot moved backwards before `.bss'
powerpc-linux-ld: u-boot: section .text lma 0xfffc0000 overlaps previous sections
powerpc-linux-ld: u-boot: section .rodata lma 0xfffef3ec overlaps previous sections
powerpc-linux-ld: u-boot: section .reloc lma 0xffffa400 overlaps previous sections
powerpc-linux-ld: u-boot: section .data lma 0xffffcd38 overlaps previous sections
powerpc-linux-ld: u-boot: section .u_boot_cmd lma 0xffffeb40 overlaps previous sections
powerpc-linux-ld: u-boot: section .bootpg lma 0xfffff198 overlaps previous sections
''',
   '''In file included from %(basedir)sarch/sandbox/cpu/cpu.c:9:0:
%(basedir)sarch/sandbox/include/asm/state.h:44:0: warning: "xxxx" redefined [enabled by default]
%(basedir)sarch/sandbox/include/asm/state.h:43:0: note: this is the location of the previous definition
%(basedir)sarch/sandbox/cpu/cpu.c: In function 'do_reset':
%(basedir)sarch/sandbox/cpu/cpu.c:27:1: error: unknown type name 'blah'
%(basedir)sarch/sandbox/cpu/cpu.c:28:12: error: expected declaration specifiers or '...' before numeric constant
make[2]: *** [arch/sandbox/cpu/cpu.o] Error 1
make[1]: *** [arch/sandbox/cpu] Error 2
make[1]: *** Waiting for unfinished jobs....
In file included from %(basedir)scommon/board_f.c:55:0:
%(basedir)sarch/sandbox/include/asm/state.h:44:0: warning: "xxxx" redefined [enabled by default]
%(basedir)sarch/sandbox/include/asm/state.h:43:0: note: this is the location of the previous definition
make: *** [sub-make] Error 2
'''
]


# hash, subject, return code, list of errors/warnings
commits = [
    ['1234', 'upstream/master, migration warning', 0, []],
    ['5678', 'Second commit, a warning', 0, errors[0:1]],
    ['9012', 'Third commit, error', 1, errors[0:2]],
    ['3456', 'Fourth commit, warning', 0, [errors[0], errors[2]]],
    ['7890', 'Fifth commit, link errors', 1, [errors[0], errors[3]]],
    ['abcd', 'Sixth commit, fixes all errors', 0, []],
    ['ef01', 'Seventh commit, fix migration, check directory suppression', 1,
     [errors[4]]],
]

BOARDS = [
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 1', 'board0',  ''],
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 2', 'board1', ''],
    ['Active', 'powerpc', 'powerpc', '', 'Tester', 'PowerPC board 1', 'board2', ''],
    ['Active', 'powerpc', 'mpc83xx', '', 'Tester', 'PowerPC board 2', 'board3', ''],
    ['Active', 'sandbox', 'sandbox', '', 'Tester', 'Sandbox board', 'board4', ''],
]

BASE_DIR = 'base'

OUTCOME_OK, OUTCOME_WARN, OUTCOME_ERR = range(3)

class Options:
    """Class that holds build options"""
    def __init__(self):
        self.git = None
        self.summary = False
        self.jobs = None
        self.dry_run = False
        self.branch = None
        self.force_build = False
        self.list_tool_chains = False
        self.count = -1
        self.git_dir = None
        self.threads = None
        self.show_unknown = False
        self.quick = False
        self.show_errors = False
        self.keep_outputs = False

class TestBuild(unittest.TestCase):
    """Test buildman

    TODO: Write tests for the rest of the functionality
    """
    def setUp(self):
        # Set up commits to build
        self.commits = []
        sequence = 0
        for commit_info in commits:
            comm = commit.Commit(commit_info[0])
            comm.subject = commit_info[1]
            comm.return_code = commit_info[2]
            comm.error_list = commit_info[3]
            if sequence < 6:
                comm.error_list += [migration]
            comm.sequence = sequence
            sequence += 1
            self.commits.append(comm)

        # Set up boards to build
        self.brds = boards.Boards()
        for brd in BOARDS:
            self.brds.add_board(board.Board(*brd))
        self.brds.select_boards([])

        # Add some test settings
        bsettings.setup(None)
        bsettings.add_file(settings_data)

        # Set up the toolchains
        self.toolchains = toolchain.Toolchains()
        self.toolchains.add('arm-linux-gcc', test=False)
        self.toolchains.add('sparc-linux-gcc', test=False)
        self.toolchains.add('powerpc-linux-gcc', test=False)
        self.toolchains.add('/path/to/aarch64-linux-gcc', test=False)
        self.toolchains.add('gcc', test=False)

        # Avoid sending any output
        terminal.set_print_test_mode()
        self._col = terminal.Color()

        self.base_dir = tempfile.mkdtemp()
        if not os.path.isdir(self.base_dir):
            os.mkdir(self.base_dir)

        self.cur_time = 0
        self.valid_pids = []
        self.finish_time = None
        self.finish_pid = None

    def tearDown(self):
        shutil.rmtree(self.base_dir)

    def make(self, commit, brd, stage, *args, **kwargs):
        """Mock make function for testing build output"""
        result = command.CommandResult()
        boardnum = int(brd.target[-1])
        result.return_code = 0
        result.stderr = ''
        result.stdout = (f'This is the test output for board {brd.target}, '
                         f'commit {commit.hash}')
        if ((boardnum >= 1 and boardnum >= commit.sequence) or
                boardnum == 4 and commit.sequence == 6):
            result.return_code = commit.return_code
            result.stderr = (''.join(commit.error_list)
                % {'basedir' : self.base_dir + '/.bm-work/00/'})
        elif commit.sequence < 6:
            result.stderr = migration

        result.combined = result.stdout + result.stderr
        return result

    def assert_summary(self, text, arch, plus, brds, outcome=OUTCOME_ERR):
        """Check that the summary text matches expectations"""
        col = self._col
        expected_colour = (col.GREEN if outcome == OUTCOME_OK else
                           col.YELLOW if outcome == OUTCOME_WARN else col.RED)
        expect = f'{arch:>10}: '
        # TODO(sjg@chromium.org): If plus is '', we shouldn't need this
        expect += ' ' + col.build(expected_colour, plus)
        expect += '  '
        for brd in brds:
            expect += col.build(expected_colour, f' {brd}')
        self.assertEqual(text, expect)

    def _setup_test(self, echo_lines=False, threads=1, **kwdisplay_args):
        """Set up the test by running a build and summary

        Args:
            echo_lines: True to echo lines to the terminal to aid test
                development
            kwdisplay_args: Dict of arguments to pass to
                Builder.SetDisplayOptions()

        Returns:
            Iterator containing the output lines, each a PrintLine() object
        """
        build = builder.Builder(self.toolchains, self.base_dir, None, threads,
                                2, checkout=False, show_unknown=False)
        build.do_make = self.make
        board_selected = self.brds.get_selected_dict()

        # Build the boards for the pre-defined commits and warnings/errors
        # associated with each. This calls our Make() to inject the fake output.
        build.build_boards(self.commits, board_selected, keep_outputs=False,
                           verbose=False, fragments='')
        lines = terminal.get_print_test_lines()
        count = 0
        for line in lines:
            if line.text.strip():
                count += 1

        # We should get two starting messages, an update for every commit built
        # and a summary message
        self.assertEqual(count, len(commits) * len(BOARDS) + 3)
        build.set_display_options(**kwdisplay_args)
        build.show_summary(self.commits, board_selected)
        if echo_lines:
            terminal.echo_print_test_lines()
        return iter(terminal.get_print_test_lines())

    def _check_output(self, lines, list_error_boards=False,
                     filter_dtb_warnings=False,
                     filter_migration_warnings=False):
        """Check for expected output from the build summary

        Args:
            lines: Iterator containing the lines returned from the summary
            list_error_boards: Adjust the check for output produced with the
               --list-error-boards flag
            filter_dtb_warnings: Adjust the check for output produced with the
               --filter-dtb-warnings flag
        """
        def add_line_prefix(prefix, brds, error_str, colour):
            """Add a prefix to each line of a string

            The training \n in error_str is removed before processing

            Args:
                prefix: String prefix to add
                error_str: Error string containing the lines
                colour: Expected colour for the line. Note that the board list,
                    if present, always appears in magenta

            Returns:
                New string where each line has the prefix added
            """
            lines = error_str.strip().splitlines()
            new_lines = []
            for line in lines:
                if brds:
                    expect = self._col.build(colour, prefix + '(')
                    expect += self._col.build(self._col.MAGENTA, brds,
                                              bright=False)
                    expect += self._col.build(colour, f') {line}')
                else:
                    expect = self._col.build(colour, prefix + line)
                new_lines.append(expect)
            return '\n'.join(new_lines)

        col = terminal.Color()
        boards01234 = ('board0 board1 board2 board3 board4'
                       if list_error_boards else '')
        boards1234 = 'board1 board2 board3 board4' if list_error_boards else ''
        boards234 = 'board2 board3 board4' if list_error_boards else ''
        boards34 = 'board3 board4' if list_error_boards else ''
        boards4 = 'board4' if list_error_boards else ''

        # Upstream commit: migration warnings only
        self.assertEqual(next(lines).text, f'01: {commits[0][1]}')

        if not filter_migration_warnings:
            self.assert_summary(next(lines).text, 'arm', 'w+',
                               ['board0', 'board1'], outcome=OUTCOME_WARN)
            self.assert_summary(next(lines).text, 'powerpc', 'w+',
                               ['board2', 'board3'], outcome=OUTCOME_WARN)
            self.assert_summary(next(lines).text, 'sandbox', 'w+', ['board4'],
                               outcome=OUTCOME_WARN)

            self.assertEqual(next(lines).text,
                add_line_prefix('+', boards01234, migration, col.RED))

        # Second commit: all archs should fail with warnings
        self.assertEqual(next(lines).text, f'02: {commits[1][1]}')

        if filter_migration_warnings:
            self.assert_summary(next(lines).text, 'arm', 'w+',
                               ['board1'], outcome=OUTCOME_WARN)
            self.assert_summary(next(lines).text, 'powerpc', 'w+',
                               ['board2', 'board3'], outcome=OUTCOME_WARN)
            self.assert_summary(next(lines).text, 'sandbox', 'w+', ['board4'],
                               outcome=OUTCOME_WARN)

        # Second commit: The warnings should be listed
        self.assertEqual(next(lines).text,
            add_line_prefix('w+', boards1234, errors[0], col.YELLOW))

        # Third commit: Still fails
        self.assertEqual(next(lines).text, f'03: {commits[2][1]}')
        if filter_migration_warnings:
            self.assert_summary(next(lines).text, 'arm', '',
                               ['board1'], outcome=OUTCOME_OK)
        self.assert_summary(next(lines).text, 'powerpc', '+',
                           ['board2', 'board3'])
        self.assert_summary(next(lines).text, 'sandbox', '+', ['board4'])

        # Expect a compiler error
        self.assertEqual(next(lines).text,
                         add_line_prefix('+', boards234, errors[1], col.RED))

        # Fourth commit: Compile errors are fixed, just have warning for board3
        self.assertEqual(next(lines).text, f'04: {commits[3][1]}')
        if filter_migration_warnings:
            expect = f"{'powerpc':>10}: "
            expect += ' ' + col.build(col.GREEN, '')
            expect += '  '
            expect += col.build(col.GREEN, ' board2')
            expect += ' ' + col.build(col.YELLOW, 'w+')
            expect += '  '
            expect += col.build(col.YELLOW, ' board3')
            self.assertEqual(next(lines).text, expect)
        else:
            self.assert_summary(next(lines).text, 'powerpc', 'w+',
                               ['board2', 'board3'], outcome=OUTCOME_WARN)
        self.assert_summary(next(lines).text, 'sandbox', 'w+', ['board4'],
                               outcome=OUTCOME_WARN)

        # Compile error fixed
        self.assertEqual(next(lines).text,
                         add_line_prefix('-', boards234, errors[1], col.GREEN))

        if not filter_dtb_warnings:
            self.assertEqual(
                next(lines).text,
                add_line_prefix('w+', boards34, errors[2], col.YELLOW))

        # Fifth commit
        self.assertEqual(next(lines).text, f'05: {commits[4][1]}')
        if filter_migration_warnings:
            self.assert_summary(next(lines).text, 'powerpc', '', ['board3'],
                               outcome=OUTCOME_OK)
        self.assert_summary(next(lines).text, 'sandbox', '+', ['board4'])

        # The second line of errors[3] is a duplicate, so buildman will drop it
        expect = errors[3].rstrip().split('\n')
        expect = [expect[0]] + expect[2:]
        expect = '\n'.join(expect)
        self.assertEqual(next(lines).text,
                         add_line_prefix('+', boards4, expect, col.RED))

        if not filter_dtb_warnings:
            self.assertEqual(
                next(lines).text,
                add_line_prefix('w-', boards34, errors[2], col.CYAN))

        # Sixth commit
        self.assertEqual(next(lines).text, f'06: {commits[5][1]}')
        if filter_migration_warnings:
            self.assert_summary(next(lines).text, 'sandbox', '', ['board4'],
                               outcome=OUTCOME_OK)
        else:
            self.assert_summary(next(lines).text, 'sandbox', 'w+', ['board4'],
                               outcome=OUTCOME_WARN)

        # The second line of errors[3] is a duplicate, so buildman will drop it
        expect = errors[3].rstrip().split('\n')
        expect = [expect[0]] + expect[2:]
        expect = '\n'.join(expect)
        self.assertEqual(next(lines).text,
                         add_line_prefix('-', boards4, expect, col.GREEN))
        self.assertEqual(next(lines).text,
                         add_line_prefix('w-', boards4, errors[0], col.CYAN))

        # Seventh commit
        self.assertEqual(next(lines).text, f'07: {commits[6][1]}')
        if filter_migration_warnings:
            self.assert_summary(next(lines).text, 'sandbox', '+', ['board4'])
        else:
            self.assert_summary(next(lines).text, 'arm', '', ['board0', 'board1'],
                               outcome=OUTCOME_OK)
            self.assert_summary(next(lines).text, 'powerpc', '',
                               ['board2', 'board3'], outcome=OUTCOME_OK)
            self.assert_summary(next(lines).text, 'sandbox', '+', ['board4'])

        # Pick out the correct error lines
        expect_str = errors[4].rstrip().replace('%(basedir)s', '').split('\n')
        expect = expect_str[3:8] + [expect_str[-1]]
        expect = '\n'.join(expect)
        if not filter_migration_warnings:
            self.assertEqual(
                next(lines).text,
                add_line_prefix('-', boards01234, migration, col.GREEN))

        self.assertEqual(next(lines).text,
                         add_line_prefix('+', boards4, expect, col.RED))

        # Now the warnings lines
        expect = [expect_str[0]] + expect_str[10:12] + [expect_str[9]]
        expect = '\n'.join(expect)
        self.assertEqual(next(lines).text,
                         add_line_prefix('w+', boards4, expect, col.YELLOW))

    def test_output(self):
        """Test basic builder operation and output

        This does a line-by-line verification of the summary output.
        """
        lines = self._setup_test(show_errors=True)
        self._check_output(lines, list_error_boards=False,
                          filter_dtb_warnings=False)

    def test_error_boards(self):
        """Test output with --list-error-boards

        This does a line-by-line verification of the summary output.
        """
        lines = self._setup_test(show_errors=True, list_error_boards=True)
        self._check_output(lines, list_error_boards=True)

    def test_filter_dtb(self):
        """Test output with --filter-dtb-warnings

        This does a line-by-line verification of the summary output.
        """
        lines = self._setup_test(show_errors=True, filter_dtb_warnings=True)
        self._check_output(lines, filter_dtb_warnings=True)

    def test_filter_migration(self):
        """Test output with --filter-migration-warnings

        This does a line-by-line verification of the summary output.
        """
        lines = self._setup_test(show_errors=True,
                                filter_migration_warnings=True)
        self._check_output(lines, filter_migration_warnings=True)

    def test_single_thread(self):
        """Test operation without threading"""
        lines = self._setup_test(show_errors=True, threads=0)
        self._check_output(lines, list_error_boards=False,
                          filter_dtb_warnings=False)

    def _test_git(self):
        """Test basic builder operation by building a branch"""
        options = Options()
        options.git = os.getcwd()
        options.summary = False
        options.jobs = None
        options.dry_run = False
        #options.git = os.path.join(self.base_dir, 'repo')
        options.branch = 'test-buildman'
        options.force_build = False
        options.list_tool_chains = False
        options.count = -1
        options.git_dir = None
        options.threads = None
        options.show_unknown = False
        options.quick = False
        options.show_errors = False
        options.keep_outputs = False
        args = ['tegra20']
        control.do_buildman(options, args)

    def test_board_single(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['sandbox']),
                         ({'all': ['board4'], 'sandbox': ['board4']}, []))

    def test_board_arch(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['arm']),
                         ({'all': ['board0', 'board1'],
                          'arm': ['board0', 'board1']}, []))

    def test_board_arch_single(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['arm sandbox']),
                         ({'sandbox': ['board4'],
                          'all': ['board0', 'board1', 'board4'],
                          'arm': ['board0', 'board1']}, []))


    def test_board_arch_single_multi_word(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['arm', 'sandbox']),
                         ({'sandbox': ['board4'],
                          'all': ['board0', 'board1', 'board4'],
                          'arm': ['board0', 'board1']}, []))

    def test_board_single_and(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['Tester & arm']),
                         ({'Tester&arm': ['board0', 'board1'],
                           'all': ['board0', 'board1']}, []))

    def test_board_two_and(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['Tester', '&', 'arm',
                                                   'Tester' '&', 'powerpc',
                                                   'sandbox']),
                         ({'sandbox': ['board4'],
                          'all': ['board0', 'board1', 'board2', 'board3',
                                  'board4'],
                          'Tester&powerpc': ['board2', 'board3'],
                          'Tester&arm': ['board0', 'board1']}, []))

    def test_board_all(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards([]),
                         ({'all': ['board0', 'board1', 'board2', 'board3',
                                  'board4']}, []))

    def test_board_regular_expression(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['T.*r&^Po']),
                         ({'all': ['board2', 'board3'],
                          'T.*r&^Po': ['board2', 'board3']}, []))

    def test_board_duplicate(self):
        """Test single board selection"""
        self.assertEqual(self.brds.select_boards(['sandbox sandbox',
                                                   'sandbox']),
                         ({'all': ['board4'], 'sandbox': ['board4']}, []))
    def check_dirs(self, build, dirname):
        """Check that the output directories are correct"""
        self.assertEqual(f'base{dirname}', build.get_output_dir(1))
        self.assertEqual(f'base{dirname}/fred', build.get_build_dir(1, 'fred'))
        self.assertEqual(f'base{dirname}/fred/done',
                         build.get_done_file(1, 'fred'))
        self.assertEqual(f'base{dirname}/fred/u-boot.sizes',
                         build.get_func_sizes_file(1, 'fred', 'u-boot'))
        self.assertEqual(f'base{dirname}/fred/u-boot.objdump',
                         build.get_objdump_file(1, 'fred', 'u-boot'))
        self.assertEqual(f'base{dirname}/fred/err',
                         build.get_err_file(1, 'fred'))

    def test_output_dir(self):
        """Test output-directory naming for a commit"""
        build = builder.Builder(self.toolchains, BASE_DIR, None, 1, 2,
                                checkout=False, show_unknown=False)
        build.commits = self.commits
        build.commit_count = len(self.commits)
        subject = self.commits[1].subject.translate(builder.trans_valid_chars)
        dirname = f'/{2:02d}_g{commits[1][0]}_{subject[:20]}'
        self.check_dirs(build, dirname)

    def test_output_dir_current(self):
        """Test output-directory naming for current source"""
        build = builder.Builder(self.toolchains, BASE_DIR, None, 1, 2,
                                checkout=False, show_unknown=False)
        build.commits = None
        build.commit_count = 0
        self.check_dirs(build, '/current')

    def test_output_dir_no_subdirs(self):
        """Test output-directory naming without subdirectories"""
        build = builder.Builder(self.toolchains, BASE_DIR, None, 1, 2,
                                checkout=False, show_unknown=False,
                                no_subdirs=True)
        build.commits = None
        build.commit_count = 0
        self.check_dirs(build, '')

    def test_toolchain_aliases(self):
        """Test that toolchain aliases are handled correctly"""
        self.assertTrue(self.toolchains.select('arm') is not None)
        with self.assertRaises(ValueError):
            self.toolchains.select('no-arch')
        with self.assertRaises(ValueError):
            self.toolchains.select('x86')

        self.toolchains = toolchain.Toolchains()
        self.toolchains.add('x86_64-linux-gcc', test=False)
        self.assertTrue(self.toolchains.select('x86') is not None)

        self.toolchains = toolchain.Toolchains()
        self.toolchains.add('i386-linux-gcc', test=False)
        self.assertTrue(self.toolchains.select('x86') is not None)

    def test_toolchain_download(self):
        """Test that we can download toolchains"""
        if use_network:
            with terminal.capture() as (stdout, stderr):
                url = self.toolchains.locate_arch_url('arm')
            self.assertRegex(url, 'https://www.kernel.org/pub/tools/'
                    'crosstool/files/bin/x86_64/.*/'
                    'x86_64-gcc-.*-nolibc[-_]arm-.*linux-gnueabi.tar.xz')

    def test_get_env_args(self):
        """Test the GetEnvArgs() function"""
        tc = self.toolchains.select('arm')
        self.assertEqual('arm-linux-',
                         tc.get_env_args(toolchain.VAR_CROSS_COMPILE))
        self.assertEqual('', tc.get_env_args(toolchain.VAR_PATH))
        self.assertEqual('arm',
                         tc.get_env_args(toolchain.VAR_ARCH))
        self.assertEqual('', tc.get_env_args(toolchain.VAR_MAKE_ARGS))

        tc = self.toolchains.select('sandbox')
        self.assertEqual('', tc.get_env_args(toolchain.VAR_CROSS_COMPILE))

        self.toolchains.add('/path/to/x86_64-linux-gcc', test=False)
        tc = self.toolchains.select('x86')
        self.assertEqual('/path/to',
                         tc.get_env_args(toolchain.VAR_PATH))
        tc.override_toolchain = 'clang'
        self.assertEqual('HOSTCC=clang CC=clang',
                         tc.get_env_args(toolchain.VAR_MAKE_ARGS))

        # Test config with ccache wrapper
        bsettings.setup(None)
        bsettings.add_file(settings_data_wrapper)

        tc = self.toolchains.select('arm')
        self.assertEqual('ccache arm-linux-',
                         tc.get_env_args(toolchain.VAR_CROSS_COMPILE))

        tc = self.toolchains.select('sandbox')
        self.assertEqual('', tc.get_env_args(toolchain.VAR_CROSS_COMPILE))

    def test_make_environment(self):
        """Test the make_environment function"""
        os.environ.pop('CROSS_COMPILE', None)
        tc = self.toolchains.select('arm')
        env = tc.make_environment(False)
        self.assertEqual(env[b'CROSS_COMPILE'], b'arm-linux-')

        tc = self.toolchains.select('sandbox')
        env = tc.make_environment(False)
        self.assertTrue(b'CROSS_COMPILE' not in env)

        # Test config with ccache wrapper
        bsettings.setup(None)
        bsettings.add_file(settings_data_wrapper)

        tc = self.toolchains.select('arm')
        env = tc.make_environment(False)
        self.assertEqual(env[b'CROSS_COMPILE'], b'ccache arm-linux-')

        tc = self.toolchains.select('sandbox')
        env = tc.make_environment(False)
        self.assertTrue(b'CROSS_COMPILE' not in env)

    def test_prepare_output_space(self):
        """Test preparation of output-directory space"""
        def _touch(fname):
            tools.write_file(os.path.join(base_dir, fname), b'')

        base_dir = tempfile.mkdtemp()

        # Add various files that we want removed and left alone
        to_remove = ['01_g0982734987_title', '102_g92bf_title',
                     '01_g2938abd8_title']
        to_leave = ['something_else', '01-something.patch', '01_another']
        for name in to_remove + to_leave:
            _touch(name)

        build = builder.Builder(self.toolchains, base_dir, None, 1, 2)
        build.commits = self.commits
        build.commit_count = len(commits)
        result = set(build._get_output_space_removals())
        expected = {os.path.join(base_dir, f) for f in to_remove}
        self.assertEqual(expected, result)

    def test_adjust_cfg_nop(self):
        """check various adjustments of config that are nops"""
        # enable an enabled CONFIG
        self.assertEqual(
            'CONFIG_FRED=y',
            cfgutil.adjust_cfg_line('CONFIG_FRED=y', {'FRED':'FRED'})[0])

        # disable a disabled CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line(
                '# CONFIG_FRED is not set', {'FRED':'~FRED'})[0])

        # use the adjust_cfg_lines() function
        self.assertEqual(
            ['CONFIG_FRED=y'],
            cfgutil.adjust_cfg_lines(['CONFIG_FRED=y'], {'FRED':'FRED'}))
        self.assertEqual(
            ['# CONFIG_FRED is not set'],
            cfgutil.adjust_cfg_lines(['CONFIG_FRED=y'], {'FRED':'~FRED'}))

        # handling an empty line
        self.assertEqual('#', cfgutil.adjust_cfg_line('#', {'FRED':'~FRED'})[0])

    def test_adjust_cfg(self):
        """check various adjustments of config"""
        # disable a CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line('CONFIG_FRED=1' , {'FRED':'~FRED'})[0])

        # enable a disabled CONFIG
        self.assertEqual(
            'CONFIG_FRED=y',
            cfgutil.adjust_cfg_line(
                '# CONFIG_FRED is not set', {'FRED':'FRED'})[0])

        # enable a CONFIG that doesn't exist
        self.assertEqual(
            ['CONFIG_FRED=y'],
            cfgutil.adjust_cfg_lines([], {'FRED':'FRED'}))

        # disable a CONFIG that doesn't exist
        self.assertEqual(
            ['# CONFIG_FRED is not set'],
            cfgutil.adjust_cfg_lines([], {'FRED':'~FRED'}))

        # disable a value CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line('CONFIG_FRED="fred"' , {'FRED':'~FRED'})[0])

        # setting a value CONFIG
        self.assertEqual(
            'CONFIG_FRED="fred"',
            cfgutil.adjust_cfg_line('# CONFIG_FRED is not set' ,
                                    {'FRED':'FRED="fred"'})[0])

        # changing a value CONFIG
        self.assertEqual(
            'CONFIG_FRED="fred"',
            cfgutil.adjust_cfg_line('CONFIG_FRED="ernie"' ,
                                    {'FRED':'FRED="fred"'})[0])

        # setting a value for a CONFIG that doesn't exist
        self.assertEqual(
            ['CONFIG_FRED="fred"'],
            cfgutil.adjust_cfg_lines([], {'FRED':'FRED="fred"'}))

    def test_convert_adjust_cfg_list(self):
        """Check conversion of the list of changes into a dict"""
        self.assertEqual({}, cfgutil.convert_list_to_dict(None))

        expect = {
            'FRED':'FRED',
            'MARY':'~MARY',
            'JOHN':'JOHN=0x123',
            'ALICE':'ALICE="alice"',
            'AMY':'AMY',
            'ABE':'~ABE',
            'MARK':'MARK=0x456',
            'ANNA':'ANNA="anna"',
            }
        actual = cfgutil.convert_list_to_dict(
            ['FRED', '~MARY', 'JOHN=0x123', 'ALICE="alice"',
             'CONFIG_AMY', '~CONFIG_ABE', 'CONFIG_MARK=0x456',
             'CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

        # Test comma-separated values
        actual = cfgutil.convert_list_to_dict(
            ['FRED,~MARY,JOHN=0x123', 'ALICE="alice"',
             'CONFIG_AMY,~CONFIG_ABE', 'CONFIG_MARK=0x456,CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

        # Test mixed comma-separated and individual values
        actual = cfgutil.convert_list_to_dict(
            ['FRED,~MARY', 'JOHN=0x123', 'ALICE="alice",CONFIG_AMY',
             '~CONFIG_ABE,CONFIG_MARK=0x456', 'CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

    def test_check_cfg_file(self):
        """Test check_cfg_file detects conflicts as expected"""
        # Check failure to disable CONFIG
        result = cfgutil.check_cfg_lines(['CONFIG_FRED=1'], {'FRED':'~FRED'})
        self.assertEqual([['~FRED', 'CONFIG_FRED=1']], result)

        result = cfgutil.check_cfg_lines(
            ['CONFIG_FRED=1', 'CONFIG_MARY="mary"'], {'FRED':'~FRED'})
        self.assertEqual([['~FRED', 'CONFIG_FRED=1']], result)

        result = cfgutil.check_cfg_lines(
            ['CONFIG_FRED=1', 'CONFIG_MARY="mary"'], {'MARY':'~MARY'})
        self.assertEqual([['~MARY', 'CONFIG_MARY="mary"']], result)

        # Check failure to enable CONFIG
        result = cfgutil.check_cfg_lines(
            ['# CONFIG_FRED is not set'], {'FRED':'FRED'})
        self.assertEqual([['FRED', '# CONFIG_FRED is not set']], result)

        # Check failure to set CONFIG value
        result = cfgutil.check_cfg_lines(
            ['# CONFIG_FRED is not set', 'CONFIG_MARY="not"'],
            {'MARY':'MARY="mary"', 'FRED':'FRED'})
        self.assertEqual([
            ['FRED', '# CONFIG_FRED is not set'],
            ['MARY="mary"', 'CONFIG_MARY="not"']], result)

        # Check failure to add CONFIG value
        result = cfgutil.check_cfg_lines([], {'MARY':'MARY="mary"'})
        self.assertEqual([
            ['MARY="mary"', 'Missing expected line: CONFIG_MARY="mary"']], result)

    def get_procs(self):
        """Get list of running process IDs from the running file"""
        running_fname = os.path.join(self.base_dir, control.RUNNING_FNAME)
        items = tools.read_file(running_fname, binary=False).split()
        return [int(x) for x in items]

    def get_time(self):
        """Get current mock time for testing"""
        return self.cur_time

    def inc_time(self, amount):
        """Increment mock time, handling process exit if scheduled"""
        self.cur_time += amount

        # Handle a process exiting
        if self.finish_time == self.cur_time:
            self.valid_pids = [pid for pid in self.valid_pids
                               if pid != self.finish_pid]

    def kill(self, pid, signal):
        """Mock kill function that validates process IDs"""
        if pid not in self.valid_pids:
            raise OSError('Invalid PID')

    def test_process_limit(self):
        """Test wait_for_process_limit() function"""
        tmpdir = self.base_dir

        with (patch('time.time', side_effect=self.get_time),
              patch('time.perf_counter', side_effect=self.get_time),
              patch('time.monotonic', side_effect=self.get_time),
              patch('time.sleep', side_effect=self.inc_time),
              patch('os.kill', side_effect=self.kill)):
            # Grab the process. Since there is no other profcess, this should
            # immediately succeed
            control.wait_for_process_limit(1, tmpdir=tmpdir, pid=1)
            lines = terminal.get_print_test_lines()
            self.assertEqual(0, self.cur_time)
            self.assertEqual('Waiting for other buildman processes...',
                             lines[0].text)
            self.assertEqual(self._col.RED, lines[0].colour)
            self.assertEqual(False, lines[0].newline)
            self.assertEqual(True, lines[0].bright)

            self.assertEqual('done...', lines[1].text)
            self.assertEqual(None, lines[1].colour)
            self.assertEqual(False, lines[1].newline)
            self.assertEqual(True, lines[1].bright)

            self.assertEqual('starting build', lines[2].text)
            self.assertEqual([1], control.read_procs(tmpdir))
            self.assertEqual(None, lines[2].colour)
            self.assertEqual(False, lines[2].newline)
            self.assertEqual(True, lines[2].bright)

            # Try again, with a different PID...this should eventually timeout
            # and start the build anyway
            self.cur_time = 0
            self.valid_pids = [1]
            control.wait_for_process_limit(1, tmpdir=tmpdir, pid=2)
            lines = terminal.get_print_test_lines()
            self.assertEqual('Waiting for other buildman processes...',
                             lines[0].text)
            self.assertEqual('timeout...', lines[1].text)
            self.assertEqual(None, lines[1].colour)
            self.assertEqual(False, lines[1].newline)
            self.assertEqual(True, lines[1].bright)
            self.assertEqual('starting build', lines[2].text)
            self.assertEqual([1, 2], control.read_procs(tmpdir))
            self.assertEqual(control.RUN_WAIT_S, self.cur_time)

            # Check lock-busting
            self.cur_time = 0
            self.valid_pids = [1, 2]
            lock_fname = os.path.join(tmpdir, control.LOCK_FNAME)
            lock = FileLock(lock_fname)
            lock.acquire(timeout=1)
            control.wait_for_process_limit(1, tmpdir=tmpdir, pid=3)
            lines = terminal.get_print_test_lines()
            self.assertEqual('Waiting for other buildman processes...',
                             lines[0].text)
            self.assertEqual('failed to get lock: busting...', lines[1].text)
            self.assertEqual(None, lines[1].colour)
            self.assertEqual(False, lines[1].newline)
            self.assertEqual(True, lines[1].bright)
            self.assertEqual('timeout...', lines[2].text)
            self.assertEqual('starting build', lines[3].text)
            self.assertEqual([1, 2, 3], control.read_procs(tmpdir))
            self.assertEqual(control.RUN_WAIT_S, self.cur_time)
            lock.release()

            # Check handling of dead processes. Here we have PID 2 as a running
            # process, even though the PID file contains 1, 2 and 3. So we can
            # add one more PID, to make 2 and 4
            self.cur_time = 0
            self.valid_pids = [2]
            control.wait_for_process_limit(2, tmpdir=tmpdir, pid=4)
            lines = terminal.get_print_test_lines()
            self.assertEqual('Waiting for other buildman processes...',
                             lines[0].text)
            self.assertEqual('done...', lines[1].text)
            self.assertEqual('starting build', lines[2].text)
            self.assertEqual([2, 4], control.read_procs(tmpdir))
            self.assertEqual(0, self.cur_time)

            # Try again, with PID 2 quitting at time 50. This allows the new
            # build to start
            self.cur_time = 0
            self.valid_pids = [2, 4]
            self.finish_pid = 2
            self.finish_time = 50
            control.wait_for_process_limit(2, tmpdir=tmpdir, pid=5)
            lines = terminal.get_print_test_lines()
            self.assertEqual('Waiting for other buildman processes...',
                             lines[0].text)
            self.assertEqual('done...', lines[1].text)
            self.assertEqual('starting build', lines[2].text)
            self.assertEqual([4, 5], control.read_procs(tmpdir))
            self.assertEqual(self.finish_time, self.cur_time)

    def call_make_environment(self, tchn, full_path, in_env=None):
        """Call Toolchain.make_environment() and process the result

        Args:
            tchn (Toolchain): Toolchain to use
            full_path (bool): True to return the full path in CROSS_COMPILE
                rather than adding it to the PATH variable
            in_env (dict): Input environment to use, None to use current env

        Returns:
            tuple:
                dict: Changes that MakeEnvironment has made to the environment
                    key: Environment variable that was changed
                    value: New value (for PATH this only includes components
                        which were added)
                str: Full value of the new PATH variable
        """
        env = tchn.make_environment(full_path, env=in_env)

        # Get the original environment
        orig_env = dict(os.environb if in_env is None else in_env)
        orig_path = orig_env[b'PATH'].split(b':')

        # Find new variables
        diff = dict((k, env[k]) for k in env if orig_env.get(k) != env[k])

        # Find new / different path components
        diff_path = None
        new_path = None
        if b'PATH' in diff:
            new_path = diff[b'PATH'].split(b':')
            diff_paths = [p for p in new_path if p not in orig_path]
            diff_path = b':'.join(p for p in new_path if p not in orig_path)
            if diff_path:
                diff[b'PATH'] = diff_path
            else:
                del diff[b'PATH']
        return diff, new_path

    def test_toolchain_env(self):
        """Test PATH and other environment settings for toolchains"""
        # Use a toolchain which has a path, so that full_path makes a difference
        tchn = self.toolchains.select('aarch64')

        # Normal cases
        diff = self.call_make_environment(tchn, full_path=False)[0]
        self.assertEqual(
            {b'CROSS_COMPILE': b'aarch64-linux-', b'LC_ALL': b'C',
             b'PATH': b'/path/to'}, diff)

        diff = self.call_make_environment(tchn, full_path=True)[0]
        self.assertEqual(
            {b'CROSS_COMPILE': b'/path/to/aarch64-linux-', b'LC_ALL': b'C'},
            diff)

        # When overriding the toolchain, only LC_ALL should be set
        tchn.override_toolchain = True
        diff = self.call_make_environment(tchn, full_path=True)[0]
        self.assertEqual({b'LC_ALL': b'C'}, diff)

        # Test that Python sandbox is handled correctly
        tchn.override_toolchain = False
        sys.prefix = '/some/venv'
        env = dict(os.environb)
        env[b'PATH'] = b'/some/venv/bin:other/things'
        tchn.path = '/my/path'
        diff, diff_path = self.call_make_environment(tchn, False, env)

        self.assertIn(b'PATH', diff)
        self.assertEqual([b'/some/venv/bin', b'/my/path', b'other/things'],
                         diff_path)
        self.assertEqual(
            {b'CROSS_COMPILE': b'aarch64-linux-', b'LC_ALL': b'C',
             b'PATH': b'/my/path'}, diff)

        # Handle a toolchain wrapper
        tchn.path = ''
        bsettings.add_section('toolchain-wrapper')
        bsettings.set_item('toolchain-wrapper', 'my-wrapper', 'fred')
        diff = self.call_make_environment(tchn, full_path=True)[0]
        self.assertEqual(
            {b'CROSS_COMPILE': b'fred aarch64-linux-', b'LC_ALL': b'C'}, diff)

    def test_skip_dtc(self):
        """Test skipping building the dtc tool"""
        os.environ.pop('DTC', None)
        old_path = os.getenv('PATH')
        try:
            os.environ['PATH'] = self.base_dir

            # Check a missing tool
            with self.assertRaises(ValueError) as exc:
                builder.Builder(self.toolchains, self.base_dir, None, 0, 2,
                                dtc_skip=True)
            self.assertIn('Cannot find dtc', str(exc.exception))

            # Create a fake tool to use
            dtc = os.path.join(self.base_dir, 'dtc')
            tools.write_file(dtc, b'xx')
            os.chmod(dtc, 0o777)

            build = builder.Builder(self.toolchains, self.base_dir, None, 0, 2,
                                    dtc_skip=True)
            tch = self.toolchains.select('arm')
            env = build.make_environment(tch)
            self.assertIn(b'DTC', env)

            # Try the normal case, i.e. not skipping the dtc build
            build = builder.Builder(self.toolchains, self.base_dir, None, 0, 2)
            tch = self.toolchains.select('arm')
            env = build.make_environment(tch)
            self.assertNotIn(b'DTC', env)
        finally:
            os.environ['PATH'] = old_path

    def test_homedir(self):
        """Test using ~ in a toolchain or toolchain-prefix section"""
        # Add some test settings
        bsettings.setup(None)
        bsettings.add_file(settings_data_homedir)

        # Set up the toolchains
        home = os.path.expanduser('~')
        toolchains = toolchain.Toolchains()
        toolchains.get_settings()
        self.assertEqual([f'{home}/mypath'], toolchains.paths)

        # Check scanning
        with terminal.capture() as (stdout, _):
            toolchains.scan(verbose=True, raise_on_error=False)
        lines = iter(stdout.getvalue().splitlines() + ['##done'])
        self.assertEqual('Scanning for tool chains', next(lines))
        self.assertEqual(f"   - scanning prefix '{home}/mypath-x86-'",
                         next(lines))
        self.assertEqual(
            f"Error: No tool chain found for prefix '{home}/mypath-x86-gcc'",
            next(lines))
        self.assertEqual(f"   - scanning path '{home}/mypath'", next(lines))
        self.assertEqual(f"      - looking in '{home}/mypath/.'", next(lines))
        self.assertEqual(f"      - looking in '{home}/mypath/bin'", next(lines))
        self.assertEqual(f"      - looking in '{home}/mypath/usr/bin'",
                         next(lines))
        self.assertEqual('##done', next(lines))

        # Check adding a toolchain
        with terminal.capture() as (stdout, _):
            toolchains.add('~/aarch64-linux-gcc', test=True, verbose=True)
        lines = iter(stdout.getvalue().splitlines() + ['##done'])
        self.assertEqual('Tool chain test:  BAD', next(lines))
        self.assertEqual(f'Command: {home}/aarch64-linux-gcc --version',
                         next(lines))
        self.assertEqual('', next(lines))
        self.assertEqual('', next(lines))
        self.assertEqual('##done', next(lines))

    def test_kconfig_changed_since(self):
        """Test the kconfig_changed_since() function"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a reference file
            ref_file = os.path.join(tmpdir, 'done')
            tools.write_file(ref_file, b'0\n')

            # Test with no Kconfig files - should return False
            self.assertFalse(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # Wait a bit to ensure timestamp difference
            time.sleep(0.1)

            # Create a Kconfig file newer than the reference
            kconfig = os.path.join(tmpdir, 'Kconfig')
            tools.write_file(kconfig, b'config TEST\n')

            # Should now return True since Kconfig is newer
            self.assertTrue(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # Create a new reference file (newer than Kconfig)
            time.sleep(0.1)
            tools.write_file(ref_file, b'0\n')

            # Should now return False since reference is newer
            self.assertFalse(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # Test with non-existent reference file
            self.assertFalse(
                builderthread.kconfig_changed_since(
                    os.path.join(tmpdir, 'nonexistent'), tmpdir))

            # Test with Kconfig in subdirectory
            subdir = os.path.join(tmpdir, 'sub')
            os.makedirs(subdir)
            time.sleep(0.1)
            tools.write_file(os.path.join(subdir, 'Kconfig.sub'),
                             b'config SUBTEST\n')

            # Should return True due to newer Kconfig.sub in subdir
            self.assertTrue(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # Create a new reference file (newer than all Kconfig files)
            time.sleep(0.1)
            tools.write_file(ref_file, b'0\n')

            # Should now return False
            self.assertFalse(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # Test with defconfig file - need target parameter
            configs_dir = os.path.join(tmpdir, 'configs')
            os.makedirs(configs_dir)
            time.sleep(0.1)
            tools.write_file(os.path.join(configs_dir, 'sandbox_defconfig'),
                             b'CONFIG_SANDBOX=y\n')

            # Without target, defconfig is not checked
            self.assertFalse(
                builderthread.kconfig_changed_since(ref_file, tmpdir))

            # With matching target, defconfig is checked
            self.assertTrue(
                builderthread.kconfig_changed_since(ref_file, tmpdir,
                                                    'sandbox'))

            # With non-matching target, defconfig is not checked
            self.assertFalse(
                builderthread.kconfig_changed_since(ref_file, tmpdir,
                                                    'other_board'))


if __name__ == "__main__":
    unittest.main()
