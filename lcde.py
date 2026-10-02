#!/usr/bin/env python3
"""``lcde`` 命令行入口。

用法::

    python lcde.py env
    python lcde.py char list
    python lcde.py story show 序章
    python lcde.py validate

也可以直接调用包：``python -m lcde``（在 tools/ 目录下）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lcde.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
