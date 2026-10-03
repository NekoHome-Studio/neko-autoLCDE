"""与 ``lcde`` 工具包的桥接层。

autoLCDE 不重复实现任何格式细节：字节布局、命令表、枚举、校验规则全部复用
``lcde``。这里只做一件事——**找到内核**并集中导入，让「依赖了哪些东西」一眼可见
（游戏更新后要改的地方也只有这一处）。

内核位置由 :mod:`autoLCDE.paths` 决定，因此同一份源码：

* 在 LCDE 仓库里跑时用同仓库的 ``tools/lcde``；
* 作为独立工具包分发时用自带的 ``lcde/``（或 ``_vendor/lcde``）；
* 打成单文件 ``.pyz`` 后由 zipimport 从归档根解决。
"""

from __future__ import annotations

import sys

from .paths import lcde_roots, lcde_source_root

#: 实际提供内核的目录；``.pyz`` 里为 ``None``（内核就在归档根，直接 import）。
LCDE_ROOT = lcde_source_root()
if LCDE_ROOT is not None and str(LCDE_ROOT) not in sys.path:
    sys.path.insert(0, str(LCDE_ROOT))

# 找不到真实目录时也要走到这里：单文件归档靠 zipimport 解决 `import lcde`
for _candidate in lcde_roots():
    if (_candidate / "lcde" / "__init__.py").is_file() and str(_candidate) not in sys.path:
        sys.path.append(str(_candidate))

from lcde import FORMAT_NAME, FORMAT_VERSION                      # noqa: E402
from lcde import __version__ as LCDE_VERSION                      # noqa: E402
from lcde.commands import BY_NAME, COMMANDS, coerce_value          # noqa: E402
from lcde.errors import LcdeError                                  # noqa: E402
from lcde.jsonfmt import (                                         # noqa: E402
    character_to_doc,
    dumps,
    loads,
    project_from_doc,
    story_to_doc,
)
from lcde.model import (                                           # noqa: E402
    Character,
    Portrait,
    Project,
    Story,
)
from lcde.placeholder import (                                     # noqa: E402
    BACKGROUND_SIZE,
    HEAD_SIZE,
    PORTRAIT_SIZE,
    ROOT_HALF_WIDTH,
    STAGE_SIZE,
    fit_rect,
    generate_for_document,
    place_for_center_x,
)
from lcde.validate import (                                        # noqa: E402
    ERROR,
    INFO,
    WARNING,
    Issue,
    has_errors,
    validate_character,
    validate_project,
    validate_story,
)

__all__ = [
    "LCDE_ROOT", "FORMAT_NAME", "FORMAT_VERSION", "LCDE_VERSION",
    "BY_NAME", "COMMANDS", "coerce_value", "LcdeError",
    "character_to_doc", "story_to_doc", "dumps", "loads", "project_from_doc",
    "Character", "Portrait", "Project", "Story",
    "PORTRAIT_SIZE", "HEAD_SIZE", "BACKGROUND_SIZE", "STAGE_SIZE",
    "ROOT_HALF_WIDTH", "fit_rect", "place_for_center_x", "generate_for_document",
    "ERROR", "WARNING", "INFO", "Issue", "has_errors",
    "validate_character", "validate_story", "validate_project",
]
