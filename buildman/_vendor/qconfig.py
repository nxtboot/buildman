#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+

"""Build and query a Kconfig database for boards.

See doc/develop/qconfig.rst for documentation.

Author: Masahiro Yamada <yamada.masahiro@socionext.com>
Author: Simon Glass <sjg@chromium.org>
"""

from argparse import ArgumentParser, Namespace
import collections
from contextlib import ExitStack
import doctest
import filecmp
import fnmatch
import glob
import io
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from buildman import kconfiglib
from u_boot_pylib import terminal
from u_boot_pylib.terminal import tprint
from u_boot_pylib import tools

SLEEP_TIME=0.03

CONFIG_DATABASE = 'qconfig.db'
FAILED_LIST = 'qconfig.failed'

CONFIG_LEN = len('CONFIG_')

SIZES = {
    'SZ_1':    0x00000001, 'SZ_2':    0x00000002,
    'SZ_4':    0x00000004, 'SZ_8':    0x00000008,
    'SZ_16':   0x00000010, 'SZ_32':   0x00000020,
    'SZ_64':   0x00000040, 'SZ_128':  0x00000080,
    'SZ_256':  0x00000100, 'SZ_512':  0x00000200,
    'SZ_1K':   0x00000400, 'SZ_2K':   0x00000800,
    'SZ_4K':   0x00001000, 'SZ_8K':   0x00002000,
    'SZ_16K':  0x00004000, 'SZ_32K':  0x00008000,
    'SZ_64K':  0x00010000, 'SZ_128K': 0x00020000,
    'SZ_256K': 0x00040000, 'SZ_512K': 0x00080000,
    'SZ_1M':   0x00100000, 'SZ_2M':   0x00200000,
    'SZ_4M':   0x00400000, 'SZ_8M':   0x00800000,
    'SZ_16M':  0x01000000, 'SZ_32M':  0x02000000,
    'SZ_64M':  0x04000000, 'SZ_128M': 0x08000000,
    'SZ_256M': 0x10000000, 'SZ_512M': 0x20000000,
    'SZ_1G':   0x40000000, 'SZ_2G':   0x80000000,
    'SZ_4G':  0x100000000
}

RE_REMOVE_DEFCONFIG = re.compile(r'(.*)_defconfig')

# CONFIG symbols present in the build system (from Linux) but not actually used
# in U-Boot; KCONFIG symbols
IGNORE_SYMS = ['DEBUG_SECTION_MISMATCH', 'FTRACE_MCOUNT_RECORD', 'GCOV_KERNEL',
               'GCOV_PROFILE_ALL', 'KALLSYMS', 'KASAN', 'MODVERSIONS', 'SHELL',
               'TPL_BUILD', 'VPL_BUILD', 'IS_ENABLED', 'FOO', 'IF_ENABLED_INT',
               'IS_ENABLED_', 'IS_ENABLED_1', 'IS_ENABLED_2', 'IS_ENABLED_3',
               'SPL_', 'TPL_', 'SPL_FOO', 'TPL_FOO', 'TOOLS_FOO',
               'ACME', 'SPL_ACME', 'TPL_ACME', 'TRACE_BRANCH_PROFILING',
               'VAL', '_UNDEFINED', 'SPL_BUILD', 'XPL_BUILD', ]

SPL_PREFIXES = ['SPL_', 'TPL_', 'VPL_', 'TOOLS_']

### helper functions ###
def check_top_directory():
    """Exit if we are not at the top of source directory."""
    for fname in 'README', 'Licenses':
        if not os.path.exists(fname):
            sys.exit('Please run at the top of source directory.')

def get_matched_defconfig(line):
    """Get the defconfig files that match a pattern

    Args:
        line (str): Path or filename to match, e.g. 'configs/snow_defconfig' or
            'k2*_defconfig'. If no directory is provided, 'configs/' is
            prepended

    Returns:
        list of str: a list of matching defconfig files
    """
    dirname = os.path.dirname(line)
    if dirname:
        pattern = line
    else:
        pattern = os.path.join('configs', line)
    return glob.glob(pattern) + glob.glob(pattern + '_defconfig')

def get_matched_defconfigs(defconfigs_in):
    """Get all the defconfig files that match the patterns given.

    Args:
        defconfigs_in (str or list of str): File containing a list of
            defconfigs to process, or '-' to read the list from stdin, or a
            list of defconfig names

    Returns:
        list of str: A list of paths to defconfig files, with no duplicates
    """
    defconfigs = []
    with ExitStack() as stack:
        if isinstance(defconfigs_in, list):
            inf = defconfigs_in
        elif defconfigs_in == '-':
            inf = sys.stdin
            defconfigs_in = 'stdin'
        else:
            inf = stack.enter_context(open(defconfigs_in, encoding='utf-8'))
        for i, line in enumerate(inf):
            line = line.strip()
            if not line:
                continue # skip blank lines silently
            if ' ' in line:
                line = line.split(' ')[0]  # handle 'git log' input
            matched = get_matched_defconfig(line)
            if not matched:
                print(f"warning: {defconfigs_in}:{i + 1}: no defconfig matched '{line}'",
                      file=sys.stderr)

            defconfigs += matched

    # use set() to drop multiple matching
    return [defconfig[len('configs') + 1:]  for defconfig in set(defconfigs)]

def get_all_defconfigs():
    """Get all the defconfig files under the configs/ directory.

    Returns:
        list of str: List of paths to defconfig files
    """
    defconfigs = []
    for (dirpath, _, filenames) in os.walk('configs'):
        dirpath = dirpath[len('configs') + 1:]
        for filename in fnmatch.filter(filenames, '*_defconfig'):
            defconfigs.append(os.path.join(dirpath, filename))

    return defconfigs

def write_file(fname, data):
    """Write data to a file

    Args:
        fname (str): Filename to write to
        data (list of str): Lines to write (with or without trailing newline);
            or str to write
    """
    with open(fname, 'w', encoding='utf-8') as out:
        if isinstance(data, list):
            for line in data:
                print(line.rstrip('\n'), file=out)
        else:
            out.write(data)

def read_file(fname, as_lines=True, skip_unicode=False):
    """Read a file and return the contents

    Args:
        fname (str): Filename to read from
        as_lines (bool): Return file contents as a list of lines
        skip_unicode (bool): True to report unicode errors and continue

    Returns:
        iter of str: List of ;ines from the file with newline removed; str if
            as_lines is False with newlines intact; or None if a unicode error
            occurred

    Raises:
        UnicodeDecodeError: Unicode error occurred when reading
    """
    with open(fname, encoding='utf-8') as inf:
        try:
            if as_lines:
                return [line.rstrip('\n') for line in inf.readlines()]
            return inf.read()
        except UnicodeDecodeError as exc:
            if not skip_unicode:
                raise
            print(f"Failed on file '{fname}: {exc}")
            return None


### classes ###
class Progress:
    """Progress Indicator"""

    def __init__(self, col, total):
        """Create a new progress indicator.

        Args:
            col (terminal.Color): Colour-output class
            total (int): A number of defconfig files to process.

            current (int): Number of boards processed so far
            failed (int): Number of failed boards
            failure_msg (str): Message indicating number of failures, '' if none
        """
        self.col = col
        self.total = total

        self.current = 0
        self.good = 0
        self.failed = None
        self.failure_msg = None

    def inc(self, success):
        """Increment the number of processed defconfig files.

        Args:
            success (bool): True if processing succeeded
        """
        self.good += success
        self.current += 1

    def show(self):
        """Display the progress."""
        if self.current != self.total:
            line = self.col.build(self.col.GREEN, f'{self.good:5d}')
            line += self.col.build(self.col.RED,
                                   f'{self.current - self.good:5d}')
            line += self.col.build(self.col.MAGENTA,
                                   f'/{self.total - self.current}')
            print(f'{line}  \r', end='')
        sys.stdout.flush()

    def completed(self):
        """Set up extra properties when completed"""
        self.failed = self.total - self.good
        self.failure_msg = f'{self.failed} failed, ' if self.failed else ''


def scan_kconfig():
    """Scan all the Kconfig files and create a Config object

    Returns:
        Kconfig object
    """
    # Define environment variables referenced from Kconfig
    os.environ['srctree'] = os.getcwd()
    os.environ['UBOOTVERSION'] = 'dummy'
    os.environ['KCONFIG_OBJDIR'] = ''
    os.environ['CC'] = 'gcc'
    return kconfiglib.Kconfig()


def _cpp_preprocess(srcdir, fname):
    """Run the C preprocessor on a file to expand #include directives

    Args:
        srcdir (str): Source-tree directory (used as include path)
        fname (str): Path to the file to preprocess

    Returns:
        str: Path to a temporary file with the preprocessed output.
            Caller must delete it.
    """
    cpp = os.getenv('CPP', 'cpp').split()
    cmd = cpp + ['-nostdinc', '-P', '-I', srcdir,
                 '-undef', '-x', 'assembler-with-cpp', fname]
    stdout = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    tmp = tempfile.NamedTemporaryFile(prefix='qconfig-', delete=False)
    tmp.write(stdout)
    tmp.close()
    return tmp.name


def _load_defconfig(kconf, srcdir, fname):
    """Load a defconfig, preprocessing #include directives if present

    Args:
        kconf (kconfiglib.Kconfig): Kconfig instance
        srcdir (str): Source-tree directory
        fname (str): Path to the defconfig file
    """
    if b'#include' in tools.read_file(fname):
        tmp = _cpp_preprocess(srcdir, fname)
        kconf.load_config(tmp)
        os.unlink(tmp)
    else:
        kconf.load_config(fname)


def _scan_defconfigs_worker(srcdir, defconfigs, queue, error_queue):
    """Worker process that scans defconfigs using kconfiglib

    Each worker creates its own Kconfig instance (parsing is done once per
    process) then loads each defconfig in turn, collecting all CONFIG values.

    Args:
        srcdir (str): Source-tree directory
        defconfigs (list of str): Defconfig filenames to process, e.g.
            ['sandbox_defconfig', 'snow_defconfig']
        queue (multiprocessing.Queue): Output queue for (defconfig, configs)
        error_queue (multiprocessing.Queue): Output queue for failed defconfigs
    """
    os.environ['srctree'] = srcdir
    os.environ['UBOOTVERSION'] = 'dummy'
    os.environ['KCONFIG_OBJDIR'] = ''
    os.environ['CC'] = 'gcc'
    kconf = kconfiglib.Kconfig(warn=False)

    for defconfig in defconfigs:
        fname = os.path.join(srcdir, 'configs', defconfig)
        try:
            _load_defconfig(kconf, srcdir, fname)

            configs = {}
            for sym in kconf.unique_defined_syms:
                conf = sym.config_string
                if not conf or conf.startswith('#'):
                    continue
                config, value = conf.rstrip('\n').split('=', 1)
                configs[config] = value
            queue.put((defconfig, configs))
        except Exception as exc:
            error_queue.put((defconfig, str(exc)))


