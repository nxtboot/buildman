# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Google LLC
#
"""Decide whether a commit can affect a board, using build dependencies

When building a branch, most commits do not affect most boards: a commit
touching one driver is only compiled by the boards which enable it. Even a
'no-op' incremental build has a significant cost per board (make must
re-check every rule, binman always re-runs and buildman re-runs the various
binutils tools on the output), which adds up to more than half the build
time for a typical branch across many boards.

This module works out whether the files changed by a commit can possibly
affect a board, using the dependency files (.cmd) which Kbuild writes when
building. These list every source file, header, devicetree file and linker
script used by the build, so a changed file which does not appear in them
(and is not one of the 'risky' files below) cannot change the build output.

Files which affect the build in ways make does not track (Makefiles,
Kconfig files, scripts, tools, defconfigs and anything unrecognised) are
always treated as affecting every board, so buildman errs on the side of
rebuilding.
"""

import os
import re

# Extensions of files which are fully tracked by the .cmd dependency files:
# if a changed file with one of these extensions is not listed as a
# dependency, it cannot affect the build
DEP_EXTS = {'.c', '.h', '.S', '.dts', '.dtsi'}

# Extensions of files which can never affect the build
SAFE_EXTS = {'.rst', '.txt', '.md'}

# Filenames which can never affect the build, in any directory
SAFE_NAMES = {'MAINTAINERS', '.gitignore', '.gitattributes', '.mailmap',
              'OWNERS', '.gitlab-ci.yml', '.azure-pipelines.yml'}

# Path prefixes which can never affect the build. Note that most of tools/
# is risky (binman and dtoc run during the build) but these tools do not
SAFE_PREFIXES = ('doc/', 'tools/buildman/', 'tools/patman/', 'test/py/')

# Top-level directories where even tracked-extension files are risky:
# scripts/ can change how everything is built and (with --shared-dtc) the
# dtc source is not a tracked dependency of each board
RISKY_DIRS = {'scripts'}

# Matches a defconfig file, so that changes to other boards' defconfigs do
# not force a rebuild
RE_DEFCONFIG = re.compile(r'configs/([^/]*)_defconfig$')


def scan_deps(out_dir, src_dir):
    """Scan the .cmd files in a build directory to find its dependencies

    Reads all the .cmd files written by Kbuild and collects the source
    files they mention, as paths relative to the source tree. This
    includes C sources, headers, devicetree files and linker scripts.

    Args:
        out_dir (str): Build (output) directory to scan
        src_dir (str): Source directory used for the build, so that
            absolute paths can be converted to tree-relative ones

    Returns:
        set of str: Source-tree-relative paths of all dependencies
    """
    src_real = os.path.realpath(src_dir)
    prefix = src_real + os.sep
    deps = set()
    for dirpath, _, fnames in os.walk(out_dir):
        for fname in fnames:
            if not fname.endswith('.cmd') or not fname.startswith('.'):
                continue
            try:
                with open(os.path.join(dirpath, fname), 'r',
                          encoding='utf-8', errors='replace') as inf:
                    data = inf.read()
            except OSError:
                continue
            for token in data.split():
                token = token.strip('"\\')
                if token.startswith(prefix):
                    rel = os.path.normpath(token[len(prefix):])
                    deps.add(rel)
    return deps


# pylint: disable=R0911
def is_safe(fname, deps, defconfigs):
    """Check whether a changed file can be ignored for a board

    Args:
        fname (str): Source-tree-relative path of the changed file
        deps (set of str): Dependencies of the board's previous build, from
            scan_deps()
        defconfigs (set of str): Names of the defconfig files used by this
            board (e.g. {'snow_defconfig'}), which must force a rebuild;
            other defconfigs are ignored

    Returns:
        bool: True if the change cannot affect the board's build
    """
    if fname in deps:
        return False
    base = os.path.basename(fname)
    top = fname.split('/', 1)[0]
    if fname.startswith(SAFE_PREFIXES) or base in SAFE_NAMES:
        return True
    ext = os.path.splitext(base)[1]
    if ext in SAFE_EXTS:
        return True
    m = RE_DEFCONFIG.match(fname)
    if m:
        return f'{m.group(1)}_defconfig' not in defconfigs
    if top in RISKY_DIRS:
        return False
    if ext in DEP_EXTS:
        # Tracked by the dependency files and not listed there
        return True
    return False


def can_skip(files, deps, defconfigs):
    """Check whether a board's build can be skipped for a set of changes

    Args:
        files (list of str): Source-tree-relative paths changed by the
            commit(s) being considered
        deps (set of str): Dependencies of the board's previous build, from
            scan_deps()
        defconfigs (set of str): Names of the defconfig files used by this
            board

    Returns:
        bool: True if none of the changed files can affect the board, so
            the build can be skipped
    """
    if not deps:
        return False
    return all(is_safe(fname, deps, defconfigs) for fname in files)
