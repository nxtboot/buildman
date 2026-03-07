# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.
#

"""Control module for buildman

This holds the main control logic for buildman, when not running tests.
"""

import getpass
import multiprocessing
import os
import signal
import shutil
import sys
import tempfile
import threading
import time

from buildman import boards
from buildman import bsettings
from buildman import cfgutil
from buildman import machine
from buildman import toolchain
from buildman.builder import Builder
from buildman.outcome import DisplayOptions
from buildman.resulthandler import ResultHandler
import qconfig
from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib.terminal import print_clear, tprint

from patman import patchstream

TEST_BUILDER = None

# Space-separated list of buildman process IDs currently running jobs
RUNNING_FNAME = f'buildmanq.{getpass.getuser()}'

# Lock file for access to RUNNING_FILE
LOCK_FNAME = f'{RUNNING_FNAME}.lock'

# Wait time for access to lock (seconds)
LOCK_WAIT_S = 10

# Wait time to start running
RUN_WAIT_S = 300

def get_plural(count):
    """Returns a plural 's' if count is not 1"""
    return 's' if count != 1 else ''


def count_build_commits(commits, step):
    """Calculate the number of commits to be built

    Args:
        commits (list of Commit): Commits to build or None
        step (int): Step value for commits, typically 1

    Returns:
        Number of commits that will be built
    """
    if commits:
        count = len(commits)
        return (count + step - 1) // step
    return 0


def get_action_summary(is_summary, commit_count, selected, threads, jobs,
                       no_local=False):
    """Return a string summarising the intended action.

    Args:
        is_summary (bool): True if this is a summary (otherwise it is building)
        commit_count (int): Number of commits being built
        selected (list of Board): List of Board objects that are marked
        threads (int): Number of processor threads being used
        jobs (int): Number of jobs to build at once
        no_local (bool): True if all builds are remote (no local threads)

    Returns:
        str: Summary string
    """
    if commit_count:
        commit_str = f'{commit_count} commit{get_plural(commit_count)}'
    else:
        commit_str = 'current source'
    msg = (f"{'Summary of' if is_summary else 'Building'} "
           f'{commit_str} for {len(selected)} boards')
    if not no_local:
        msg += (f' ({threads} thread{get_plural(threads)}, '
                f'{jobs} job{get_plural(jobs)} per thread)')
    return msg

# pylint: disable=R0913,R0917
def show_actions(series, why_selected, boards_selected, output_dir,
                 board_warnings, step, threads, jobs, verbose):
    """Display a list of actions that we would take, if not a dry run.

    Args:
        series (Series): Series object
        why_selected (dict): Dictionary where each key is a buildman argument
            provided by the user, and the value is the list of boards
            brought in by that argument. For example, 'arm' might bring
            in 400 boards, so in this case the key would be 'arm' and
            the value would be a list of board names.
        boards_selected (dict): Dict of selected boards, key is target name,
            value is Board object
        output_dir (str): Output directory for builder
        board_warnings (list of str): List of warnings obtained from board
            selected
        step (int): Step increment through commits
        threads (int): Number of processor threads being used
        jobs (int): Number of jobs to build at once
        verbose (bool): True to indicate why each board was selected
    """
    col = terminal.Color()
    print('Dry run, so not doing much. But I would do this:')
    print()
    if series:
        commits = series.commits
    else:
        commits = None
    print(get_action_summary(False, count_build_commits(commits, step),
                             boards_selected, threads, jobs))
    print(f'Build directory: {output_dir}')
    if commits:
        for upto in range(0, len(series.commits), step):
            commit = series.commits[upto]
            print('   ', col.build(col.YELLOW, commit.hash[:8], bright=False),
                  end=' ')
            print(commit.subject)
    print()
    for arg in why_selected:
        # When -x is used, only the 'all' member exists
        if arg != 'all' or len(why_selected) == 1:
            print(arg, f': {len(why_selected[arg])} boards')
            if verbose:
                print(f"   {' '.join(why_selected[arg])}")
    print('Total boards to build for each '
          f"commit: {len(why_selected['all'])}\n")
    if board_warnings:
        for warning in board_warnings:
            print(col.build(col.YELLOW, warning))

