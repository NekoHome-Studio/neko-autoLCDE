"""路径解析 —— 同一份源码既能在 LCDE 仓库里跑，也能作为**独立工具包**运行。

两种运行模式（**不做任何硬编码路径**，靠探测）：

============ ============================================ ==========================
模式         判定                                          默认落点
============ ============================================ ==========================
仓库模式     ``<LDCEROOT>/tools/aiplay/``，上级有 SPEC.md  产出 → ``<LDCEROOT>/projects``
独立模式     其余情形（分发包 / 单文件 .pyz）              产出 → **当前工作目录**
============ ============================================ ==========================

格式内核（``lcde``）按以下顺序寻找，谁先命中用谁：

1. ``<工具根>/_vendor``（若把内核收进子目录）
2. ``<工具根>``（分发包的常见形态：``aiplay/`` 与 ``lcde/`` 同级）
3. ``<LDCEROOT>/tools``（仓库模式）

单文件 ``.pyz`` 里没有真实目录，``import lcde`` 由 zipimport 直接从归档根解决，
所以这里一次都不需要命中——仍然能正常工作。

配置文件同样是「跟随工具」：优先工具根，其次当前目录；已存在的那份优先读。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "PKG_DIR", "TOOL_ROOT", "REPO_ROOT", "is_repo_mode", "workspace_root",
    "projects_root", "lcde_roots", "lcde_source_root", "config_candidates",
    "resolve_config_path", "default_config_path", "describe",
]

#: ``aiplay`` 包目录本身。
PKG_DIR = Path(__file__).resolve().parent


def _detect_tool_root() -> Path:
    """工具的根目录：仓库里是 ``tools/``，分发包里是包根。

    单文件 ``.pyz`` 下 ``PKG_DIR.parent`` 是归档内路径（不是真实目录），
    这种情况退回当前工作目录，保证配置/产出有地方可写。
    """
    parent = PKG_DIR.parent
    return parent if parent.is_dir() else Path.cwd()


TOOL_ROOT = _detect_tool_root()

#: 仓库根（仓库模式下才有意义；独立包里只是「工具根的上一级」）。
REPO_ROOT = TOOL_ROOT.parent

#: 配置文件在工具根下的固定名字。
CONFIG_NAME = "aiplay.config.json"


def is_repo_mode() -> bool:
    """是不是在 LCDE 仓库里跑（判据：``tools/`` 的上一级有 SPEC.md 与 read.md）。"""
    return (TOOL_ROOT.name == "tools"
            and (REPO_ROOT / "SPEC.md").is_file())


def workspace_root() -> Path:
    """默认产出目录的基准：仓库模式给仓库根，独立模式给当前工作目录。"""
    return REPO_ROOT if is_repo_mode() else Path.cwd()


def projects_root() -> Path:
    return workspace_root() / "projects"


def lcde_roots() -> list[Path]:
    """``lcde`` 内核的候选根目录（按优先级）。"""
    return [TOOL_ROOT / "_vendor", TOOL_ROOT, REPO_ROOT / "tools"]


def lcde_source_root() -> Path | None:
    """真正提供 ``lcde`` 的那个根目录；找不到返回 ``None``（例如 .pyz 里）。"""
    for root in lcde_roots():
        if (root / "lcde" / "__init__.py").is_file():
            return root
    return None


def config_candidates() -> list[Path]:
    """配置文件候选位置：工具根优先，其次当前目录（便携包可随包带走配置）。"""
    if is_repo_mode():
        return [REPO_ROOT / CONFIG_NAME, TOOL_ROOT / CONFIG_NAME]
    return [TOOL_ROOT / CONFIG_NAME, Path.cwd() / CONFIG_NAME]


def default_config_path() -> Path:
    """写入时用的默认位置（候选里的第一个）。"""
    return config_candidates()[0]


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path:
    """按 ``--config`` → ``AIPLAY_CONFIG`` → 已存在的候选 → 主候选 解析配置路径。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("AIPLAY_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in config_candidates():
        if candidate.is_file():
            return candidate
    return default_config_path()


def describe() -> str:
    """一行摘要，``doctor`` 里用来交代「现在是哪种模式」。"""
    mode = "仓库模式" if is_repo_mode() else "独立模式"
    source = lcde_source_root()
    return ("%s｜工具根 %s｜格式内核 %s｜产出基准 %s"
            % (mode, TOOL_ROOT, source if source else "（归档内自带）", workspace_root()))
