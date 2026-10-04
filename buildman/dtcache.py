# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Google LLC
#
"""Shared build of dtc and pylibfdt for use by all boards

Building U-Boot normally builds dtc and pylibfdt in each output directory,
even though they do not depend on the board being built. When building many
boards (particularly with -P, where each board gets a fresh output
directory), this adds several seconds of CPU time to every board and the
serialised pylibfdt build (swig) sits on the critical path.

This module builds dtc and pylibfdt once, in a shared directory, and
provides the paths so that board builds can use them via the DTC and
PYTHONPATH environment variables. U-Boot's Makefile skips building the
in-tree dtc/pylibfdt when DTC is provided.

The shared build is keyed by a hash of the scripts/dtc source files, so a
commit which changes dtc gets a fresh build, while the (common) case of many
commits with unchanged dtc shares a single build. The cache directory
persists across buildman runs, so subsequent runs pay nothing.
"""

import glob
import hashlib
import multiprocessing
import os
import threading

from u_boot_pylib import command

# Path to the dtc source within the U-Boot tree
DTC_SUBPATH = 'scripts/dtc'

# Length of the hash prefix used for cache-directory names
HASH_LEN = 16


def hash_src_tree(src_dir):
    """Calculate a hash of the dtc source files in a source tree

    This reads all files in scripts/dtc (including pylibfdt) and hashes
    their names and contents, so any change to the dtc source produces a
    different hash. Reading the files directly (rather than asking git)
    means this works for dirty trees and trees without git.

    Args:
        src_dir (str): Path to the U-Boot source tree

    Returns:
        str: Hash of the dtc source, or None if it could not be read
    """
    dtc_dir = os.path.join(src_dir, DTC_SUBPATH)
    if not os.path.isdir(dtc_dir):
        return None
    hasher = hashlib.sha1()
    for dirpath, _, fnames in sorted(os.walk(dtc_dir)):
        for fname in sorted(fnames):
            pathname = os.path.join(dirpath, fname)
            rel = os.path.relpath(pathname, dtc_dir)
            try:
                with open(pathname, 'rb') as inf:
                    data = inf.read()
            except OSError:
                continue
            hasher.update(rel.encode('utf-8'))
            hasher.update(b'\0')
            hasher.update(data)
            hasher.update(b'\0')
    return hasher.hexdigest()


# pylint: disable=R0903
class DtcCache:
    """Cache of shared dtc/pylibfdt builds

    Thread-safe: the first builder thread to need a particular version of
    dtc builds it while other threads needing the same version wait; they
    all then share the result. Threads needing a different version (i.e. a
    commit which changes scripts/dtc) build it in a separate directory, in
    parallel.

    Properties:
        base_dir (str): Directory in which to place the shared builds, one
            subdirectory per dtc version
        num_jobs (int or None): Number of jobs to use when building, None to
            use the number of CPUs
    """
    def __init__(self, base_dir, num_jobs=None):
        self.base_dir = base_dir
        self.num_jobs = num_jobs or multiprocessing.cpu_count()
        self._lock = threading.Lock()
        self._key_locks = {}
        self._results = {}
        self._warned = False

    def obtain(self, src_dir, gnu_make='make'):
        """Obtain a shared dtc/pylibfdt build for a source tree

        Builds dtc/pylibfdt if this version has not been built yet,
        otherwise returns the existing build. This blocks while another
        thread is building the same version.

        Args:
            src_dir (str): Path to the U-Boot source tree to build from,
                already checked out at the required commit
            gnu_make (str): Command name of GNU Make

        Returns:
            tuple or None:
                str: Path to the dtc binary
                str: Path to the pylibfdt directory (for PYTHONPATH)
            None if the shared build failed, in which case the caller
                should fall back to letting each board build its own dtc
        """
        key = hash_src_tree(src_dir)
        if not key:
            return None
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            if key not in self._results:
                self._results[key] = self._build(key, src_dir, gnu_make)
            return self._results[key]

    def _check_existing(self, out_dir):
        """Check for a usable build in a cache directory

        Args:
            out_dir (str): Cache directory to check

        Returns:
            tuple or None: (dtc path, pylibfdt path), or None if not usable
        """
        dtc = os.path.join(out_dir, DTC_SUBPATH, 'dtc')
        pylibfdt = os.path.join(out_dir, DTC_SUBPATH, 'pylibfdt')
        if (os.path.exists(dtc) and
                glob.glob(os.path.join(pylibfdt, '_libfdt*.so'))):
            return dtc, pylibfdt
        return None

    def _build(self, key, src_dir, gnu_make):
        """Build dtc/pylibfdt into the cache directory for the given key

        Uses 'make sandbox_defconfig' to configure (any config would do,
        but sandbox enables PYLIBFDT) and then builds just the scripts_dtc
        target.

        Args:
            key (str): Hash of the dtc source, used to name the directory
            src_dir (str): Path to the U-Boot source tree to build from
            gnu_make (str): Command name of GNU Make

        Returns:
            tuple or None: (dtc path, pylibfdt path), or None on failure
        """
        out_dir = os.path.join(self.base_dir, key[:HASH_LEN])
        found = self._check_existing(out_dir)
        if found:
            return found

        os.makedirs(out_dir, exist_ok=True)
        env = dict(os.environ)
        env.pop('DTC', None)
        for target in ['sandbox_defconfig', 'scripts_dtc']:
            result = command.run_one(
                gnu_make, f'O={out_dir}', '-s', '-j', str(self.num_jobs),
                target, capture=True, capture_stderr=True, cwd=src_dir,
                env=env, raise_on_error=False)
            if result.return_code:
                self._warn_once(result.stderr)
                return None
        found = self._check_existing(out_dir)
        if not found:
            self._warn_once('build did not produce dtc/pylibfdt')
        return found

    def _warn_once(self, msg):
        """Print a warning about falling back, only once per run

        Args:
            msg (str): Detail of what went wrong
        """
        if not self._warned:
            self._warned = True
            print(f'warning: shared dtc build failed, boards will build '
                  f'their own dtc: {msg.strip()}')
