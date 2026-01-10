# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2014 Google, Inc
#

import io
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from buildman import board
from buildman import boards
from buildman.boards import Extended
from buildman import bsettings
from buildman import builderthread
from buildman import cmdline
from buildman import control
from buildman import toolchain
from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import test_util
from u_boot_pylib import tools

SETTINGS_DATA = '''
# Buildman settings file
[global]

[toolchain]

[toolchain-alias]

[make-flags]
src=/home/sjg/c/src
chroot=/home/sjg/c/chroot
vboot=VBOOT_DEBUG=1 MAKEFLAGS_VBOOT=DEBUG=1 CFLAGS_EXTRA_VBOOT=-DUNROLL_LOOPS VBOOT_SOURCE=${src}/platform/vboot_reference
chromeos_coreboot=VBOOT=${chroot}/build/link/usr ${vboot}
chromeos_daisy=VBOOT=${chroot}/build/daisy/usr ${vboot}
chromeos_peach=VBOOT=${chroot}/build/peach_pit/usr ${vboot}
'''

BOARDS = [
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 0', 'board0',  ''],
    ['Active', 'arm', 'armv7', '', 'Tester', 'ARM Board 1', 'board1', ''],
    ['Active', 'powerpc', 'powerpc', '', 'Tester', 'PowerPC board 1', 'board2', ''],
    ['Active', 'sandbox', 'sandbox', '', 'Tester', 'Sandbox board', 'board4', ''],
]

COMMIT_SHORTLOG = """4aca821 patman: Avoid changing the order of tags
39403bb patman: Use --no-pager' to stop git from forking a pager
db6e6f2 patman: Remove the -a option
f2ccf03 patman: Correct unit tests to run correctly
1d097f9 patman: Fix indentation in terminal.py
d073747 patman: Support the 'reverse' option for 'git log
"""

commit_log = ["""commit 7f6b8315d18f683c5181d0c3694818c1b2a20dcd
Author: Masahiro Yamada <yamada.m@jp.panasonic.com>
Date:   Fri Aug 22 19:12:41 2014 +0900

    buildman: refactor help message

    "buildman [options]" is displayed by default.

    Append the rest of help messages to parser.usage
    instead of replacing it.

    Besides, "-b <branch>" is not mandatory since commit fea5858e.
    Drop it from the usage.

    Signed-off-by: Masahiro Yamada <yamada.m@jp.panasonic.com>
""",
"""commit d0737479be6baf4db5e2cdbee123e96bc5ed0ba8
Author: Simon Glass <sjg@chromium.org>
Date:   Thu Aug 14 16:48:25 2014 -0600

    patman: Support the 'reverse' option for 'git log'

    This option is currently not supported, but needs to be, for buildman to
    operate as expected.

    Series-changes: 7
    - Add new patch to fix the 'reverse' bug

    Series-version: 8

    Change-Id: I79078f792e8b390b8a1272a8023537821d45feda
    Reported-by: York Sun <yorksun@freescale.com>
    Signed-off-by: Simon Glass <sjg@chromium.org>

""",
"""commit 1d097f9ab487c5019152fd47bda126839f3bf9fc
Author: Simon Glass <sjg@chromium.org>
Date:   Sat Aug 9 11:44:32 2014 -0600

    patman: Fix indentation in terminal.py

    This code came from a different project with 2-character indentation. Fix
    it for U-Boot.

    Series-changes: 6
    - Add new patch to fix indentation in teminal.py

    Change-Id: I5a74d2ebbb3cc12a665f5c725064009ac96e8a34
    Signed-off-by: Simon Glass <sjg@chromium.org>

""",
"""commit f2ccf03869d1e152c836515a3ceb83cdfe04a105
Author: Simon Glass <sjg@chromium.org>
Date:   Sat Aug 9 11:08:24 2014 -0600

    patman: Correct unit tests to run correctly

    It seems that doctest behaves differently now, and some of the unit tests
    do not run. Adjust the tests to work correctly.

     ./tools/patman/patman --test
    <unittest.result.TestResult run=10 errors=0 failures=0>

    Series-changes: 6
    - Add new patch to fix patman unit tests

    Change-Id: I3d2ca588f4933e1f9d6b1665a00e4ae58269ff3b

""",
"""commit db6e6f2f9331c5a37647d6668768d4a40b8b0d1c
Author: Simon Glass <sjg@chromium.org>
Date:   Sat Aug 9 12:06:02 2014 -0600

    patman: Remove the -a option

    It seems that this is no longer needed, since checkpatch.pl will catch
    whitespace problems in patches. Also the option is not widely used, so
    it seems safe to just remove it.

    Series-changes: 6
    - Add new patch to remove patman's -a option

    Suggested-by: Masahiro Yamada <yamada.m@jp.panasonic.com>
    Change-Id: I5821a1c75154e532c46513486ca40b808de7e2cc

""",
"""commit 39403bb4f838153028a6f21ca30bf100f3791133
Author: Simon Glass <sjg@chromium.org>
Date:   Thu Aug 14 21:50:52 2014 -0600

    patman: Use --no-pager' to stop git from forking a pager

""",
"""commit 4aca821e27e97925c039e69fd37375b09c6f129c
Author: Simon Glass <sjg@chromium.org>
Date:   Fri Aug 22 15:57:39 2014 -0600

    patman: Avoid changing the order of tags

    patman collects tags that it sees in the commit and places them nicely
    sorted at the end of the patch. However, this is not really necessary and
    in fact is apparently not desirable.

    Series-changes: 9
    - Add new patch to avoid changing the order of tags

    Series-version: 9

    Suggested-by: Masahiro Yamada <yamada.m@jp.panasonic.com>
    Change-Id: Ib1518588c1a189ad5c3198aae76f8654aed8d0db
"""]

TEST_BRANCH = '__testbranch'

