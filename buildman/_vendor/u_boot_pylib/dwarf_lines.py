# SPDX-License-Identifier: GPL-2.0
#
# Copyright 2025 Canonical Ltd
#
"""DWARF-based extraction of the source lines compiled into a build.

This reads the line tables from the DWARF debug info in the object files of a
build and works out which lines of each source file generated code. It is
shared by codman (multi-board code analysis) and buildman (per-commit build
footprint), so the two tools produce comparable data.

The extraction works on '.o' files (not the linked image), so it sees every
line that was compiled, including code later dropped by the linker. The build
must be made without LTO, since with LTO the object files hold GIMPLE rather
than machine code and carry no useful line table.
"""

import functools
import multiprocessing
import os
import shutil
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor


@functools.lru_cache(maxsize=1)
def _get_readelf():
    """Return the readelf executable, resolved to an absolute path

    The result is cached, so the PATH search happens only once per process.
    Spawning readelf per object file thousands of times otherwise repeats the
    PATH search every time, which adds up.

    Returns:
        str: Absolute path to readelf, or 'readelf' if it is not on the PATH
            (in which case the subprocess call will fail as before)
    """
    return shutil.which('readelf') or 'readelf'


# Build directories holding the objects for each xPL phase
XPL_DIRS = ('spl', 'tpl', 'vpl')


def _resolve_header(header_path, obj_dir, srcdir, cache):
    """Resolve a source path from a readelf section header to a real file

    DWARF records source paths as absolute, or relative to the compile
    directory. The same headers (especially shared headers) recur across
    thousands of object files, so results are memoised in cache to avoid
    repeating the realpath()/exists() filesystem calls.

    Args:
        header_path (str): Source path as printed by readelf (no trailing ':')
        obj_dir (str): Directory of the object file, relative to the build dir
        srcdir (str): Path to the source root directory
        cache (dict): Memoisation cache keyed by (header_path, obj_dir); shared
            across the workers of a single extract_lines() call

    Returns:
        str: Absolute path to the source file, or None if it does not exist
    """
    key = (header_path, obj_dir)
    if key in cache:
        return cache[key]
    if os.path.isabs(header_path):
        # Absolute path in DWARF
        abs_path = os.path.realpath(header_path)
    else:
        # Relative path - try relative to srcdir and obj_dir
        abs_path = os.path.realpath(os.path.join(srcdir, obj_dir, header_path))
        if not os.path.exists(abs_path):
            # xPL objects are built under a directory named after the phase,
            # e.g. spl/common/spl/spl.o, but their source is common/spl/spl.c
            parts = obj_dir.split(os.sep, 1)
            if len(parts) == 2 and parts[0] in XPL_DIRS:
                abs_path = os.path.realpath(os.path.join(srcdir, parts[1],
                                                         header_path))
        if not os.path.exists(abs_path):
            abs_path = os.path.realpath(os.path.join(srcdir, header_path))
    result = abs_path if os.path.exists(abs_path) else None
    cache[key] = result
    return result


def worker(args):
    """Extract line numbers from DWARF debug info in an object file.

    Uses readelf --debug-dump=decodedline to get the line table, then parses
    section headers and line entries to determine which source lines were
    compiled into the object.

    Args:
        args (tuple): Tuple of (obj_path, build_dir, srcdir, cache), where
            cache is a dict for memoising path resolution (see
            _resolve_header())

    Returns:
        tuple: (source_lines_dict, error_msg) where source_lines_dict is a
            mapping of source file paths to sets of line numbers, and
            error_msg is None on success or an error string on failure
    """
    obj_path, build_dir, srcdir, cache = args
    source_lines = defaultdict(set)

    # Get the directory of the .o file relative to build_dir
    rel_to_build = os.path.relpath(obj_path, build_dir)
    obj_dir = os.path.dirname(rel_to_build)

    # Use readelf to extract decoded line information
    try:
        result = subprocess.run(
            [_get_readelf(), '--debug-dump=decodedline', obj_path],
            capture_output=True, text=True, check=False,
            encoding='utf-8', errors='ignore')
        if result.returncode != 0:
            error_msg = (f'readelf failed on {obj_path} with return code '
                        f'{result.returncode}\nstderr: {result.stderr}')
            return (source_lines, error_msg)

        # Parse the output
        # Format is: Section header with full path, then data lines
        current_file = None
        for line in result.stdout.splitlines():
            # Skip header lines and empty lines
            if not line or line.startswith('Contents of') or \
               line.startswith('File name') or line.strip() == '' or \
               line.startswith(' '):
                continue

            # Look for section headers with full path (e.g., '/path/to/file.c:')
            if line.endswith(':'):
                abs_path = _resolve_header(line.rstrip(':'), obj_dir, srcdir,
                                           cache)
                # Leave current_file unchanged if the path does not resolve
                if abs_path:
                    current_file = abs_path
                continue

            # Parse data lines - use current_file from section header
            if current_file:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        line_num = int(parts[1])
                        # Skip special line numbers (like '-')
                        if line_num > 0:
                            source_lines[current_file].add(line_num)
                    except (ValueError, IndexError):
                        continue
    except (OSError, subprocess.SubprocessError) as e:
        error_msg = f'Failed to execute readelf on {obj_path}: {e}'
        return (source_lines, error_msg)

    return (source_lines, None)


