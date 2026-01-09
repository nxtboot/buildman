# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.

"""Outcome class and constants for buildman build results"""

from collections import namedtuple

# Build-outcome codes
OUTCOME_OK, OUTCOME_WARNING, OUTCOME_ERROR, OUTCOME_UNKNOWN = list(range(4))

# Board status for display purposes
#   ok: List of boards fixed since last commit
#   warn: List of boards with warnings since last commit
#   err: List of new broken boards since last commit
#   new: List of boards that didn't exist last time
#   unknown: List of boards that were not built
BoardStatus = namedtuple('BoardStatus', 'ok warn err new unknown')

# Error line information for display
#   char: Character representation: '+': error, '-': fixed error, 'w+': warning,
#       'w-' = fixed warning
#   brds: List of Board objects which have line in the error/warning output
#   errline: The text of the error line
ErrLine = namedtuple('ErrLine', 'char brds errline')


class Outcome:
    """Records a build outcome for a single make invocation

    Public Members:
        rc: Outcome value (OUTCOME_...)
        err_lines: List of error lines or [] if none
        sizes: Dictionary of image size information, keyed by filename
            - Each value is itself a dictionary containing
                values for 'text', 'data' and 'bss', being the integer
                size in bytes of each section.
        func_sizes: Dictionary keyed by filename - e.g. 'u-boot'. Each
                value is itself a dictionary:
                    key: function name
                    value: Size of function in bytes
        config: Dictionary keyed by filename - e.g. '.config'. Each
                value is itself a dictionary:
                    key: config name
                    value: config value
        environment: Dictionary keyed by environment variable, Each
                 value is the value of environment variable.
    """
    def __init__(self, rc, err_lines, sizes, func_sizes, config,
                 environment):
        self.rc = rc
        self.err_lines = err_lines
        self.sizes = sizes
        self.func_sizes = func_sizes
        self.config = config
        self.environment = environment
