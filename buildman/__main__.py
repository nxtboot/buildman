# SPDX-License-Identifier: GPL-2.0+

"""Allow buildman to be run with 'python3 -m buildman'"""

import sys

from buildman.main import run_buildman

sys.exit(run_buildman())
