# SPDX-License-Identifier: GPL-2.0+
# Copyright 2022 Google LLC
# Written by Simon Glass <sjg@chromium.org>
#

"""Utility functions for dealing with Kconfig .config files"""

import os
import re
import tempfile

from u_boot_pylib import command
from u_boot_pylib import tools


class Config:
    """Holds information about configuration settings for a board."""
    def __init__(self, config_filename, target):
        self.target = target
        self.config = {}
        for fname in config_filename:
            self.config[fname] = {}

    def add(self, fname, key, value):
        """Add a configuration value

        Args:
            fname (str): Filename to add to (e.g. '.config')
            key (str): Config key (e.g. 'CONFIG_DM')
            value (str): Config value (e.g. 'y')
        """
        self.config[fname][key] = value

    def __hash__(self):
        val = 0
        for _, config in self.config.items():
            for key, value in config.items():
                print(key, value)
                val = val ^ hash(key) & hash(value)
        return val


RE_LINE = re.compile(r'(# )?CONFIG_([A-Z0-9_]+)(=(.*)| is not set)')
RE_CFG = re.compile(r'(~?)(CONFIG_)?([A-Z0-9_]+)(=.*)?')

def make_cfg_line(opt, adj):
    """Make a new config line for an option

    Args:
        opt (str): Option to process, without CONFIG_ prefix
        adj (str): Adjustment to make (C is config option without prefix):
             C to enable C
             ~C to disable C
             C=val to set the value of C (val must have quotes if C is
                 a string Kconfig)

    Returns:
        str: New line to use, one of:
            CONFIG_opt=y               - option is enabled
            # CONFIG_opt is not set    - option is disabled
            CONFIG_opt=val             - option is getting a new value (val is
                in quotes if this is a string)
    """
    if adj[0] == '~':
        return f'# CONFIG_{opt} is not set'
    if '=' in adj:
        return f'CONFIG_{adj}'
    return f'CONFIG_{opt}=y'

def adjust_cfg_line(line, adjust_cfg, done=None):
    """Make an adjustment to a single of line from a .config file

    This processes a .config line, producing a new line if a change for this
    CONFIG is requested in adjust_cfg

    Args:
        line (str): line to process, e.g. '# CONFIG_FRED is not set' or
            'CONFIG_FRED=y' or 'CONFIG_FRED=0x123' or 'CONFIG_FRED="fred"'
        adjust_cfg (dict of str): Changes to make to .config file before
                building:
             key: str config to change, without the CONFIG_ prefix, e.g.
                 FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)
        done (set of set): Adds the config option to this set if it is changed
            in some way. This is used to track which ones have been processed.
            None to skip.

    Returns:
        tuple:
            str: New string for this line (maybe unchanged)
            str: Adjustment string that was used
    """
    out_line = line
    m_line = RE_LINE.match(line)
    adj = None
    if m_line:
        _, opt, _, _ = m_line.groups()
        adj = adjust_cfg.get(opt)
        if adj:
            out_line = make_cfg_line(opt, adj)
            if done is not None:
                done.add(opt)

    return out_line, adj

def adjust_cfg_lines(lines, adjust_cfg):
    """Make adjustments to a list of lines from a .config file

    Args:
        lines (list of str): List of lines to process
        adjust_cfg (dict of str): Changes to make to .config file before
                building:
             key: str config to change, without the CONFIG_ prefix, e.g.
                 FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)

    Returns:
        list of str: New list of lines resulting from the processing
    """
    out_lines = []
    done = set()
    for line in lines:
        out_line, _ = adjust_cfg_line(line, adjust_cfg, done)
        out_lines.append(out_line)

    for opt in adjust_cfg:
        if opt not in done:
            adj = adjust_cfg.get(opt)
            out_line = make_cfg_line(opt, adj)
            out_lines.append(out_line)

    return out_lines

