# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2012 The Chromium OS Authors.


"""A single board which can be selected and built"""

# pylint: disable=too-many-instance-attributes,too-few-public-methods
class Board:
    """A particular board that we can build"""
    # pylint: disable=too-many-arguments
    def __init__(self, status, arch, cpu, soc, vendor, board_name, target,
                 cfg_name, extended=None, orig_target=None):
        """Create a new board type.

        Args:
            status (str): Either 'Active' or 'Orphaned'
            arch (str): Architecture name (e.g. arm)
            cpu (str): Cpu name (e.g. arm1136)
            soc (str): Name of SOC, or '' if none (e.g. mx31)
            vendor (str): Name of vendor (e.g. armltd)
            board_name (str): Name of board (e.g. integrator)
            target (str): Target name (use make <target>_defconfig to configure)
            cfg_name (str): Config-file name (in includes/configs/)
            extended (boards.Extended): Extended board, if this board is one
            orig_target (str): Name of target this extended board is based on
        """
        self.target = target
        self.status = status
        self.arch = arch
        self.cpu = cpu
        self.soc = soc
        self.vendor = vendor
        self.board_name = board_name
        self.cfg_name = cfg_name
        self.props = [self.target, self.arch, self.cpu, self.board_name,
                      self.vendor, self.soc, self.cfg_name]
        self.build_it = False
        self.extended = extended
        self.orig_target = orig_target
