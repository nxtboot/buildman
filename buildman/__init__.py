# SPDX-License-Identifier: GPL-2.0+

"""Buildman build tool for U-Boot

buildman bundles copies of some libraries from the U-Boot tree in _vendor/.
Make these importable by their usual names, ahead of any other copies, e.g.
in a U-Boot tree. They are kept out of the top level of site-packages, where
they would clash with U-Boot's tools and with other packages which provide
them.
"""

import os
import sys

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           '_vendor')
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
