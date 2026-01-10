# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2013 The Chromium OS Authors.

"""Result handler for buildman build results"""


class ResultHandler:
    """Handles display of build results and summaries

    This class is responsible for displaying build results, including
    size information, errors, warnings, and configuration changes.
    """

    def __init__(self, col, opts):
        """Create a new ResultHandler

        Args:
            col: terminal.Color object for coloured output
            opts (DisplayOptions): Options controlling what to display
        """
        self._col = col
        self._opts = opts
        self._builder = None

    def set_builder(self, builder):
        """Set the builder for this result handler

        Args:
            builder (Builder): Builder object to use for getting results
        """
        self._builder = builder