def adjust_cfg_file(fname, adjust_cfg):
    """Make adjustments to a .config file

    Args:
        fname (str): Filename of .config file to change
        adjust_cfg (dict of str): Changes to make to .config file before
                building:
             key: str config to change, without the CONFIG_ prefix, e.g.
                 FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)
    """
    lines = tools.read_file(fname, binary=False).splitlines()
    out_lines = adjust_cfg_lines(lines, adjust_cfg)
    out = '\n'.join(out_lines) + '\n'
    tools.write_file(fname, out, binary=False)

def convert_list_to_dict(adjust_cfg_list):
    """Convert a list of config changes into the dict used by adjust_cfg_file()

    Args:
        adjust_cfg_list (list of str): List of changes to make to .config file
            before building. Each is one of (where C is the config option with
            or without the CONFIG_ prefix). Items can be comma-separated.

                C to enable C
                ~C to disable C
                C=val to set the value of C (val must have quotes if C is
                    a string Kconfig

    Returns:
        dict of str: Changes to make to .config file before building:
             key: str config to change, without the CONFIG_ prefix, e.g. FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)

    Raises:
        ValueError: if an item in adjust_cfg_list has invalid syntax
    """
    result = {}
    for item in adjust_cfg_list or []:
        # Split by comma to support comma-separated values
        for cfg in item.split(','):
            cfg = cfg.strip()
            if not cfg:
                continue
            m_cfg = RE_CFG.match(cfg)
            if not m_cfg:
                raise ValueError(f"Invalid CONFIG adjustment '{cfg}'")
            negate, _, opt, val = m_cfg.groups()
            result[opt] = f'%s{opt}%s' % (negate or '', val or '')

    return result

def check_cfg_lines(lines, adjust_cfg):
    """Check that lines do not conflict with the requested changes

    If a line enables a CONFIG which was requested to be disabled, etc., then
    this is an error. This function finds such errors.

    Args:
        lines (list of str): List of lines to process
        adjust_cfg (dict of str): Changes to make to .config file before
                building:
             key: str config to change, without the CONFIG_ prefix, e.g.
                 FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)

    Returns:
        list of tuple: list of errors, each a tuple:
            str: cfg adjustment requested
            str: line of the config that conflicts
    """
    bad = []
    done = set()
    for line in lines:
        out_line, adj = adjust_cfg_line(line, adjust_cfg, done)
        if out_line != line:
            bad.append([adj, line])

    for opt in adjust_cfg:
        if opt not in done:
            adj = adjust_cfg.get(opt)
            out_line = make_cfg_line(opt, adj)
            bad.append([adj, f'Missing expected line: {out_line}'])

    return bad

def check_cfg_file(fname, adjust_cfg):
    """Check that a config file has been adjusted according to adjust_cfg

    Args:
        fname (str): Filename of .config file to change
        adjust_cfg (dict of str): Changes to make to .config file before
                building:
             key: str config to change, without the CONFIG_ prefix, e.g.
                 FRED
             value: str change to make (C is config option without prefix):
                 C to enable C
                 ~C to disable C
                 C=val to set the value of C (val must have quotes if C is
                     a string Kconfig)

    Returns:
        str: None if OK, else an error string listing the problems
    """
    lines = tools.read_file(fname, binary=False).splitlines()
    bad_cfgs = check_cfg_lines(lines, adjust_cfg)
    if bad_cfgs:
        out = [f'{cfg:20}  {line}' for cfg, line in bad_cfgs]
        content = '\\n'.join(out)
        return f'''
Some CONFIG adjustments did not take effect. This may be because
the request CONFIGs do not exist or conflict with others.

Failed adjustments:

{content}
'''
    return None