class TestFunctional(unittest.TestCase):
    """Functional test for buildman.

    This aims to test from just below the invocation of buildman (parsing
    of arguments) to 'make' and 'git' invocation. It is not a true
    emd-to-end test, as it mocks git, make and the tool chain. But this
    makes it easier to detect when the builder is doing the wrong thing,
    since in many cases this test code will fail. For example, only a
    very limited subset of 'git' arguments is supported - anything
    unexpected will fail.
    """
    def setUp(self):
        self._base_dir = tempfile.mkdtemp()
        self._output_dir = tempfile.mkdtemp()
        self._git_dir = os.path.join(self._base_dir, 'src')
        self._buildman_pathname = sys.argv[0]
        self._buildman_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
        command.TEST_RESULT = self._handle_command
        bsettings.setup(None)
        bsettings.add_file(SETTINGS_DATA)
        self.setup_toolchains()
        self._toolchains.add('arm-gcc', test=False)
        self._toolchains.add('powerpc-gcc', test=False)
        self._boards = boards.Boards()
        for brd in BOARDS:
            self._boards.add_board(board.Board(*brd))

        # Directories where the source been cloned
        self._clone_dirs = []
        self._commits = len(COMMIT_SHORTLOG.splitlines()) + 1
        self._total_builds = self._commits * len(BOARDS)

        # Number of calls to make
        self._make_calls = 0

        # Map of [board, commit] to error messages
        self._error = {}

        self._test_branch = TEST_BRANCH

        # Set to True to report missing blobs
        self._missing = False

        self._buildman_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
        self._test_dir = os.path.join(self._buildman_dir, 'test')

        # Set up some fake source files
        shutil.copytree(self._test_dir, self._git_dir)

        # Avoid sending any output and clear all terminal output
        terminal.set_print_test_mode()
        terminal.get_print_test_lines()

    def tearDown(self):
        shutil.rmtree(self._base_dir)
        shutil.rmtree(self._output_dir)

    def setup_toolchains(self):
        self._toolchains = toolchain.Toolchains()
        self._toolchains.add('gcc', test=False)

    def _run_buildman(self, *args):
        all_args = [self._buildman_pathname] + list(args)
        return command.run_one(*all_args, capture=True, capture_stderr=True)

    def _run_control(self, *args, brds=False, clean_dir=False,
                    test_thread_exceptions=False, get_builder=True):
        """Run buildman

        Args:
            args: List of arguments to pass
            brds: Boards object, or False to pass self._boards, or None to pass
                None
            clean_dir: Used for tests only, indicates that the existing output_dir
                should be removed before starting the build
            test_thread_exceptions: Uses for tests only, True to make the threads
                raise an exception instead of reporting their result. This simulates
                a failure in the code somewhere
            get_builder (bool): Set self._builder to the resulting builder

        Returns:
            result code from buildman
        """
        sys.argv = [sys.argv[0]] + list(args)
        args = cmdline.parse_args()
        if brds is False:
            brds = self._boards
        result = control.do_buildman(
            args, toolchains=self._toolchains, make_func=self._handle_make,
            brds=brds, clean_dir=clean_dir,
            test_thread_exceptions=test_thread_exceptions)
        if get_builder:
            self._builder = control.TEST_BUILDER
        return result

    def test_full_help(self):
        command.TEST_RESULT = None
        result = self._run_buildman('-H')
        help_file = os.path.join(self._buildman_dir, 'README.rst')
        # Remove possible extraneous strings
        extra = '::::::::::::::\n' + help_file + '\n::::::::::::::\n'
        gothelp = result.stdout.replace(extra, '')
        self.assertEqual(len(gothelp), os.path.getsize(help_file))
        self.assertEqual(0, len(result.stderr))
        self.assertEqual(0, result.return_code)

    def test_help(self):
        command.TEST_RESULT = None
        result = self._run_buildman('-h')
        help_file = os.path.join(self._buildman_dir, 'README.rst')
        self.assertTrue(len(result.stdout) > 1000)
        self.assertEqual(0, len(result.stderr))
        self.assertEqual(0, result.return_code)

    def test_git_setup(self):
        """Test gitutils.Setup(), from outside the module itself"""
        command.TEST_RESULT = command.CommandResult(return_code=1)
        gitutil.setup()
        self.assertEqual(gitutil.USE_NO_DECORATE, False)

        command.TEST_RESULT = command.CommandResult(return_code=0)
        gitutil.setup()
        self.assertEqual(gitutil.USE_NO_DECORATE, True)

    def _handle_command_git_log(self, args):
        if args[-1] == '--':
            args = args[:-1]
        if '-n0' in args:
            return command.CommandResult(return_code=0)
        elif args[-1] == 'upstream/master..%s' % self._test_branch:
            return command.CommandResult(return_code=0, stdout=COMMIT_SHORTLOG)
        elif args[:3] == ['--no-color', '--no-decorate', '--reverse']:
            if args[-1] == self._test_branch:
                count = int(args[3][2:])
                return command.CommandResult(return_code=0,
                                            stdout=''.join(commit_log[:count]))

        # Not handled, so abort
        print('git log', args)
        sys.exit(1)

    def _handle_command_git_config(self, args):
        config = args[0]
        if config == 'sendemail.aliasesfile':
            return command.CommandResult(return_code=0)
        elif config.startswith('branch.badbranch'):
            return command.CommandResult(return_code=1)
        elif config == 'branch.%s.remote' % self._test_branch:
            return command.CommandResult(return_code=0, stdout='upstream\n')
        elif config == 'branch.%s.merge' % self._test_branch:
            return command.CommandResult(return_code=0,
                                         stdout='refs/heads/master\n')

        # Not handled, so abort
        print('git config', args)
        sys.exit(1)

    def _handle_command_git(self, in_args):
        """Handle execution of a git command

        This uses a hacked-up parser.

        Args:
            in_args: Arguments after 'git' from the command line
        """
        git_args = []           # Top-level arguments to git itself
        sub_cmd = None          # Git sub-command selected
        args = []               # Arguments to the git sub-command
        for arg in in_args:
            if sub_cmd:
                args.append(arg)
            elif arg[0] == '-':
                git_args.append(arg)
            else:
                if git_args and git_args[-1] in ['--git-dir', '--work-tree']:
                    git_args.append(arg)
                else:
                    sub_cmd = arg
        if sub_cmd == 'config':
            return self._handle_command_git_config(args)
        elif sub_cmd == 'log':
            return self._handle_command_git_log(args)
        elif sub_cmd == 'clone':
            return command.CommandResult(return_code=0)
        elif sub_cmd == 'checkout':
            return command.CommandResult(return_code=0)
        elif sub_cmd == 'worktree':
            return command.CommandResult(return_code=0)

        # Not handled, so abort
        print('git', git_args, sub_cmd, args)
        sys.exit(1)

    def _handle_command_nm(self, args):
        # Return nm --size-sort output with function sizes that vary between
        # calls to simulate changes between commits
        self._nm_calls = getattr(self, '_nm_calls', 0) + 1
        base = self._nm_calls * 0x10
        stdout = f'''{0x100 + base:08x} T main
{0x80 + base:08x} T board_init
{0x50 + base:08x} t local_func
{0x30:08x} d data_var
'''
        return command.CommandResult(return_code=0, stdout=stdout)

    def _handle_command_objdump(self, args):
        # Return objdump -h output with .rodata section
        stdout = '''
u-boot:     file format elf32-littlearm

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .text         00010000  00000000  00000000  00001000  2**2
  1 .rodata       00001000  00010000  00010000  00011000  2**2
  2 .data         00000100  00011000  00011000  00012000  2**2
'''
        return command.CommandResult(return_code=0, stdout=stdout)

    def _handle_command_objcopy(self, args):
        return command.CommandResult(return_code=0)

    def _handle_command_size(self, args):
        # Return size output - vary the size based on call count to simulate
        # changes between commits
        self._size_calls = getattr(self, '_size_calls', 0) + 1
        text = 10000 + self._size_calls * 100
        data = 1000
        bss = 500
        total = text + data + bss
        fname = args[-1] if args else 'u-boot'
        stdout = f'''   text    data     bss     dec     hex filename
  {text}    {data}     {bss}   {total}    {total:x} {fname}
'''
        return command.CommandResult(return_code=0, stdout=stdout)

    def _handle_command_cpp(self, args):
        # args ['-nostdinc', '-P', '-I', '/tmp/tmp7f17xk_o/src', '-undef',
        # '-x', 'assembler-with-cpp', fname]
        fname = args[7]
        buf = io.StringIO()
        for line in tools.read_file(fname, False).splitlines():
            if line.startswith('#include'):
                # Example: #include <configs/renesas_rcar2.config>
                m_incfname = re.match('#include <(.*)>', line)
                data = tools.read_file(m_incfname.group(1), False)
                for line in data.splitlines():
                    print(line, file=buf)
            else:
                print(line, file=buf)
        return command.CommandResult(stdout=buf.getvalue(), return_code=0)

    def _handle_command(self, **kwargs):
        """Handle a command execution.

        The command is in kwargs['pipe-list'], as a list of pipes, each a
        list of commands. The command should be emulated as required for
        testing purposes.

        Returns:
            A CommandResult object
        """
        pipe_list = kwargs['pipe_list']
        wc = False
        if len(pipe_list) != 1:
            if pipe_list[1] == ['wc', '-l']:
                wc = True
            else:
                print('invalid pipe', kwargs)
                sys.exit(1)
        cmd = pipe_list[0][0]
        args = pipe_list[0][1:]
        result = None
        if cmd == 'git':
            result = self._handle_command_git(args)
        elif cmd == './scripts/show-gnu-make':
            return command.CommandResult(return_code=0, stdout='make')
        elif cmd.endswith('nm'):
            return self._handle_command_nm(args)
        elif cmd.endswith('objdump'):
            return self._handle_command_objdump(args)
        elif cmd.endswith('objcopy'):
            return self._handle_command_objcopy(args)
        elif cmd.endswith( 'size'):
            return self._handle_command_size(args)
        elif cmd.endswith( 'cpp'):
            return self._handle_command_cpp(args)
        elif cmd == 'gcc' and args[0] == '-E':
            return self._handle_command_cpp(args[1:])
        if not result:
            # Not handled, so abort
            print('unknown command', kwargs)
            sys.exit(1)

        if wc:
            result.stdout = len(result.stdout.splitlines())
        return result

    def _handle_make(self, commit, brd, stage, cwd, *args, **kwargs):
        """Handle execution of 'make'

        Args:
            commit: Commit object that is being built
            brd: Board object that is being built
            stage: Stage that we are at (mrproper, config, build)
            cwd: Directory where make should be run
            args: Arguments to pass to make
            kwargs: Arguments to pass to command.run_one()
        """
        self._make_calls += 1
        # Capture args for tests that need to inspect them
        if hasattr(self, '_captured_make_args') and stage == 'build':
            self._captured_make_args.append(args)
        out_dir = ''
        for arg in args:
            if arg.startswith('O='):
                out_dir = arg[2:]
        if stage == 'mrproper':
            return command.CommandResult(return_code=0)
        elif stage == 'config':
            fname = os.path.join(cwd or '', out_dir, '.config')
            # Vary config based on commit to simulate config changes
            seq = commit.sequence if hasattr(commit, 'sequence') else 0
            config = f'CONFIG_SOMETHING={seq + 1}\n'
            if seq > 0:
                config += 'CONFIG_NEW_OPTION=y\n'
            tools.write_file(fname, config.encode('utf-8'))
            # Also create u-boot.cfg which buildman reads for -K flag
            cfg_fname = os.path.join(cwd or '', out_dir, 'u-boot.cfg')
            cfg_content = f'#define CONFIG_VALUE {seq + 100}\n'
            if seq > 0:
                cfg_content += '#define CONFIG_EXTRA 1\n'
            tools.write_file(cfg_fname, cfg_content.encode('utf-8'))
            return command.CommandResult(return_code=0,
                    combined='Test configuration complete')
        elif stage == 'oldconfig':
            return command.CommandResult(return_code=0)
        elif stage == 'build':
            stderr = ''
            fname = os.path.join(cwd or '', out_dir, 'u-boot')
            tools.write_file(fname, b'U-Boot')

            # Handle missing blobs
            if self._missing:
                if 'BINMAN_ALLOW_MISSING=1' in args:
                    stderr = '''+Image 'main-section' is missing external blobs and is non-functional: intel-descriptor intel-ifwi intel-fsp-m intel-fsp-s intel-vbt
Image 'main-section' has faked external blobs and is non-functional: descriptor.bin fsp_m.bin fsp_s.bin vbt.bin

Some images are invalid'''
                else:
                    stderr = "binman: Filename 'fsp.bin' not found in input path"
            elif type(commit) is not str:
                stderr = self._error.get((brd.target, commit.sequence))
            else:
                # For current source builds, commit is 'current'
                stderr = self._error.get((brd.target, commit))

            if stderr:
                return command.CommandResult(return_code=2, stderr=stderr)
            return command.CommandResult(return_code=0)

        # Not handled, so abort
        print('_handle_make failure: make', stage)
        sys.exit(1)

    # Example function to print output lines
    def print_lines(self, lines):
        print(len(lines))
        for line in lines:
            print(line)
        #self.print_lines(terminal.get_print_test_lines())

    def test_no_boards(self):
        """Test that buildman aborts when there are no boards"""
        self._boards = boards.Boards()
        with self.assertRaises(SystemExit):
            self._run_control()

    def test_current_source(self):
        """Very simple test to invoke buildman on the current source"""
        self.setup_toolchains()
        self._run_control('-o', self._output_dir)
        lines = terminal.get_print_test_lines()
        self.assertIn('Building current source for %d boards' % len(BOARDS),
                      lines[0].text)

    def test_bad_branch(self):
        """Test that we can detect an invalid branch"""
        with self.assertRaises(ValueError):
            self._run_control('-b', 'badbranch')

    def test_bad_toolchain(self):
        """Test that missing toolchains are detected"""
        self.setup_toolchains()
        ret_code = self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        lines = terminal.get_print_test_lines()

        # Buildman always builds the upstream commit as well
        self.assertIn('Building %d commits for %d boards' %
                (self._commits, len(BOARDS)), lines[0].text)
        self.assertEqual(self._builder.count, self._total_builds)

        # Only sandbox should succeed, the others don't have toolchains
        self.assertEqual(self._builder.fail,
                         self._total_builds - self._commits)
        self.assertEqual(ret_code, 100)

        for commit in range(self._commits):
            for brd in self._boards.get_list():
                if brd.arch != 'sandbox':
                  errfile = self._builder.get_err_file(commit, brd.target)
                  fd = open(errfile)
                  self.assertEqual(
                      fd.readlines(),
                      [f'Tool chain error for {brd.arch}: '
                       f"No tool chain found for arch '{brd.arch}'"])
                  fd.close()

    def test_toolchain_errors(self):
        """Test that toolchain errors are reported in the summary

        When toolchains are missing, boards cannot be built. The summary
        should report which boards were not built.
        """
        self.setup_toolchains()
        # Build with missing toolchains - only sandbox will succeed
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)

        # Now show summary - should report boards not built
        terminal.get_print_test_lines()  # Clear
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir, '-s',
                         clean_dir=False)
        lines = terminal.get_print_test_lines()
        text = '\n'.join(line.text for line in lines)

        # Check that boards with missing toolchains are reported as not built
        self.assertIn('Boards not built', text)
        self.assertIn('board0', text)
        self.assertIn('board1', text)
        self.assertIn('board2', text)

    def test_branch(self):
        """Test building a branch with all toolchains present"""
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 0)

    def test_current_source_ide(self):
        """Test building current source with IDE mode enabled

        This tests that:
        - Build errors are written to stderr
        - Progress output does not go to stdout in IDE mode
        - Summary mode (-s) shows output again
        """
        # Set up a build error for sandbox board (board4) which has toolchain
        # For current source builds, commit is 'current' string
        error_msg = 'test_error_msg.c:123: error: test failure\n'
        self._error['board4', 'current'] = error_msg

        # Capture stderr during the build
        captured_stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = captured_stderr
            terminal.get_print_test_lines()  # Clear any previous output
            self._run_control('-o', self._output_dir, '-I')
        finally:
            sys.stderr = old_stderr

        # Verify there is a build failure
        self.assertEqual(self._builder.fail, 1)

        # Check stderr has exactly the expected error
        self.assertEqual(captured_stderr.getvalue(),
                         'test_error_msg.c:123: error: test failure\n')

        # In IDE mode, there should be no stdout output at all
        self.assertEqual(terminal.get_print_test_lines(), [])

        # Now run with -s to show summary - output should appear again
        terminal.get_print_test_lines()  # Clear
        self._run_control('-o', self._output_dir, '-s', clean_dir=False)
        lines = terminal.get_print_test_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn('Summary of', lines[0].text)
        self.assertIn('board4', lines[1].text)

    def test_branch_ide(self):
        """Test building a branch with IDE mode and summary

        This tests _print_ide_output() which outputs errors to stderr during
        the summary phase for branch builds.
        """
        # Set up error for commit 1 on sandbox board
        error_msg = 'branch_error.c:456: error: branch failure\n'
        self._error['board4', 1] = error_msg

        # Build branch normally first (writes results to disk)
        terminal.get_print_test_lines()
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        self.assertEqual(self._builder.fail, 1)

        # Run summary with IDE mode - errors go to stderr via _print_ide_output
        captured_stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = captured_stderr
            terminal.get_print_test_lines()
            self._run_control('-b', TEST_BRANCH, '-o', self._output_dir, '-sI',
                             clean_dir=False)
        finally:
            sys.stderr = old_stderr

        # Check stderr has the error from _print_ide_output
        self.assertEqual(captured_stderr.getvalue(),
                         'branch_error.c:456: error: branch failure\n')

    def test_branch_summary(self):
        """Test building a branch and then showing a summary"""
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 0)

        # Now run with -s to show summary
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-s', '-o', self._output_dir,
                         clean_dir=False)
        # Summary should not trigger any builds
        self.assertEqual(self._make_calls, 0)
        lines = terminal.get_print_test_lines()
        self.assertIn('(no errors to report)', lines[-1].text)

        # Now run with -S to show sizes as well
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-sS', '-o', self._output_dir,
                         clean_dir=False)
        self.assertEqual(self._make_calls, 0)
        lines = terminal.get_print_test_lines()
        # Check that size information is displayed (arch names with size deltas)
        text = '\n'.join(line.text for line in lines)
        self.assertIn('arm', text)
        self.assertIn('sandbox', text)
        self.assertIn('(no errors to report)', lines[-1].text)

        # Now run with -B to show bloat (function size changes)
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-sSB', '-o', self._output_dir,
                         clean_dir=False)
        self.assertEqual(self._make_calls, 0)
        lines = terminal.get_print_test_lines()
        text = '\n'.join(line.text for line in lines)
        # Check function names appear in the bloat output
        self.assertIn('main', text)
        self.assertIn('board_init', text)
        self.assertIn('(no errors to report)', lines[-1].text)

        # Now run with -K to show config changes
        # First, create config files in the output directory to simulate
        # varying configs between commits. Use the builder to get correct paths.
        for commit_num in range(self._commits):
            for brd in BOARDS:
                target = brd[6]  # target name is 7th element
                board_dir = self._builder.get_build_dir(commit_num, target)
                cfg_fname = os.path.join(board_dir, 'u-boot.cfg')
                cfg_content = f'#define CONFIG_VALUE {commit_num + 100}\n'
                if commit_num == 0:
                    # Add a config that will be removed in later commits
                    cfg_content += '#define CONFIG_OLD_OPTION 1\n'
                if commit_num > 0:
                    cfg_content += '#define CONFIG_NEW_OPTION 1\n'
                tools.write_file(cfg_fname, cfg_content.encode('utf-8'))

        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-sK', '-o', self._output_dir,
                         clean_dir=False)
        self.assertEqual(self._make_calls, 0)
        lines = terminal.get_print_test_lines()
        text = '\n'.join(line.text for line in lines)
        # Check config options appear in the output - both additions and
        # value changes should be detected
        self.assertIn('CONFIG_NEW_OPTION', text)  # Addition
        self.assertIn('CONFIG_VALUE', text)  # Value change
        self.assertIn('(no errors to report)', lines[-1].text)

        # Now run with -U to show environment changes
        # Create uboot.env files with varying content between commits
        for commit_num in range(self._commits):
            for brd in BOARDS:
                target = brd[6]  # target name is 7th element
                board_dir = self._builder.get_build_dir(commit_num, target)
                env_fname = os.path.join(board_dir, 'uboot.env')
                # Environment uses null-terminated strings
                env_content = f'bootdelay={commit_num + 1}\x00'
                if commit_num == 0:
                    # Add a variable that will be removed in later commits
                    env_content += 'oldvar=removed\x00'
                if commit_num > 0:
                    env_content += 'newvar=value\x00'
                tools.write_file(env_fname, env_content.encode('utf-8'))

        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-sU', '-o', self._output_dir,
                         clean_dir=False)
        self.assertEqual(self._make_calls, 0)
        lines = terminal.get_print_test_lines()
        text = '\n'.join(line.text for line in lines)
        # Check environment variables appear in the output
        self.assertIn('bootdelay', text)
        self.assertIn('(no errors to report)', lines[-1].text)

    def test_warnings_as_errors(self):
        """Test the -E flag adds -Werror to make arguments"""
        self._captured_make_args = []
        self._run_control('-o', self._output_dir, '-E')

        # Check that at least one build had -Werror flags
        found_werror = False
        for args in self._captured_make_args:
            args_str = ' '.join(args)
            if 'KCFLAGS=-Werror' in args_str:
                found_werror = True
                self.assertIn('HOSTCFLAGS=-Werror', args_str)
                break
        self.assertTrue(found_werror, 'KCFLAGS=-Werror not found in make args')

    def test_count(self):
        """Test building a specific number of commitst"""
        self._run_control('-b', TEST_BRANCH, '-c2', '-o', self._output_dir)
        self.assertEqual(self._builder.count, 2 * len(BOARDS))
        self.assertEqual(self._builder.fail, 0)
        # Each board has a config, and then one make per commit
        self.assertEqual(self._make_calls, len(BOARDS) * (1 + 2))

    def test_incremental(self):
        """Test building a branch twice - the second time should do nothing"""
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)

        # Each board has a mrproper, config, and then one make per commit
        self.assertEqual(self._make_calls, len(BOARDS) * (self._commits + 1))
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir, clean_dir=False)
        self.assertEqual(self._make_calls, 0)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 0)

    def test_force_build(self):
        """The -f flag should force a rebuild"""
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-f', '-o', self._output_dir, clean_dir=False)
        # Each board has a config and one make per commit
        self.assertEqual(self._make_calls, len(BOARDS) * (self._commits + 1))

    def test_force_reconfigure(self):
        """The -f flag should force a rebuild"""
        self._run_control('-b', TEST_BRANCH, '-C', '-o', self._output_dir)
        # Each commit has a config and make
        self.assertEqual(self._make_calls, len(BOARDS) * self._commits * 2)

    def test_mrproper(self):
        """The -f flag should force a rebuild"""
        self._run_control('-b', TEST_BRANCH, '-m', '-o', self._output_dir)
        # Each board has a mkproper, config and then one make per commit
        self.assertEqual(self._make_calls, len(BOARDS) * (self._commits + 2))

    def test_errors(self):
        """Test handling of build errors"""
        self._error['board2', 1] = 'fred\n'
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 1)

        # Remove the error. This should have no effect since the commit will
        # not be rebuilt
        del self._error['board2', 1]
        self._make_calls = 0
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir, clean_dir=False)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._make_calls, 0)
        self.assertEqual(self._builder.fail, 1)

        # Now use the -F flag to force rebuild of the bad commit
        self._run_control('-b', TEST_BRANCH, '-o', self._output_dir, '-F', clean_dir=False)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 0)
        self.assertEqual(self._make_calls, 2)

    def test_branch_with_slash(self):
        """Test building a branch with a '/' in the name"""
        self._test_branch = '/__dev/__testbranch'
        self._run_control('-b', self._test_branch, '-o', self._output_dir,
                         clean_dir=False)
        self.assertEqual(self._builder.count, self._total_builds)
        self.assertEqual(self._builder.fail, 0)

    def test_environment(self):
        """Test that the done and environment files are written to out-env"""
        self._run_control('-o', self._output_dir)
        board0_dir = os.path.join(self._output_dir, 'current', 'board0')
        self.assertTrue(os.path.exists(os.path.join(board0_dir, 'done')))
        self.assertTrue(os.path.exists(os.path.join(board0_dir, 'out-env')))

    def test_environment_unicode(self):
        """Test there are no unicode errors when the env has non-ASCII chars"""
        try:
            varname = b'buildman_test_var'
            os.environb[varname] = b'strange\x80chars'
            self.assertEqual(0, self._run_control('-o', self._output_dir))
            board0_dir = os.path.join(self._output_dir, 'current', 'board0')
            self.assertTrue(os.path.exists(os.path.join(board0_dir, 'done')))
            self.assertTrue(os.path.exists(os.path.join(board0_dir, 'out-env')))
        finally:
            del os.environb[varname]

    def test_work_in_output(self):
        """Test the -w option which should write directly to the output dir"""
        board_list = boards.Boards()
        board_list.add_board(board.Board(*BOARDS[0]))
        self._run_control('-o', self._output_dir, '-w', clean_dir=False,
                         brds=board_list)
        self.assertTrue(
            os.path.exists(os.path.join(self._output_dir, 'u-boot')))
        self.assertTrue(
            os.path.exists(os.path.join(self._output_dir, 'done')))
        self.assertTrue(
            os.path.exists(os.path.join(self._output_dir, 'out-env')))

    def test_work_in_output_fail(self):
        """Test the -w option failures"""
        with self.assertRaises(SystemExit) as e:
            self._run_control('-o', self._output_dir, '-w', clean_dir=False)
        self.assertIn("single board", str(e.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self._output_dir, 'u-boot')))

        board_list = boards.Boards()
        board_list.add_board(board.Board(*BOARDS[0]))
        with self.assertRaises(SystemExit) as e:
            self._run_control('-b', self._test_branch, '-o', self._output_dir,
                             '-w', clean_dir=False, brds=board_list)
        self.assertIn("single commit", str(e.exception))

        board_list = boards.Boards()
        board_list.add_board(board.Board(*BOARDS[0]))
        with self.assertRaises(SystemExit) as e:
            self._run_control('-w', clean_dir=False)
        self.assertIn("specify -o", str(e.exception))

    def test_thread_exceptions(self):
        """Test that exceptions in threads are reported"""
        with terminal.capture() as (stdout, stderr):
            self.assertEqual(102, self._run_control('-o', self._output_dir,
                                                   test_thread_exceptions=True))
        self.assertIn(
            'Thread exception (use -T0 to run without threads): test exception',
            stdout.getvalue())

    def test_blobs(self):
        """Test handling of missing blobs"""
        self._missing = True

        board0_dir = os.path.join(self._output_dir, 'current', 'board0')
        errfile = os.path.join(board0_dir, 'err')
        logfile = os.path.join(board0_dir, 'log')

        # We expect failure when there are missing blobs
        result = self._run_control('board0', '-o', self._output_dir)
        self.assertEqual(100, result)
        self.assertTrue(os.path.exists(os.path.join(board0_dir, 'done')))
        self.assertTrue(os.path.exists(errfile))
        self.assertIn(b"Filename 'fsp.bin' not found in input path",
                      tools.read_file(errfile))

    def test_blobs_allow_missing(self):
        """Allow missing blobs - still failure but a different exit code"""
        self._missing = True
        result = self._run_control('board0', '-o', self._output_dir, '-M',
                                  clean_dir=True)
        self.assertEqual(101, result)
        board0_dir = os.path.join(self._output_dir, 'current', 'board0')
        errfile = os.path.join(board0_dir, 'err')
        self.assertTrue(os.path.exists(errfile))
        self.assertIn(b'Some images are invalid', tools.read_file(errfile))

    def test_blobs_warning(self):
        """Allow missing blobs and ignore warnings"""
        self._missing = True
        result = self._run_control('board0', '-o', self._output_dir, '-MW')
        self.assertEqual(0, result)
        board0_dir = os.path.join(self._output_dir, 'current', 'board0')
        errfile = os.path.join(board0_dir, 'err')
        self.assertIn(b'Some images are invalid', tools.read_file(errfile))

    def test_blob_settings(self):
        """Test with no settings"""
        self.assertEqual(False,
                         control.get_allow_missing(False, False, 1, False))
        self.assertEqual(True,
                         control.get_allow_missing(True, False, 1, False))
        self.assertEqual(False,
                         control.get_allow_missing(True, True, 1, False))

    def test_blob_settings_always(self):
        """Test the 'always' policy"""
        bsettings.set_item('global', 'allow-missing', 'always')
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 1, False))
        self.assertEqual(False,
                         control.get_allow_missing(False, True, 1, False))

    def test_blob_settings_branch(self):
        """Test the 'branch' policy"""
        bsettings.set_item('global', 'allow-missing', 'branch')
        self.assertEqual(False,
                         control.get_allow_missing(False, False, 1, False))
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 1, True))
        self.assertEqual(False,
                         control.get_allow_missing(False, True, 1, True))

    def test_blob_settings_multiple(self):
        """Test the 'multiple' policy"""
        bsettings.set_item('global', 'allow-missing', 'multiple')
        self.assertEqual(False,
                         control.get_allow_missing(False, False, 1, False))
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 2, False))
        self.assertEqual(False,
                         control.get_allow_missing(False, True, 2, False))

    def test_blob_settings_branch_multiple(self):
        """Test the 'branch multiple' policy"""
        bsettings.set_item('global', 'allow-missing', 'branch multiple')
        self.assertEqual(False,
                         control.get_allow_missing(False, False, 1, False))
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 1, True))
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 2, False))
        self.assertEqual(True,
                         control.get_allow_missing(False, False, 2, True))
        self.assertEqual(False,
                         control.get_allow_missing(False, True, 2, True))

    def check_command(self, *extra_args):
        """Run a command with the extra arguments and return the commands used

        Args:
            extra_args (list of str): List of extra arguments

        Returns:
            list of str: Lines returned in the out-cmd file
        """
        self._run_control('-o', self._output_dir, *extra_args)
        board0_dir = os.path.join(self._output_dir, 'current', 'board0')
        self.assertTrue(os.path.exists(os.path.join(board0_dir, 'done')))
        cmd_fname = os.path.join(board0_dir, 'out-cmd')
        self.assertTrue(os.path.exists(cmd_fname))
        data = tools.read_file(cmd_fname)

        config_fname = os.path.join(board0_dir, '.config')
        self.assertTrue(os.path.exists(config_fname))
        cfg_data = tools.read_file(config_fname)

        return data.splitlines(), cfg_data

    def test_cmd_file(self):
        """Test that the -cmd-out file is produced"""
        lines = self.check_command()[0]
        self.assertEqual(2, len(lines))
        self.assertRegex(lines[0], b'make O=/.*board0_defconfig')
        self.assertRegex(lines[0], b'make O=/.*-s.*')

    def test_no_lto(self):
        """Test that the --no-lto flag works"""
        lines = self.check_command('-L')[0]
        self.assertIn(b'NO_LTO=1', lines[0])

    def test_fragments(self):
        """Test passing of configuration fragments to the make command"""
        # Single fragment passed as argument
        extra_args = ['board0', '--fragments', 'f1.config']
        lines, cfg_data = self.check_command(*extra_args)
        self.assertRegex(lines[0].decode('utf-8'),
                         r'make O=/.*board0_defconfig\s+f1\.config',
                         'Test single fragment')
        # Multiple fragments passed as comma-separated list
        extra_args = ['board0', '--fragments', 'f1.config,f2.config']
        lines, cfg_data = self.check_command(*extra_args)
        self.assertRegex(lines[0].decode('utf-8'),
                         r'make O=/.*board0_defconfig\s+f1\.config\s+f2\.config',
                         'Test multiple fragments')

    def test_reproducible(self):
        """Test that the -r flag works"""
        lines, cfg_data = self.check_command('-r')
        self.assertIn(b'SOURCE_DATE_EPOCH=0', lines[0])

        # We should see CONFIG_LOCALVERSION_AUTO unset
        self.assertEqual(b'''CONFIG_SOMETHING=1
# CONFIG_LOCALVERSION_AUTO is not set
''', cfg_data)

        with terminal.capture() as (stdout, stderr):
            lines, cfg_data = self.check_command('-r', '-a', 'LOCALVERSION')
        self.assertIn(b'SOURCE_DATE_EPOCH=0', lines[0])

        # We should see CONFIG_LOCALVERSION_AUTO unset
        self.assertEqual(b'''CONFIG_SOMETHING=1
CONFIG_LOCALVERSION=y
''', cfg_data)
        self.assertIn('Not dropping LOCALVERSION_AUTO', stdout.getvalue())

    def test_scan_defconfigs(self):
        """Test scanning the defconfigs to obtain all the boards"""
        src = self._git_dir

        # Scan the test directory which contains a Kconfig and some *_defconfig
        # files
        params, warnings = self._boards.scan_defconfigs(src, src)

        # We should get two boards
        self.assertEqual(2, len(params))
        self.assertFalse(warnings)
        first = 0 if params[0]['target'] == 'board0' else 1
        board0 = params[first]
        board2 = params[1 - first]

        self.assertEqual('arm', board0['arch'])
        self.assertEqual('armv7', board0['cpu'])
        self.assertEqual('-', board0['soc'])
        self.assertEqual('Tester', board0['vendor'])
        self.assertEqual('ARM Board 0', board0['board'])
        self.assertEqual('config0', board0['config'])
        self.assertEqual('board0', board0['target'])

        self.assertEqual('powerpc', board2['arch'])
        self.assertEqual('ppc', board2['cpu'])
        self.assertEqual('mpc85xx', board2['soc'])
        self.assertEqual('Tester', board2['vendor'])
        self.assertEqual('PowerPC board 1', board2['board'])
        self.assertEqual('config2', board2['config'])
        self.assertEqual('board2', board2['target'])

    def test_output_is_new(self):
        """Test detecting new changes to Kconfig"""
        base = self._base_dir
        src = self._git_dir
        config_dir = os.path.join(src, 'configs')
        delay = 0.02

        # Create a boards.cfg file
        boards_cfg = os.path.join(base, 'boards.cfg')
        content = b'''#
# List of boards
#   Automatically generated by buildman/boards.py: don't edit
#
# Status, Arch, CPU, SoC, Vendor, Board, Target, Config, Maintainers

Active  aarch64     armv8 - armltd corstone1000 board0
Active  aarch64     armv8 - armltd total_compute board2
'''
        # Check missing file
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))

        # Check that the board.cfg file is newer
        time.sleep(delay)
        tools.write_file(boards_cfg, content)
        self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))

        # Touch the Kconfig files after a show delay to avoid a race
        time.sleep(delay)
        Path(os.path.join(src, 'Kconfig')).touch()
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))
        Path(boards_cfg).touch()
        self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))

        # Touch a different Kconfig file
        time.sleep(delay)
        Path(os.path.join(src, 'Kconfig.something')).touch()
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))
        Path(boards_cfg).touch()
        self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))

        # Touch a MAINTAINERS file
        time.sleep(delay)
        Path(os.path.join(src, 'MAINTAINERS')).touch()
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))

        Path(boards_cfg).touch()
        self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))

        # Touch a defconfig file
        time.sleep(delay)
        Path(os.path.join(config_dir, 'board0_defconfig')).touch()
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))
        Path(boards_cfg).touch()
        self.assertTrue(boards.output_is_new(boards_cfg, config_dir, src))

        # Remove a board and check that the board.cfg file is now older
        Path(os.path.join(config_dir, 'board0_defconfig')).unlink()
        self.assertFalse(boards.output_is_new(boards_cfg, config_dir, src))

    def test_maintainers(self):
        """Test detecting boards without a MAINTAINERS entry"""
        src = self._git_dir
        main = os.path.join(src, 'boards', 'board0', 'MAINTAINERS')
        other = os.path.join(src, 'boards', 'board2', 'MAINTAINERS')
        kc_file = os.path.join(src, 'Kconfig')
        config_dir = os.path.join(src, 'configs')
        params_list, warnings = self._boards.build_board_list(config_dir, src)

        # There should be two boards no warnings
        self.assertEqual(2, len(params_list))
        self.assertFalse(warnings)

        # Set an invalid status line in the file
        orig_data = tools.read_file(main, binary=False)
        lines = ['S:      Other\n' if line.startswith('S:') else line
                  for line in orig_data.splitlines(keepends=True)]
        tools.write_file(main, ''.join(lines), binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        params = params_list[0]
        if params['target'] == 'board2':
            params = params_list[1]
        self.assertEqual('-', params['status'])
        self.assertEqual(["WARNING: Other: unknown status for 'board0'"],
                          warnings)

        # Remove the status line (S:) from a file
        lines = [line for line in orig_data.splitlines(keepends=True)
                 if not line.startswith('S:')]
        tools.write_file(main, ''.join(lines), binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertEqual(["WARNING: -: unknown status for 'board0'"], warnings)

        # Remove the configs/ line (F:) from a file - this is the last line
        data = ''.join(orig_data.splitlines(keepends=True)[:-1])
        tools.write_file(main, data, binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertEqual(["WARNING: no maintainers for 'board0'"], warnings)

        # Mark a board as orphaned - this should give a warning
        lines = ['S: Orphaned' if line.startswith('S') else line
                 for line in orig_data.splitlines(keepends=True)]
        tools.write_file(main, ''.join(lines), binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertEqual(["WARNING: no maintainers for 'board0'"], warnings)

        # Change the maintainer to '-' - this should give a warning
        lines = ['M: -' if line.startswith('M') else line
                 for line in orig_data.splitlines(keepends=True)]
        tools.write_file(main, ''.join(lines), binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertEqual(["WARNING: -: unknown status for 'board0'"], warnings)

        # Remove the maintainer line (M:) from a file
        lines = [line for line in orig_data.splitlines(keepends=True)
                 if not line.startswith('M:')]
        tools.write_file(main, ''.join(lines), binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertEqual(["WARNING: no maintainers for 'board0'"], warnings)

        # Move the contents of the second file into this one, removing the
        # second file, to check multiple records in a single file.
        both_data = orig_data + tools.read_file(other, binary=False)
        tools.write_file(main, both_data, binary=False)
        os.remove(other)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertFalse(warnings)

        # Add another record, this should be ignored with a warning
        extra = '\n\nAnother\nM: Fred\nF: configs/board9_defconfig\nS: other\n'
        tools.write_file(main, both_data + extra, binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertFalse(warnings)

        # Add another TARGET to the Kconfig
        tools.write_file(main, both_data, binary=False)
        orig_kc_data = tools.read_file(kc_file)
        extra = (b'''
if TARGET_BOARD2
config TARGET_OTHER
\tbool "other"
\tdefault y
endif
''')
        tools.write_file(kc_file, orig_kc_data + extra)
        params_list, warnings = self._boards.build_board_list(config_dir, src,
                                                              warn_targets=True)
        self.assertEqual(2, len(params_list))
        self.assertEqual(
            ['WARNING: board2_defconfig: Duplicate TARGET_xxx: board2 and other'],
             warnings)

        # Remove the TARGET_BOARD0 Kconfig option
        lines = [b'' if line == b'config TARGET_BOARD2\n' else line
                  for line in orig_kc_data.splitlines(keepends=True)]
        tools.write_file(kc_file, b''.join(lines))
        params_list, warnings = self._boards.build_board_list(config_dir, src,
                                                              warn_targets=True)
        self.assertEqual(2, len(params_list))
        self.assertEqual(
            ['WARNING: board2_defconfig: No TARGET_BOARD2 enabled'],
             warnings)
        tools.write_file(kc_file, orig_kc_data)

        # Replace the last F: line of board 2 with an N: line
        data = ''.join(both_data.splitlines(keepends=True)[:-1])
        tools.write_file(main, data + 'N: oa.*2\n', binary=False)
        params_list, warnings = self._boards.build_board_list(config_dir, src)
        self.assertEqual(2, len(params_list))
        self.assertFalse(warnings)

    def test_regen_boards(self):
        """Test that we can regenerate the boards.cfg file"""
        outfile = os.path.join(self._output_dir, 'test-boards.cfg')
        if os.path.exists(outfile):
            os.remove(outfile)
        with terminal.capture() as (stdout, stderr):
            result = self._run_control('-R', outfile, brds=None,
                                      get_builder=False)
        self.assertTrue(os.path.exists(outfile))

    def test_print_prefix(self):
        """Test that we can print the toolchain prefix"""
        with terminal.capture() as (stdout, stderr):
            result = self._run_control('-A', 'board0')
        self.assertEqual('arm-\n', stdout.getvalue())
        self.assertEqual('', stderr.getvalue())

    def test_exclude_one(self):
        """Test excluding a single board from an arch"""
        self._run_control('arm', '-x', 'board1', '-o', self._output_dir)
        self.assertEqual(['board0'],
                         [b.target for b in self._boards.get_selected()])

    def test_exclude_arch(self):
        """Test excluding an arch"""
        self._run_control('-x', 'arm', '-o', self._output_dir)
        self.assertEqual(['board2', 'board4'],
                         [b.target for b in self._boards.get_selected()])

    def test_exclude_comma(self):
        """Test excluding a comma-separated list of things"""
        self._run_control('-x', 'arm,powerpc', '-o', self._output_dir)
        self.assertEqual(['board4'],
                         [b.target for b in self._boards.get_selected()])

    def test_exclude_list(self):
        """Test excluding a list of things"""
        self._run_control('-x', 'board2', '-x' 'board4', '-o', self._output_dir)
        self.assertEqual(['board0', 'board1'],
                         [b.target for b in self._boards.get_selected()])

    def test_single_boards(self):
        """Test building single boards"""
        self._run_control('--boards', 'board1', '-o', self._output_dir)
        self.assertEqual(1, self._builder.count)

        self._run_control('--boards', 'board1', '--boards', 'board2',
                         '-o', self._output_dir)
        self.assertEqual(2, self._builder.count)

        self._run_control('--boards', 'board1,board2', '--boards', 'board4',
                         '-o', self._output_dir)
        self.assertEqual(3, self._builder.count)

    def test_print_arch(self):
        """Test that we can print the board architecture"""
        with terminal.capture() as (stdout, stderr):
            result = self._run_control('--print-arch', 'board0')
        self.assertEqual('arm\n', stdout.getvalue())
        self.assertEqual('', stderr.getvalue())

    def test_kconfig_scanner(self):
        """Test using the kconfig scanner to determine important values

        Note that there is already a test_scan_defconfigs() which checks the
        higher-level scan_defconfigs() function. This test checks just the
        scanner itself
        """
        src = self._git_dir
        scanner = boards.KconfigScanner(src)

        # First do a simple sanity check
        norm = os.path.join(src, 'board0_defconfig')
        tools.write_file(norm, 'CONFIG_TARGET_BOARD0=y', False)
        res = scanner.scan(norm, True)
        self.assertEqual(({
            'arch': 'arm',
            'cpu': 'armv7',
            'soc': '-',
            'vendor': 'Tester',
            'board': 'ARM Board 0',
            'config': 'config0',
            'target': 'board0'}, []), res)

        # Check that the SoC cannot be changed and the filename does not affect
        # the resulting board
        tools.write_file(norm, '''CONFIG_TARGET_BOARD2=y
CONFIG_SOC="fred"
''', False)
        res = scanner.scan(norm, True)
        self.assertEqual(({
            'arch': 'powerpc',
            'cpu': 'ppc',
            'soc': 'mpc85xx',
            'vendor': 'Tester',
            'board': 'PowerPC board 1',
            'config': 'config2',
            'target': 'board0'}, []), res)

        # Check handling of missing information
        tools.write_file(norm, '', False)
        res = scanner.scan(norm, True)
        self.assertEqual(({
            'arch': '-',
            'cpu': '-',
            'soc': '-',
            'vendor': '-',
            'board': '-',
            'config': '-',
            'target': 'board0'},
            ['WARNING: board0_defconfig: No TARGET_BOARD0 enabled']), res)

        # check handling of #include files; see _handle_command_cpp()
        inc = os.path.join(src, 'common')
        tools.write_file(inc, b'CONFIG_TARGET_BOARD0=y\n')
        tools.write_file(norm, f'#include <{inc}>', False)
        res = scanner.scan(norm, True)
        self.assertEqual(({
            'arch': 'arm',
            'cpu': 'armv7',
            'soc': '-',
            'vendor': 'Tester',
            'board': 'ARM Board 0',
            'config': 'config0',
            'target': 'board0'}, []), res)

    def test_target(self):
        """Test that the --target flag works"""
        lines = self.check_command('--target', 'u-boot.dtb')[0]

        # It should not affect the defconfig line
        self.assertNotIn(b'u-boot.dtb', lines[0])

        # It should appear at the end of the build line
        self.assertEqual(b'u-boot.dtb', lines[1].split()[-1])

    def test_extended(self):
        """Test parsing of extended (.buildman) files"""
        data = '''
name: acpi_boards
desc: Build RISC-V QEMU builds with ACPI
fragment: acpi
targets:
  qemu_riscv*

name: am62x_dfu
desc: Build Android variant of 'k3' boards, with DFU
fragment: am62x_r5_usbdfu
fragment: am62x_a53_android
targets:
  CONFIG_SYS_SOC="k3"
'''
        fname = os.path.join(self._base_dir, 'try.buildman')
        tools.write_file(fname, data.encode('utf-8'))
        result = boards.ExtendedParser.parse_file(fname)
        self.maxDiff = None  # pylint: disable=C0103
        self.assertEqual([
            Extended(name='acpi_boards',
                     desc='Build RISC-V QEMU builds with ACPI',
                     fragments=['acpi'],
                     targets=[
                         ['regex', 'qemu_riscv*']]),
            Extended(name='am62x_dfu',
                     desc="Build Android variant of 'k3' boards, with DFU",
                     fragments=['am62x_r5_usbdfu', 'am62x_a53_android'],
                     targets=[
                         ['CONFIG_SYS_SOC', '"k3"']]
                     )], result)

    def test_extended_bad_indent(self):
        """Test unexpected indentation"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('mary', ' name: fred')
        self.assertEqual('mary:1: Unexpected indent',
                         str(exc.exception))

    def test_extended_invalid_config(self):
        """Test a bad CONFIG_xxx= line"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('anna', '''
name: joan
targets:
  CONFIG_SOMETHING=val="
''')
        self.assertEqual('anna:4: Invalid CONFIG syntax',
                         str(exc.exception))

    def test_extended_invalid_target(self):
        """Test a bad target regex"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('john', '''
name: fred
targets:
  qemu* *riscv
''')
        self.assertEqual('john:4: Invalid target regex',
                         str(exc.exception))

    def test_extended_invalid_tag(self):
        """Test a bad tag"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('hannah', '''
name: frank :was here
''')
        self.assertEqual('hannah:2: Invalid tag',
                         str(exc.exception))

    def test_extended_unknown_tag(self):
        """Test a unknown tag"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('susan', '''
name: allan
something: me
''')
        self.assertEqual("susan:3: Unknown tag 'something'",
                         str(exc.exception))

    def test_extended_bad_name(self):
        """Test a name with an invalid char"""
        with self.assertRaises(ValueError) as exc:
            boards.ExtendedParser.parse_data('bert', 'name: katie was here')
        self.assertEqual('bert:1: Invalid name',
                         str(exc.exception))

    def test_kconfig_change(self):
        """Test that Kconfig change detection triggers reconfig"""
        # Track calls to kconfig_changed_since
        call_count = [0]
        config_exists = [False]

        def mock_kconfig_changed(fname, srcdir='.', target=None):
            call_count[0] += 1
            # Return True only if .config exists (i.e. not the first build)
            # The first build has no .config to compare against
            exists = os.path.exists(fname)
            if exists:
                config_exists[0] = True
            return exists

        # Run buildman with kconfig checking enabled. Use -P to give each
        # board its own output directory, preventing .config from leaking
        # between boards when a thread processes multiple boards sequentially.
        with mock.patch.object(builderthread, 'kconfig_changed_since',
                               mock_kconfig_changed):
            self._run_control('-b', TEST_BRANCH, '-c2', '-o', self._output_dir,
                             '-P')

        # Verify kconfig_changed_since was called
        self.assertGreater(call_count[0], 0)

        # Each board should have one reconfig (for the second commit)
        # First commit has no .config, so no reconfig
        # Second commit has .config from first commit, so reconfig triggered
        self.assertEqual(len(BOARDS), self._builder.kconfig_reconfig)

    def test_kconfig_change_disabled(self):
        """Test that -Z flag disables Kconfig change detection"""
        call_count = [0]

        def mock_kconfig_changed(fname, srcdir='.', target=None):
            call_count[0] += 1
            return True  # Would trigger reconfig if checking was enabled

        # Run buildman with kconfig checking disabled (-Z)
        with mock.patch.object(builderthread, 'kconfig_changed_since',
                               mock_kconfig_changed):
            self._run_control('-b', TEST_BRANCH, '-c2', '-o', self._output_dir,
                             '-Z')

        # Verify kconfig_changed_since was NOT called (feature disabled)
        self.assertEqual(0, call_count[0])

        # No reconfigs should be triggered
        self.assertEqual(0, self._builder.kconfig_reconfig)

