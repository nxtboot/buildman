# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.
#
# Bloat-o-meter code used here Copyright 2004 Matt Mackall <mpm@selenic.com>
#

"""Build manager for U-Boot builds across multiple boards and commits"""

# pylint: disable=R0902,R0903

import collections
from datetime import datetime, timedelta
import glob
import os
import re
import queue
import shutil
import signal
import sys
import threading

from buildman import builderthread
from buildman.cfgutil import Config, process_config
from buildman.outcome import (BoardStatus, ErrLine, Outcome,
                              OUTCOME_OK, OUTCOME_WARNING, OUTCOME_ERROR,
                              OUTCOME_UNKNOWN)
from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib.terminal import tprint

# This indicates an new int or hex Kconfig property with no default
# It hangs the build since the 'conf' tool cannot proceed without valid input.
#
# We get a repeat sequence of something like this:
# >>
# Break things (BREAK_ME) [] (NEW)
# Error in reading or end of file.
# <<
# which indicates that BREAK_ME has an empty default
RE_NO_DEFAULT = re.compile(br'\((\w+)\) \[] \(NEW\)')

# Regex patterns for matching compiler output
RE_FUNCTION = re.compile('(.*): In function.*')
RE_FILES = re.compile('In file included from.*')
RE_WARNING = re.compile(r'(.*):(\d*):(\d*): warning: .*')
RE_DTB_WARNING = re.compile('(.*): Warning .*')
RE_NOTE = re.compile(
    r'(.*):(\d*):(\d*): note: this is the location of the previous.*')
RE_MIGRATION_WARNING = re.compile(r'^={21} WARNING ={22}\n.*\n=+\n',
                                  re.MULTILINE | re.DOTALL)
RE_MAKE_ERR = re.compile('(make.*Waiting for unfinished)|(Segmentation fault)')

# Symbol types which appear in the bloat feature (-B). Others are silently
# dropped when reading in the 'nm' output
NM_SYMBOL_TYPES = 'tTdDbBr'

"""
Theory of Operation

Please see README for user documentation, and you should be familiar with
that before trying to make sense of this.

Buildman works by keeping the machine as busy as possible, building different
commits for different boards on multiple CPUs at once.

The source repo (self.git_dir) contains all the commits to be built. Each
thread works on a single board at a time. It checks out the first commit,
configures it for that board, then builds it. Then it checks out the next
commit and builds it (typically without re-configuring). When it runs out
of commits, it gets another job from the builder and starts again with that
board.

Clearly the builder threads could work either way - they could check out a
commit and then built it for all boards. Using separate directories for each
commit/board pair they could leave their build product around afterwards
also.

The intent behind building a single board for multiple commits, is to make
use of incremental builds. Since each commit is built incrementally from
the previous one, builds are faster. Reconfiguring for a different board
removes all intermediate object files.

Many threads can be working at once, but each has its own working directory.
When a thread finishes a build, it puts the output files into a result
directory.

The base directory used by buildman is normally '../<branch>', i.e.
a directory higher than the source repository and named after the branch
being built.

Within the base directory, we have one subdirectory for each commit. Within
that is one subdirectory for each board. Within that is the build output for
that commit/board combination.

Buildman also create working directories for each thread, in a .bm-work/
subdirectory in the base dir.

As an example, say we are building branch 'us-net' for boards 'sandbox' and
'seaboard', and say that us-net has two commits. We will have directories
like this:

us-net/             base directory
    01_g4ed4ebc_net--Add-tftp-speed-/
        sandbox/
            u-boot.bin
        seaboard/
            u-boot.bin
    02_g4ed4ebc_net--Check-tftp-comp/
        sandbox/
            u-boot.bin
        seaboard/
            u-boot.bin
    .bm-work/
        00/         working directory for thread 0 (contains source checkout)
            build/  build output
        01/         working directory for thread 1
            build/  build output
        ...
u-boot/             source directory
    .git/           repository
"""

# Translate a commit subject into a valid filename (and handle unicode)
trans_valid_chars = str.maketrans('/: ', '---')

BASE_CONFIG_FILENAMES = [
    'u-boot.cfg', 'u-boot-spl.cfg', 'u-boot-tpl.cfg'
]

EXTRA_CONFIG_FILENAMES = [
    '.config', '.config-spl', '.config-tpl',
    'autoconf.mk', 'autoconf-spl.mk', 'autoconf-tpl.mk',
    'autoconf.h', 'autoconf-spl.h','autoconf-tpl.h',
]

class Environment:
    """Holds information about environment variables for a board."""
    def __init__(self, target):
        self.target = target
        self.environment = {}

    def add(self, key, value):
        """Add an environment variable

        Args:
            key (str): Environment variable name
            value (str): Environment variable value
        """
        self.environment[key] = value

