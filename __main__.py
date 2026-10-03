#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便携目录入口：``python <本目录>`` 等价于 ``python autoLCDE.py``。"""

import sys

from autoLCDE.cli import main

if __name__ == "__main__":
    sys.exit(main())