def show_toolchain_prefix(brds, toolchains):
    """Show information about a the tool chain used by one or more boards

    The function checks that all boards use the same toolchain, then prints
    the correct value for CROSS_COMPILE.

    Args:
        brds (Boards): Boards object containing selected boards
        toolchains (Toolchains): Toolchains object containing available
            toolchains
    """
    board_selected = brds.get_selected_dict()
    tc_set = set()
    for brd in board_selected.values():
        tc_set.add(toolchains.select(brd.arch))
    if len(tc_set) != 1:
        sys.exit('Supplied boards must share one toolchain')
    tchain = tc_set.pop()
    print(tchain.get_env_args(toolchain.VAR_CROSS_COMPILE))

def show_arch(brds):
    """Show information about a the architecture used by one or more boards

    The function checks that all boards use the same architecture, then prints
    the correct value for ARCH.

    Args:
        brds (Boards): Boards object containing selected boards
    """
    board_selected = brds.get_selected_dict()
    arch_set = set()
    for brd in board_selected.values():
        arch_set.add(brd.arch)
    if len(arch_set) != 1:
        sys.exit('Supplied boards must share one arch')
    print(arch_set.pop())

def get_allow_missing(opt_allow, opt_no_allow, num_selected, has_branch):
    """Figure out whether to allow external blobs

    Uses the allow-missing setting and the provided arguments to decide whether
    missing external blobs should be allowed

    Args:
        opt_allow (bool): True if --allow-missing flag is set
        opt_no_allow (bool): True if --no-allow-missing flag is set
        num_selected (int): Number of selected board
        has_branch (bool): True if a git branch (to build) has been provided

    Returns:
        bool: True to allow missing external blobs, False to produce an error if
            external blobs are used
    """
    allow_missing = False
    am_setting = bsettings.get_global_item_value('allow-missing')
    if am_setting:
        if am_setting == 'always':
            allow_missing = True
        if 'multiple' in am_setting and num_selected > 1:
            allow_missing = True
        if 'branch' in am_setting and has_branch:
            allow_missing = True

    if opt_allow:
        allow_missing = True
    if opt_no_allow:
        allow_missing = False
    return allow_missing


def count_commits(branch, count, col, git_dir):
    """Could the number of commits in the branch/ranch being built

    Args:
        branch (str): Name of branch to build, or None if none
        count (int): Number of commits to build, or -1 for all
        col (Terminal.Color): Color object to use
        git_dir (str): Git directory to use, e.g. './.git'

    Returns:
        tuple:
            Number of commits being built
            True if the 'branch' string contains a range rather than a simple
                name
    """
    has_range = branch and '..' in branch
    if count == -1:
        if not branch:
            count = 1
        else:
            if has_range:
                count, msg = gitutil.count_commits_in_range(git_dir, branch)
            else:
                count, msg = gitutil.count_commits_in_branch(git_dir, branch)
            if count is None:
                sys.exit(col.build(col.RED, msg))
            elif count == 0:
                sys.exit(col.build(col.RED,
                                   f"Range '{branch}' has no commits"))
            if msg:
                print(col.build(col.YELLOW, msg))
            count += 1   # Build upstream commit also

    if not count:
        msg = (f"No commits found to process in branch '{branch}': "
               "set branch's upstream or use -c flag")
        sys.exit(col.build(col.RED, msg))
    return count, has_range


# pylint: disable=R0917
def determine_series(selected, col, git_dir, count, branch, work_in_output):
    """Determine the series which is to be built, if any

    If there is a series, the commits in that series are numbered by setting
    their sequence value (starting from 0). This is used by tests.

    Args:
        selected (list of Board): List of Board objects that are marked
            selected
        col (Terminal.Color): Color object to use
        git_dir (str): Git directory to use, e.g. './.git'
        count (int): Number of commits in branch
        branch (str): Name of branch to build, or None if none
        work_in_output (bool): True to work in the output directory

    Returns:
        Series: Series to build, or None for none

    Read the metadata from the commits. First look at the upstream commit,
    then the ones in the branch. We would like to do something like
    upstream/master~..branch but that isn't possible if upstream/master is
    a merge commit (it will list all the commits that form part of the
    merge)

    Conflicting tags are not a problem for buildman, since it does not use
    them. For example, Series-version is not useful for buildman. On the
    other hand conflicting tags will cause an error. So allow later tags
    to overwrite earlier ones by setting allow_overwrite=True
    """

    # Work out how many commits to build. We want to build everything on the
    # branch. We also build the upstream commit as a control so we can see
    # problems introduced by the first commit on the branch.
    count, has_range = count_commits(branch, count, col, git_dir)
    if work_in_output:
        if len(selected) != 1:
            sys.exit(col.build(col.RED,
                               '-w can only be used with a single board'))
        if count != 1:
            sys.exit(col.build(col.RED,
                               '-w can only be used with a single commit'))

    if branch:
        if count == -1:
            if has_range:
                range_expr = branch
            else:
                range_expr = gitutil.get_range_in_branch(git_dir, branch)
            upstream_commit = gitutil.get_upstream(git_dir, branch)
            series = patchstream.get_metadata_for_list(upstream_commit,
                git_dir, 1, series=None, allow_overwrite=True)

            series = patchstream.get_metadata_for_list(range_expr,
                    git_dir, None, series, allow_overwrite=True)
        else:
            # Honour the count
            series = patchstream.get_metadata_for_list(branch,
                    git_dir, count, series=None, allow_overwrite=True)

        # Number the commits for test purposes
        for i, commit in enumerate(series.commits):
            commit.sequence = i
    else:
        series = None
    return series