class Builder:
    """Class for building U-Boot for a particular commit.

    Public members: (many should ->private)
        already_done: Number of builds already completed
        kconfig_reconfig: Number of builds triggered by Kconfig changes
        base_dir: Base directory to use for builder
        checkout: True to check out source, False to skip that step.
            This is used for testing.
        col: terminal.Color() object
        count: Total number of commits to build, which is the number of commits
            multiplied by the number of boards
        do_make: Method to call to invoke Make
        fail: Number of builds that failed due to error
        force_build: Force building even if a build already exists
        force_config_on_failure: If a commit fails for a board, disable
            incremental building for the next commit we build for that
            board, so that we will see all warnings/errors again.
        force_build_failures: If a previously-built build (i.e. built on
            a previous run of buildman) is marked as failed, rebuild it.
        git_dir: Git directory containing source repository
        num_jobs: Number of jobs to run at once (passed to make as -j)
        num_threads: Number of builder threads to run
        out_queue: Queue of results to process
        queue: Queue of jobs to run
        threads: List of active threads
        toolchains: Toolchains object to use for building
        upto: Current commit number we are building (0.count-1)
        warned: Number of builds that produced at least one warning
        force_reconfig: Reconfigure U-Boot on each comiit. This disables
            incremental building, where buildman reconfigures on the first
            commit for a baord, and then just does an incremental build for
            the following commits. In fact buildman will reconfigure and
            retry for any failing commits, so generally the only effect of
            this option is to slow things down.
        in_tree: Build U-Boot in-tree instead of specifying an output
            directory separate from the source code. This option is really
            only useful for testing in-tree builds.
        work_in_output: Use the output directory as the work directory and
            don't write to a separate output directory.
        thread_exceptions: List of exceptions raised by thread jobs
        no_lto (bool): True to set the NO_LTO flag when building
        reproducible_builds (bool): True to set SOURCE_DATE_EPOCH=0 for builds

    Private members:
        _base_board_dict: Last-summarised Dict of boards
        _base_err_lines: Last-summarised list of errors
        _base_warn_lines: Last-summarised list of warnings
        _build_period_us: Time taken for a single build (float object).
        _complete_delay: Expected delay until completion (timedelta)
        _next_delay_update: Next time we plan to display a progress update
                (datatime)
        _show_unknown: Show unknown boards (those not built) in summary
        _start_time: Start time for the build
        _timestamps: List of timestamps for the completion of the last
            last _timestamp_count builds. Each is a datetime object.
        _timestamp_count: Number of timestamps to keep in our list.
        _working_dir: Base working directory containing all threads
        _single_builder: BuilderThread object for the singer builder, if
            threading is not being used
        _terminated: Thread was terminated due to an error
        _restarting_config: True if 'Restart config' is detected in output
        _ide: Produce output suitable for an Integrated Development Environment
            i.e. don't emit progress information and put errors on stderr
    """

    def __init__(self, toolchains, base_dir, git_dir, num_threads, num_jobs,
                 gnu_make='make', checkout=True, show_unknown=True, step=1,
                 no_subdirs=False, full_path=False, verbose_build=False,
                 mrproper=False, fallback_mrproper=False,
                 per_board_out_dir=False, config_only=False,
                 squash_config_y=False, warnings_as_errors=False,
                 work_in_output=False, test_thread_exceptions=False,
                 adjust_cfg=None, allow_missing=False, no_lto=False,
                 reproducible_builds=False, force_build=False,
                 force_build_failures=False, kconfig_check=True,
                 force_reconfig=False,
                 in_tree=False, force_config_on_failure=False, make_func=None,
                 dtc_skip=False, build_target=None):
        """Create a new Builder object

        Args:
            toolchains: Toolchains object to use for building
            base_dir: Base directory to use for builder
            git_dir: Git directory containing source repository
            num_threads: Number of builder threads to run
            num_jobs: Number of jobs to run at once (passed to make as -j)
            gnu_make: the command name of GNU Make.
            checkout: True to check out source, False to skip that step.
                This is used for testing.
            show_unknown: Show unknown boards (those not built) in summary
            step: 1 to process every commit, n to process every nth commit
            no_subdirs: Don't create subdirectories when building current
                source for a single board
            full_path: Return the full path in CROSS_COMPILE and don't set
                PATH
            verbose_build: Run build with V=1 and don't use 'make -s'
            mrproper: Always run 'make mrproper' when configuring
            fallback_mrproper: Run 'make mrproper' and retry on build failure
            per_board_out_dir: Build in a separate persistent directory per
                board rather than a thread-specific directory
            config_only: Only configure each build, don't build it
            squash_config_y: Convert CONFIG options with the value 'y' to '1'
            warnings_as_errors: Treat all compiler warnings as errors
            work_in_output: Use the output directory as the work directory and
                don't write to a separate output directory.
            test_thread_exceptions: Uses for tests only, True to make the
                threads raise an exception instead of reporting their result.
                This simulates a failure in the code somewhere
            adjust_cfg_list (list of str): List of changes to make to .config
                file before building. Each is one of (where C is the config
                option with or without the CONFIG_ prefix)

                    C to enable C
                    ~C to disable C
                    C=val to set the value of C (val must have quotes if C is
                        a string Kconfig
            allow_missing: Run build with BINMAN_ALLOW_MISSING=1
            no_lto (bool): True to set the NO_LTO flag when building
            force_build (bool): Rebuild even commits that are already built
            force_build_failures (bool): Rebuild commits that have not been
                built, or failed to build
            kconfig_check (bool): Check if Kconfig files have changed and force
                a rebuild if so (default True)
            force_reconfig (bool): Reconfigure on each commit
            in_tree (bool): Bulid in tree instead of out-of-tree
            force_config_on_failure (bool): Reconfigure the build before
                retrying a failed build
            make_func (function): Function to call to run 'make'
            dtc_skip (bool): True to skip building dtc and use the system one
            build_target (str): Build target to use (None to use the default)
        """
        self.toolchains = toolchains
        self.base_dir = base_dir
        if work_in_output:
            self._working_dir = base_dir
        else:
            self._working_dir = os.path.join(base_dir, '.bm-work')
        self.threads = []
        self.do_make = make_func or self.make
        self.gnu_make = gnu_make
        self.checkout = checkout
        self.num_threads = num_threads
        self.num_jobs = num_jobs
        self.already_done = 0
        self.kconfig_reconfig = 0
        self.force_build = False
        self.git_dir = git_dir
        self._show_unknown = show_unknown
        self._timestamp_count = 10
        self._build_period_us = None
        self._complete_delay = None
        self._next_delay_update = datetime.now()
        self._start_time = None
        self._step = step
        self._error_lines = 0
        self.no_subdirs = no_subdirs
        self.full_path = full_path
        self.verbose_build = verbose_build
        self.config_only = config_only
        self.squash_config_y = squash_config_y
        self.config_filenames = BASE_CONFIG_FILENAMES
        self.work_in_output = work_in_output
        self.adjust_cfg = adjust_cfg
        self.allow_missing = allow_missing
        self._ide = False
        self.no_lto = no_lto
        self.reproducible_builds = reproducible_builds
        self.force_build = force_build
        self.force_build_failures = force_build_failures
        self.kconfig_check = kconfig_check
        self.force_reconfig = force_reconfig
        self.in_tree = in_tree
        self.force_config_on_failure = force_config_on_failure
        self.fallback_mrproper = fallback_mrproper
        if dtc_skip:
            self.dtc = shutil.which('dtc')
            if not self.dtc:
                raise ValueError('Cannot find dtc')
        else:
            self.dtc = None
        self.build_target = build_target

        if not self.squash_config_y:
            self.config_filenames += EXTRA_CONFIG_FILENAMES
        self._terminated = False
        self._restarting_config = False

        self.warnings_as_errors = warnings_as_errors
        self.col = terminal.Color()

        self.thread_exceptions = []
        self.test_thread_exceptions = test_thread_exceptions

        # Attributes used by set_display_options()
        self._show_errors = False
        self._show_sizes = False
        self._show_detail = False
        self._show_bloat = False
        self._list_error_boards = False
        self._show_config = False
        self._show_environment = False
        self._filter_dtb_warnings = False
        self._filter_migration_warnings = False

        # Attributes set by other methods
        self._build_period = None
        self.commit = None
        self.upto = 0
        self.warned = 0
        self.fail = 0
        self.commit_count = 0
        self.commits = None
        self.count = 0
        self._timestamps = None
        self._verbose = False

        # Attributes for result summaries
        self._base_board_dict = {}
        self._base_err_lines = []
        self._base_warn_lines = []
        self._base_err_line_boards = {}
        self._base_warn_line_boards = {}
        self._base_config = None
        self._base_environment = None

        self._setup_threads(mrproper, per_board_out_dir, test_thread_exceptions)

        ignore_lines = ['(make.*Waiting for unfinished)',
                        '(Segmentation fault)']
        self.re_make_err = re.compile('|'.join(ignore_lines))

        # Handle existing graceful with SIGINT / Ctrl-C
        signal.signal(signal.SIGINT, self.signal_handler)

    def _setup_threads(self, mrproper, per_board_out_dir,
                       test_thread_exceptions):
        """Set up builder threads

        Args:
            mrproper (bool): True to run 'make mrproper' before building
            per_board_out_dir (bool): True to use a separate output directory
                per board
            test_thread_exceptions (bool): True to make threads raise an
                exception instead of reporting their result (for tests)
        """
        if self.num_threads:
            self._single_builder = None
            self.queue = queue.Queue()
            self.out_queue = queue.Queue()
            for i in range(self.num_threads):
                t = builderthread.BuilderThread(
                        self, i, mrproper, per_board_out_dir,
                        test_exception=test_thread_exceptions)
                t.daemon = True
                t.start()
                self.threads.append(t)

            t = builderthread.ResultThread(self)
            t.daemon = True
            t.start()
            self.threads.append(t)
        else:
            self._single_builder = builderthread.BuilderThread(
                self, -1, mrproper, per_board_out_dir)

    def __del__(self):
        """Get rid of all threads created by the builder"""
        self.threads.clear()

    def signal_handler(self, _signum, _frame):
        """Handle a signal by exiting"""
        sys.exit(1)

    def make_environment(self, toolchain):
        """Create the environment to use for building

        Args:
            toolchain (Toolchain): Toolchain to use for building

        Returns:
            dict:
                key (str): Variable name
                value (str): Variable value
        """
        env = toolchain.make_environment(self.full_path)
        if self.dtc:
            env[b'DTC'] = tools.to_bytes(self.dtc)
        return env

    def set_display_options(self, show_errors=False, show_sizes=False,
                          show_detail=False, show_bloat=False,
                          list_error_boards=False, show_config=False,
                          show_environment=False, filter_dtb_warnings=False,
                          filter_migration_warnings=False, ide=False):
        """Setup display options for the builder.

        Args:
            show_errors (bool): True to show summarised error/warning info
            show_sizes (bool): Show size deltas
            show_detail (bool): Show size delta detail for each board if
                show_sizes
            show_bloat (bool): Show detail for each function
            list_error_boards (bool): Show the boards which caused each
                error/warning
            show_config (bool): Show config deltas
            show_environment (bool): Show environment deltas
            filter_dtb_warnings (bool): Filter out any warnings from the
                device-tree compiler
            filter_migration_warnings (bool): Filter out any warnings about
                migrating a board to driver model
            ide (bool): Create output that can be parsed by an IDE. There is
                no '+' prefix on error lines and output on stderr stays on
                stderr.
        """
        self._show_errors = show_errors
        self._show_sizes = show_sizes
        self._show_detail = show_detail
        self._show_bloat = show_bloat
        self._list_error_boards = list_error_boards
        self._show_config = show_config
        self._show_environment = show_environment
        self._filter_dtb_warnings = filter_dtb_warnings
        self._filter_migration_warnings = filter_migration_warnings
        self._ide = ide

    def _add_timestamp(self):
        """Add a new timestamp to the list and record the build period.

        The build period is the length of time taken to perform a single
        build (one board, one commit).
        """
        now = datetime.now()
        self._timestamps.append(now)
        count = len(self._timestamps)
        delta = self._timestamps[-1] - self._timestamps[0]
        seconds = delta.total_seconds()

        # If we have enough data, estimate build period (time taken for a
        # single build) and therefore completion time.
        if count > 1 and self._next_delay_update < now:
            self._next_delay_update = now + timedelta(seconds=2)
            if seconds > 0:
                self._build_period = float(seconds) / count
                todo = self.count - self.upto
                self._complete_delay = timedelta(microseconds=
                        self._build_period * todo * 1000000)
                # Round it
                self._complete_delay -= timedelta(
                        microseconds=self._complete_delay.microseconds)

        if seconds > 60:
            self._timestamps.popleft()
            count -= 1

    def select_commit(self, commit, checkout=True):
        """Checkout the selected commit for this build

        Args:
            commit (Commit): Commit object that is being built
            checkout (bool): True to checkout the commit
        """
        self.commit = commit
        if checkout and self.checkout:
            gitutil.checkout(commit.hash)

    def _check_output_for_loop(self, data):
        """Check output for config restart loops

        This detects when Kconfig enters a restart loop due to missing
        defaults. It looks for 'Restart config' followed by multiple
        occurrences of the same Kconfig item with no default.

        Args:
            data (bytes): Output data to check

        Returns:
            bool: True to terminate the command, False to continue
        """
        if b'Restart config' in data:
            self._restarting_config = True

        # If we see 'Restart config' followed by multiple errors
        if self._restarting_config:
            matches = RE_NO_DEFAULT.findall(data)

            # Number of occurrences of each Kconfig item
            multiple = [matches.count(val) for val in set(matches)]

            # If any of them occur more than once, we have a loop
            if [val for val in multiple if val > 1]:
                self._terminated = True
                return True
        return False

    def make(self, _commit, _brd, _stage, cwd, *args, **kwargs):
        """Run make

        Args:
            cwd (str): Directory where make should be run
            args: Arguments to pass to make
            kwargs: Arguments to pass to command.run_one()

        Returns:
            CommandResult: Result of the make operation
        """
        self._restarting_config = False
        self._terminated = False
        cmd = [self.gnu_make] + list(args)
        result = command.run_one(
            *cmd, capture=True, capture_stderr=True, cwd=cwd,
            raise_on_error=False, infile='/dev/null',
            output_func=lambda stream, data: self._check_output_for_loop(data),
            **kwargs)

        if self._terminated:
            # Try to be helpful
            result.stderr += \
                '(** did you define an int/hex Kconfig with no default? **)'

        if self.verbose_build:
            result.stdout = f"{' '.join(cmd)}\n" + result.stdout
            result.combined = f"{' '.join(cmd)}\n" + result.combined
        return result

    def process_result(self, result):
        """Process the result of a build, showing progress information

        Args:
            result (CommandResult): Result object, which indicates the result
                for a single build
        """
        if result:
            target = result.brd.target

            self.upto += 1
            if result.return_code != 0:
                self.fail += 1
            elif result.stderr:
                self.warned += 1
            if result.already_done:
                self.already_done += 1
            if result.kconfig_reconfig:
                self.kconfig_reconfig += 1
            if self._ide:
                if result.stderr:
                    sys.stderr.write(result.stderr)
            elif self._verbose:
                terminal.print_clear()
                boards_selected = {target : result.brd}
                self.reset_result_summary(boards_selected)
                self.produce_result_summary(result.commit_upto, self.commits,
                                          boards_selected)
        else:
            target = '(starting)'

        # Display separate counts for ok, warned and fail
        ok = self.upto - self.warned - self.fail
        line = '\r' + self.col.build(self.col.GREEN, f'{ok:5d}')
        line += self.col.build(self.col.YELLOW, f'{self.warned:5d}')
        line += self.col.build(self.col.RED, f'{self.fail:5d}')

        line += f' /{self.count:<5d}  '
        remaining = self.count - self.upto
        if remaining:
            line += self.col.build(self.col.MAGENTA, f' -{remaining:<5d}  ')
        else:
            line += ' ' * 8

        # Add our current completion time estimate
        self._add_timestamp()
        if self._complete_delay:
            line += f'{self._complete_delay}  : '

        line += target
        if not self._ide:
            terminal.print_clear()
            tprint(line, newline=False, limit_to_line=True)

    def get_output_dir(self, commit_upto):
        """Get the name of the output directory for a commit number

        The output directory is typically .../<branch>/<commit>.

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)

        Returns:
            str: Path to the output directory
        """
        if self.work_in_output:
            return self._working_dir

        commit_dir = None
        if self.commits:
            commit = self.commits[commit_upto]
            subject = commit.subject.translate(trans_valid_chars)
            # See _get_output_space_removals() which parses this name
            commit_dir = f'{commit_upto + 1:02d}_g{commit.hash}_{subject[:20]}'
        elif not self.no_subdirs:
            commit_dir = 'current'
        if not commit_dir:
            return self.base_dir
        return os.path.join(self.base_dir, commit_dir)

    def get_build_dir(self, commit_upto, target):
        """Get the name of the build directory for a commit number

        The build directory is typically .../<branch>/<commit>/<target>.

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name

        Return:
            str: Output directory to use, or '' if None
        """
        output_dir = self.get_output_dir(commit_upto)
        if self.work_in_output:
            return output_dir or ''
        return os.path.join(output_dir, target)

    def get_done_file(self, commit_upto, target):
        """Get the name of the done file for a commit number

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name

        Returns:
            str: Path to the done file
        """
        return os.path.join(self.get_build_dir(commit_upto, target), 'done')

    def get_sizes_file(self, commit_upto, target):
        """Get the name of the sizes file for a commit number

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name

        Returns:
            str: Path to the sizes file
        """
        return os.path.join(self.get_build_dir(commit_upto, target), 'sizes')

    def get_func_sizes_file(self, commit_upto, target, elf_fname):
        """Get the name of the funcsizes file for a commit number and ELF file

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name
            elf_fname (str): Filename of elf image

        Returns:
            str: Path to the funcsizes file
        """
        return os.path.join(self.get_build_dir(commit_upto, target),
                            f"{elf_fname.replace('/', '-')}.sizes")

    def get_objdump_file(self, commit_upto, target, elf_fname):
        """Get the name of the objdump file for a commit number and ELF file

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name
            elf_fname (str): Filename of elf image

        Returns:
            str: Path to the objdump file
        """
        return os.path.join(self.get_build_dir(commit_upto, target),
                            f"{elf_fname.replace('/', '-')}.objdump")

    def get_err_file(self, commit_upto, target):
        """Get the name of the err file for a commit number

        Args:
            commit_upto (int): Commit number to use (0..self.count-1)
            target (str): Target name

        Returns:
            str: Path to the err file
        """
        output_dir = self.get_build_dir(commit_upto, target)
        return os.path.join(output_dir, 'err')

    def filter_errors(self, lines):
        """Filter out errors in which we have no interest

        We should probably use map().

        Args:
            lines (list of str): List of error lines, each a string

        Returns:
            list of str: New list with only interesting lines included
        """
        out_lines = []
        if self._filter_migration_warnings:
            text = '\n'.join(lines)
            text = RE_MIGRATION_WARNING.sub('', text)
            lines = text.splitlines()
        for line in lines:
            if RE_MAKE_ERR.search(line):
                continue
            if self._filter_dtb_warnings and RE_DTB_WARNING.search(line):
                continue
            out_lines.append(line)
        return out_lines

    def read_func_sizes(self, _fname, fd):
        """Read function sizes from the output of 'nm'

        Args:
            fd (file): File containing data to read

        Returns:
            dict: Dictionary containing size of each function in bytes, indexed
                by function name.
        """
        sym = {}
        for line in fd.readlines():
            line = line.strip()
            parts = line.split()
            if line and len(parts) == 3:
                size, sym_type, name = line.split()
                if sym_type in NM_SYMBOL_TYPES:
                    # function names begin with '.' on 64-bit powerpc
                    if '.' in name[1:]:
                        name = 'static.' + name.split('.')[0]
                    sym[name] = sym.get(name, 0) + int(size, 16)
        return sym

    def _process_environment(self, fname):
        """Read in a uboot.env file

        This function reads in environment variables from a file.

        Args:
            fname: Filename to read

        Returns:
            Dictionary:
                key: environment variable (e.g. bootlimit)
                value: value of environment variable (e.g. 1)
        """
        environment = {}
        if os.path.exists(fname):
            with open(fname, encoding='utf-8') as fd:
                for line in fd.read().split('\0'):
                    try:
                        key, value = line.split('=', 1)
                        environment[key] = value
                    except ValueError:
                        # ignore lines we can't parse
                        pass
        return environment

    def _read_done_file(self, commit_upto, target, done_file, sizes_file):
        """Read the done file and collect build results

        Args:
            commit_upto (int): Commit number to check (0..n-1)
            target (str): Target board to check
            done_file (str): Filename of done file
            sizes_file (str): Filename of sizes file

        Returns:
            tuple: (rc, err_lines, sizes) where:
                rc: OUTCOME_OK, OUTCOME_WARNING or OUTCOME_ERROR
                err_lines: list of error lines
                sizes: dict of sizes
        """
        with open(done_file, 'r', encoding='utf-8') as fd:
            try:
                return_code = int(fd.readline())
            except ValueError:
                # The file may be empty due to running out of disk space.
                # Try a rebuild
                return_code = 1
            err_lines = []
            err_file = self.get_err_file(commit_upto, target)
            if os.path.exists(err_file):
                with open(err_file, 'r', encoding='utf-8') as fd:
                    err_lines = self.filter_errors(fd.readlines())

            # Decide whether the build was ok, failed or created warnings
            if return_code:
                rc = OUTCOME_ERROR
            elif err_lines:
                rc = OUTCOME_WARNING
            else:
                rc = OUTCOME_OK

            # Convert size information to our simple format
            sizes = {}
            if os.path.exists(sizes_file):
                with open(sizes_file, 'r', encoding='utf-8') as fd:
                    for line in fd.readlines():
                        values = line.split()
                        rodata = 0
                        if len(values) > 6:
                            rodata = int(values[6], 16)
                        size_dict = {
                            'all' : int(values[0]) + int(values[1]) +
                                    int(values[2]),
                            'text' : int(values[0]) - rodata,
                            'data' : int(values[1]),
                            'bss' : int(values[2]),
                            'rodata' : rodata,
                        }
                        sizes[values[5]] = size_dict
        return rc, err_lines, sizes

    def get_build_outcome(self, commit_upto, target, read_func_sizes,
                        read_config, read_environment):
        """Work out the outcome of a build.

        Args:
            commit_upto (int): Commit number to check (0..n-1)
            target (str): Target board to check
            read_func_sizes (bool): True to read function size information
            read_config (bool): True to read .config and autoconf.h files
            read_environment (bool): True to read uboot.env files

        Returns:
            Outcome: Outcome object
        """
        done_file = self.get_done_file(commit_upto, target)
        sizes_file = self.get_sizes_file(commit_upto, target)
        func_sizes = {}
        config = {}
        environment = {}
        if os.path.exists(done_file):
            rc, err_lines, sizes = self._read_done_file(
                commit_upto, target, done_file, sizes_file)

            if read_func_sizes:
                pattern = self.get_func_sizes_file(commit_upto, target, '*')
                for fname in glob.glob(pattern):
                    with open(fname, 'r', encoding='utf-8') as fd:
                        dict_name = os.path.basename(fname).replace('.sizes',
                                                                    '')
                        func_sizes[dict_name] = self.read_func_sizes(fname, fd)

            if read_config:
                output_dir = self.get_build_dir(commit_upto, target)
                for name in self.config_filenames:
                    fname = os.path.join(output_dir, name)
                    config[name] = process_config(fname, self.squash_config_y)

            if read_environment:
                output_dir = self.get_build_dir(commit_upto, target)
                fname = os.path.join(output_dir, 'uboot.env')
                environment = self._process_environment(fname)

            return Outcome(rc, err_lines, sizes, func_sizes, config,
                                   environment)

        return Outcome(OUTCOME_UNKNOWN, [], {}, {}, {}, {})

    @staticmethod
    def _add_line(lines_summary, lines_boards, line, brd):
        """Add a line to the summary and boards list

        Args:
            lines_summary (list): List of line strings
            lines_boards (dict): Dict of line strings to list of boards
            line (str): Line to add
            brd (Board): Board that produced this line
        """
        line = line.rstrip()
        if line in lines_boards:
            lines_boards[line].append(brd)
        else:
            lines_boards[line] = [brd]
            lines_summary.append(line)

    def _categorise_err_lines(self, err_lines, brd, err_lines_summary,
                              err_lines_boards, warn_lines_summary,
                              warn_lines_boards):
        """Categorise error lines into errors and warnings

        Args:
            err_lines (list): List of error-line strings
            brd (Board): Board that produced these lines
            err_lines_summary (list): List of error-line strings
            err_lines_boards (dict): Dict of error-line strings to boards
            warn_lines_summary (list): List of warning-line strings
            warn_lines_boards (dict): Dict of warning-line strings to boards
        """
        last_func = None
        last_was_warning = False
        for line in err_lines:
            if line:
                if (RE_FUNCTION.match(line) or
                        RE_FILES.match(line)):
                    last_func = line
                else:
                    is_warning = (RE_WARNING.match(line) or
                                  RE_DTB_WARNING.match(line))
                    is_note = RE_NOTE.match(line)
                    if is_warning or (last_was_warning and is_note):
                        if last_func:
                            self._add_line(warn_lines_summary,
                                           warn_lines_boards, last_func, brd)
                        self._add_line(warn_lines_summary, warn_lines_boards,
                                       line, brd)
                    else:
                        if last_func:
                            self._add_line(err_lines_summary, err_lines_boards,
                                           last_func, brd)
                        self._add_line(err_lines_summary, err_lines_boards,
                                       line, brd)
                    last_was_warning = is_warning
                    last_func = None

    def get_result_summary(self, boards_selected, commit_upto, read_func_sizes,
                         read_config, read_environment):
        """Calculate a summary of the results of building a commit.

        Args:
            boards_selected (dict): Dict containing boards to summarise
            commit_upto (int): Commit number to summarize (0..self.count-1)
            read_func_sizes (bool): True to read function size information
            read_config (bool): True to read .config and autoconf.h files
            read_environment (bool): True to read uboot.env files

        Returns:
            tuple: Tuple containing:
                Dict containing boards which built this commit:
                    key: board.target
                    value: Outcome object
                List containing a summary of error lines
                Dict keyed by error line, containing a list of the Board
                    objects with that error
                List containing a summary of warning lines
                Dict keyed by error line, containing a list of the Board
                    objects with that warning
                Dictionary keyed by board.target. Each value is a dictionary:
                    key: filename - e.g. '.config'
                    value is itself a dictionary:
                        key: config name
                        value: config value
                Dictionary keyed by board.target. Each value is a dictionary:
                    key: environment variable
                    value: value of environment variable
        """
        board_dict = {}
        err_lines_summary = []
        err_lines_boards = {}
        warn_lines_summary = []
        warn_lines_boards = {}
        config = {}
        environment = {}

        for brd in boards_selected.values():
            outcome = self.get_build_outcome(commit_upto, brd.target,
                                           read_func_sizes, read_config,
                                           read_environment)
            board_dict[brd.target] = outcome
            self._categorise_err_lines(outcome.err_lines, brd,
                                       err_lines_summary, err_lines_boards,
                                       warn_lines_summary, warn_lines_boards)
            tconfig = Config(self.config_filenames, brd.target)
            for fname in self.config_filenames:
                if outcome.config:
                    for key, value in outcome.config[fname].items():
                        tconfig.add(fname, key, value)
            config[brd.target] = tconfig

            tenvironment = Environment(brd.target)
            if outcome.environment:
                for key, value in outcome.environment.items():
                    tenvironment.add(key, value)
            environment[brd.target] = tenvironment

        return (board_dict, err_lines_summary, err_lines_boards,
                warn_lines_summary, warn_lines_boards, config, environment)

    def add_outcome(self, board_dict, arch_list, changes, char, color):
        """Add an output to our list of outcomes for each architecture

        This simple function adds failing boards (changes) to the
        relevant architecture string, so we can print the results out
        sorted by architecture.

        Args:
             board_dict (dict): Dict containing all boards
             arch_list (dict): Dict keyed by arch name. Value is a string
                 containing a list of board names which failed for that arch.
             changes (list): List of boards to add to arch_list
             char (str): Character to display for this board
             color (int): terminal.Colour object
        """
        done_arch = {}
        for target in changes:
            if target in board_dict:
                arch = board_dict[target].arch
            else:
                arch = 'unknown'
            text = self.col.build(color, ' ' + target)
            if arch not in done_arch:
                text = f' {self.col.build(color, char)}  {text}'
                done_arch[arch] = True
            if arch not in arch_list:
                arch_list[arch] = text
            else:
                arch_list[arch] += text


    def colour_num(self, num):
        """Format a number with colour depending on its value

        Args:
            num (int): Number to format

        Returns:
            str: Formatted string (red if positive, green if negative/zero)
        """
        color = self.col.RED if num > 0 else self.col.GREEN
        if num == 0:
            return '0'
        return self.col.build(color, str(num))

    def reset_result_summary(self, board_selected):
        """Reset the results summary ready for use.

        Set up the base board list to be all those selected, and set the
        error lines to empty.

        Following this, calls to print_result_summary() will use this
        information to work out what has changed.

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
        """
        self._base_board_dict = {}
        for brd in board_selected:
            self._base_board_dict[brd] = Outcome(0, [], [], {}, {}, {})
        self._base_err_lines = []
        self._base_warn_lines = []
        self._base_err_line_boards = {}
        self._base_warn_line_boards = {}
        self._base_config = None
        self._base_environment = None

    def print_func_size_detail(self, fname, old, new):
        """Print detailed size information for each function

        Args:
            fname (str): Filename to print (e.g. 'u-boot')
            old (dict): Dictionary of old function sizes, keyed by function name
            new (dict): Dictionary of new function sizes, keyed by function name
        """
        grow, shrink, add, remove, up, down = 0, 0, 0, 0, 0, 0
        delta, common = [], {}

        for a in old:
            if a in new:
                common[a] = 1

        for name in old:
            if name not in common:
                remove += 1
                down += old[name]
                delta.append([-old[name], name])

        for name in new:
            if name not in common:
                add += 1
                up += new[name]
                delta.append([new[name], name])

        for name in common:
            diff = new.get(name, 0) - old.get(name, 0)
            if diff > 0:
                grow, up = grow + 1, up + diff
            elif diff < 0:
                shrink, down = shrink + 1, down - diff
            delta.append([diff, name])

        delta.sort()
        delta.reverse()

        args = [add, -remove, grow, -shrink, up, -down, up - down]
        if max(args) == 0 and min(args) == 0:
            return
        args = [self.colour_num(x) for x in args]
        indent = ' ' * 15
        tprint(f'{indent}{self.col.build(self.col.YELLOW, fname)}: add: '
               f'{args[0]}/{args[1]}, grow: {args[2]}/{args[3]} bytes: '
               f'{args[4]}/{args[5]} ({args[6]})')
        tprint(f'{indent}  {"function":<38s} {"old":>7s} {"new":>7s} '
               f'{"delta":>7s}')
        for diff, name in delta:
            if diff:
                color = self.col.RED if diff > 0 else self.col.GREEN
                msg = (f'{indent}  {name:<38s} {old.get(name, "-"):>7} '
                       f'{new.get(name, "-"):>7} {diff:+7d}')
                tprint(msg, colour=color)


    def print_size_detail(self, target_list, show_bloat):
        """Show details size information for each board

        Args:
            target_list (list): List of targets, each a dict containing:
                    'target': Target name
                    'total_diff': Total difference in bytes across all areas
                    <part_name>: Difference for that part
            show_bloat (bool): Show detail for each function
        """
        targets_by_diff = sorted(target_list, reverse=True,
        key=lambda x: x['_total_diff'])
        for result in targets_by_diff:
            printed_target = False
            for name in sorted(result):
                diff = result[name]
                if name.startswith('_'):
                    continue
                colour = self.col.RED if diff > 0 else self.col.GREEN
                msg = f' {name} {diff:+d}'
                if not printed_target:
                    tprint(f'{"":10s}  {result["_target"]:<15s}:',
                          newline=False)
                    printed_target = True
                tprint(msg, colour=colour, newline=False)
            if printed_target:
                tprint()
                if show_bloat:
                    target = result['_target']
                    outcome = result['_outcome']
                    base_outcome = self._base_board_dict[target]
                    for fname in outcome.func_sizes:
                        self.print_func_size_detail(fname,
                                                 base_outcome.func_sizes[fname],
                                                 outcome.func_sizes[fname])


    @staticmethod
    def _calc_image_size_changes(target, sizes, base_sizes):
        """Calculate size changes for each image/part

        Args:
            target (str): Target board name
            sizes (dict): Dict of image sizes, keyed by image name
            base_sizes (dict): Dict of base image sizes, keyed by image name

        Returns:
            dict: Size changes, e.g.:
                {'_target': 'snapper9g45', 'data': 5, 'u-boot-spl:text': -4}
                meaning U-Boot data increased by 5 bytes, SPL text decreased
                by 4
        """
        err = {'_target' : target}
        for image in sizes:
            if image in base_sizes:
                base_image = base_sizes[image]
                # Loop through the text, data, bss parts
                for part in sorted(sizes[image]):
                    diff = sizes[image][part] - base_image[part]
                    if diff:
                        if image == 'u-boot':
                            name = part
                        else:
                            name = image + ':' + part
                        err[name] = diff
        return err

    def _calc_size_changes(self, board_selected, board_dict):
        """Calculate changes in size for different image parts

        The previous sizes are in Board.sizes, for each board

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.

        Returns:
            tuple: (arch_list, arch_count) where:
                arch_list: dict keyed by arch name, containing a list of
                    size-change dicts
                arch_count: dict keyed by arch name, containing the number of
                    boards for that arch
        """
        arch_list = {}
        arch_count = {}
        for target in board_dict:
            if target not in board_selected:
                continue
            base_sizes = self._base_board_dict[target].sizes
            outcome = board_dict[target]
            sizes = outcome.sizes
            err = self._calc_image_size_changes(target, sizes, base_sizes)
            arch = board_selected[target].arch
            if not arch in arch_count:
                arch_count[arch] = 1
            else:
                arch_count[arch] += 1
            if not sizes:
                pass    # Only add to our list when we have some stats
            elif not arch in arch_list:
                arch_list[arch] = [err]
            else:
                arch_list[arch].append(err)
        return arch_list, arch_count

    def print_size_summary(self, board_selected, board_dict, show_detail,
                         show_bloat):
        """Print a summary of image sizes broken down by section.

        The summary takes the form of one line per architecture. The
        line contains deltas for each of the sections (+ means the section
        got bigger, - means smaller). The numbers are the average number
        of bytes that a board in this section increased by.

        For example:
           powerpc: (622 boards)   text -0.0
          arm: (285 boards)   text -0.0

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            show_detail (bool): Show size delta detail for each board
            show_bloat (bool): Show detail for each function
        """
        arch_list, arch_count = self._calc_size_changes(board_selected,
                                                        board_dict)

        # We now have a list of image size changes sorted by arch
        # Print out a summary of these
        for arch, target_list in arch_list.items():
            # Get total difference for each type
            totals = {}
            for result in target_list:
                total = 0
                for name, diff in result.items():
                    if name.startswith('_'):
                        continue
                    total += diff
                    if name in totals:
                        totals[name] += diff
                    else:
                        totals[name] = diff
                result['_total_diff'] = total
                result['_outcome'] = board_dict[result['_target']]

            self._print_arch_size_summary(arch, target_list, arch_count,
                                          totals, show_detail, show_bloat)

    def _print_arch_size_summary(self, arch, target_list, arch_count, totals,
                                 show_detail, show_bloat):
        """Print size summary for a single architecture

        Args:
            arch (str): Architecture name
            target_list (list): List of size-change dicts for this arch
            arch_count (dict): Dict of arch name to board count
            totals (dict): Dict of name to total size diff
            show_detail (bool): Show size delta detail for each board
            show_bloat (bool): Show detail for each function
        """
        count = len(target_list)
        printed_arch = False
        for name in sorted(totals):
            diff = totals[name]
            if diff:
                # Display the average difference in this name for this
                # architecture
                avg_diff = float(diff) / count
                color = self.col.RED if avg_diff > 0 else self.col.GREEN
                msg = f' {name} {avg_diff:+1.1f}'
                if not printed_arch:
                    tprint(f'{arch:>10s}: (for {count}/{arch_count[arch]} '
                           'boards)', newline=False)
                    printed_arch = True
                tprint(msg, colour=color, newline=False)

        if printed_arch:
            tprint()
            if show_detail:
                self.print_size_detail(target_list, show_bloat)

    def _classify_boards(self, board_selected, board_dict):
        """Classify boards into outcome categories

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.

        Returns:
            BoardStatus: Named tuple containing lists of board targets
        """
        ok = []      # List of boards fixed since last commit
        warn = []    # List of boards with warnings since last commit
        err = []     # List of new broken boards since last commit
        new = []     # List of boards that didn't exist last time
        unknown = [] # List of boards that were not built

        for target in board_dict:
            if target not in board_selected:
                continue

            # If the board was built last time, add its outcome to a list
            if target in self._base_board_dict:
                base_outcome = self._base_board_dict[target].rc
                outcome = board_dict[target]
                if outcome.rc == OUTCOME_UNKNOWN:
                    unknown.append(target)
                elif outcome.rc < base_outcome:
                    if outcome.rc == OUTCOME_WARNING:
                        warn.append(target)
                    else:
                        ok.append(target)
                elif outcome.rc > base_outcome:
                    if outcome.rc == OUTCOME_WARNING:
                        warn.append(target)
                    else:
                        err.append(target)
            else:
                new.append(target)
        return BoardStatus(ok, warn, err, new, unknown)

    @staticmethod
    def _calc_config(delta, name, config):
        """Calculate configuration changes

        Args:
            delta: Type of the delta, e.g. '+'
            name: name of the file which changed (e.g. .config)
            config: configuration change dictionary
                key: config name
                value: config value
        Returns:
            String containing the configuration changes which can be
                printed
        """
        out = ''
        for key in sorted(config.keys()):
            out += f'{key}={config[key]} '
        return f'{delta} {name}: {out}'

    @classmethod
    def _add_config(cls, lines, name, config_plus, config_minus, config_change):
        """Add changes in configuration to a list

        Args:
            lines: list to add to
            name: config file name
            config_plus: configurations added, dictionary
                key: config name
                value: config value
            config_minus: configurations removed, dictionary
                key: config name
                value: config value
            config_change: configurations changed, dictionary
                key: config name
                value: config value
        """
        if config_plus:
            lines.append(cls._calc_config('+', name, config_plus))
        if config_minus:
            lines.append(cls._calc_config('-', name, config_minus))
        if config_change:
            lines.append(cls._calc_config('c', name, config_change))

    def _output_config_info(self, lines):
        """Output configuration change information

        Args:
            lines: List of configuration change strings
        """
        for line in lines:
            if not line:
                continue
            col = None
            if line[0] == '+':
                col = self.col.GREEN
            elif line[0] == '-':
                col = self.col.RED
            elif line[0] == 'c':
                col = self.col.YELLOW
            tprint('   ' + line, newline=True, colour=col)

    def _show_environment_changes(self, board_selected, board_dict,
                                  environment):
        """Show changes in environment variables

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            environment (dict): Dict of environment changes, keyed by
                board.target
        """
        lines = []
        for target in board_dict:
            if target not in board_selected:
                continue

            tbase = self._base_environment[target]
            tenvironment = environment[target]
            environment_plus = {}
            environment_minus = {}
            environment_change = {}
            base = tbase.environment
            for key, value in tenvironment.environment.items():
                if key not in base:
                    environment_plus[key] = value
            for key, value in base.items():
                if key not in tenvironment.environment:
                    environment_minus[key] = value
            for key, value in base.items():
                new_value = tenvironment.environment.get(key)
                if new_value and value != new_value:
                    desc = f'{value} -> {new_value}'
                    environment_change[key] = desc

            self._add_config(lines, target, environment_plus,
                             environment_minus, environment_change)
        self._output_config_info(lines)

    def _calc_config_changes(self, target, arch, config, arch_config_plus,
                              arch_config_minus, arch_config_change):
        """Calculate configuration changes for a single target

        Args:
            target (str): Target board name
            arch (str): Architecture name
            config (dict): Dict of config changes, keyed by board.target
            arch_config_plus (dict): Dict to update with added configs by
                arch
            arch_config_minus (dict): Dict to update with removed configs by
                arch
            arch_config_change (dict): Dict to update with changed configs by
                arch

        Returns:
            str: Summary of config changes for this target
        """
        all_config_plus = {}
        all_config_minus = {}
        all_config_change = {}
        tbase = self._base_config[target]
        tconfig = config[target]
        lines = []
        for name in self.config_filenames:
            if not tconfig.config[name]:
                continue
            config_plus = {}
            config_minus = {}
            config_change = {}
            base = tbase.config[name]
            for key, value in tconfig.config[name].items():
                if key not in base:
                    config_plus[key] = value
                    all_config_plus[key] = value
            for key, value in base.items():
                if key not in tconfig.config[name]:
                    config_minus[key] = value
                    all_config_minus[key] = value
            for key, value in base.items():
                new_value = tconfig.config[name].get(key)
                if new_value and value != new_value:
                    desc = f'{value} -> {new_value}'
                    config_change[key] = desc
                    all_config_change[key] = desc

            arch_config_plus[arch][name].update(config_plus)
            arch_config_minus[arch][name].update(config_minus)
            arch_config_change[arch][name].update(config_change)

            self._add_config(lines, name, config_plus, config_minus,
                             config_change)
        self._add_config(lines, 'all', all_config_plus,
                         all_config_minus, all_config_change)
        return '\n'.join(lines)

    def _print_arch_config_summary(self, arch, arch_config_plus,
                                    arch_config_minus, arch_config_change):
        """Print configuration summary for a single architecture

        Args:
            arch (str): Architecture name
            arch_config_plus (dict): Dict of added configs by arch/filename
            arch_config_minus (dict): Dict of removed configs by arch/filename
            arch_config_change (dict): Dict of changed configs by arch/filename
        """
        lines = []
        all_plus = {}
        all_minus = {}
        all_change = {}
        for name in self.config_filenames:
            all_plus.update(arch_config_plus[arch][name])
            all_minus.update(arch_config_minus[arch][name])
            all_change.update(arch_config_change[arch][name])
            self._add_config(lines, name,
                             arch_config_plus[arch][name],
                             arch_config_minus[arch][name],
                             arch_config_change[arch][name])
        self._add_config(lines, 'all', all_plus, all_minus, all_change)
        if lines:
            tprint(f'{arch}:')
            self._output_config_info(lines)

    def _show_config_changes(self, board_selected, board_dict, config):
        """Show changes in configuration

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            config (dict): Dict of config changes, keyed by board.target
        """
        summary = {}
        arch_config_plus = {}
        arch_config_minus = {}
        arch_config_change = {}
        arch_list = []

        for target in board_dict:
            if target not in board_selected:
                continue
            arch = board_selected[target].arch
            if arch not in arch_list:
                arch_list.append(arch)

        for arch in arch_list:
            arch_config_plus[arch] = {}
            arch_config_minus[arch] = {}
            arch_config_change[arch] = {}
            for name in self.config_filenames:
                arch_config_plus[arch][name] = {}
                arch_config_minus[arch][name] = {}
                arch_config_change[arch][name] = {}

        for target in board_dict:
            if target not in board_selected:
                continue
            arch = board_selected[target].arch
            summary[target] = self._calc_config_changes(
                target, arch, config, arch_config_plus, arch_config_minus,
                arch_config_change)

        lines_by_target = {}
        for target, lines in summary.items():
            if lines in lines_by_target:
                lines_by_target[lines].append(target)
            else:
                lines_by_target[lines] = [target]

        for arch in arch_list:
            self._print_arch_config_summary(arch, arch_config_plus,
                                            arch_config_minus,
                                            arch_config_change)

        for lines, targets in lines_by_target.items():
            if not lines:
                continue
            tprint(f"{' '.join(sorted(targets))} :")
            self._output_config_info(lines.split('\n'))

    def _output_err_lines(self, err_lines, colour):
        """Output the line of error/warning lines, if not empty

        Also increments self._error_lines if err_lines not empty

        Args:
            err_lines: List of ErrLine objects, each an error or warning
                line, possibly including a list of boards with that
                error/warning
            colour: Colour to use for output
        """
        if err_lines:
            out_list = []
            for line in err_lines:
                names = [brd.target for brd in line.brds]
                board_str = ' '.join(names) if names else ''
                if board_str:
                    out = self.col.build(colour, line.char + '(')
                    out += self.col.build(self.col.MAGENTA, board_str,
                                          bright=False)
                    out += self.col.build(colour, f') {line.errline}')
                else:
                    out = self.col.build(colour, line.char + line.errline)
                out_list.append(out)
            tprint('\n'.join(out_list))
            self._error_lines += 1

    def _display_arch_results(self, board_selected, brd_status, better_err,
                              worse_err, better_warn, worse_warn):
        """Display results by architecture

        Args:
            board_selected (dict): Dict containing boards to summarise
            brd_status (BoardStatus): Named tuple with board classifications
            better_err: List of ErrLine for fixed errors
            worse_err: List of ErrLine for new errors
            better_warn: List of ErrLine for fixed warnings
            worse_warn: List of ErrLine for new warnings
        """
        if self._ide:
            return
        if not any((brd_status.ok, brd_status.warn, brd_status.err,
                    brd_status.unknown, brd_status.new, worse_err, better_err,
                    worse_warn, better_warn)):
            return
        arch_list = {}
        self.add_outcome(board_selected, arch_list, brd_status.ok, '',
                         self.col.GREEN)
        self.add_outcome(board_selected, arch_list, brd_status.warn, 'w+',
                         self.col.YELLOW)
        self.add_outcome(board_selected, arch_list, brd_status.err, '+',
                         self.col.RED)
        self.add_outcome(board_selected, arch_list, brd_status.new, '*',
                         self.col.BLUE)
        if self._show_unknown:
            self.add_outcome(board_selected, arch_list, brd_status.unknown,
                             '?', self.col.MAGENTA)
        for arch, target_list in arch_list.items():
            tprint(f'{arch:>10s}: {target_list}')
            self._error_lines += 1
        self._output_err_lines(better_err, colour=self.col.GREEN)
        self._output_err_lines(worse_err, colour=self.col.RED)
        self._output_err_lines(better_warn, colour=self.col.CYAN)
        self._output_err_lines(worse_warn, colour=self.col.YELLOW)

    def _print_ide_output(self, board_selected, board_dict):
        """Print output for IDE mode

        Args:
            board_selected (dict): Dict of selected boards, keyed by target
            board_dict (dict): Dict of boards that were built, keyed by target
        """
        if not self._ide:
            return
        for target in board_dict:
            if target not in board_selected:
                continue
            outcome = board_dict[target]
            for line in outcome.err_lines:
                sys.stderr.write(line)

    def print_result_summary(self, board_selected, board_dict, err_lines,
                           err_line_boards, warn_lines, warn_line_boards,
                           config, environment, show_sizes, show_detail,
                           show_bloat, show_config, show_environment):
        """Compare results with the base results and display delta.

        Only boards mentioned in board_selected will be considered. This
        function is intended to be called repeatedly with the results of
        each commit. It therefore shows a 'diff' between what it saw in
        the last call and what it sees now.

        Args:
            board_selected (dict): Dict containing boards to summarise, keyed
                by board.target
            board_dict (dict): Dict containing boards for which we built this
                commit, keyed by board.target. The value is an Outcome object.
            err_lines (list): A list of errors for this commit, or [] if there
                is none, or we don't want to print errors
            err_line_boards (dict): Dict keyed by error line, containing a list
                of the Board objects with that error
            warn_lines (list): A list of warnings for this commit, or [] if
                there is none, or we don't want to print errors
            warn_line_boards (dict): Dict keyed by warning line, containing a
                list of the Board objects with that warning
            config (dict): Dictionary keyed by filename - e.g. '.config'. Each
                    value is itself a dictionary:
                        key: config name
                        value: config value
            environment (dict): Dictionary keyed by environment variable, Each
                     value is the value of environment variable.
            show_sizes (bool): Show image size deltas
            show_detail (bool): Show size delta detail for each board if
                show_sizes
            show_bloat (bool): Show detail for each function
            show_config (bool): Show config changes
            show_environment (bool): Show environment changes
        """
        def _board_list(line, line_boards):
            """Helper function to get a line of boards containing a line

            Args:
                line: Error line to search for
                line_boards: boards to search, each a Board
            Return:
                List of boards with that error line, or [] if the user has not
                    requested such a list
            """
            brds = []
            board_set = set()
            if self._list_error_boards:
                for brd in line_boards[line]:
                    if not brd in board_set:
                        brds.append(brd)
                        board_set.add(brd)
            return brds

        def _calc_error_delta(base_lines, base_line_boards, lines, line_boards,
                            char):
            """Calculate the required output based on changes in errors

            Args:
                base_lines: List of errors/warnings for previous commit
                base_line_boards: Dict keyed by error line, containing a list
                    of the Board objects with that error in the previous commit
                lines: List of errors/warning for this commit, each a str
                line_boards: Dict keyed by error line, containing a list
                    of the Board objects with that error in this commit
                char: Character representing error ('') or warning ('w'). The
                    broken ('+') or fixed ('-') characters are added in this
                    function

            Returns:
                Tuple
                    List of ErrLine objects for 'better' lines
                    List of ErrLine objects for 'worse' lines
            """
            better_lines = []
            worse_lines = []
            for line in lines:
                if line not in base_lines:
                    errline = ErrLine(
                        char + '+', _board_list(line, line_boards), line)
                    worse_lines.append(errline)
            for line in base_lines:
                if line not in lines:
                    errline = ErrLine(char + '-',
                                      _board_list(line, base_line_boards), line)
                    better_lines.append(errline)
            return better_lines, worse_lines

        brd_status = self._classify_boards(board_selected, board_dict)

        # Get a list of errors and warnings that have appeared, and disappeared
        better_err, worse_err = _calc_error_delta(self._base_err_lines,
                self._base_err_line_boards, err_lines, err_line_boards, '')
        better_warn, worse_warn = _calc_error_delta(self._base_warn_lines,
                self._base_warn_line_boards, warn_lines, warn_line_boards, 'w')

        # For the IDE mode, print out all the output
        self._print_ide_output(board_selected, board_dict)

        # Display results by arch
        self._display_arch_results(board_selected, brd_status, better_err,
                                   worse_err, better_warn, worse_warn)

        if show_sizes:
            self.print_size_summary(board_selected, board_dict, show_detail,
                                  show_bloat)

        if show_environment and self._base_environment:
            self._show_environment_changes(board_selected, board_dict,
                                           environment)

        if show_config and self._base_config:
            self._show_config_changes(board_selected, board_dict, config)


        # Save our updated information for the next call to this function
        self._base_board_dict = board_dict
        self._base_err_lines = err_lines
        self._base_warn_lines = warn_lines
        self._base_err_line_boards = err_line_boards
        self._base_warn_line_boards = warn_line_boards
        self._base_config = config
        self._base_environment = environment

        self._show_not_built(board_selected, board_dict)

    @staticmethod
    def _show_not_built(board_selected, board_dict):
        """Show boards that were not built

        This reports boards that couldn't be built due to toolchain issues.
        These have OUTCOME_UNKNOWN (no result file) or OUTCOME_ERROR with
        "Tool chain error" in the error lines.

        Args:
            board_selected (dict): Dict of selected boards, keyed by target
            board_dict (dict): Dict of boards that were built, keyed by target
        """
        not_built = []
        for target in board_selected:
            if target not in board_dict:
                not_built.append(target)
            else:
                outcome = board_dict[target]
                if outcome.rc == OUTCOME_UNKNOWN:
                    not_built.append(target)
                elif outcome.rc == OUTCOME_ERROR:
                    # Check for toolchain error in the error lines
                    for line in outcome.err_lines:
                        if 'Tool chain error' in line:
                            not_built.append(target)
                            break
        if not_built:
            tprint(f"Boards not built ({len(not_built)}): "
                   f"{', '.join(not_built)}")

    def produce_result_summary(self, commit_upto, commits, board_selected):
        """Produce a summary of the results for a single commit

        Args:
            commit_upto (int): Commit number to summarise (0..self.count-1)
            commits (list): List of commits being built
            board_selected (dict): Dict containing boards to summarise
        """
        (board_dict, err_lines, err_line_boards, warn_lines,
         warn_line_boards, config, environment) = self.get_result_summary(
                board_selected, commit_upto,
                read_func_sizes=self._show_bloat,
                read_config=self._show_config,
                read_environment=self._show_environment)
        if commits:
            msg = f'{commit_upto + 1:02d}: {commits[commit_upto].subject}'
            tprint(msg, colour=self.col.BLUE)
        self.print_result_summary(board_selected, board_dict,
                err_lines if self._show_errors else [], err_line_boards,
                warn_lines if self._show_errors else [], warn_line_boards,
                config, environment, self._show_sizes, self._show_detail,
                self._show_bloat, self._show_config, self._show_environment)

    def show_summary(self, commits, board_selected):
        """Show a build summary for U-Boot for a given board list.

        Reset the result summary, then repeatedly call GetResultSummary on
        each commit's results, then display the differences we see.

        Args:
            commits (list): Commit objects to summarise
            board_selected (dict): Dict containing boards to summarise
        """
        self.commit_count = len(commits) if commits else 1
        self.commits = commits
        self.reset_result_summary(board_selected)
        self._error_lines = 0

        for commit_upto in range(0, self.commit_count, self._step):
            self.produce_result_summary(commit_upto, commits, board_selected)
        if not self._error_lines:
            tprint('(no errors to report)', colour=self.col.GREEN)


    def setup_build(self, board_selected, _commits):
        """Set up ready to start a build.

        Args:
            board_selected (dict): Selected boards to build
        """
        # First work out how many commits we will build
        count = (self.commit_count + self._step - 1) // self._step
        self.count = len(board_selected) * count
        self.upto = self.warned = self.fail = 0
        self._timestamps = collections.deque()

    def get_thread_dir(self, thread_num):
        """Get the directory path to the working dir for a thread.

        Args:
            thread_num (int): Number of thread to check (-1 for main process,
                which is treated as 0)

        Returns:
            str: Path to the thread's working directory
        """
        if self.work_in_output:
            return self._working_dir
        return os.path.join(self._working_dir, f'{max(thread_num, 0):02d}')

    def _prepare_thread(self, thread_num, setup_git):
        """Prepare the working directory for a thread.

        This clones or fetches the repo into the thread's work directory.
        Optionally, it can create a linked working tree of the repo in the
        thread's work directory instead.

        Args:
            thread_num: Thread number (0, 1, ...)
            setup_git:
               'clone' to set up a git clone
               'worktree' to set up a git worktree
        """
        thread_dir = self.get_thread_dir(thread_num)
        builderthread.mkdir(thread_dir)
        git_dir = os.path.join(thread_dir, '.git') if thread_dir else None

        # Create a worktree or a git repo clone for this thread if it
        # doesn't already exist
        if setup_git and self.git_dir:
            src_dir = os.path.abspath(self.git_dir)
            if os.path.isdir(git_dir):
                # This is a clone of the src_dir repo, we can keep using
                # it but need to fetch from src_dir.
                tprint(f'\rFetching repo for thread {thread_num}',
                      newline=False)
                gitutil.fetch(git_dir, thread_dir)
                terminal.print_clear()
            elif os.path.isfile(git_dir):
                # This is a worktree of the src_dir repo, we don't need to
                # create it again or update it in any way.
                pass
            elif os.path.exists(git_dir):
                # Don't know what could trigger this, but we probably
                # can't create a git worktree/clone here.
                raise ValueError(f'Git dir {git_dir} exists, but is not a '
                                 'file or a directory.')
            elif setup_git == 'worktree':
                tprint(f'\rChecking out worktree for thread {thread_num}',
                      newline=False)
                gitutil.add_worktree(src_dir, thread_dir)
                terminal.print_clear()
            elif setup_git == 'clone' or setup_git is True:
                tprint(f'\rCloning repo for thread {thread_num}',
                      newline=False)
                gitutil.clone(src_dir, thread_dir)
                terminal.print_clear()
            else:
                raise ValueError(f"Can't setup git repo with {setup_git}.")

    def _prepare_working_space(self, max_threads, setup_git):
        """Prepare the working directory for use.

        Set up the git repo for each thread. Creates a linked working tree
        if git-worktree is available, or clones the repo if it isn't.

        Args:
            max_threads: Maximum number of threads we expect to need. If 0 then
                1 is set up, since the main process still needs somewhere to
                work
            setup_git: True to set up a git worktree or a git clone
        """
        builderthread.mkdir(self._working_dir)
        if setup_git and self.git_dir:
            src_dir = os.path.abspath(self.git_dir)
            if gitutil.check_worktree_is_available(src_dir):
                setup_git = 'worktree'
                # If we previously added a worktree but the directory for it
                # got deleted, we need to prune its files from the repo so
                # that we can check out another in its place.
                gitutil.prune_worktrees(src_dir)
            else:
                setup_git = 'clone'

        # Always do at least one thread
        for thread in range(max(max_threads, 1)):
            self._prepare_thread(thread, setup_git)

    def _get_output_space_removals(self):
        """Get the output directories ready to receive files.

        Figure out what needs to be deleted in the output directory before it
        can be used. We only delete old buildman directories which have the
        expected name pattern. See get_output_dir().

        Returns:
            List of full paths of directories to remove
        """
        if not self.commits:
            return []
        dir_list = []
        for commit_upto in range(self.commit_count):
            dir_list.append(self.get_output_dir(commit_upto))

        to_remove = []
        for dirname in glob.glob(os.path.join(self.base_dir, '*')):
            if dirname not in dir_list:
                leaf = dirname[len(self.base_dir) + 1:]
                m =  re.match('[0-9]+_g[0-9a-f]+_.*', leaf)
                if m:
                    to_remove.append(dirname)
        return to_remove

    def _prepare_output_space(self):
        """Get the output directories ready to receive files.

        We delete any output directories which look like ones we need to
        create. Having left over directories is confusing when the user wants
        to check the output manually.
        """
        to_remove = self._get_output_space_removals()
        if to_remove:
            tprint(f'Removing {len(to_remove)} old build directories...',
                  newline=False)
            for dirname in to_remove:
                shutil.rmtree(dirname)
            terminal.print_clear()

    def build_boards(self, commits, board_selected, keep_outputs, verbose,
                     fragments):
        """Build all commits for a list of boards

        Args:
            commits (list): List of commits to be build, each a Commit object
            board_selected (dict): Dict of selected boards, key is target name,
                    value is Board object
            keep_outputs (bool): True to save build output files
            verbose (bool): Display build results as they are completed
            fragments (str): config fragments added to defconfig

        Returns:
            tuple: Tuple containing:
                - number of boards that failed to build
                - number of boards that issued warnings
                - list of thread exceptions raised
        """
        self.commit_count = len(commits) if commits else 1
        self.commits = commits
        self._verbose = verbose

        self.reset_result_summary(board_selected)
        builderthread.mkdir(self.base_dir, parents = True)
        self._prepare_working_space(min(self.num_threads, len(board_selected)),
                commits is not None)
        self._prepare_output_space()
        if not self._ide:
            tprint('\rStarting build...', newline=False)
        self._start_time = datetime.now()
        self.setup_build(board_selected, commits)
        self.process_result(None)
        self.thread_exceptions = []
        # Create jobs to build all commits for each board
        for brd in board_selected.values():
            job = builderthread.BuilderJob()
            job.brd = brd
            job.commits = commits
            job.keep_outputs = keep_outputs
            job.work_in_output = self.work_in_output
            job.adjust_cfg = self.adjust_cfg
            job.fragments = fragments
            job.step = self._step
            if self.num_threads:
                self.queue.put(job)
            else:
                self._single_builder.run_job(job)

        if self.num_threads:
            term = threading.Thread(target=self.queue.join)
            term.daemon = True
            term.start()
            while term.is_alive():
                term.join(100)

            # Wait until we have processed all output
            self.out_queue.join()
        if not self._ide:
            self._print_build_summary()

        return (self.fail, self.warned, self.thread_exceptions)

    def _print_build_summary(self):
        """Print a summary of the build results

        Show the number of boards built, how many were already done, duration
        and build rate. Also show any thread exceptions that occurred.
        """
        tprint()

        msg = f'Completed: {self.count} total built'
        if self.already_done or self.kconfig_reconfig:
            parts = []
            if self.already_done:
                parts.append(f'{self.already_done} previously')
            if self.already_done != self.count:
                parts.append(f'{self.count - self.already_done} newly')
            if self.kconfig_reconfig:
                parts.append(f'{self.kconfig_reconfig} reconfig')
            msg += ' (' + ', '.join(parts) + ')'
        duration = datetime.now() - self._start_time
        if duration > timedelta(microseconds=1000000):
            if duration.microseconds >= 500000:
                duration = duration + timedelta(seconds=1)
            duration -= timedelta(microseconds=duration.microseconds)
            rate = float(self.count) / duration.total_seconds()
            msg += f', duration {duration}, rate {rate:1.2f}'
        tprint(msg)
        if self.thread_exceptions:
            tprint(
                f'Failed: {len(self.thread_exceptions)} thread exceptions',
                colour=self.col.RED)