def do_build_db(args):
    """Build the CONFIG database using kconfiglib instead of make

    This evaluates the Kconfig tree directly in Python for each defconfig,
    avoiding the overhead of spawning make subprocesses and the need for
    cross-compiler toolchains.

    Args:
        args (Namespace): Program arguments (uses jobs, defconfigs,
            defconfiglist, nocolour)

    Returns:
        tuple:
            config_db (dict): configs for each defconfig
            Progress: progress indicator
    """
    srcdir = os.getcwd()

    if args.defconfigs:
        defconfigs = [os.path.basename(d)
                      for d in get_matched_defconfigs(args.defconfigs)]
    elif args.defconfiglist:
        defconfigs = [os.path.basename(d)
                      for d in get_matched_defconfigs(args.defconfiglist)]
    else:
        defconfigs = get_all_defconfigs()

    col = terminal.Color(terminal.COLOR_NEVER if args.nocolour
                         else terminal.COLOR_IF_TERMINAL)
    progress = Progress(col, len(defconfigs))

    jobs = args.jobs
    total = len(defconfigs)
    result_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()
    processes = []
    for i in range(jobs):
        chunk = defconfigs[total * i // jobs:total * (i + 1) // jobs]
        if not chunk:
            continue
        proc = multiprocessing.Process(
            target=_scan_defconfigs_worker,
            args=(srcdir, chunk, result_queue, error_queue))
        proc.start()
        processes.append(proc)

    config_db = {}
    remaining = total
    while remaining:
        # Drain both queues without blocking forever
        found = False
        while not result_queue.empty():
            defconfig, configs = result_queue.get()
            config_db[defconfig] = configs
            progress.inc(True)
            progress.show()
            remaining -= 1
            found = True
        while not error_queue.empty():
            defconfig, msg = error_queue.get()
            print(col.build(col.RED, f'{defconfig}: {msg}', bright=True),
                  file=sys.stderr)
            progress.inc(False)
            progress.show()
            remaining -= 1
            found = True
        if not found:
            time.sleep(SLEEP_TIME)

    for proc in processes:
        proc.join()

    progress.completed()
    return config_db, progress


def _get_min_config_lines(kconf, fname):
    """Get the set of minimal config lines for a defconfig

    Args:
        kconf (kconfiglib.Kconfig): Kconfig instance (will be modified)
        fname (str): Path to preprocessed defconfig (or plain defconfig)

    Returns:
        set of str: Lines from write_min_config output (without header)
    """
    kconf.load_config(fname)
    tmp = tempfile.NamedTemporaryFile(mode='w', prefix='qconfig-mc-',
                                     delete=False)
    tmp.close()
    kconf.write_min_config(tmp.name)
    with open(tmp.name) as inf:
        lines = set(inf.readlines())
    os.unlink(tmp.name)
    return lines


def _config_name(line):
    """Extract config name (e.g. 'CONFIG_ARM') from a defconfig line

    Args:
        line (str): A defconfig line

    Returns:
        str or None: The config name, or None if not a config line
    """
    stripped = line.strip()
    if stripped.startswith('CONFIG_'):
        return stripped.split('=', 1)[0]
    if stripped.startswith('# CONFIG_'):
        return stripped.split()[1]
    return None


def _get_defconfig_entries(fname):
    """Parse a preprocessed defconfig file to get config entries by name

    Args:
        fname (str): Path to the preprocessed defconfig file

    Returns:
        dict: Mapping of config name (e.g. 'CONFIG_ARM') to the full line
            including the value (e.g. 'CONFIG_ARM=y')
    """
    entries = {}
    with open(fname, encoding='utf-8') as inf:
        for line in inf:
            line = line.strip()
            name = _config_name(line)
            if name:
                entries[name] = line
    return entries


def _sync_plain_defconfig(kconf, orig, dry_run):
    """Sync a plain defconfig (no #include)

    Args:
        kconf (kconfiglib.Kconfig): Kconfig instance
        orig (str): Path to the original defconfig file
        dry_run (bool): If True, do not update defconfig files

    Returns:
        bool: True if the defconfig was (or would be) updated
    """
    kconf.load_config(orig)
    confdir = os.path.dirname(orig)
    tmp = tempfile.NamedTemporaryFile(
        mode='w', prefix='qconfig-', suffix='_defconfig',
        dir=confdir, delete=False)
    tmp.close()
    kconf.write_min_config(tmp.name)

    updated = not filecmp.cmp(orig, tmp.name)
    if updated and not dry_run:
        shutil.move(tmp.name, orig)
    else:
        os.unlink(tmp.name)
    return updated


def _format_sym_value(sym, value=None):
    """Format a symbol value as a defconfig line

    Args:
        sym (kconfiglib.Symbol): The symbol
        value (str or None): Value to format; uses sym.str_value if None

    Returns:
        str: The defconfig line
    """
    if value is None:
        value = sym.str_value
    if sym.orig_type in (kconfiglib.BOOL, kconfiglib.TRISTATE):
        if value == 'n':
            return f'# CONFIG_{sym.name} is not set'
        return f'CONFIG_{sym.name}={value}'
    if sym.orig_type == kconfiglib.STRING:
        return f'CONFIG_{sym.name}="{kconfiglib.escape(value)}"'
    return f'CONFIG_{sym.name}={value}'


def _rebuild_overlay(orig, needed, base_entries, full_entries):
    """Rebuild a #include defconfig preserving the original line ordering

    Keeps existing overlay lines that are still needed (updating values
    that changed), drops lines that are now redundant, and appends
    genuinely new entries at the end.

    Args:
        orig (str): Path to the original defconfig file
        needed (dict): Mapping of config name to defconfig line value
        base_entries (dict): Mapping of config name to defconfig line for
            entries provided by the include files; overlay lines which
            simply restate one of these are kept, to avoid churn
        full_entries (dict): Mapping of config name to defconfig line for
            the full minimal config; overlay lines which match are still
            correct, so are kept in place

    Returns:
        bytes: The rebuilt defconfig content
    """
    orig_lines = tools.read_file(orig, binary=False).splitlines(keepends=True)
    keep = set()
    lines = []
    for line in orig_lines:
        if line.startswith('#include'):
            lines.append(line)
            continue
        name = _config_name(line)
        if not name:
            # Blank lines and comments are structure; keep them
            lines.append(line)
            continue
        if name in needed:
            # Keep this line (possibly with updated value)
            lines.append(needed[name] + '\n')
            keep.add(name)
        elif full_entries.get(name) == line.strip():
            # The line still matches the minimal config; keep it in place
            lines.append(line)
            keep.add(name)
        elif base_entries.get(name) == line.strip():
            # The line simply restates what the include files already
            # provide; keep it to avoid churn
            lines.append(line)
        # else: drop the line (no longer needed in overlay)

    # Append new entries that weren't in the original overlay, making sure
    # the existing content ends with a newline first
    new_entries = []
    for name, line in needed.items():
        if name not in keep:
            new_entries.append(line + '\n')
    if new_entries:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.extend(sorted(new_entries))

    return ''.join(lines).encode()


def _verify_defconfig(kconf, srcdir, confdir, target, target_visible, needed,
                      new_content):
    """Verify an #include defconfig and fix remaining config differences

    Loads the candidate defconfig through kconfiglib, compares against the
    target config, and iteratively adds corrections for any remaining
    differences (from include entries needing explicit resets or transitive
    Kconfig dependency effects).

    Args:
        kconf (kconfiglib.Kconfig): Kconfig instance
        srcdir (str): Source-tree directory
        confdir (str): Directory for temp file placement
        target (dict): Target config {sym_name: str_value}
        target_visible (set of str): Symbols which are visible in the
            target config; corrections are only added for these, since an
            invisible symbol takes its value from its dependencies and is
            fixed by correcting them instead
        needed (dict): Overlay entries {config_name: line}, updated in place
        new_content (bytes): Current defconfig content to verify

    Returns:
        bytes: The verified (possibly updated) defconfig content
    """
    for _ in range(3):
        with tempfile.NamedTemporaryFile(suffix='_defconfig',
                                         dir=confdir) as tmp:
            tmp.write(new_content)
            tmp.flush()
            verify_pp = _cpp_preprocess(srcdir, tmp.name)
            kconf.load_config(verify_pp)
            os.unlink(verify_pp)

        # Find configs that differ from target and add corrections
        extra_lines = []
        for sym in kconf.unique_defined_syms:
            if sym.name not in target or sym.str_value == target[sym.name]:
                continue
            if sym.name not in target_visible:
                continue
            line = _format_sym_value(sym, target[sym.name])
            name = _config_name(line)
            if name not in needed:
                needed[name] = line
                extra_lines.append(line + '\n')

        if not extra_lines:
            break
        new_content = new_content.rstrip(b'\n') + b'\n'
        for line in sorted(extra_lines):
            new_content += line.encode()

    return new_content


def _sync_include_defconfig(kconf, srcdir, orig, dry_run):
    """Sync a defconfig that uses #include directives

    Computes the overlay delta needed on top of the include files to produce
    the target config, preserving the original line ordering. The result is
    verified against the target config; if any differences remain, the
    missing entries are added iteratively.

    Args:
        kconf (kconfiglib.Kconfig): Kconfig instance
        srcdir (str): Source-tree directory
        orig (str): Path to the original defconfig file
        dry_run (bool): If True, do not update defconfig files

    Returns:
        bool: True if the defconfig was (or would be) updated
    """
    # Get the full min_config and target config values
    full_tmp = _cpp_preprocess(srcdir, orig)
    full_lines = _get_min_config_lines(kconf, full_tmp)
    os.unlink(full_tmp)

    # Save the target config for verification, noting which symbols are
    # visible there (invisible symbols take their value from their
    # dependencies, so never need an explicit entry)
    target = {sym.name: sym.str_value
              for sym in kconf.unique_defined_syms}
    target_visible = {sym.name for sym in kconf.unique_defined_syms
                      if sym.visibility}

    # Build a temp file with just the #include lines (no overlay CONFIGs)
    include_lines = []
    with open(orig, 'rb') as inf:
        for line in inf:
            if line.startswith(b'#include'):
                include_lines.append(line)

    with tempfile.NamedTemporaryFile(suffix='_defconfig',
                                     dir=os.path.dirname(orig)) as tmp:
        tmp.writelines(include_lines)
        tmp.flush()
        base_pp = _cpp_preprocess(srcdir, tmp.name)

    base_entries = _get_defconfig_entries(base_pp)
    kconf.load_config(base_pp)
    base_effective = {sym.name: sym.str_value
                      for sym in kconf.unique_defined_syms}
    os.unlink(base_pp)

    # Build the set of configs needed in the overlay: full_min entries which
    # the include files provide neither textually nor effectively. The
    # textual check covers entries hidden by unmet dependencies when the
    # includes are loaded alone; the effective check covers values the
    # includes produce indirectly, so that entries are not added merely
    # because an include file is not in canonical minimal form. If this
    # wrongly deems an entry provided, the verification pass adds it back
    needed = {}
    full_entries = {}
    for line in full_lines:
        name = _config_name(line)
        if not name:
            continue
        full_entries[name] = line.strip()
        sym_name = name[len('CONFIG_'):]
        if (base_entries.get(name) == line.strip() or
                base_effective.get(sym_name) == target.get(sym_name)):
            continue
        needed[name] = line.strip()

    new_content = _rebuild_overlay(orig, needed, base_entries, full_entries)
    new_content = _verify_defconfig(kconf, srcdir, os.path.dirname(orig),
                                    target, target_visible, needed,
                                    new_content)

    # Overlay entries which simply restate the include files are kept, so
    # any remaining difference is a real change: a stale entry dropped, a
    # value fixed or a missing entry added
    if new_content == tools.read_file(orig):
        return False
    if not dry_run:
        tools.write_file(orig, new_content)
    return True


def _sync_defconfigs_worker(srcdir, defconfigs, result_queue, error_queue,
                            dry_run, ref_srcdir=None):
    """Worker process that syncs defconfigs using kconfiglib

    For each defconfig, loads it via kconfiglib and writes a minimal config
    (equivalent to 'make savedefconfig'), then compares with the original.

    When ref_srcdir is set (the -r option), loads each defconfig against the
    reference Kconfig tree first, writes a full .config, then loads that
    .config into the current tree's Kconfig and writes a minimal config.

    Args:
        srcdir (str): Source-tree directory
        defconfigs (list of str): Defconfig filenames to process
        result_queue (multiprocessing.Queue): Output queue for
            (defconfig, updated) tuples
        error_queue (multiprocessing.Queue): Output queue for failed defconfigs
        dry_run (bool): If True, do not update defconfig files
        ref_srcdir (str or None): Reference source tree for -r option
    """
    os.environ['UBOOTVERSION'] = 'dummy'
    os.environ['KCONFIG_OBJDIR'] = ''
    os.environ['CC'] = 'gcc'

    os.environ['srctree'] = srcdir
    kconf = kconfiglib.Kconfig(warn=False)

    if ref_srcdir:
        os.environ['srctree'] = ref_srcdir
        ref_kconf = kconfiglib.Kconfig(warn=False)
        os.environ['srctree'] = srcdir

    for defconfig in defconfigs:
        orig = os.path.join(srcdir, 'configs', defconfig)
        try:
            if ref_srcdir:
                # Leave #include defconfigs alone: write_min_config() would
                # flatten them, destroying the include structure
                if b'#include' in tools.read_file(orig):
                    result_queue.put((defconfig, False, None))
                    continue

                # Load defconfig against the reference Kconfig tree, write
                # a full .config, then load it into the current tree
                ref_orig = os.path.join(ref_srcdir, 'configs', defconfig)
                if not os.path.exists(ref_orig):
                    ref_orig = orig
                _load_defconfig(ref_kconf, ref_srcdir, ref_orig)
                tmp_config = tempfile.NamedTemporaryFile(
                    prefix='qconfig-cfg-', delete=False)
                tmp_config.close()
                ref_kconf.write_config(tmp_config.name)
                kconf.load_config(tmp_config.name, replace=True)
                os.unlink(tmp_config.name)
            else:
                raw = tools.read_file(orig)
                if b'#include' in raw:
                    updated = _sync_include_defconfig(kconf, srcdir, orig,
                                                      dry_run)
                    result_queue.put((defconfig, updated, None))
                    continue
                _load_defconfig(kconf, srcdir, orig)

            confdir = os.path.dirname(orig)
            tmp = tempfile.NamedTemporaryFile(
                mode='w', prefix='qconfig-', suffix='_defconfig',
                dir=confdir, delete=False)
            tmp.close()
            kconf.write_min_config(tmp.name)

            updated = not filecmp.cmp(orig, tmp.name)
            if updated and not dry_run:
                shutil.move(tmp.name, orig)
            else:
                os.unlink(tmp.name)
            result_queue.put((defconfig, updated, None))
        except Exception as exc:
            error_queue.put((defconfig, str(exc)))


def do_sync_defconfigs(args):
    """Sync defconfig files using kconfiglib instead of make

    Evaluates each defconfig through kconfiglib and writes a minimal config
    (equivalent to 'make savedefconfig'), updating the original if it differs.

    When -r (git-ref) is given, loads each defconfig against the Kconfig tree
    from the reference commit first, then normalises it against the current
    tree, capturing any Kconfig default changes.

    Args:
        args (Namespace): Program arguments (uses jobs, defconfigs,
            defconfiglist, nocolour, dry_run, force_sync, git_ref)

    Returns:
        Progress: progress indicator
    """
    srcdir = os.getcwd()

    if args.git_ref:
        reference_src = ReferenceSource(args.git_ref)
        ref_srcdir = reference_src.get_dir()
    else:
        ref_srcdir = None

    if args.defconfigs:
        defconfigs = [os.path.basename(d)
                      for d in get_matched_defconfigs(args.defconfigs)]
    elif args.defconfiglist:
        defconfigs = [os.path.basename(d)
                      for d in get_matched_defconfigs(args.defconfiglist)]
    else:
        defconfigs = get_all_defconfigs()

    col = terminal.Color(terminal.COLOR_NEVER if args.nocolour
                         else terminal.COLOR_IF_TERMINAL)
    progress = Progress(col, len(defconfigs))

    jobs = args.jobs
    total = len(defconfigs)
    result_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()
    processes = []
    for i in range(jobs):
        chunk = defconfigs[total * i // jobs:total * (i + 1) // jobs]
        if not chunk:
            continue
        proc = multiprocessing.Process(
            target=_sync_defconfigs_worker,
            args=(srcdir, chunk, result_queue, error_queue, args.dry_run,
                  ref_srcdir))
        proc.start()
        processes.append(proc)

    remaining = total
    updated_count = 0
    while remaining:
        found = False
        while not result_queue.empty():
            defconfig, updated, msg = result_queue.get()
            if updated:
                updated_count += 1
                name = defconfig[:-len('_defconfig')]
                log = col.build(col.BLUE, 'defconfig updated', bright=True)
                if args.dry_run:
                    log = col.build(col.YELLOW, 'would update', bright=True)
                print(f'{name.ljust(20)} {log}')
            elif msg:
                name = defconfig[:-len('_defconfig')]
                log = col.build(col.RED, f'ignored: {msg}', bright=True)
                print(f'{name.ljust(20)} {log}')
            progress.inc(True)
            progress.show()
            remaining -= 1
            found = True
        while not error_queue.empty():
            defconfig, msg = error_queue.get()
            print(col.build(col.RED, f'{defconfig}: {msg}', bright=True),
                  file=sys.stderr)
            progress.inc(False)
            progress.show()
            remaining -= 1
            found = True
        if not found:
            time.sleep(SLEEP_TIME)

    for proc in processes:
        proc.join()

    progress.completed()
    if updated_count:
        print(col.build(col.BLUE,
                         f'{updated_count} defconfig(s) updated', bright=True))
    return progress


class ReferenceSource:

    """Reference source against which original configs should be parsed."""

    def __init__(self, commit):
        """Create a reference source directory based on a specified commit.

        Args:
          commit: commit to git-clone
        """
        self.src_dir = tempfile.mkdtemp()
        print('Cloning git repo to a separate work directory...')
        subprocess.check_output(['git', 'clone', os.getcwd(), '.'],
                                cwd=self.src_dir)
        rev = subprocess.check_output(['git', 'rev-parse', '--short',
                                       commit]).strip()
        print(f"Checkout '{rev}' to build the original autoconf.mk.")
        subprocess.check_output(['git', 'checkout', commit],
                                stderr=subprocess.STDOUT, cwd=self.src_dir)

    def __del__(self):
        """Delete the reference source directory

        This function makes sure the temporary directory is cleaned away
        even if Python suddenly dies due to error.  It should be done in here
        because it is guaranteed the destructor is always invoked when the
        instance of the class gets unreferenced.
        """
        shutil.rmtree(self.src_dir)

    def get_dir(self):
        """Return the absolute path to the reference source directory."""

        return self.src_dir

def find_kconfig_rules(kconf, config, imply_config):
    """Check whether a config has a 'select' or 'imply' keyword

    Args:
        kconf (Kconfiglib.Kconfig): Kconfig object
        config (str): Name of config to check (without CONFIG_ prefix)
        imply_config (str): Implying config (without CONFIG_ prefix) which may
            or may not have an 'imply' for 'config')

    Returns:
        Symbol object for 'config' if found, else None
    """
    sym = kconf.syms.get(imply_config)
    if sym:
        for sel, _ in (sym.selects + sym.implies):
            if sel.name == config:
                return sym
    return None

def check_imply_rule(kconf, imply_config):
    """Check if we can add an 'imply' option

    This finds imply_config in the Kconfig and looks to see if it is possible
    to add an 'imply' for 'config' to that part of the Kconfig.

    Args:
        kconf (Kconfiglib.Kconfig): Kconfig object
        imply_config (str): Implying config (without CONFIG_ prefix) which may
            or may not have an 'imply' for 'config')

    Returns:
        tuple:
            str: filename of Kconfig file containing imply_config, or None if
                none
            int: line number within the Kconfig file, or 0 if none
            str: message indicating the result
    """
    sym = kconf.syms.get(imply_config)
    if not sym:
        return 'cannot find sym'
    nodes = sym.nodes
    if len(nodes) != 1:
        return f'{len(nodes)} locations'
    node = nodes[0]
    fname, linenum = node.filename, node.linenr
    cwd = os.getcwd()
    if cwd and fname.startswith(cwd):
        fname = fname[len(cwd) + 1:]
    file_line = f' at {fname}:{linenum}'
    data = read_file(fname)
    if data[linenum - 1] != f'config {imply_config}':
        return None, 0, f'bad sym format {data[linenum]}{file_line})'
    return fname, linenum, f'adding{file_line}'

def add_imply_rule(config, fname, linenum):
    """Add a new 'imply' option to a Kconfig

    Args:
        config (str): config option to add an imply for (without CONFIG_ prefix)
        fname (str): Kconfig filename to update
        linenum (int): Line number to place the 'imply' before

    Returns:
        Message indicating the result
    """
    file_line = f' at {fname}:{linenum}'
    data = read_file(fname)
    linenum -= 1

    for offset, line in enumerate(data[linenum:]):
        if line.strip().startswith('help') or not line:
            data.insert(linenum + offset, f'\timply {config}')
            write_file(fname, data)
            return f'added{file_line}'

    return 'could not insert%s'

(IMPLY_MIN_2, IMPLY_TARGET, IMPLY_CMD, IMPLY_NON_ARCH_BOARD) = (
    1, 2, 4, 8)

IMPLY_FLAGS = {
    'min2': [IMPLY_MIN_2, 'Show options which imply >2 boards (normally >5)'],
    'target': [IMPLY_TARGET, 'Allow CONFIG_TARGET_... options to imply'],
    'cmd': [IMPLY_CMD, 'Allow CONFIG_CMD_... to imply'],
    'non-arch-board': [
        IMPLY_NON_ARCH_BOARD,
        'Allow Kconfig options outside arch/ and /board/ to imply'],
}


def read_database():
    """Read in the config database

    Returns:
        tuple:
            set of all config options seen (each a str)
            set of all defconfigs seen (each a str)
            dict of configs for each defconfig:
                key: defconfig name, e.g. "MPC8548CDS_legacy_defconfig"
                value: dict:
                    key: CONFIG option
                    value: Value of option
            dict of defconfigs for each config:
                key: CONFIG option
                value: set of boards using that option

    """
    if not os.path.exists(CONFIG_DATABASE):
        sys.exit(f"Error: database '{CONFIG_DATABASE}' not found. "
                 f"Did you forget to generate it first with the -b option?")

    configs = {}

    # key is defconfig name, value is dict of (CONFIG_xxx, value)
    config_db = {}

    # Set of all config options we have seen
    all_configs = set()

    # Set of all defconfigs we have seen
    all_defconfigs = set()

    defconfig_db = collections.defaultdict(set)
    defconfig = None
    for line in read_file(CONFIG_DATABASE):
        line = line.rstrip()
        if not line:  # Separator between defconfigs
            config_db[defconfig] = configs
            all_defconfigs.add(defconfig)
            configs = {}
        elif line[0] == ' ':  # CONFIG line
            config, value = line.strip().split('=', 1)
            configs[config] = value
            defconfig_db[config].add(defconfig)
            all_configs.add(config)
        else:  # New defconfig
            defconfig = line

    return all_configs, all_defconfigs, config_db, defconfig_db


def do_imply_config(config_list, add_imply, imply_flags, skip_added,
                    check_kconfig=True, find_superset=False):
    """Find CONFIG options which imply those in the list

    Some CONFIG options can be implied by others and this can help to reduce
    the size of the defconfig files. For example, CONFIG_X86 implies
    CONFIG_CMD_IRQ, so we can put 'imply CMD_IRQ' under 'config X86' and
    all x86 boards will have that option, avoiding adding CONFIG_CMD_IRQ to
    each of the x86 defconfig files.

    This function uses the qconfig database to find such options. It
    displays a list of things that could possibly imply those in the list.
    The algorithm ignores any that start with CONFIG_TARGET since these
    typically refer to only a few defconfigs (often one). It also does not
    display a config with less than 5 defconfigs.

    The algorithm works using sets. For each target config in config_list:
        - Get the set 'defconfigs' which use that target config
        - For each config (from a list of all configs):
            - Get the set 'imply_defconfig' of defconfigs which use that config
            -
            - If imply_defconfigs contains anything not in defconfigs then
              this config does not imply the target config

    Args:
        config_list (list of str): List of CONFIG options to check
        add_imply (bool): Automatically add an 'imply' for each config.
        imply_flags (int): Flags which control which implying configs are allowed
           (IMPLY_...)
        skip_added (bool): Don't show options which already have an imply added.
        check_kconfig (bool): Check if implied symbols already have an 'imply' or
            'select' for the target config, and show this information if so.
        find_superset (bool): True to look for configs which are a superset of those
            already found. So for example if CONFIG_EXYNOS5 implies an option,
            but CONFIG_EXYNOS covers a larger set of defconfigs and also
            implies that option, this will drop the former in favour of the
            latter. In practice this option has not proved very used.

    Note the terminoloy:
        config - a CONFIG_XXX options (a string, e.g. 'CONFIG_CMD_EEPROM')
        defconfig - a defconfig file (a string, e.g. 'configs/snow_defconfig')
    """
    kconf = scan_kconfig() if check_kconfig else None
    if add_imply and add_imply != 'all':
        add_imply = add_imply.split(',')

    all_configs, all_defconfigs, _, defconfig_db = read_database()

    # Work through each target config option in turn, independently
    for config in config_list:
        defconfigs = defconfig_db.get(config)
        if not defconfigs:
            print(f'{config} not found in any defconfig')
            continue

        # Get the set of defconfigs without this one (since a config cannot
        # imply itself)
        non_defconfigs = all_defconfigs - defconfigs
        num_defconfigs = len(defconfigs)
        print(f'{config} found in {num_defconfigs}/{len(all_configs)} defconfigs')

        # This will hold the results: key=config, value=defconfigs containing it
        imply_configs = {}
        rest_configs = all_configs - set([config])

        # Look at every possible config, except the target one
        for imply_config in rest_configs:
            if 'ERRATUM' in imply_config:
                continue
            if not imply_flags & IMPLY_CMD:
                if 'CONFIG_CMD' in imply_config:
                    continue
            if not imply_flags & IMPLY_TARGET:
                if 'CONFIG_TARGET' in imply_config:
                    continue

            # Find set of defconfigs that have this config
            imply_defconfig = defconfig_db[imply_config]

            # Get the intersection of this with defconfigs containing the
            # target config
            common_defconfigs = imply_defconfig & defconfigs

            # Get the set of defconfigs containing this config which DO NOT
            # also contain the taret config. If this set is non-empty it means
            # that this config affects other defconfigs as well as (possibly)
            # the ones affected by the target config. This means it implies
            # things we don't want to imply.
            not_common_defconfigs = imply_defconfig & non_defconfigs
            if not_common_defconfigs:
                continue

            # If there are common defconfigs, imply_config may be useful
            if common_defconfigs:
                skip = False
                if find_superset:
                    for prev in list(imply_configs.keys()):
                        prev_count = len(imply_configs[prev])
                        count = len(common_defconfigs)
                        if (prev_count > count and
                            (imply_configs[prev] & common_defconfigs ==
                            common_defconfigs)):
                            # skip imply_config because prev is a superset
                            skip = True
                            break
                        if count > prev_count:
                            # delete prev because imply_config is a superset
                            del imply_configs[prev]
                if not skip:
                    imply_configs[imply_config] = common_defconfigs

        # Now we have a dict imply_configs of configs which imply each config
        # The value of each dict item is the set of defconfigs containing that
        # config. Rank them so that we print the configs that imply the largest
        # number of defconfigs first.
        ranked_iconfigs = sorted(imply_configs,
                            key=lambda k: len(imply_configs[k]), reverse=True)
        kconfig_info = ''
        cwd = os.getcwd()
        add_list = collections.defaultdict(list)
        for iconfig in ranked_iconfigs:
            num_common = len(imply_configs[iconfig])

            # Don't bother if there are less than 5 defconfigs affected.
            if num_common < (2 if imply_flags & IMPLY_MIN_2 else 5):
                continue
            missing = defconfigs - imply_configs[iconfig]
            missing_str = ', '.join(missing) if missing else 'all'
            missing_str = ''
            show = True
            if kconf:
                sym = find_kconfig_rules(kconf, config[CONFIG_LEN:],
                                         iconfig[CONFIG_LEN:])
                kconfig_info = ''
                if sym:
                    nodes = sym.nodes
                    if len(nodes) == 1:
                        fname, linenum = nodes[0].filename, nodes[0].linenr
                        if cwd and fname.startswith(cwd):
                            fname = fname[len(cwd) + 1:]
                        kconfig_info = f'{fname}:{linenum}'
                        if skip_added:
                            show = False
                else:
                    sym = kconf.syms.get(iconfig[CONFIG_LEN:])
                    fname = ''
                    if sym:
                        nodes = sym.nodes
                        if len(nodes) == 1:
                            fname, linenum = nodes[0].filename, nodes[0].linenr
                            if cwd and fname.startswith(cwd):
                                fname = fname[len(cwd) + 1:]
                    in_arch_board = not sym or (fname.startswith('arch') or
                                                fname.startswith('board'))
                    if (not in_arch_board and
                        not imply_flags & IMPLY_NON_ARCH_BOARD):
                        continue

                    if add_imply and (add_imply == 'all' or
                                      iconfig in add_imply):
                        fname, linenum, kconfig_info = (check_imply_rule(kconf,
                                iconfig[CONFIG_LEN:]))
                        if fname:
                            add_list[fname].append(linenum)

            if show and kconfig_info != 'skip':
                print(f'{num_common:5} : '
                      f'{iconfig.ljust(30)}{kconfig_info.ljust(25)} {missing_str}')

        # Having collected a list of things to add, now we add them. We process
        # each file from the largest line number to the smallest so that
        # earlier additions do not affect our line numbers. E.g. if we added an
        # imply at line 20 it would change the position of each line after
        # that.
        for fname, linenums in add_list.items():
            for linenum in sorted(linenums, reverse=True):
                add_imply_rule(config[CONFIG_LEN:], fname, linenum)

def defconfig_matches(configs, re_match, re_val):
    """Check if any CONFIG option matches a regex

    The match must be complete, i.e. from the start to end of the CONFIG option.

    Args:
        configs (dict): Dict of CONFIG options:
            key: CONFIG option
            value: Value of option
        re_match (re.Pattern): Match to check
        re_val (re.Pattern): Regular expression to check against value (or None)

    Returns:
        bool: True if any CONFIG matches the regex
    """
    for cfg, val in configs.items():
        if re_match.fullmatch(cfg):
            if not re_val or re_val.fullmatch(val):
                return True
    return False

def find_config(dbase, config_list):
    """Find all defconfigs which match a config list

    Args:
        dbase (tuple): Database from read_database()
        config_list (list of str): List of CONFIG options to check (each a regex
            consisting of a config option, with or without a CONFIG_ prefix. If
            an option is preceded by a tilde (~) then it must be false,
            otherwise it must be true)

    Returns:
        set: matching defconfigs, without the '_defconfig' suffix
    """
    # Start with all defconfigs
    _, all_defconfigs, config_db, _ = dbase

    out = all_defconfigs

    # Work through each config in turn
    for item in config_list:
        # Get the real config name and whether we want this config or not
        cfg = item
        want = True
        if cfg[0] == '~':
            want = False
            cfg = cfg[1:]
        val = None
        re_val = None
        if '=' in cfg:
            cfg, val = cfg.split('=', maxsplit=1)
            re_val = re.compile(val)

        # Search everything that is still in the running. If it has a config
        # that we want, or doesn't have one that we don't, add it into the
        # running for the next stage
        in_list = out
        out = set()
        re_match = re.compile(cfg)
        for defc in in_list:
            has_cfg = defconfig_matches(config_db[defc], re_match, re_val)
            if has_cfg == want:
                out.add(defc)

    result = {c.split('_defconfig')[0] for c in out}

    return result

def do_find_config(config_list, list_format, jobs):
    """Find boards with a given combination of CONFIGs

    Rebuilds the database automatically if it is missing or stale.

    Args:
        config_list (list of str): List of CONFIG options to check (each a regex
            consisting of a config option, with or without a CONFIG_ prefix. If
            an option is preceded by a tilde (~) then it must be false,
            otherwise it must be true)
        list_format (bool): True to write in 'list' format, one board name per
            line
        jobs (int): Number of threads to use if the database needs rebuilding

    Returns:
        int: exit code (0 for success)
    """
    dbase = ensure_database(jobs)
    out = find_config(dbase, config_list)
    if not list_format:
        print(f'{len(out)} matches')
    sep = '\n' if list_format else ' '
    print(sep.join(sorted(list(out))))
    return 0


def prefix_config(cfg):
    """Prefix a config with CONFIG_ if needed

    This handles ~ operator, which indicates that the CONFIG should be disabled

    >>> prefix_config('FRED')
    'CONFIG_FRED'
    >>> prefix_config('CONFIG_FRED')
    'CONFIG_FRED'
    >>> prefix_config('~FRED')
    '~CONFIG_FRED'
    >>> prefix_config('~CONFIG_FRED')
    '~CONFIG_FRED'
    >>> prefix_config('A123')
    'CONFIG_A123'
    """
    oper = ''
    if cfg[0] == '~':
        oper = cfg[0]
        cfg = cfg[1:]
    if not cfg.startswith('CONFIG_'):
        cfg = 'CONFIG_' + cfg
    return oper + cfg


# CONFIG options in Makefiles, with an optional $(SPL_)/$(PHASE_) prefix
RE_MK_CONFIGS = re.compile(
    r'CONFIG_(\$\(SPL_(?:TPL_)?\)|\$\(PHASE_\))?([A-Za-z0-9_]*)')

# Makefile ifdefs: this only handles 'else' on its own line, so not
# 'else ifeq ...', for example
RE_IF = re.compile(
    r'^(ifdef|ifndef|endif|ifeq|ifneq|else)([^,]*,([^,]*)\))?.*$')

# Normal CONFIG options in C
RE_C_CONFIGS = re.compile(r'CONFIG_([A-Za-z0-9_]*)')

# CONFIG_IS_ENABLED() construct
RE_CONFIG_IS = re.compile(r'CONFIG_IS_ENABLED\(([A-Za-z0-9_]*)\)')

# Preprocessor #if/#ifdef directives, etc.
RE_IFDEF = re.compile(
    r'^\s*#\s*(ifdef|ifndef|endif|if|elif|else)\s*(?:#.*)?(.*)$')

class ConfigUse:
    """Holds information about a use of a CONFIG option"""
    def __init__(self, cfg, is_spl, conds, fname, rest, is_mk):
        """Set up a new use of a CONFIG option

        Args:
            cfg (str): Config option, without the CONFIG_
            is_spl (bool): True if it indicates an SPL option, i.e. has a
                $(SPL_) or similar, False if not
            conds (list of str): List of conditions for this use, e.g.
                ['SPL_BUILD']
            fname (str): Filename containing the use
            rest (str): Entire line from the file
            is_mk (bool): True if this is in a Makefile, False if not
        """
        self.cfg = cfg
        self.is_spl = is_spl
        self.conds = list(c for c in conds if '(NONE)' not in c)
        self.fname = fname
        self.rest = rest
        self.is_mk = is_mk

    def __hash__(self):
        return hash((self.cfg, self.is_spl))

    def __str__(self):
        return (f'ConfigUse({self.cfg}, is_spl={self.is_spl}, '
                f'is_mk={self.is_mk}, fname={self.fname}, rest={self.rest}')

def scan_makefiles(fname_dict):
    """Scan Makefiles looking for Kconfig options

    Looks for uses of CONFIG options in Makefiles

    Args:
        fname_dict (dict lines):
            key: Makefile filename where the option was found
            value: List of str lines of the Makefile

    Returns:
        tuple:
            dict: all_uses
                key (ConfigUse): object
                value (list of str): matching lines
            dict: Uses by filename
                key (str): filename
                value (set of ConfigUse): uses in that filename

    >>> RE_MK_CONFIGS.search('CONFIG_FRED').groups()
    (None, 'FRED')
    >>> RE_MK_CONFIGS.search('CONFIG_$(SPL_)MARY').groups()
    ('$(SPL_)', 'MARY')
    >>> RE_MK_CONFIGS.search('CONFIG_$(PHASE_)MARY').groups()
    ('$(PHASE_)', 'MARY')
    >>> RE_IF.match('ifdef CONFIG_SPL_BUILD').groups()
    ('ifdef', None, None)
    >>> RE_IF.match('endif # CONFIG_SPL_BUILD').groups()
    ('endif', None, None)
    >>> RE_IF.match('endif').groups()
    ('endif', None, None)
    >>> RE_IF.match('else').groups()
    ('else', None, None)
    """
    all_uses = collections.defaultdict(list)
    fname_uses = {}
    for fname, rest_list in fname_dict.items():
        conds = []
        for rest in rest_list:
            m_iter = RE_MK_CONFIGS.finditer(rest)
            m_cond = RE_IF.match(rest)
            last_use = None
            for m in m_iter:
                real_opt = m.group(2)
                if real_opt == '':
                    continue
                is_spl = False
                if m.group(1):
                    is_spl = True
                use = ConfigUse(real_opt, is_spl, conds, fname, rest, True)
                if fname not in fname_uses:
                    fname_uses[fname] = set()
                fname_uses[fname].add(use)
                all_uses[use].append(rest)
                last_use = use
            if m_cond:
                cfg = last_use.cfg if last_use else '(NONE)'
                cond = m_cond.group(1)
                if cond == 'ifdef':
                    conds.append(cfg)
                elif cond == 'ifndef':
                    conds.append(f'~{cfg}')
                elif cond == 'ifeq':
                    conds.append(f'{m_cond.group(3)}={cfg}')
                elif cond == 'ifneq':
                    conds.append(f'{m_cond.group(3)}={cfg}')
                elif cond == 'endif':
                    # The grep output only contains matching lines, so a
                    # condition may be unbalanced (its opening line did not
                    # match). Ignore an endif with nothing on the stack.
                    if conds:
                        conds.pop()
                elif cond == 'else':
                    if conds:
                        cond = conds.pop()
                        if cond.startswith('~'):
                            cond = cond[1:]
                        else:
                            cond = f'~{cond}'
                        conds.append(cond)
                else:
                    print(f'unknown condition: {rest}')
    return all_uses, fname_uses


def scan_src_files(fname_dict, all_uses, fname_uses):
    """Scan source files (other than Makefiles) looking for Kconfig options

    Looks for uses of CONFIG options

    Args:
        fname_dict (dict):
            key (str): Filename
            value (list of str): List of lines in that filename
        all_uses (dict): to add more uses to:
            key (ConfigUse): object
            value (list of str): matching lines
        fname_uses (dict): to add more uses-by-filename to
            key (str): filename
            value (set of ConfigUse): uses in that filename

    >>> RE_C_CONFIGS.search('CONFIG_FRED').groups()
    ('FRED',)
    >>> RE_CONFIG_IS.search('CONFIG_IS_ENABLED(MARY)').groups()
    ('MARY',)
    >>> RE_CONFIG_IS.search('#if CONFIG_IS_ENABLED(OF_PLATDATA)').groups()
    ('OF_PLATDATA',)
    >>> RE_IFDEF.match('#ifdef CONFIG_SPL_BUILD').groups()
    ('ifdef', 'CONFIG_SPL_BUILD')
    >>> RE_IFDEF.match('#endif # CONFIG_SPL_BUILD').groups()
    ('endif', '')
    >>> RE_IFDEF.match('#endif').groups()
    ('endif', '')
    >>> RE_IFDEF.match('#else').groups()
    ('else', '')
    """
    fname = None
    rest = None

    def add_uses(m_iter, is_spl):
        last_use = None
        for mat in m_iter:
            real_opt = mat.group(1)
            if real_opt == '':
                continue
            use = ConfigUse(real_opt, is_spl, conds, fname, rest, False)
            if fname not in fname_uses:
                fname_uses[fname] = set()
            fname_uses[fname].add(use)
            all_uses[use].append(rest)
            last_use = use
        return last_use

    for fname, rest_list in fname_dict.items():
        conds = []
        for rest in rest_list:
            m_iter = RE_C_CONFIGS.finditer(rest)
            m_cond = RE_IFDEF.match(rest)
            last_use1 = add_uses(m_iter, False)

            m_iter2 = RE_CONFIG_IS.finditer(rest)
            last_use2 = add_uses(m_iter2, True)
            last_use = last_use1 or last_use2
            if m_cond:
                cfg = last_use.cfg if last_use else '(NONE)'
                cond = m_cond.group(1)
                if cond == 'ifdef':
                    conds.append(cfg)
                elif cond == 'ifndef':
                    conds.append(f'~{cfg}')
                elif cond == 'if':
                    conds.append(f'{m_cond.group(2)}={cfg}')
                elif cond == 'endif':
                    # The grep output only contains matching lines, so a
                    # condition may be unbalanced (its opening #if did not
                    # match). Ignore an #endif with nothing on the stack.
                    if conds:
                        conds.pop()
                elif cond == 'else':
                    if conds:
                        cond = conds.pop()
                        if cond.startswith('~'):
                            cond = cond[1:]
                        else:
                            cond = f'~{cond}'
                        conds.append(cond)
                elif cond == 'elif':
                    if conds:
                        conds.pop()
                    conds.append(cfg)
                else:
                    print(f'{fname}: unknown condition: {rest}')


MODE_NORMAL, MODE_SPL, MODE_PROPER = range(3)

def do_scan_source(path, do_update, do_update_source, show_conflicts,
                   do_commit):
    """Scan the source tree for Kconfig inconsistencies

    Args:
        path (str): Path to source tree
        do_update (bool): True to write to scripts/conf_nospl file
        do_update_source (bool): True to update source to remove invalid use of
           CONFIG_IS_ENABLED()
        show_conflicts (bool): True to show conflicts between the source code
           and the Kconfig (such as missing options, or options which imply an
           SPL option that does not exist)
        do_commit (bool): Create commits to remove use of SPL_TPL_ and
           CONFIG_IS_ENABLED macros when there is no SPL symbol.
    """
    def is_not_proper(name):
        for prefix in SPL_PREFIXES:
            if name.startswith(prefix):
                return name[len(prefix):]
        return False

    def check_not_found(all_uses, spl_mode):
        """Check for Kconfig options mentioned in the source but not in Kconfig

        Args:
            all_uses (dict):
                key (ConfigUse): object
                value (list of str): matching lines
            spl_mode (int): If MODE_SPL, look at source code which implies
                an xPL_ option, but for which there is none;
                for MOD_PROPER, look at source code which implies a Proper
                option (i.e. use of CONFIG_IS_ENABLED() or $(PHASE_) but for
                which there none;
                if MODE_NORMAL, ignore SPL

        Returns:
            dict:
                key (str): CONFIG name (without 'CONFIG_' prefix
                value (list of ConfigUse): List of uses of this CONFIG
        """
        # Make sure we know about all the options
        not_found = collections.defaultdict(list)
        for use, _ in all_uses.items():
            name = use.cfg
            if name in IGNORE_SYMS:
                continue
            check = True

            if spl_mode == MODE_SPL:
                check = use.is_spl

                # If it is an SPL symbol, try prepending all xPL_ prefixes to
                # find at least one SPL symbol
                if use.is_spl:
                    for prefix in SPL_PREFIXES:
                        try_name = prefix + name
                        sym = kconf.syms.get(try_name)
                        if sym:
                            break
                    if not sym:
                        not_found[f'SPL_{name}'].append(use)
                    continue
            elif spl_mode == MODE_PROPER:
                # Try to find the Proper version of this symbol, i.e. without
                # the xPL_ prefix
                proper_name = is_not_proper(name)
                if proper_name:
                    name = proper_name
                elif not use.is_spl:
                    check = False
            else: # MODE_NORMAL
                sym = kconf.syms.get(name)
                if not sym:
                    proper_name = is_not_proper(name)
                    if proper_name:
                        name = proper_name
                    sym = kconf.syms.get(name)
                if not sym:
                    for prefix in SPL_PREFIXES:
                        try_name = prefix + name
                        sym = kconf.syms.get(try_name)
                        if sym:
                            break
                if not sym:
                    not_found[name].append(use)
                continue

            sym = kconf.syms.get(name)
            if not sym and check:
                not_found[name].append(use)
        return not_found

    def show_uses(uses):
        """Show a list of uses along with their filename and code snippet

        Args:
            uses (dict):
                key (str): CONFIG name (without 'CONFIG_' prefix
                value (list of ConfigUse): List of uses of this CONFIG
        """
        for name in sorted(uses):
            print(f'{name}: ', end='')
            for i, use in enumerate(uses[name]):
                print(f'{"   " if i else ""}{use.fname}: {use.rest.strip()}')


    def do_scan():
        """Scan the source tree

        This runs a 'git grep' and then picks out uses of CONFIG options from
        the resulting output

        Returns: tuple
            dict of uses of CONFIG in Makefiles:
                key (str): Filename
                value (list of str): List of lines in that filename
            dict of uses of CONFIG in source files:
                key (str): Filename
                value (list of str): List of lines in that filename
        """
        def finish_file(last_fname, rest_list):
            """Add the collected lines for a file to the right dict

            Args:
                last_fname (str or None): Filename the lines belong to, or
                    None if at the start of the scan
                rest_list (list of str): Grep output lines for the file
            """
            if last_fname is None:
                return
            if is_mkfile:
                mk_dict[last_fname] = rest_list
            else:
                src_dict[last_fname] = rest_list

        print(f'Scanning source in {path}')
        args = ['git', 'grep', '-E', r'IS_ENABLED|\bCONFIG|if|else|endif']
        with subprocess.Popen(args, stdout=subprocess.PIPE) as proc:
            out, _ = proc.communicate()
        lines = out.splitlines()
        re_fname = re.compile('^([^:]*):(.*)')
        src_dict = collections.OrderedDict()
        mk_dict = collections.OrderedDict()
        last_fname = None
        rest_list = []
        is_mkfile = None
        for line in lines:
            linestr = line.decode('utf-8')
            m_fname = re_fname.search(linestr)
            if not m_fname:
                continue
            fname, rest = m_fname.groups()
            if fname != last_fname:
                finish_file(last_fname, rest_list)
                rest_list = []
                last_fname = fname

            dirname, leaf = os.path.split(fname)
            root, ext = os.path.splitext(leaf)
            if (dirname.startswith('test/') or
                dirname.startswith('lib/efi_selftest')):
                # Ignore test code since it is mostly only for sandbox
                pass
            elif ext == '.autoconf':
                pass
            elif ext in ['.c', '.h', '.S', '.lds', '.dts', '.dtsi', '.asl',
                         '.cfg', '.env', '.tmpl']:
                rest_list.append(rest)
                is_mkfile = False
            elif 'Makefile' in root or ext == '.mk':
                rest_list.append(rest)
                is_mkfile = True
            elif ext in ['.yml', '.sh', '.py', '.awk', '.pl', '.rst', '',
                         '.sed', '.src', '.inc', '.l', '.i_shipped', '.txt',
                         '.cmd', '.y', '.cocci', '.ini', '.asn1', '.base',
                         '.cnf', '.patch', '.mak', '.its', '.svg', '.tcl',
                         '.css', '.config', '.conf', '.yaml', '.dtso', '.key',
                         '.pem', '.toml', '.in']:
                pass
            elif 'Kconfig' in root or 'Kbuild' in root:
                pass
            elif 'README' in root:
                pass
            elif dirname in ['configs']:
                pass
            elif (dirname.startswith('doc') or
                  dirname.startswith('scripts/kconfig')):
                pass
            elif dirname.startswith('lib/mbedtls/external'):
                pass
            elif dirname.startswith('lib/lwip/lwip'):
                pass
            else:
                print(f'Not sure how to handle file {fname}')
        finish_file(last_fname, rest_list)
        return mk_dict, src_dict

    def check_missing(all_uses, spl_not_found, proper_not_found,
                      show_conflicts):
        """Check for missing Kconfig options and conflicting uses

        Args:
            all_uses (dict): All uses of CONFIG options:
                key (ConfigUse): Use of the option
                value (list of str): Lines using the option
            spl_not_found (set of str): Updated with options which are used
                in an SPL context but have no SPL_ Kconfig
            proper_not_found (set of str): Updated with options which are
                used in a Proper context but have no Kconfig
            show_conflicts (bool): True to show the missing options
        """
        # Make sure we know about all the options used
        not_found = check_not_found(all_uses, MODE_NORMAL)
        if show_conflicts:
            print('\nCONFIG options present in source but not Kconfig:')
            show_uses(not_found)

        not_found = check_not_found(all_uses, MODE_SPL)
        spl_not_found |= set(is_not_proper(key) or key
                             for key in not_found.keys())
        if show_conflicts:
            print('\nCONFIG options present in source but not Kconfig (SPL):')
            show_uses(not_found)

        not_found = check_not_found(all_uses, MODE_PROPER)
        proper_not_found |= set(key for key in not_found.keys())
        if show_conflicts:
            print('\nCONFIG options used as Proper in source but without a '
                  'non-SPL_ variant:')
            show_uses(not_found)

    def show_summary(spl_not_found, proper_not_found):
        """Show a summary of the missing Kconfig options

        Args:
            spl_not_found (set of str): Options which are used in an SPL
                context but have no SPL_ Kconfig
            proper_not_found (set of str): Options which are used in a
                Proper context but have no Kconfig
        """
        print('\nCONFIG options used as SPL but without an SPL_ variant:')
        for item in sorted(spl_not_found):
            print(f'   {item}')

        print('\nCONFIG options used as Proper but without a non-SPL_ variant:')
        for item in sorted(proper_not_found):
            print(f'   {item}')

    def write_update(spl_not_found):
        """Write the updated scripts/conf_nospl file

        Args:
            spl_not_found (set of str): Options which are used in an SPL
                context but have no SPL_ Kconfig
        """
        with open(os.path.join(path, 'scripts', 'conf_nospl'), 'w',
                  encoding='utf-8') as out:
            print('''# Options which are never enabled in SPL.

Generally, options which have no SPL_ prefix (e.g. CONFIG_FOO) apply to all
SPL build phases. This allows things like ARCH_ARM to propagate to all builds
without the hassle of generating a separate SPL version for each phase. But in
some cases this is not wanted.

This file lists options which don't have an SPL equivalent, but still should not
be enabled in SPL builds. It is necessary since kconfig cannot tell (just by
looking at the Kconfig description) whether it applies to Proper builds only,
or to all builds.
''',
                  file=out)
            for item in sorted(spl_not_found):
                print(item, file=out)

    def check_conflict(kconf, all_uses, show):
        """Check conflicts between uses of CONFIG options in source

        Sometimes an option is used in an SPL context in one place and not in
        another. For example if we see CONFIG_SPL_FOO and CONFIG_FOO then we
        don't know whether FOO should be enabled in SPL if CONFIG_FOO is
        enabled, or only if CONFIG_SPL_FOO is enabled.

        This function detects such situations

        Args:
            kconf (Kconfiglib.Kconfig): Kconfig tree read from source
            all_uses (dict): dict to add more uses to:
                key (ConfigUse): object
                value (list of str): matching lines
            show (bool): True to show the problems

        Returns:
            tuple:
                dict of conflicts:
                    key: filename
                    value: list of ConfigUse objects
                dict of conflicts by config:
                    key: config name
                    value: list of ConfigUse objects
        """
        conflict_dict = collections.defaultdict(list)
        cfg_dict = collections.defaultdict(list)

        uses = collections.defaultdict(list)
        for use in all_uses:
            uses[use.cfg].append(use)

        bad = 0
        for cfg in sorted(uses):
            use_list = uses[cfg]

            # Check if
            #  - there is no SPL_ version of the symbol, and
            #  - some uses are SPL and some are not
            is_spl = list(set(u.is_spl for u in use_list))
            if len(is_spl) > 1 and f'SPL_{cfg}' not in kconf.syms:
                if show:
                    print(f'{cfg}: {is_spl}')
                for use in use_list:
                    if show:
                        print(f'   spl={use.is_spl}: {use.conds} {use.fname} '
                              f'{use.rest}')
                    if use.is_spl:
                        conflict_dict[use.fname].append(use)
                        cfg_dict[use.cfg].append(use)
                bad += 1
        return conflict_dict, cfg_dict

    def replace_in_file(fname, use_or_uses):
        """Replace a CONFIG pattern in a file

        Args:
            fname (str): Filename to replace in
            use_or_uses (ConfigUse or list of ConfigUse): Uses to replace
        """
        with open(fname, encoding='utf-8') as inf:
            data = inf.read()
        out = io.StringIO()

        if isinstance(use_or_uses, list):
            uses = use_or_uses
        else:
            uses = [use_or_uses]

        pairs = []
        for use in uses:
            if use.is_mk:
                expr = (r'CONFIG_(?:\$\(SPL_(?:TPL_)?\)|\$\(PHASE_\))'
                        fr'{use.cfg}\b')
            else:
                expr = r'CONFIG_IS_ENABLED\(%s\)' % use.cfg
            pairs.append((re.compile(expr), use))

        for line in data.splitlines():
            new_line = line
            if 'CONFIG_' in line:
                todo = []
                for re_cfg, use in pairs:
                    if not re_cfg.search(line):
                        todo.append((re_cfg, use))
                    if use.is_mk:
                        new_line = re_cfg.sub(f'CONFIG_{use.cfg}', new_line)
                    else:
                        new = f'IS_ENABLED(CONFIG_{use.cfg})'
                        new_line = re_cfg.sub(new, new_line)
                pairs = todo
            out.write(new_line)
            out.write('\n')
        out_data = out.getvalue()
        if out_data == data:
            print(f'warning: no replacement done in {fname}')
        with open(fname, 'w', encoding='utf-8') as outf:
            outf.write(out_data)

    def update_source(conflicts):
        """Replace any SPL constructs with plain non-SPL ones

        CONFIG_IS_ENABLED(FOO) becomes IS_ENABLED(CONFIG_FOO)
        CONFIG_$(SPL_TPL_)FOO becomes CONFIG_FOO

        Args:
            conflicts (dict): dict of conflicts:
                key (str): filename
                value (list of ConfigUse): uses within that file
        """
        print(f'Updating {len(conflicts)} files')
        for fname, uses in conflicts.items():
            replace_in_file(fname, uses)

    def create_commits(cfg_dict):
        """Create a set of commits to fix SPL conflicts

        Args:
            cfg_dict (dict): dict of conflicts by config:
                key (str): config name
                value (list of ConfigUse): uses of that config
        """
        print(f'Creating {len(cfg_dict)} commits')
        for cfg, uses in cfg_dict.items():
            print(f'Processing {cfg}')
            for use in uses:
                replace_in_file(use.fname, use)
            plural = 's' if len(uses) > 1 else ''
            msg = f'Correct SPL use{plural} of {cfg}'
            msg += f'\n\nThis converts {len(uses)} usage{plural} '
            msg += 'of this option to the non-SPL form, since there is\n'
            msg += f'no SPL_{cfg} defined in Kconfig'
            if not write_commit(msg):
                print('failed\n', uses)
                break

    print('Scanning Kconfig')
    kconf = scan_kconfig()

    mk_dict, src_dict = do_scan()

    # Scan the Makefiles and source code
    all_uses, fname_uses = scan_makefiles(mk_dict)
    scan_src_files(src_dict, all_uses, fname_uses)

    conflicts, cfg_dict = check_conflict(kconf, all_uses, show_conflicts)

    if do_update_source:
        update_source(conflicts)
    if do_commit:
        create_commits(cfg_dict)

    # Look for CONFIG options that are not found
    spl_not_found = set()
    proper_not_found = set()
    check_missing(all_uses, spl_not_found, proper_not_found, show_conflicts)

    show_summary(spl_not_found, proper_not_found)

    # Write out the updated information
    if do_update:
        write_update(spl_not_found)


def parse_args():
    """Parse the program arguments

    Returns:
        tuple:
            argparse.ArgumentParser: parser
            argparse.Namespace: Parsed arguments
    """
    try:
        cpu_count = multiprocessing.cpu_count()
    except NotImplementedError:
        cpu_count = 1

    epilog = '''Move config options from headers to defconfig files. See
doc/develop/qconfig.rst for documentation.'''

    parser = ArgumentParser(epilog=epilog)
    # Add arguments here
    parser.add_argument('-a', '--add-imply', type=str, default='',
                      help='comma-separated list of CONFIG options to add '
                      "an 'imply' statement to for the CONFIG in -i")
    parser.add_argument('-A', '--skip-added', action='store_true', default=False,
                      help="don't show options which are already marked as "
                      'implying others')
    parser.add_argument('-b', '--build-db', action='store_true', default=False,
                      help='build a CONFIG database')
    parser.add_argument('-C', '--commit', action='store_true', default=False,
                      help='Create a git commit for the operation')
    parser.add_argument('--nocolour', action='store_true', default=False,
                      help="don't display the log in colour")
    parser.add_argument('-d', '--defconfigs', type=str,
                      help='a file containing a list of defconfigs to move, '
                      "one per line (for example 'snow_defconfig') "
                      "or '-' to read from stdin")
    parser.add_argument('-D', '--defconfiglist', type=str, nargs='*',
                        help='list of defconfigs to move')
    parser.add_argument('-e', '--exit-on-error', action='store_true',
                      default=False,
                      help='exit immediately on any error')
    parser.add_argument('-f', '--find', action='store_true', default=False,
                      help='Find boards with a given config combination')
    parser.add_argument('-i', '--imply', action='store_true', default=False,
                      help='find options which imply others')
    parser.add_argument('-l', '--list', action='store_true', default=False,
                      help='Show a sorted list of board names, one per line')
    parser.add_argument('-I', '--imply-flags', type=str, default='',
                      help="control the -i option ('help' for help")
    parser.add_argument('-j', '--jobs', type=int, default=cpu_count,
                      help='the number of jobs to run simultaneously')
    parser.add_argument('-L', '--list-problems', action='store_true',
                        help='list problems found with --scan-source')
    parser.add_argument('-n', '--dry-run', action='store_true', default=False,
                      help='perform a trial run (show log with no changes)')
    parser.add_argument('-r', '--git-ref', type=str,
                      help='the git ref to clone for building the autoconf.mk')
    parser.add_argument('-s', '--force-sync', action='store_true', default=False,
                      help='force sync by savedefconfig')
    parser.add_argument('-S', '--spl', action='store_true', default=False,
                      help='parse config options defined for SPL build')
    parser.add_argument('--scan-source', action='store_true', default=False,
                      help='scan source for uses of CONFIG options')
    parser.add_argument('-t', '--test', action='store_true', default=False,
                      help='run unit tests')
    parser.add_argument('-y', '--yes', action='store_true', default=False,
                      help="respond 'yes' to any prompts")
    parser.add_argument('-u', '--update', action='store_true', default=False,
                      help="update scripts/ files (use with --scan-source)")
    parser.add_argument('-U', '--update-source', action='store_true',
                      help="update source code (use with --scan-source)")
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                      help='show any build errors as boards are built')
    parser.add_argument('configs', nargs='*')

    args = parser.parse_args()
    if not any((args.force_sync, args.build_db, args.imply, args.find,
                args.scan_source, args.test)):
        parser.print_usage()
        sys.exit(1)

    return parser, args


def imply(args):
    """Handle checking for flags which imply others

    Args:
        args (argparse.Namespace): Program arguments

    Returns:
        int: exit code (0 for success)
    """
    imply_flags = 0
    if args.imply_flags == 'all':
        imply_flags = -1

    elif args.imply_flags:
        for flag in args.imply_flags.split(','):
            bad = flag not in IMPLY_FLAGS
            if bad:
                print(f"Invalid flag '{flag}'")
            if flag == 'help' or bad:
                print("Imply flags: (separate with ',')")
                for name, info in IMPLY_FLAGS.items():
                    print(f' {name.ljust(15)}: {info[1]}')
                return 1
            imply_flags |= IMPLY_FLAGS[flag][0]

    do_imply_config(args.configs, args.add_imply, imply_flags, args.skip_added)
    return 0


def write_commit(msg):
    """Create a new commit with all modified files

    Args:
        msg (str): Commit message

    Returns:
        bool: True if the commit was created successfully
    """
    subprocess.call(['git', 'add', '-u'])
    rc = subprocess.call(['git', 'commit', '-s', '-m', msg])
    return rc == 0


def add_commit(configs):
    """Add a commit indicating which CONFIG options were converted

    Args:
        configs (list of str) List of CONFIG_... options to process
    """
    subprocess.call(['git', 'add', '-u'])
    if configs:
        part = 'et al ' if len(configs) > 1 else ''
        msg = f'Convert {configs[0]} {part}to Kconfig'
        msg += ('\n\nThis converts the following to Kconfig:\n   %s\n' %
                '\n   '.join(configs))
    else:
        msg = 'configs: Resync with savedefconfig'
        msg += '\n\nResync all defconfig files using qconfig.py'
    subprocess.call(['git', 'commit', '-s', '-m', msg])


def write_db(config_db, progress):
    """Write the database to a file

    Args:
        config_db (dict of dict): configs for each defconfig
            key: defconfig name, e.g. "MPC8548CDS_legacy_defconfig"
            value: dict:
                key: CONFIG option
                value: Value of option
        progress (Progress): Progress indicator.

    Returns:
        int: exit code (0 for success)
    """
    col = progress.col
    with open(CONFIG_DATABASE, 'w', encoding='utf-8') as outf:
        for defconfig, configs in config_db.items():
            outf.write(f'{defconfig}\n')
            for config in sorted(configs.keys()):
                outf.write(f'   {config}={configs[config]}\n')
            outf.write('\n')
    print(col.build(
        col.RED if progress.failed else col.GREEN,
        f'{progress.failure_msg}{len(config_db)} boards written to {CONFIG_DATABASE}'))
    return 0


def move_done(progress):
    """Write a message indicating that the move is done

    Args:
        progress (Progress): Progress indicator.

    Returns:
        int: exit code (0 for success)
    """
    col = progress.col
    if progress.failed:
        if progress.good:
            tprint(f'{progress.good} OK, ', newline=False, colour=col.GREEN)
        tprint(f'{progress.failure_msg}see {FAILED_LIST}', colour=col.RED)
    else:
        # Add enough spaces to overwrite the progress indicator
        print(col.build(
            col.GREEN, f'{progress.total} processed        ', bright=True))
    return 0

class SyncTests(unittest.TestCase):
    """Tests for defconfig sync using kconfiglib"""

    @classmethod
    def setUpClass(cls):
        """Create a shared Kconfig instance for all tests"""
        os.environ['srctree'] = os.getcwd()
        os.environ['UBOOTVERSION'] = 'dummy'
        os.environ['KCONFIG_OBJDIR'] = ''
        os.environ['CC'] = 'gcc'
        cls.kconf = kconfiglib.Kconfig(warn=False)
        cls.srcdir = os.getcwd()

    def test_sync_plain_noop(self):
        """Syncing an already-minimal defconfig produces no change"""
        # sandbox_defconfig should already be synced if the tree is clean
        orig = 'configs/sandbox_defconfig'
        updated = _sync_plain_defconfig(self.kconf, orig, dry_run=True)
        # This may or may not be updated depending on tree state, but
        # it should not crash
        self.assertIsInstance(updated, bool)

    def test_sync_include_preserves_structure(self):
        """Syncing a #include defconfig preserves the #include lines"""
        orig = 'configs/sandbox_nocmdline_defconfig'
        if not os.path.exists(orig):
            self.skipTest(f'{orig} not found')

        # Dry-run should not modify the file
        content_before = tools.read_file(orig)
        updated = _sync_include_defconfig(self.kconf, self.srcdir, orig,
                                          dry_run=True)
        content_after = tools.read_file(orig)
        self.assertEqual(content_before, content_after)

        # The output should still start with #include
        self.assertIn(b'#include', content_after)

    def test_sync_include_skips_redundant(self):
        """Syncing a #include defconfig keeps include-redundant entries"""
        # Pick an option which sandbox_defconfig sets explicitly, so the
        # overlay entry simply restates the include
        with open('configs/sandbox_defconfig', encoding='utf-8') as inf:
            explicit = next(line for line in inf
                            if line.startswith('CONFIG_') and
                            line.rstrip().endswith('=y'))
        with tempfile.NamedTemporaryFile(
                mode='w', prefix='test-', suffix='_defconfig',
                dir='configs', delete=False) as tmp:
            tmp.write('#include "sandbox_defconfig"\n')
            tmp.write(explicit)
            tmp_name = tmp.name
        try:
            updated = _sync_include_defconfig(self.kconf, self.srcdir,
                                              tmp_name, dry_run=False)
            # An entry which restates the include is harmless, so the file
            # should not be rewritten just to drop it
            self.assertFalse(updated)
            with open(tmp_name) as inf:
                result = inf.read()
            # File should be unchanged
            self.assertIn(explicit.strip(), result)
            self.assertIn('#include "sandbox_defconfig"', result)
        finally:
            os.unlink(tmp_name)

    def test_sync_include_drops_stale(self):
        """Syncing a #include defconfig removes stale options"""
        with tempfile.NamedTemporaryFile(
                mode='w', prefix='test-', suffix='_defconfig',
                dir='configs', delete=False) as tmp:
            tmp.write('#include "sandbox_defconfig"\n')
            tmp.write('# CONFIG_CMDLINE is not set\n')
            tmp.write('CONFIG_NON_EXISTENT_OPTION=y\n')
            tmp_name = tmp.name
        try:
            updated = _sync_include_defconfig(self.kconf, self.srcdir,
                                              tmp_name, dry_run=False)
            # The stale option must be dropped, with the real override and
            # the include structure preserved
            self.assertTrue(updated)
            with open(tmp_name) as inf:
                result = inf.read()
            self.assertNotIn('CONFIG_NON_EXISTENT_OPTION', result)
            self.assertIn('# CONFIG_CMDLINE is not set', result)
            self.assertIn('#include "sandbox_defconfig"', result)
        finally:
            os.unlink(tmp_name)

    def test_sync_include_keeps_override(self):
        """Syncing a #include defconfig keeps CONFIGs that differ from base"""
        # Create a temp defconfig that includes sandbox and disables CMDLINE
        with tempfile.NamedTemporaryFile(
                mode='w', prefix='test-', suffix='_defconfig',
                dir='configs', delete=False) as tmp:
            tmp.write('#include "sandbox_defconfig"\n')
            tmp.write('# CONFIG_CMDLINE is not set\n')
            tmp_name = tmp.name
        try:
            _sync_include_defconfig(self.kconf, self.srcdir, tmp_name,
                                    dry_run=False)
            with open(tmp_name) as inf:
                result = inf.read()
            # Disabling CMDLINE is an override — should be kept
            self.assertIn('# CONFIG_CMDLINE is not set', result)
            self.assertIn('#include "sandbox_defconfig"', result)
        finally:
            os.unlink(tmp_name)

    def test_sync_include_effective_config(self):
        """Syncing a #include defconfig must not change the effective config"""
        orig = 'configs/alt_defconfig'
        if not os.path.exists(orig):
            self.skipTest(f'{orig} not found')

        # Load the original to get the target config
        full_pp = _cpp_preprocess(self.srcdir, orig)
        self.kconf.load_config(full_pp)
        os.unlink(full_pp)
        target = {sym.name: sym.str_value
                  for sym in self.kconf.unique_defined_syms}

        # Sync into a temp copy
        tmp_name = orig + '.test_tmp'
        shutil.copy2(orig, tmp_name)
        try:
            _sync_include_defconfig(self.kconf, self.srcdir, tmp_name,
                                    dry_run=False)

            # Load synced defconfig and compare
            synced_pp = _cpp_preprocess(self.srcdir, tmp_name)
            self.kconf.load_config(synced_pp)
            os.unlink(synced_pp)
            result = {sym.name: sym.str_value
                      for sym in self.kconf.unique_defined_syms}

            diffs = {name for name in set(target) | set(result)
                     if target.get(name) != result.get(name)}
            self.assertEqual(diffs, set(),
                             'Synced defconfig changed effective config')
        finally:
            os.unlink(tmp_name)


def do_tests():
    """Run doctests and unit tests"""
    sys.argv = [sys.argv[0]]
    fail, _ = doctest.testmod()
    if fail:
        return 1
    unittest.main()
    return 0


def db_is_current():
    """Check if the CONFIG database is up to date

    Returns:
        bool: True if the database exists and is newer than all Kconfig and
            defconfig files
    """
    if not os.path.exists(CONFIG_DATABASE):
        return False

    db_time = os.path.getctime(CONFIG_DATABASE)

    for dirpath, _, filenames in os.walk('configs'):
        for fname in fnmatch.filter(filenames, '*_defconfig'):
            if db_time < os.path.getctime(os.path.join(dirpath, fname)):
                return False

    for dirpath, _, filenames in os.walk('.'):
        for fname in filenames:
            if fname.startswith('Kconfig'):
                if db_time < os.path.getctime(os.path.join(dirpath, fname)):
                    return False

    return True


def ensure_database(threads):
    """Return a qconfig database, rebuilding it if stale or missing

    Checks whether the database is newer than all Kconfig and defconfig files.
    If not, it is rebuilt automatically.

    Args:
        threads (int): Number of threads to use when processing

    Returns:
        tuple:
            set of all config options seen (each a str)
            set of all defconfigs seen (each a str)
            dict of configs for each defconfig:
                key: defconfig name, e.g. "MPC8548CDS_legacy_defconfig"
                value: dict:
                    key: CONFIG option
                    value: Value of option
            dict of defconfigs for each config:
                key: CONFIG option
                value: set of boards using that option
    """
    if not db_is_current():
        print(f'Building {CONFIG_DATABASE} database...')
        args = Namespace(build_db=True, verbose=False, force_sync=False,
                         dry_run=False, exit_on_error=False, jobs=threads,
                         git_ref=None, defconfigs=None, defconfiglist=None,
                         nocolour=False)
        config_db, progress = do_build_db(args)

        write_db(config_db, progress)

    return read_database()


def main():
    """Main program"""
    parser, args = parse_args()
    check_top_directory()

    # prefix the option name with CONFIG_ if missing
    args.configs = [prefix_config(cfg) for cfg in args.configs]

    if args.test:
        return do_tests()
    if args.scan_source:
        do_scan_source(os.getcwd(), args.update, args.update_source,
                       args.list_problems, args.commit)
        return 0
    if args.imply:
        if imply(args):
            parser.print_usage()
            sys.exit(1)
        return 0
    if args.find:
        return do_find_config(args.configs, args.list, args.jobs)

    if args.build_db:
        config_db, progress = do_build_db(args)
        return write_db(config_db, progress)

    if args.force_sync:
        progress = do_sync_defconfigs(args)
        if args.commit:
            add_commit(args.configs)
        return move_done(progress)

    parser.print_usage()
    return 1


if __name__ == '__main__':
    sys.exit(main())