def do_fetch_arch(toolchains, col, fetch_arch):
    """Handle the --fetch-arch option

    Args:
        toolchains (Toolchains): Tool chains to use
        col (terminal.Color): Color object to build
        fetch_arch (str): Argument passed to the --fetch-arch option

    Returns:
        int: Return code for buildman
    """
    if fetch_arch == 'list':
        sorted_list = toolchains.list_archs()
        print(col.build(
            col.BLUE,
            f"Available architectures: {' '.join(sorted_list)}\n"))
        return 0

    if fetch_arch == 'all':
        fetch_arch = ','.join(toolchains.list_archs())
        print(col.build(col.CYAN,
                        f'\nDownloading toolchains: {fetch_arch}'))
    for arch in fetch_arch.split(','):
        print()
        ret = toolchains.fetch_and_install(arch)
        if ret:
            return ret
    return 0


# pylint: disable=R0917
def get_toolchains(toolchains, col, override_toolchain, fetch_arch,
                   list_tool_chains, verbose):
    """Get toolchains object to use

    Args:
        toolchains (Toolchains or None): Toolchains to use. If None, then a
            Toolchains object will be created and scanned
        col (Terminal.Color): Color object
        override_toolchain (str or None): Override value for toolchain, or None
        fetch_arch (bool): True to fetch the toolchain for the architectures
        list_tool_chains (bool): True to list all tool chains
        verbose (bool): True for verbose output when listing toolchains

    Returns:
        Either:
            int: Operation completed and buildman should exit with exit code
            Toolchains: Toolchains object to use
    """
    no_toolchains = toolchains is None
    if no_toolchains:
        toolchains = toolchain.Toolchains(override_toolchain)

    if fetch_arch:
        return do_fetch_arch(toolchains, col, fetch_arch)

    if no_toolchains:
        toolchains.get_settings()
        toolchains.scan(list_tool_chains and verbose,
                        raise_on_error=not list_tool_chains)
    if list_tool_chains:
        toolchains.list()
        print()
        return 0
    return toolchains


# pylint: disable=R0917
def get_boards_obj(output_dir, regen_board_list, maintainer_check, full_check,
                   threads, verbose):
    """Object the Boards object to use

    Creates the output directory and ensures there is a boards.cfg file, then
    read it in.

    Args:
        output_dir (str): Output directory to use, or None to use current dir
        regen_board_list (bool): True to just regenerate the board list
        maintainer_check (bool): True to just run a maintainer check
        full_check (bool): True to just run a full check of Kconfig and
            maintainers
        threads (int or None): Number of threads to use to create boards file
        verbose (bool): False to suppress output from boards-file generation

    Returns:
        Either:
            int: Operation completed and buildman should exit with exit code
            Boards: Boards object to use
    """
    brds = boards.Boards()
    nr_cpus = threads or multiprocessing.cpu_count()
    if maintainer_check or full_check:
        warnings = brds.build_board_list(jobs=nr_cpus,
                                         warn_targets=full_check)[1]
        if warnings:
            for warn in warnings:
                print(warn, file=sys.stderr)
            return 2
        return 0

    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    board_file = os.path.join(output_dir or '', 'boards.cfg')
    if regen_board_list and regen_board_list != '-':
        board_file = regen_board_list

    okay = brds.ensure_board_list(board_file, nr_cpus, force=regen_board_list,
                                  quiet=not verbose)
    if regen_board_list:
        return 0 if okay else 2
    brds.read_boards(board_file)
    return brds