def find_object_files(build_dir):
    """Find all object files in the build directory.

    Args:
        build_dir (str): Build directory to search

    Returns:
        list: List of absolute paths to .o files
    """
    obj_files = []
    for root, _, files in os.walk(build_dir):
        for fname in files:
            if fname.endswith('.o'):
                obj_files.append(os.path.join(root, fname))
    return obj_files


def count_lines(file_path):
    """Count the number of lines in a file.

    Args:
        file_path (str): Path to file to count lines in

    Returns:
        int: Number of lines in the file, or 0 on error
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return len(f.readlines())
    except IOError:
        return 0


def extract_lines(build_dir, srcdir, jobs=None, use_threads=False,
                  obj_files=None):
    """Extract the source lines compiled into a build.

    Finds all object files under build_dir and reads their DWARF line tables
    to work out which lines of each source file generated code.

    Args:
        build_dir (str): Build directory containing .o files
        srcdir (str): Path to source root directory
        jobs (int): Number of parallel jobs (None = use all CPUs, 1 = run
            sequentially in this process)
        use_threads (bool): Parallelise with threads rather than processes.
            The work is dominated by the readelf subprocess, so threads give a
            good speed-up, and unlike a process pool they are safe to use from
            a daemon thread (as buildman's builder threads are)
        obj_files (list of str): Object files to read, or None to read every
            object file under build_dir

    Returns:
        tuple:
            dict: Mapping of source file path to a set of line numbers that
                generated code
            list of str: Error messages, one per object file that could not be
                read (empty on full success)
    """
    if obj_files is None:
        obj_files = find_object_files(build_dir)
    if not obj_files:
        return defaultdict(set), []

    # Shared path-resolution cache. With threads (the buildman case) all
    # workers share the one dict, so a header resolved for one object is reused
    # for the thousands of others that include it. Dict access is atomic under
    # the GIL and the lookup is idempotent, so no lock is needed. With
    # processes each worker gets its own copy, which is harmless
    cache = {}
    args_list = [(obj_path, build_dir, srcdir, cache)
                 for obj_path in obj_files]

    # Process in parallel (sequential when jobs=1 for thread safety)
    num_jobs = jobs if jobs else multiprocessing.cpu_count()
    if num_jobs <= 1:
        results = [worker(args) for args in args_list]
    elif use_threads:
        with ThreadPoolExecutor(max_workers=num_jobs) as pool:
            results = list(pool.map(worker, args_list))
    else:
        with multiprocessing.Pool(num_jobs) as pool:
            results = pool.map(worker, args_list)

    return _merge_results(results)


def _merge_results(results):
    """Merge per-object worker results into one mapping plus an error list

    Args:
        results (list): List of (source_lines_dict, error_msg) tuples, one per
            object file (see worker())

    Returns:
        tuple:
            dict: Mapping of source file path to a set of line numbers that
                generated code, merged across all objects
            list of str: Error messages from objects that could not be read
    """
    source_lines = defaultdict(set)
    errors = []
    for result_dict, error_msg in results:
        if error_msg:
            errors.append(error_msg)
        else:
            for source_file, lines in result_dict.items():
                source_lines[source_file].update(lines)
    return source_lines, errors


def lines_to_ranges(lines):
    """Group a set of line numbers into inclusive ranges.

    Consecutive line numbers are collapsed into (start, end) tuples for
    compact storage.

    Args:
        lines (iterable): Line numbers (ints) that are active

    Returns:
        list of tuple: (start_line, end_line) pairs, both inclusive, sorted by
            start line
    """
    ranges = []
    start = prev = None
    for line_num in sorted(lines):
        if start is None:
            start = line_num
        elif line_num > prev + 1:
            ranges.append((start, prev))
            start = line_num
        prev = line_num
    if start is not None:
        ranges.append((start, prev))
    return ranges


def format_ranges(ranges):
    """Format a list of ranges as a compact string.

    Args:
        ranges (list of tuple): (start, end) pairs, both inclusive

    Returns:
        str: Comma-separated ranges, e.g. '1,3-5,9'
    """
    return ','.join(str(start) if start == end else f'{start}-{end}'
                    for start, end in ranges)


def parse_ranges(text):
    """Parse a compact range string back into a set of line numbers.

    Args:
        text (str): Comma-separated ranges, e.g. '1,3-5,9'

    Returns:
        set: Line numbers (ints)
    """
    lines = set()
    text = text.strip()
    if not text:
        return lines
    for part in text.split(','):
        if '-' in part:
            start, end = part.split('-', 1)
            lines.update(range(int(start), int(end) + 1))
        else:
            lines.add(int(part))
    return lines
