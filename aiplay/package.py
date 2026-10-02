"""打包器 —— 把 aiplay「推出去」成一个**独立可搬走的工具包**。

产物（默认落在 ``<产出基准>/dist``，仓库模式下就是 ``<仓库根>/dist``）：

============================ ====================================================
``aiplay-<版本>/``            便携目录：解压即用，``python aiplay.py …``
``aiplay-<版本>.zip``         同上的压缩包（UTF-8 文件名，Windows 资源管理器可解）
``aiplay.pyz``                单文件版：``python aiplay.pyz …``，一个文件走天下
============================ ====================================================

打包内容与「为什么是独立的」：

* ``aiplay/`` —— 生成器本体；
* ``lcde/`` + ``lcde.py`` + ``schema/`` —— **内置格式内核**（来自 ``tools/lcde``，
  原样拷贝、不做改动）。aiplay 从不重复实现格式细节，但它离不开内核，
  所以内核跟着一起走；附带的好处是独立包里也能直接用
  ``python lcde.py validate`` 检查游戏存档目录。

**打包即自检**：构建完会在新目录里真的跑一遍（``--provider mock`` 生成一部短剧、
再用内置 ``lcde.py`` 校验），并试跑单文件版。产物坏了当场就会失败，
而不是等用户解压后才发现少文件。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .bridge import FORMAT_VERSION, LCDE_VERSION
from .paths import PKG_DIR, TOOL_ROOT, lcde_roots, lcde_source_root, workspace_root

__all__ = ["PackResult", "build_package", "package_name"]

#: 便携目录名（带版本号，方便并存多个版本）。
PACKAGE_PREFIX = "aiplay"

#: 覆盖前用来确认「这个目录是我们自己上一次打的包」的标记文件。
MARKER = "build-info.json"

#: 包内离线示例的目录名（相对包根，故意不带绝对路径）。
EXAMPLE_NAME = "末班雪（离线示例）"

#: 单文件版在归档根需要的入口（zipimport 从归档根解析 import）。
ZIPAPP_MAIN = '''\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aiplay 单文件版入口。

    python aiplay.pyz --help
    python aiplay.pyz --provider mock gen --premise "..."

包内自带 LCDE 格式内核，不需要仓库、不需要安装。
"""

import sys

from aiplay.cli import main

if __name__ == "__main__":
    sys.exit(main())
'''

#: 便携目录里的 __main__.py（这样 `python <目录>` 也能直接跑）。
DIR_MAIN = '''\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便携目录入口：``python <本目录>`` 等价于 ``python aiplay.py``。"""

import sys

from aiplay.cli import main

if __name__ == "__main__":
    sys.exit(main())
'''

SHEBANG = b"#!/usr/bin/env python3\n"


#: 便携目录里的 Windows 启动器。写成常量而不是外部模板文件：
#: 这样「在独立包里再打包一次」时不会因为找不到模板而失败。
LAUNCHER_CMD = """\
@echo off
rem ===========================================================================
rem  aiplay launcher - finds a Python interpreter and forwards all arguments.
rem  ASCII only on purpose: this file must render correctly in any console code page.
rem ===========================================================================
setlocal
set "HERE=%~dp0"
set "PY="

where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [aiplay] Python not found. Install Python 3.9+ and make sure "python" is on PATH.
  echo [aiplay] Download: https://www.python.org/downloads/
  exit /b 1
)