def determine_boards(brds, args, col, opt_boards, exclude_list):
    """Determine which boards to build

    Each element of args and exclude can refer to a board name, arch or SoC

    Args:
        brds (Boards): Boards object
        args (list of str): Arguments describing boards to build
        col (Terminal.Color): Color object
        opt_boards (list of str): Specific boards to build, or None for all
        exclude_list (list of str): Arguments describing boards to exclude

    Returns:
        tuple:
            list of Board: List of Board objects that are marked selected
            why_selected: Dictionary where each key is a buildman argument
                    provided by the user, and the value is the list of boards
                    brought in by that argument. For example, 'arm' might bring
                    in 400 boards, so in this case the key would be 'arm' and
                    the value would be a list of board names.
            board_warnings: List of warnings obtained from board selected
    """
    exclude = []
    if exclude_list:
        for arg in exclude_list:
            exclude += arg.split(',')

    if opt_boards:
        requested_boards = []
        for brd in opt_boards:
            requested_boards += brd.split(',')
    else:
        requested_boards = None
    why_selected, board_warnings = brds.select_boards(args, exclude,
                                                      requested_boards)
    selected = brds.get_selected()
    if not selected:
        sys.exit(col.build(col.RED, 'No matching boards found'))
    return selected, why_selected, board_warnings


