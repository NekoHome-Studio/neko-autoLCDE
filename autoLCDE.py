#!/usr/bin/env python3
"""``autoLCDE`` 命令行入口 —— 接外部大模型 API 的 LCDE 剧本生成器。

用法::

    python tools/autoLCDE.py providers
    python tools/autoLCDE.py init
    python tools/autoLCDE.py doctor --ping
    python tools/autoLCDE.py gen --premise "雪夜，末班车停了十年，还有人每天来等"
    python tools/autoLCDE.py web

需要外部 API key 才能干活（DeepSeek / OpenAI / 任意 OpenAI 兼容服务）。
想完全离线看看它会产出什么，加 ``--provider mock``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoLCDE.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