def process_config(fname, squash_config_y):
    """Read in a .config, autoconf.mk or autoconf.h file

    This function handles all config file types. It ignores comments and
    any #defines which don't start with CONFIG_.

    Args:
        fname (str): Filename to read
        squash_config_y (bool): If True, replace 'y' values with '1'

    Returns:
        dict: Dictionary with:
            key: Config name (e.g. CONFIG_DM)
            value: Config value (e.g. '1')
    """
    config = {}
    if os.path.exists(fname):
        with open(fname, encoding='utf-8') as fd:
            for line in fd:
                line = line.strip()
                if line.startswith('#define'):
                    values = line[8:].split(' ', 1)
                    if len(values) > 1:
                        key, value = values
                    else:
                        key = values[0]
                        value = '1' if squash_config_y else ''
                    if not key.startswith('CONFIG_'):
                        continue
                elif not line or line[0] in ['#', '*', '/']:
                    continue
                else:
                    key, value = line.split('=', 1)
                if squash_config_y and value == 'y':
                    value = '1'
                config[key] = value
    return config


def adjust_cfg_to_fragment(adjust_cfg):
    """Convert adjust_cfg dict to config fragment content

    Args:
        adjust_cfg (dict): Changes to make to .config file. Keys are config
            names (without CONFIG_ prefix), values are the setting. Format
            matches make_cfg_line():
                ~...     - disable the option
                ...=val  - set the option to val (val contains full assignment)
                other    - enable the option with =y

    Returns:
        str: Config fragment content suitable for merge_config.sh
    """
    lines = []
    for opt, val in adjust_cfg.items():
        if val.startswith('~'):
            lines.append(f'# CONFIG_{opt} is not set')
        elif '=' in val:
            lines.append(f'CONFIG_{val}')
        else:
            lines.append(f'CONFIG_{opt}=y')
    return '\n'.join(lines) + '\n' if lines else ''


def run_merge_config(src_dir, out_dir, cfg_file, adjust_cfg, env):
    """Run merge_config.sh to apply config changes with Kconfig resolution

    This uses scripts/kconfig/merge_config.sh to merge config fragments
    into the .config file, then runs 'make alldefconfig' to resolve all
    Kconfig dependencies including 'imply' and 'select'.

    To properly resolve 'imply' relationships, we must use a minimal
    defconfig as the base (not the full .config). The full .config contains
    '# CONFIG_xxx is not set' lines which count as "specified" and prevent
    imply from taking effect. Using savedefconfig output ensures only
    explicitly set options are in the base, allowing imply to work.

    Args:
        src_dir (str): Source directory (containing scripts/kconfig)
        out_dir (str): Output directory containing .config
        cfg_file (str): Path to the .config file
        adjust_cfg (dict): Config changes to apply
        env (dict): Environment variables

    Returns:
        CommandResult: Result of the merge_config.sh operation
    """
    # Create a temporary fragment file with the config changes
    fragment_content = adjust_cfg_to_fragment(adjust_cfg)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.config',
                                     delete=False) as frag:
        frag.write(fragment_content)
        frag_path = frag.name

    # Create a minimal defconfig from the current .config
    # This is necessary for 'imply' to work - the full .config has
    # '# CONFIG_xxx is not set' lines that prevent imply from taking effect
    defconfig_path = os.path.join(out_dir or '.', 'defconfig')
    make_cmd = ['make', f'O={out_dir}' if out_dir else None,
                f'KCONFIG_CONFIG={cfg_file}', 'savedefconfig']
    make_cmd = [x for x in make_cmd if x]  # Remove None elements
    result = command.run_one(*make_cmd, cwd=src_dir, env=env, capture=True,
                             capture_stderr=True)
    if result.return_code:
        if os.path.exists(frag_path):
            os.unlink(frag_path)
        return result

    try:
        # Run merge_config.sh with the minimal defconfig as base
        # -O sets output dir; defconfig is the base, fragment is merged
        merge_script = os.path.join(src_dir or '.', 'scripts', 'kconfig',
                                    'merge_config.sh')
        out = out_dir or '.'
        cmd = [merge_script, '-O', out, defconfig_path, frag_path]
        result = command.run_one(*cmd, cwd=src_dir, env=env, capture=True,
                                capture_stderr=True)
    finally:
        # Clean up temporary files
        if os.path.exists(frag_path):
            os.unlink(frag_path)

    return result
