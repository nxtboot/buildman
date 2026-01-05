# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2012 The Chromium OS Authors.

"""Handles settings for buildman, reading from a config file."""

import configparser
import io
import os

# pylint: disable=C0103
settings = None
config_fname = None

def setup(fname=''):
    """Set up the buildman settings module by reading config files

    Args:
        fname (str): Config filename to read ('' for default)
    """
    global settings  # pylint: disable=W0603
    global config_fname  # pylint: disable=W0603

    settings = configparser.ConfigParser()
    if fname is not None:
        config_fname = fname
        if config_fname == '':
            config_fname = f"{os.getenv('HOME')}/.buildman"
        if not os.path.exists(config_fname):
            print('No config file found ~/.buildman\nCreating one...\n')
            create_buildman_config_file(config_fname)
            print('To install tool chains, please use the --fetch-arch option')
        if config_fname:
            settings.read(config_fname)

def add_file(data):
    """Add settings from a string

    Args:
        data (str): Config data in INI format
    """
    settings.read_file(io.StringIO(data))

def add_section(name):
    """Add a new section to the settings

    Args:
        name (str): Name of section to add
    """
    settings.add_section(name)

def get_items(section):
    """Get the items from a section of the config.

    Args:
        section (str): name of section to retrieve

    Returns:
        list of tuple: List of (name, value) tuples for the section
    """
    try:
        return settings.items(section)
    except configparser.NoSectionError:
        return []

def get_global_item_value(name):
    """Get an item from the 'global' section of the config.

    Args:
        name (str): name of item to retrieve

    Returns:
        str: Value of item, or None if not present
    """
    return settings.get('global', name, fallback=None)

def set_item(section, tag, value):
    """Set an item and write it back to the settings file"""
    settings.set(section, tag, value)
    if config_fname is not None:
        with open(config_fname, 'w', encoding='utf-8') as fd:
            settings.write(fd)

def create_buildman_config_file(cfgname):
    """Creates a new config file with no tool chain information.

    Args:
        cfgname (str): Config filename to create
    """
    try:
        with open(cfgname, 'w', encoding='utf-8') as out:
            print('''[toolchain]
# name = path
# e.g. x86 = /opt/gcc-4.6.3-nolibc/x86_64-linux
other = /

[toolchain-prefix]
# name = path to prefix
# e.g. x86 = /opt/gcc-4.6.3-nolibc/x86_64-linux/bin/x86_64-linux-

[toolchain-alias]
# arch = alias
# Indicates which toolchain should be used to build for that arch
riscv = riscv32
sh = sh4
x86 = i386

[make-flags]
# Special flags to pass to 'make' for certain boards, e.g. to pass a test
# flag and build tag to snapper boards:
# snapper-boards=ENABLE_AT91_TEST=1
# snapper9260=${snapper-boards} BUILD_TAG=442
# snapper9g45=${snapper-boards} BUILD_TAG=443
''', file=out)
    except IOError:
        print(f"Couldn't create buildman config file '{cfgname}'\n")
        raise
