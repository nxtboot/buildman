# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.

"""Outcome class for buildman build results"""


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