def adjust_args(args, series, selected):
    """Adjust arguments according to various constraints

    Updates verbose, show_errors, threads, jobs and step

    Args:
        args (Namespace): Namespace object to adjust
        series (Series): Series being built / summarised
        selected (list of Board): List of Board objects that are marked
    """
    if not series and not args.dry_run:
        args.verbose = True
        if not args.summary:
            args.show_errors = True

    # By default we have one thread per CPU. But if there are not enough jobs
    # we can have fewer threads and use a high '-j' value for make.
    if args.threads is None:
        args.threads = min(multiprocessing.cpu_count(), len(selected))
    if not args.jobs:
        args.jobs = max(1, (multiprocessing.cpu_count() +
                len(selected) - 1) // len(selected))

    if not args.step:
        args.step = len(series.commits) - 1

    # We can't show function sizes without board details at present
    if args.show_bloat:
        args.show_detail = True


# pylint: disable=R0917
def setup_output_dir(output_dir, work_in_output, branch, no_subdirs, col,
                     in_tree, clean_dir):
    """Set up the output directory

    Args:
        output_dir (str): Output directory provided by the user, or None if none
        work_in_output (bool): True to work in the output directory
        branch (str): Name of branch to build, or None if none
        no_subdirs (bool): True to put the output in the top-level output dir
        col (Terminal.Color): Color object to use
        in_tree (bool): True if doing an in-tree build
        clean_dir (bool): Used for tests only, indicates that the existing
            output_dir should be removed before starting the build

    Returns:
        str: Updated output directory pathname
    """
    if not output_dir:
        output_dir = '..'
        if work_in_output:
            if not in_tree:
                sys.exit(col.build(col.RED, '-w requires that you specify -o'))
            output_dir = None
    if branch and not no_subdirs:
        # As a special case allow the board directory to be placed in the
        # output directory itself rather than any subdirectory.
        dirname = branch.replace('/', '_')
        output_dir = os.path.join(output_dir, dirname)
        if clean_dir and os.path.exists(output_dir):
            shutil.rmtree(output_dir)
    return output_dir


def _filter_mismatched_toolchains(machines, local_toolchains):
    """Remove remote toolchains whose gcc version differs from local

    Compares the gcc version directory (e.g. gcc-13.1.0-nolibc) in
    each toolchain path. If a remote machine has a different version
    for an architecture, that architecture is removed from the
    machine's toolchain list so no boards are sent to it for that arch.

    Args:
        machines (list of Machine): Remote machines with toolchains
        local_toolchains (dict): arch -> gcc path on the local machine
    """
    local_versions = {}
    for arch, gcc in local_toolchains.items():
        ver = machine.gcc_version(gcc)
        if ver:
            local_versions[arch] = ver

    for mach in machines:
        mismatched = []
        for arch, gcc in mach.toolchains.items():
            local_ver = local_versions.get(arch)
            if not local_ver:
                continue
            remote_ver = machine.gcc_version(gcc)
            if remote_ver and remote_ver != local_ver:
                mismatched.append(arch)
        for arch in mismatched:
            del mach.toolchains[arch]


def _collect_worker_settings(args):
    """Collect build settings to send to remote workers

    Gathers the command-line flags that affect how make is invoked and
    returns them as a dict for the worker's 'configure' command.

    Args:
        args (Namespace): Command-line arguments

    Returns:
        dict: Settings dict (only includes flags that are set)
    """
    settings = {}
    flag_names = [
        'verbose_build', 'allow_missing', 'no_lto',
        'reproducible_builds', 'warnings_as_errors',
        'mrproper', 'fallback_mrproper', 'config_only',
        'force_build', 'kconfig_check',
    ]
    for name in flag_names:
        val = getattr(args, name, None)
        if val is not None:
            settings[name] = val
    return settings


def _setup_remote_builds(board_selected, args, git_dir):
    """Set up remote workers if machines are configured

    Probes machines, checks toolchains and splits boards into local
    and remote sets. Returns a WorkerPool for the remote boards.

    Args:
        board_selected (dict): All selected boards
        args (Namespace): Command-line arguments
        git_dir (str): Path to local .git directory

    Returns:
        tuple:
            dict: Boards to build locally
            dict: Boards to build remotely
            WorkerPool or None: Pool of remote workers, or None
    """
    from buildman import boss  # pylint: disable=C0415

    # Parse machine name filter from --use-machines
    machine_names = None
    if args.use_machines:
        machine_names = [n.strip() for n in args.use_machines.split(',')]

    no_local = args.no_local

    def _fail(msg):
        """Handle a failure to set up remote builds

        With --no-local, prints the error and returns empty dicts so
        nothing is built. Otherwise falls back to building everything
        locally.
        """
        if no_local:
            tprint(msg)
            return {}, {}, None
        return board_selected, {}, None

    machines_config = machine.get_machines_config()
    if not machines_config:
        return _fail('No machines configured')

    # Probe machines and their toolchains
    pool = machine.MachinePool(names=machine_names)
    available = pool.probe_all()
    if not available:
        return _fail('No machines available')

    # Check which of the boss's toolchains exist on each remote
    # machine. This makes workers use the boss's toolchain choices
    # rather than their own .buildman config.
    local_tc = toolchain.Toolchains()
    local_tc.get_settings(show_warning=False)
    local_tc.scan(verbose=False)
    local_gcc = {arch: tc.gcc for arch, tc in local_tc.toolchains.items()}

    # Resolve toolchain aliases (e.g. x86->i386) so that board
    # architectures using alias names are recognised by split_boards()
    machine.resolve_toolchain_aliases(local_gcc)

    pool.check_toolchains(
        set(), buildman_path=args.machines_buildman_path,
        local_gcc=local_gcc)
    remote_toolchains = {}
    for mach in available:
        remote_toolchains.update(mach.toolchains)

    if not remote_toolchains:
        return _fail('No remote toolchains available')

    if no_local:
        local = {}
        remote = board_selected
    else:
        local, remote = boss.split_boards(
            board_selected, remote_toolchains)

    if not remote:
        return board_selected, {}, None

    # Collect build settings to send to workers. Resolve allow_missing
    # using the .buildman config, since workers don't have it.
    settings = _collect_worker_settings(args)
    settings['allow_missing'] = get_allow_missing(
        args.allow_missing, args.no_allow_missing,
        len(board_selected), args.branch)

    # Start workers: init git, push source, start from tree
    worker_pool = boss.WorkerPool(available)
    workers = worker_pool.start_all(git_dir, 'HEAD:refs/heads/work',
                                     debug=args.debug,
                                     settings=settings)
    if not workers:
        return _fail('No remote workers available')

    return local, remote, worker_pool


def _start_remote_builds(builder, commits, board_selected, args):
    """Start remote builds in a background thread

    Splits boards between local and remote machines, launches remote
    builds in a background thread, and installs a SIGINT handler for
    clean shutdown.

    Args:
        builder (Builder): Builder to use
        commits (list of Commit): Commits to build, or None
        board_selected (dict): target -> Board for all selected boards
        args (Namespace): Command-line arguments

    Returns:
        tuple: (local_boards, remote_thread, worker_pool, extra_count,
            old_sigint)
    """
    local_boards, remote_boards, worker_pool = (
        _setup_remote_builds(board_selected, args, builder.git_dir))

    extra_count = 0
    if worker_pool and remote_boards:
        commit_count = len(commits) if commits else 1
        extra_count = len(remote_boards) * commit_count

    remote_thread = None
    if worker_pool and remote_boards:
        remote_thread = threading.Thread(
            target=worker_pool.build_boards,
            args=(remote_boards, commits, builder,
                  len(local_boards)))
        remote_thread.daemon = True
        remote_thread.start()

    # Install a SIGINT handler that cleanly shuts down workers.
    # This is more reliable than try/except KeyboardInterrupt since
    # SIGINT may terminate the process before the exception handler
    # runs.
    old_sigint = None
    if worker_pool:
        def _sigint_handler(_signum, _frame):
            worker_pool.close_all()
            signal.signal(signal.SIGINT, old_sigint or signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
        old_sigint = signal.signal(signal.SIGINT, _sigint_handler)

    return local_boards, remote_thread, worker_pool, extra_count, old_sigint


def _finish_remote_builds(remote_thread, worker_pool, old_sigint, builder):
    """Wait for remote builds to finish and clean up

    Args:
        remote_thread (Thread or None): Background remote build thread
        worker_pool (WorkerPool or None): Worker pool to shut down
        old_sigint: Previous SIGINT handler to restore
        builder (Builder): Builder for printing the summary
    """
    if remote_thread:
        try:
            while remote_thread.is_alive():
                remote_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            worker_pool.close_all()
            raise
        worker_pool.quit_all()
        builder.print_summary()

    if worker_pool and old_sigint is not None:
        signal.signal(signal.SIGINT, old_sigint)


def run_builder(builder, commits, board_selected, display_options, args):
    """Run the builder or show the summary

    Args:
        builder (Builder): Builder to use
        commits (list of Commit): List of commits being built, None if
            no branch
        board_selected (dict): Dict of selected boards:
            key: target name
            value: Board object
        display_options (DisplayOptions): Named tuple containing display
            settings
        args (Namespace): Namespace to use

    Returns:
        int: Return code for buildman
    """
    gnu_make = command.output(os.path.join(args.git,
            'scripts/show-gnu-make'), raise_on_error=False).rstrip()
    if not gnu_make:
        sys.exit('GNU Make not found')
    builder.gnu_make = gnu_make

    if not args.ide:
        commit_count = count_build_commits(commits, args.step)
        tprint(get_action_summary(args.summary, commit_count,
                                  board_selected, args.threads,
                                  args.jobs, no_local=args.no_local))

    builder.set_display_options(
        display_options, args.filter_dtb_warnings,
        args.filter_migration_warnings)
    if args.summary:
        builder.commits = commits
        builder.result_handler.show_summary(
            commits, board_selected, args.step)
    else:
        local_boards = board_selected
        remote_thread = None
        worker_pool = None
        extra_count = 0
        old_sigint = None

        if args.distribute:
            (local_boards, remote_thread, worker_pool,
             extra_count, old_sigint) = _start_remote_builds(
                builder, commits, board_selected, args)

        try:
            fail, warned, excs = builder.build_boards(
                commits, local_boards, args.keep_outputs,
                args.verbose, args.fragments,
                extra_count=extra_count,
                delay_summary=bool(remote_thread))
        except KeyboardInterrupt:
            if worker_pool:
                worker_pool.close_all()
            raise

        _finish_remote_builds(remote_thread, worker_pool,
                              old_sigint, builder)

        if args.build_summary:
            builder.commits = commits
            builder.result_handler.show_summary(
                commits, board_selected, args.step)
        if excs:
            return 102
        if fail:
            return 100
        if warned and not args.ignore_warnings:
            return 101
    return 0


def calc_adjust_cfg(adjust_cfg, reproducible_builds):
    """Calculate the value to use for adjust_cfg

    Args:
        adjust_cfg (list of str): List of configuration changes. See cfgutil for
            details
        reproducible_builds (bool): True to adjust the configuration to get
            reproduceable builds

    Returns:
        adjust_cfg (list of str): List of configuration changes
    """
    adjust_cfg = cfgutil.convert_list_to_dict(adjust_cfg)

    # Drop LOCALVERSION_AUTO since it changes the version string on every commit
    if reproducible_builds:
        # If these are mentioned, leave the local version alone
        if 'LOCALVERSION' in adjust_cfg or 'LOCALVERSION_AUTO' in adjust_cfg:
            print('Not dropping LOCALVERSION_AUTO for reproducible build')
        else:
            adjust_cfg['LOCALVERSION_AUTO'] = '~'
    return adjust_cfg


def read_procs(tmpdir=tempfile.gettempdir()):
    """Read the list of running buildman processes

    If the list is corrupted, returns an empty list

    Args:
        tmpdir (str): Temporary directory to use (for testing only)
    """
    running_fname = os.path.join(tmpdir, RUNNING_FNAME)
    procs = []
    if os.path.exists(running_fname):
        items = tools.read_file(running_fname, binary=False).split()
        try:
            procs = [int(x) for x in items]
        except ValueError: # Handle invalid format
            pass
    return procs


def check_pid(pid):
    """Check for existence of a unix PID

    See: https://stackoverflow.com/questions/568271

    Args:
        pid (int): PID to check

    Returns:
        bool: True if it exists, else False
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_procs(procs, tmpdir=tempfile.gettempdir()):
    """Write the list of running buildman processes

    Args:
        procs (list of int): List of process IDs to write
        tmpdir (str): Temporary directory to use (for testing only)
    """
    running_fname = os.path.join(tmpdir, RUNNING_FNAME)
    tools.write_file(running_fname, ' '.join([str(p) for p in procs]),
                     binary=False)

    # Allow another user to access the file
    os.chmod(running_fname, 0o666)

def wait_for_process_limit(limit, tmpdir=tempfile.gettempdir(),
                           pid=os.getpid()):
    """Wait until the number of buildman processes drops to the limit

    This uses FileLock to protect a 'running' file, which contains a list of
    PIDs of running buildman processes. The number of PIDs in the file indicates
    the number of running processes.

    When buildman starts up, it calls this function to wait until it is OK to
    start the build.

    On exit, no attempt is made to remove the PID from the file, since other
    buildman processes will notice that the PID is no-longer valid, and ignore
    it.

    Two timeouts are provided:
        LOCK_WAIT_S: length of time to wait for the lock; if this occurs, the
            lock is busted / removed before trying again
        RUN_WAIT_S: length of time to wait to be allowed to run; if this occurs,
            the build starts, with the PID being added to the file.

    Args:
        limit (int): Maximum number of buildman processes, including this one;
            must be > 0
        tmpdir (str): Temporary directory to use (for testing only)
        pid (int): Current process ID (for testing only)
    """
    # pylint: disable=C0415
    from filelock import Timeout, FileLock

    lock_fname = os.path.join(tmpdir, LOCK_FNAME)
    lock = FileLock(lock_fname)

    # Allow another user to access the file
    col = terminal.Color()
    tprint('Waiting for other buildman processes...', newline=False,
           colour=col.RED)

    claimed = False
    deadline = time.time() + RUN_WAIT_S
    while True:
        try:
            with lock.acquire(timeout=LOCK_WAIT_S):
                os.chmod(lock_fname, 0o666)
                procs = read_procs(tmpdir)

                # Drop PIDs which are not running
                procs = list(filter(check_pid, procs))

                # If we haven't hit the limit, add ourself
                if len(procs) < limit:
                    tprint('done...', newline=False)
                    claimed = True
                if time.time() >= deadline:
                    tprint('timeout...', newline=False)
                    claimed = True
                if claimed:
                    write_procs(procs + [pid], tmpdir)
                    break

        except Timeout:
            tprint('failed to get lock: busting...', newline=False)
            os.remove(lock_fname)

        time.sleep(1)
    tprint('starting build', newline=False)
    print_clear()

# pylint: disable=R0917
def do_buildman(args, toolchains=None, make_func=None, brds=None,
                clean_dir=False, test_thread_exceptions=False):
    """The main control code for buildman

    Args:
        args (Namespace): ArgumentParser object
        toolchains (Toolchains): Toolchains to use - this should be a
            Toolchains() object. If None, then it will be created and scanned
        make_func (function): Make function to use for the builder. This is
            called to execute 'make'. If this is None, the normal function
            will be used, which calls the 'make' tool with suitable
            arguments. This setting is useful for tests.
        brds (Boards): Boards() object to use, containing a list of available
            boards. If this is None it will be created and scanned.
        clean_dir (bool): Used for tests only, indicates that the existing
            output_dir should be removed before starting the build
        test_thread_exceptions (bool): Uses for tests only, True to make the
            threads raise an exception instead of reporting their result. This
            simulates a failure in the code somewhere
    """
    # Used so testing can obtain the builder: pylint: disable=W0603
    global TEST_BUILDER

    gitutil.setup()
    col = terminal.Color()

    # Handle --worker: run in worker mode for distributed builds
    if args.worker:
        from buildman import worker  # pylint: disable=C0415
        return worker.do_worker(args.debug)

    # Handle --kill-workers: kill stale workers and exit
    if args.kill_workers:
        from buildman import boss  # pylint: disable=C0415

        machines_config = machine.get_machines_config()
        if not machines_config:
            print('No machines configured')
            return 1
        return boss.kill_workers(machines_config)

    # Handle --machines: probe remote machines and show status
    if args.machines or args.machines_fetch_arch:
        return machine.do_probe_machines(
            col, fetch=args.machines_fetch_arch,
            buildman_path=args.machines_buildman_path)

    # --use-machines implies --dist
    if args.use_machines:
        args.distribute = True

    if args.no_local and not args.distribute:
        print('--no-local requires --dist')
        return 1

    git_dir = os.path.join(args.git, '.git')

    toolchains = get_toolchains(toolchains, col, args.override_toolchain,
                                args.fetch_arch, args.list_tool_chains,
                                args.verbose)
    if isinstance(toolchains, int):
        return toolchains

    output_dir = setup_output_dir(
        args.output_dir, args.work_in_output, args.branch,
        args.no_subdirs, col, args.in_tree, clean_dir)

    # Work out what subset of the boards we are building
    if not brds:
        brds = get_boards_obj(output_dir, args.regen_board_list,
                              args.maintainer_check, args.full_check,
                              args.threads, args.verbose and
                              not args.print_arch and not args.print_prefix)
        if isinstance(brds, int):
            return brds

        if args.extend:
            dbase = qconfig.ensure_database(
                args.threads or multiprocessing.cpu_count())
            brds.parse_all_extended(dbase)

    selected, why_selected, board_warnings = determine_boards(
        brds, args.terms, col, args.boards, args.exclude)

    if args.print_prefix:
        show_toolchain_prefix(brds, toolchains)
        return 0

    if args.print_arch:
        show_arch(brds)
        return 0

    series = determine_series(selected, col, git_dir, args.count,
                              args.branch, args.work_in_output)

    adjust_args(args, series, selected)

    # For a dry run, just show our actions as a sanity check
    if args.dry_run:
        show_actions(series, why_selected, selected, output_dir, board_warnings,
                     args.step, args.threads, args.jobs,
                     args.verbose)
        return 0

    if args.config_only and args.target:
        raise ValueError('Cannot use --config-only with --target')

    # Create colour, display options and result handler objects
    col = terminal.Color()
    display_options = DisplayOptions(
        show_errors=args.show_errors,
        show_sizes=args.show_sizes,
        show_detail=args.show_detail,
        show_bloat=args.show_bloat,
        show_config=args.show_config,
        show_environment=args.show_environment,
        show_unknown=args.show_unknown,
        ide=args.ide,
        list_error_boards=args.list_error_boards)
    result_handler = ResultHandler(col, display_options)

    # Create a new builder with the selected args
    builder = Builder(toolchains, output_dir, git_dir,
            args.threads, args.jobs, col=col,
            result_handler=result_handler, checkout=True, step=args.step,
            no_subdirs=args.no_subdirs, full_path=args.full_path,
            verbose_build=args.verbose_build,
            mrproper=args.mrproper,
            fallback_mrproper=args.fallback_mrproper,
            per_board_out_dir=args.per_board_out_dir,
            config_only=args.config_only,
            squash_config_y=not args.preserve_config_y,
            warnings_as_errors=args.warnings_as_errors,
            work_in_output=args.work_in_output,
            test_thread_exceptions=test_thread_exceptions,
            adjust_cfg=calc_adjust_cfg(args.adjust_cfg,
                                       args.reproducible_builds),
            allow_missing=get_allow_missing(args.allow_missing,
                                            args.no_allow_missing,
                                            len(selected), args.branch),
            no_lto=args.no_lto,
            reproducible_builds=args.reproducible_builds,
            force_build = args.force_build,
            force_build_failures = args.force_build_failures,
            kconfig_check = args.kconfig_check,
            force_reconfig = args.force_reconfig, in_tree = args.in_tree,
            force_config_on_failure=not args.quick, make_func=make_func,
            dtc_skip=args.dtc_skip, build_target=args.target)
    result_handler.set_builder(builder)

    TEST_BUILDER = builder

    if args.process_limit:
        wait_for_process_limit(args.process_limit)

    return run_builder(builder, series.commits if series else None,
                       brds.get_selected_dict(), display_options, args)