%PY% "%HERE%aiplay.py" %*
exit /b %ERRORLEVEL%
"""


def package_name(version: str = __version__) -> str:
    return "%s-%s" % (PACKAGE_PREFIX, version)


@dataclass
class PackResult:
    stage: Path
    zip_path: Path | None = None
    pyz_path: Path | None = None
    files: int = 0
    bytes_written: int = 0
    example: Path | None = None
    checks: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks) if self.checks else True

    def summary(self) -> str:
        parts = ["便携目录 %s（%d 个文件，%.2f MB）"
                 % (self.stage, self.files, self.bytes_written / 1024 / 1024)]
        if self.zip_path:
            parts.append("压缩包 %s（%.2f MB）"
                         % (self.zip_path, self.zip_path.stat().st_size / 1024 / 1024))
        if self.pyz_path:
            parts.append("单文件 %s（%.2f MB）"
                         % (self.pyz_path, self.pyz_path.stat().st_size / 1024 / 1024))
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 收集要拷的东西
# --------------------------------------------------------------------------- #

def _py_files(directory: Path) -> list[Path]:
    """目录下的 ``*.py``（不含 __pycache__），按名字排序保证打包可复现。"""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.py") if path.is_file())


def _kernel_sources() -> tuple[Path, list[Path], list[Path]]:
    """返回 ``(内核根, lcde 包文件, schema 文件)``。"""
    root = lcde_source_root()
    if root is None:
        raise FileNotFoundError(
            "找不到 lcde 格式内核；它应当在 %s 之一里"
            % "、".join(str(path) for path in lcde_roots()))
    package = _py_files(root / "lcde")
    if not package:
        raise FileNotFoundError("内核目录里没有 .py：%s" % (root / "lcde"))
    schema = sorted((root / "schema").glob("*.json")) if (root / "schema").is_dir() else []
    return root, package, schema


def _copy(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.stat().st_size


def _find_first(*candidates: Path | None) -> Path | None:
    """第一个存在的文件。

    打包器要能在**两种布局**里找到素材：仓库里（``README.standalone.md`` /
    ``launcher.cmd`` 在包目录下）和上一个独立包里（README 已改名为 ``README.md``、
    启动器叫 ``aiplay.cmd``、模板文件不存在）。找不到就返回 None，由调用方降级处理。
    """
    for candidate in candidates:
        if candidate is not None and Path(candidate).is_file():
            return Path(candidate)
    return None


def _standalone_readme() -> Path | None:
    return _find_first(
        PKG_DIR / "README.standalone.md",          # 仓库布局
        TOOL_ROOT / "README.standalone.md",
        TOOL_ROOT / "README.md",                   # 独立包布局（已改名）
        PKG_DIR / "README.md",
    )


def _license_source() -> Path | None:
    """许可文件：仓库里是 ``aiplay/LICENSE``，独立包里在包根。"""
    return _find_first(PKG_DIR / "LICENSE", TOOL_ROOT / "LICENSE")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def build_package(*, out_dir: Path | str | None = None, version: str = __version__,
                  with_example: bool = True, make_zip: bool = True, make_pyz: bool = True,
                  verify: bool = True, public: bool = False, log=None) -> PackResult:
    """打出独立工具包。

    ``public=True`` 时会把打包机器相关的信息（本机检出路径、操作系统版本）
    从 ``build-info.json`` 里抹掉——准备公开分发时用这个。
    """
    log = log or (lambda level, message: None)
    dist = Path(out_dir) if out_dir else (workspace_root() / "dist")
    stage = dist / package_name(version)
    kernel_root, kernel_files, schema_files = _kernel_sources()

    # ---- 别把自己删了 ---------------------------------------------------- #
    # 「在独立包里再打包一次」是文档里承诺的用法，所以必须挡住
    # 「输出目录正好覆盖正在运行的这份代码」这种自毁操作。
    try:
        Path(PKG_DIR).resolve().relative_to(stage.resolve())
    except ValueError:
        pass
    else:
        raise FileExistsError(
            "输出目录 %s 会覆盖正在运行的本工具；换个 --out" % stage)

    # ---- 准备目录（只敢替换自己上一次打的包） ---------------------------- #
    if stage.exists():
        if (stage / MARKER).is_file() or not any(stage.iterdir()):
            shutil.rmtree(stage)
        else:
            raise FileExistsError(
                "%s 已存在且不像是本工具打的包（缺 %s）；换个 --out，或先手工处理"
                % (stage, MARKER))
    stage.mkdir(parents=True)

    result = PackResult(stage=stage)
    written = 0
    count = 0

    def copy(src: Path, dst: Path) -> None:
        nonlocal written, count
        written += _copy(src, dst)
        count += 1

    # ---- 生成器本体 ------------------------------------------------------ #
    for path in _py_files(PKG_DIR):
        copy(path, stage / "aiplay" / path.name)

    readme = _standalone_readme()
    if readme is not None:
        copy(readme, stage / "README.md")
    else:                                                     # pragma: no cover
        result.warnings.append("没找到独立版 README，包里的 README.md 只能留空")
        (stage / "README.md").write_text(
            "# aiplay\n\nLCDE 剧本生成器（独立版）。`python aiplay.py --help`\n",
            encoding="utf-8")
        count += 1
    (stage / "aiplay.cmd").write_text(LAUNCHER_CMD, encoding="utf-8", newline="\r\n")
    written += len(LAUNCHER_CMD.encode("utf-8"))
    count += 1

    license_file = _license_source()
    if license_file is not None:
        copy(license_file, stage / "LICENSE")
    else:                                                     # pragma: no cover
        result.warnings.append("没找到 LICENSE，包内不带许可文件")

    # ---- 格式内核（原样，不改动） ---------------------------------------- #
    for path in kernel_files:
        copy(path, stage / "lcde" / path.name)
    kernel_entry = kernel_root / "lcde.py"
    if kernel_entry.is_file():
        copy(kernel_entry, stage / "lcde.py")
    for path in schema_files:
        copy(path, stage / "schema" / path.name)

    # ---- 入口 ------------------------------------------------------------ #
    entry = TOOL_ROOT / "aiplay.py"
    if entry.is_file():
        copy(entry, stage / "aiplay.py")
    else:                                                     # 从独立包里再打包
        (stage / "aiplay.py").write_text(
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
            '"""aiplay 命令行入口。"""\n\nimport sys\n\n'
            "from aiplay.cli import main\n\n"
            'if __name__ == "__main__":\n    sys.exit(main())\n', encoding="utf-8")
    (stage / "__main__.py").write_text(DIR_MAIN, encoding="utf-8")
    written += len(DIR_MAIN.encode("utf-8"))
    count += 1

    log("info", "已收集 %d 个文件（内核来自 %s）" % (count, kernel_root))

    # ---- 打包信息 -------------------------------------------------------- #
    info = {
        "name": PACKAGE_PREFIX,
        "version": version,
        "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "lcdeVersion": LCDE_VERSION,
        "formatVersion": FORMAT_VERSION,
        "license": "MIT",
        "entry": "aiplay.py",
        "singleFile": "%s.pyz" % PACKAGE_PREFIX,
        "note": "包内自带 LCDE 格式内核（lcde/），可脱离 LCDE 仓库独立运行。",
    }
    if public:
        info["builtFrom"] = "（公开构建，已省略本机路径）"
    else:
        info["builtFrom"] = str(TOOL_ROOT)
        info["python"] = platform.python_version()
        info["platform"] = platform.platform()
    (stage / MARKER).write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (stage / "VERSION").write_text("%s\n" % version, encoding="utf-8")
    count += 2

    # ---- 示例（同时是打包自检的一半） ------------------------------------ #
    if with_example and verify:
        result.example = _make_example(stage, log, result)

    # ---- 自检：在新目录里真的跑一遍 -------------------------------------- #
    if verify:
        _verify_stage(stage, log, result)

    _prune(stage)                     # 清掉自检留下的 __pycache__ 等
    result.files = count
    result.bytes_written = written

    # ---- 压缩包 ---------------------------------------------------------- #
    if make_zip:
        result.zip_path = _make_zip(dist, stage, log, result)
    if make_pyz:
        result.pyz_path = _make_pyz(dist, stage, log, result)
        if verify:
            _verify_pyz(result.pyz_path, dist, log, result)
    return result


# --------------------------------------------------------------------------- #
# 示例 / 自检
# --------------------------------------------------------------------------- #

def _run(cmd: list, cwd: Path, log_path: Path) -> int:
    """跑一个子进程，输出**重定向到文件**（不用管道，受限环境里更省事）。

    顺带关掉字节码落盘：自检是在要分发的目录里跑的，不能留下 ``__pycache__``。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("AIPLAY_CONFIG", "AIPLAY_PROVIDER", "AIPLAY_API_KEY",
                 "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        env.pop(name, None)
    with open(log_path, "w", encoding="utf-8", errors="replace") as handle:
        return subprocess.call([str(part) for part in cmd], cwd=str(cwd),
                               stdout=handle, stderr=subprocess.STDOUT, env=env)


def _prune(stage: Path) -> None:
    """打包前清掉自检留下的编译缓存与临时文件（产物要干净）。"""
    for cache in list(stage.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for junk in list(stage.rglob("*.pyc")) + list(stage.rglob("*.pyo")):
        junk.unlink(missing_ok=True)


def _record(result: PackResult, log, name: str, ok: bool, detail: str) -> bool:
    result.checks.append({"name": name, "ok": bool(ok), "detail": detail})
    log("info" if ok else "error", "%s %s：%s" % ("✓" if ok else "✗", name, detail))
    return ok


def _make_example(stage: Path, log, result: PackResult) -> Path:
    """在包里跑一次离线生成，产出 ``examples/`` 样例（也顺带证明包是活的）。

    刻意用**相对** ``--out``（配合 ``cwd=stage``）：这样示例里的路径
    （``saveDir``、生成报告里的 robocopy 片段）也是相对的——
    示例搬到任何地方都能用，公开分发时也不会泄露打包机器的绝对路径。
    """
    relative = Path("examples") / EXAMPLE_NAME
    out = stage / relative
    log("info", "在包里跑一次离线生成，产出示例……")
    code = _run([sys.executable, stage / "aiplay.py", "--provider", "mock", "gen",
                 "--premise", "四等小站的末班车停了十年，可每个雪夜都有人来等",
                 "--scenes", "3", "--scenes-per-call", "1", "--out", str(relative)],
                cwd=stage, log_path=stage / "build-example.log")
    _record(result, log, "包内离线生成", code == 0,
            "退出码 %d（日志 build-example.log）" % code)
    if code != 0:
        return out
    save = out / "save"
    code = _run([sys.executable, stage / "lcde.py", "--save-dir", save, "validate"],
                cwd=stage, log_path=stage / "build-validate.log")
    _record(result, log, "内置内核校验生成结果", code == 0, "退出码 %d" % code)
    # 自检日志不必留在包里
    for name in ("build-example.log", "build-validate.log"):
        (stage / name).unlink(missing_ok=True)
    return out


def _verify_stage(stage: Path, log, result: PackResult) -> None:
    """便携目录自检：``--help`` 能出、能列出提供方、能看到运行模式。"""
    code = _run([sys.executable, stage / "aiplay.py", "--provider", "mock", "providers"],
                cwd=stage, log_path=stage / ".check.log")
    text = (stage / ".check.log").read_text(encoding="utf-8", errors="replace")
    (stage / ".check.log").unlink(missing_ok=True)
    _record(result, log, "便携目录可运行", code == 0 and "deepseek" in text,
            "退出码 %d，输出 %d 字符" % (code, len(text)))

    code = _run([sys.executable, stage / "aiplay.py", "--provider", "mock", "doctor"],
                cwd=stage, log_path=stage / ".check2.log")
    text = (stage / ".check2.log").read_text(encoding="utf-8", errors="replace")
    (stage / ".check2.log").unlink(missing_ok=True)
    _record(result, log, "独立模式判定", "独立模式" in text,
            next((line.strip() for line in text.splitlines() if "运行模式" in line),
                 "没看到运行模式行"))


def _verify_pyz(pyz: Path, dist: Path, log, result: PackResult) -> None:
    """单文件版自检：真跑一次 plan（写到一个临时目录，检查完删掉）。"""
    tmp = dist / ".pyz-check"
    shutil.rmtree(tmp, ignore_errors=True)
    code = _run([sys.executable, pyz, "--provider", "mock", "plan",
                 "--premise", "单文件版自检", "--scenes", "1", "--out", tmp],
                cwd=dist, log_path=dist / ".pyz-check.log")
    ok = code == 0 and (tmp / "概念.json").is_file()
    detail = "退出码 %d" % code
    log_file = dist / ".pyz-check.log"
    if not ok and log_file.is_file():
        tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if tail:
            detail += "；末尾：%s" % tail[-1]
    _record(result, log, "单文件版可运行", ok, detail)
    shutil.rmtree(tmp, ignore_errors=True)
    log_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# zip / pyz
# --------------------------------------------------------------------------- #

def _make_zip(dist: Path, stage: Path, log, result: PackResult) -> Path:
    target = dist / ("%s.zip" % stage.name)
    target.unlink(missing_ok=True)
    log("info", "压缩成 %s……" % target.name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                # 存成 UTF-8 文件名（zipfile 会自动置 UTF-8 标志位）
                archive.write(path, str(Path(stage.name) / path.relative_to(stage)))
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    has_cjk = any(any("\u4e00" <= ch <= "\u9fff" for ch in name) for name in names)
    _record(result, log, "压缩包条目与中文名", bool(names) and has_cjk,
            "%d 个条目，中文名%s" % (len(names), "已保留" if has_cjk else "丢失！"))
    return target


def _make_pyz(dist: Path, stage: Path, log, result: PackResult) -> Path:
    target = dist / ("%s.pyz" % PACKAGE_PREFIX)
    target.unlink(missing_ok=True)
    log("info", "打包单文件版 %s……" % target.name)
    entries: list[tuple[str, Path]] = []
    for path in sorted((stage / "aiplay").glob("*.py")):
        entries.append(("aiplay/%s" % path.name, path))
    for path in sorted((stage / "lcde").glob("*.py")):
        entries.append(("lcde/%s" % path.name, path))
    for path in sorted((stage / "schema").glob("*.json")):
        entries.append(("schema/%s" % path.name, path))
    if (stage / "LICENSE").is_file():
        entries.append(("LICENSE", stage / "LICENSE"))

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", ZIPAPP_MAIN)
        archive.writestr("build-info.json", (stage / MARKER).read_text(encoding="utf-8"))
        for arcname, path in entries:
            archive.write(path, arcname)

    # 与官方 zipapp 一致：把 shebang 放在归档前面，于是在 POSIX 上也能直接执行
    target.write_bytes(SHEBANG + target.read_bytes())
    log("info", "单文件版 %d 个条目，%.2f MB"
        % (len(entries) + 2, target.stat().st_size / 1024 / 1024))
    return target
