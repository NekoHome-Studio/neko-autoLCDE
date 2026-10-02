"""aiplay 命令行界面。

    python tools/aiplay.py --help
    python tools/aiplay.py providers                 # 看看支持哪几家
    python tools/aiplay.py init                      # 生成配置模板
    $env:DEEPSEEK_API_KEY = "sk-..."
    python tools/aiplay.py doctor --ping             # 先确认密钥能用
    python tools/aiplay.py gen --premise "雪夜，末班车十年前就停运了，可还有人每天来等"
    python tools/aiplay.py web                       # 网页界面

退出码与 ``lcde`` 对齐：``0`` 通过、``1`` 运行失败、``2`` 校验有错。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import GENERATOR, __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    PROVIDERS,
    ConfigError,
    config_path,
    load_config_file,
    resolve_settings,
    write_config_template,
)
from .errors import AIPlayError, APIError, ScriptError
from .ir import sanitize_title
from .llm import make_client
from .paths import projects_root
from .pipeline import Pipeline, RunOptions, compile_existing

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2
EXIT_CONFIG = 3

_LEVEL_MARK = {"stage": "▸", "info": "·", "warn": "!", "error": "✗", "debug": "  "}
#: 终端编码装不下上面那些符号时的退路（老 GBK 控制台常见）。
_ASCII_MARK = {"stage": ">", "info": "-", "warn": "!", "error": "x", "debug": "  "}

#: 本次运行实际使用的提示符号（由 :func:`force_utf8_output` 按终端编码定）。
_ACTIVE_MARK = dict(_LEVEL_MARK)


def force_utf8_output() -> dict:
    """决定输出编码与提示符号。**独立工具要在别人的控制台里跑，这里必须自适应。**

    * 真实终端（tty）：保持控制台自己的编码（简体中文 Windows 上是 GBK/936），
      否则中文会反过来变成乱码；
    * 被管道或文件重定向（非 tty，例如被另一个程序捕获、写日志）：
      统一钉成 UTF-8，避免上游按 UTF-8 解码时读成乱码；
    * 两种情况都开 ``errors="replace"``，保证任何字符都不会让程序崩掉。

    返回该用哪套提示符号（终端编码装不下 ``▸ ✗`` 就退化成 ASCII）。
    """
    global _ACTIVE_MARK
    marks = _LEVEL_MARK
    for stream in (sys.stdout, sys.stderr):
        try:
            is_tty = stream.isatty()
        except (AttributeError, ValueError, OSError):
            is_tty = False
        try:
            if is_tty:
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):                 # pragma: no cover
            pass
        encoding = (getattr(stream, "encoding", "") or "utf-8")
        try:
            "▸✗·…".encode(encoding)
        except (UnicodeEncodeError, LookupError):
            marks = _ASCII_MARK
    _ACTIVE_MARK = dict(marks)
    return marks


def _out(text: str = "") -> None:
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:                               # pragma: no cover
        sys.stdout.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
        sys.stdout.flush()


def _out(text: str = "") -> None:
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:                               # pragma: no cover
        sys.stdout.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 子命令：init / providers / doctor
# --------------------------------------------------------------------------- #

def cmd_init(args) -> int:
    path = Path(args.output) if args.output else config_path(args.config)
    try:
        written = write_config_template(path, force=args.force)
    except ConfigError as exc:
        _out("错误：%s" % exc)
        return EXIT_CONFIG
    _out("已写出配置模板：%s" % written)
    _out("")
    _out("接着做两件事：")
    _out("  1) 把密钥填进环境变量（推荐）或该文件的 \"apiKey\" 字段：")
    _out("       $env:DEEPSEEK_API_KEY = \"sk-...\"")
    _out("  2) 验证连通性：")
    _out("       python tools/aiplay.py doctor --ping")
    _out("")
    _out("提示：该文件可能含密钥，别提交到版本库。")
    return EXIT_OK


def cmd_providers(args) -> int:
    rows = []
    for name, preset in sorted(PROVIDERS.items()):
        key_env = " / ".join(preset.key_env) or "—"
        rows.append((name, preset.base_url or "（需自定义）", preset.model or "（需自定义）", key_env))
    if args.json:
        _out(json.dumps({name: {
            "base_url": preset.base_url, "model": preset.model,
            "key_env": list(preset.key_env), "note": preset.note,
        } for name, preset in sorted(PROVIDERS.items())}, ensure_ascii=False, indent=2))
        return EXIT_OK
    widths = [max(len(str(row[i])) for row in rows + [("provider", "base_url", "默认模型", "密钥环境变量")])
              for i in range(4)]
    header = ("provider", "base_url", "默认模型", "密钥环境变量")
    _out("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    _out("  ".join("-" * w for w in widths))
    for row in rows:
        _out("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    _out("")
    for name, preset in sorted(PROVIDERS.items()):
        if preset.note:
            _out("* %s：%s" % (name, preset.note))
    _out("")
    _out("用 --provider <名字> 切换；任意 OpenAI 兼容服务都可以用 --base-url + --model 接上。")
    return EXIT_OK


def cmd_doctor(args) -> int:
    path = config_path(args.config)
    ok = True
    _out("aiplay %s" % __version__)
    _out("")
    try:
        from . import paths
        _out("运行模式：%s" % paths.describe())
    except Exception as exc:                                  # noqa: BLE001
        _out("运行模式：（探测失败：%s）" % exc)
    _out("配置文件：%s %s" % (path, "（存在）" if path.is_file() else "（不存在，可选）"))
    try:
        load_config_file(path)
    except ConfigError as exc:
        _out("! %s" % exc)
        ok = False
    try:
        settings = resolve_settings(args, require_key=False)
    except ConfigError as exc:
        _out("✗ %s" % exc)
        return EXIT_CONFIG

    _out("")
    _out(settings.describe())
    _out("")
    _out("完整地址：%s" % settings.endpoint)
    if not settings.api_key and not settings.is_mock:
        _out("! 没有密钥")
        ok = False

    if args.ping:
        _out("")
        _out("正在 ping 模型……")
        try:
            client = make_client(settings, log=lambda *_a: None)
            reply = client.ping()
            _out("✓ 通！模型回复：%s" % reply)
            _out("  %s" % client.usage.summary())
        except APIError as exc:
            _out("✗ 调用失败：%s" % exc)
            _out("  建议：%s" % exc.hint)
            return EXIT_ERROR
        except AIPlayError as exc:
            _out("✗ %s" % exc)
            return EXIT_ERROR
    _out("")
    _out("结论：%s" % ("可以开工" if ok else "还有问题没解决（见上面的 ! 行）"))
    return EXIT_OK if ok else EXIT_CONFIG


# --------------------------------------------------------------------------- #
# 子命令：gen / plan
# --------------------------------------------------------------------------- #

def _run_options(args, *, out_dir: Path | None = None) -> RunOptions:
    """把命令行命名空间变成 :class:`RunOptions`。

    用 ``getattr`` 取带默认值——``gen`` 与 ``plan`` 的参数集不完全一样，
    少哪个就按默认值走，不必让两个子命令都背上一堆无意义的开关。
    """
    def get(name, default=None):
        value = getattr(args, name, None)
        return default if value is None else value

    premise = _premise_of(args)
    title = get("title", "") or (premise[:12] if premise else "")
    out = Path(out_dir) if out_dir else Path(get("out") or (projects_root() / _slug(title)))
    targets = {
        "left": float(get("x_left", 220.0)),
        "center": float(get("x_center", 400.0)),
        "right": float(get("x_right", 580.0)),
    }
    codes = tuple(code.strip().upper() for code in (get("repair_warnings", "") or "").split(",")
                  if code.strip())
    return RunOptions(
        out_dir=out, premise=premise, title=get("title", "") or "",
        style=get("style", "") or "", tone=get("tone", "") or "",
        scenes=int(get("scenes", 8)), cast=int(get("cast", 4)), duration=int(get("duration", 12)),
        language=get("language", "简体中文"), audience=get("audience", "") or "",
        constraints=get("constraints", "") or "",
        scenes_per_call=int(get("scenes_per_call", 2)),
        build_files=not get("no_build", False), placeholder=not get("no_placeholder", False),
        repair_rounds=int(get("repair_rounds", 2)), repair_warning_codes=codes,
        save_dir=Path(args.save_dir) if get("save_dir") else None,
        json_name=get("json_name", "") or "",
        concept_path=Path(get("concept")) if get("concept") else None,
        stage_targets=targets, force_placeholder=bool(get("force_placeholder", False)),
        mock_flaws=int(get("mock_flaws", 0) or 0), dry_run=bool(get("dry_run", False)),
        verbose=bool(get("verbose", False)),
    )


def _premise_of(args) -> str:
    if getattr(args, "premise_file", None):
        path = Path(args.premise_file)
        if not path.is_file():
            raise ConfigError("找不到梗概文件：%s" % path)
        return path.read_text(encoding="utf-8").strip()
    premise = (args.premise or "").strip()
    if not premise and args.premise_rest:
        premise = " ".join(args.premise_rest).strip()
    if not premise and getattr(args, "concept", None):
        # --concept 已经带着骨架了，梗概只是给分场阶段的风格参考，允许缺省
        concept_path = Path(args.concept)
        if concept_path.is_file():
            try:
                data = json.loads(concept_path.read_text(encoding="utf-8-sig"))
            except ValueError:
                data = {}
            premise = (data.get("logline") or data.get("title") or "").strip()
    if not premise:
        raise ConfigError("请用 --premise 给出主题/梗概（也可以用 --premise-file 指向一个文本文件）")
    return premise


def _slug(text: str) -> str:
    clean = sanitize_title(text or "aiplay项目", fallback="aiplay项目")
    return clean.replace(" ", "")[:24] or "aiplay项目"


def _make_logger(args):
    def log(level: str, message: str) -> None:
        if level == "debug" and not args.verbose:
            return
        mark = _ACTIVE_MARK.get(level) or _ACTIVE_MARK["info"]
        _out("%s %s" % (mark, message) if mark.strip() else message)
    return log


def _summary(result) -> dict:
    return {
        "ok": result.ok,
        "out_dir": str(result.out_dir),
        "save_dir": str(result.save_dir) if result.save_dir else None,
        "title": result.play.title if result.play else None,
        "scenes": len(result.play.scenes) if result.play else 0,
        "commands": len(result.compile_result.commands) if result.compile_result else 0,
        "stats": result.compile_result.stats if result.compile_result else {},
        "errors": [{"code": i.code, "message": i.message, "location": i.location}
                   for i in result.errors],
        "warnings": [{"code": i.code, "message": i.message} for i in result.warnings],
        "repairs": result.repairs,
        "elapsed": round(result.elapsed, 1),
        "usage": result.usage_text,
        "artifacts": result.artifacts,
        "error": result.error,
    }


def _report_to_user(result, args) -> int:
    if result.error:
        _out("")
        _out("✗ 失败：%s" % result.error)
        return EXIT_ERROR
    _out("")
    if result.play is not None:
        stats = result.compile_result.stats
        _out("剧本《%s》：%d 场 / %d 角色 / %d 背景 / %d 条命令（台词类 %d 句，粗估 %s 分钟）"
             % (result.play.title, stats["scenes"], stats["cast"], stats["backgrounds"],
                stats["commands"], stats["spoken"], stats["duration_estimate"]))
    if result.usage_text:
        _out("用量：%s，耗时 %.1fs" % (result.usage_text, result.elapsed))
    if result.repairs:
        _out("回喂重写：%d 轮" % len(result.repairs))
    errors = result.errors
    warnings = result.warnings
    if result.issues:
        _out("校验：%d 错误 / %d 警告" % (len(errors), len(warnings)))
        for issue in errors:
            _out("  %s" % issue)
    if not result.error and result.compile_result is not None:
        fixed = result.compile_result.warnings
        if fixed:
            _out("自动修正 %d 处（详见 生成报告.md）" % len(fixed))
    _out("")
    _out("产出目录：%s" % result.out_dir)
    for name, description in result.artifacts.items():
        _out("  %-18s %s" % (name, description))
    _out("")
    _out("装进游戏：把 %s 合并到 %%USERPROFILE%%\\AppData\\LocalLow\\Soalin\\LCDE"
         % (result.save_dir or (result.out_dir / "save")))
    return EXIT_INVALID if errors else EXIT_OK


def cmd_gen(args) -> int:
    settings = resolve_settings(args)
    options = _run_options(args)
    log = _make_logger(args)

    events = []
    pipeline = Pipeline(settings, options, log=log,
                       event=lambda name, payload: events.append((name, payload)))
    result = pipeline.run()

    if args.json:
        _out(json.dumps(_summary(result), ensure_ascii=False, indent=2))
        return EXIT_INVALID if result.errors or result.error else EXIT_OK
    return _report_to_user(result, args)


def cmd_plan(args) -> int:
    """只做概念设计：便宜、快，用来定骨架。"""
    settings = resolve_settings(args)
    options = _run_options(args)
    log = _make_logger(args)

    pipeline = Pipeline(settings, options, log=log)
    pipeline.client = make_client(
        settings, log=log,
        **({"flaws": options.mock_flaws} if settings.is_mock else {}))
    from .prompts import concept_messages
    from .llm import extract_json

    text = pipeline.client.chat(concept_messages(options.brief), stage="concept",
                               meta={"scenes": options.scenes})
    concept = extract_json(text, expect="object")
    out = Path(options.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "概念.json"
    path.write_text(json.dumps(concept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        _out(json.dumps({"ok": True, "path": str(path), "concept": concept}, ensure_ascii=False,
                        indent=2))
        return EXIT_OK
    _out("")
    _out("《%s》—— %s" % (concept.get("title", "?"), concept.get("logline", "")))
    _out("")
    _out("角色：")
    for member in concept.get("cast") or []:
        _out("  · %s（%s）表情：%s%s"
             % (member.get("id"), member.get("camp") or "—",
                " / ".join(member.get("faces") or []),
                "　[字幕角色]" if member.get("subtitles") else ""))
    _out("")
    _out("背景：%s" % "、".join(bg.get("name", "?") if isinstance(bg, dict) else str(bg)
                              for bg in (concept.get("backgrounds") or [])))
    audio = concept.get("audio") or {}
    _out("音频：BGM %d 条 / SFX %d 条"
         % (len(audio.get("bgm") or []), len(audio.get("sfx") or [])))
    _out("")
    _out("场次：")
    for scene in concept.get("scenes") or []:
        _out("  场%s %s（%s，BGM %s）"
             % (scene.get("index", "?"), scene.get("title", ""), scene.get("background", ""),
                scene.get("bgm", "—")))
        if scene.get("summary"):
            _out("      %s" % scene["summary"])
    _out("")
    _out("已写出 %s" % path)
    _out("审一遍，满意就把这份骨架直接拿去写分场（概念阶段不再重复调用 API）：")
    _out("  python tools/aiplay.py gen --concept \"%s\"" % path)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 子命令：compile
# --------------------------------------------------------------------------- #

def cmd_compile(args) -> int:
    log = _make_logger(args)
    result = compile_existing(args.script, save_dir=args.save_dir, out_dir=args.out,
                              log=log, rebuild=not args.no_rebuild)
    if args.json:
        _out(json.dumps(_summary(result), ensure_ascii=False, indent=2))
    else:
        code = _report_to_user(result, args)
        if code:
            return code
    return EXIT_INVALID if result.errors else EXIT_OK


# --------------------------------------------------------------------------- #
# 子命令：web
# --------------------------------------------------------------------------- #

def cmd_web(args) -> int:
    from .web import serve

    return serve(host=args.host, port=args.port, open_browser=args.open,
                 token=args.token, config=args.config, provider=args.provider)


# --------------------------------------------------------------------------- #
# 子命令：pack
# --------------------------------------------------------------------------- #

def cmd_pack(args) -> int:
    """把 aiplay 打成可以单独拷走的独立工具包（便携目录 + zip + 单文件 .pyz）。"""
    from .package import build_package
    from .paths import workspace_root

    log = _make_logger(args)
    out = Path(args.out) if args.out else (workspace_root() / "dist")
    _out("aiplay %s —— 打包独立工具" % __version__)
    _out("产出目录：%s" % out)
    _out("")
    try:
        result = build_package(out_dir=out, with_example=not args.no_example,
                               make_zip=not args.no_zip, make_pyz=not args.no_pyz,
                               verify=not args.no_verify, public=args.public, log=log)
    except OSError as exc:
        sys.stderr.write("打包失败：%s\n" % exc)
        return EXIT_ERROR

    _out("")
    _out(result.summary())
    if result.example and result.example.is_dir():
        _out("")
        _out("离线示例（不需要密钥就能看产物）：")
        _out("  %s" % result.example)
    if result.checks:
        _out("")
        _out("打包自检：")
        for item in result.checks:
            _out("  %s %s —— %s" % ("✓" if item["ok"] else "✗", item["name"], item["detail"]))
    if result.warnings:
        _out("")
        for message in result.warnings:
            _out("! %s" % message)
    _out("")
    if not result.ok:
        _out("✗ 自检未全部通过，先别分发这个包。")
        return EXIT_ERROR
    _out("用法：解压后")
    _out("  python aiplay.py doctor --ping")
    _out("  python aiplay.py gen --premise \"...\"")
    _out("或直接跑单文件版：python aiplay.pyz --help")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiplay",
        description="%s —— 接外部大模型 API 的 LCDE 剧本生成器" % GENERATOR,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
例子：
  python tools/aiplay.py providers
  python tools/aiplay.py init
  python tools/aiplay.py doctor --ping
  python tools/aiplay.py gen --premise "末班车停了十年，还有人每天来等" --scenes 6
  python tools/aiplay.py plan --premise "..."            # 只出设定与场次表
  python tools/aiplay.py compile projects\\末班雪\\剧本.json
  python tools/aiplay.py web
""")
    parser.add_argument("--config", help="配置文件路径（默认 %s）" % DEFAULT_CONFIG_PATH)
    parser.add_argument("--provider", help="提供方：%s" % ", ".join(sorted(PROVIDERS)))
    parser.add_argument("--base-url", help="OpenAI 兼容的接口地址（写到 /v1 这一层）")
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--api-key", help="密钥（更推荐用环境变量，避免留在命令历史里）")
    parser.add_argument("--temperature", type=float, help="采样温度（默认 1.0）")
    parser.add_argument("--max-tokens", type=int, help="单次回复上限")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数")
    parser.add_argument("--retries", type=int, help="失败重试次数")
    parser.add_argument("--no-json-mode", dest="json_mode", action="store_false", default=None,
                        help="不发送 response_format（给不支持 JSON 模式的服务用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机读）")
    parser.add_argument("--verbose", action="store_true", help="打印调试信息")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="写出配置文件模板")
    p.add_argument("-o", "--output", help="输出路径（默认仓库根 aiplay.config.json）")
    p.add_argument("--force", action="store_true", help="覆盖已有文件")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("providers", help="列出支持的提供方")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("doctor", help="检查配置与密钥，--ping 会真的调一次")
    p.add_argument("--ping", action="store_true", help="发一次最小请求验证连通")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gen", help="一条命令：生成剧本 → 编译 → 占位素材 → 游戏文件 → 校验")
    _add_brief_args(p)
    p.add_argument("--out", help="产出目录（默认 projects\\<标题>）")
    p.add_argument("--concept", help="复用 aiplay plan 产出的概念.json，跳过概念阶段的 API 调用")
    p.add_argument("--save-dir", help="游戏文件输出目录（默认 <产出目录>\\save）")
    p.add_argument("--json-name", help="规范文档文件名（默认 <标题>.json）")
    p.add_argument("--no-build", action="store_true", help="只生成 JSON，不写游戏文件、不校验")
    p.add_argument("--no-placeholder", action="store_true", help="不生成占位素材")
    p.add_argument("--force-placeholder", action="store_true", help="占位素材覆盖同名文件")
    p.add_argument("--repair-rounds", type=int, default=2,
                   help="校验失败时回喂重写的最大轮数（默认 2，0 表示关闭）")
    p.add_argument("--repair-warnings", default="",
                   help="除错误外，把这些警告码也当作要修（如 S20,S6）")
    p.add_argument("--mock-flaws", type=int, default=None,
                   help="调试用：让 mock 故意产出非法命令，触发校验→回喂闭环")
    p.add_argument("--dry-run", action="store_true", help="照常调 API，但不写任何文件")
    p.add_argument("--x-left", type=float, help="立绘左侧槽位的舞台横向中心（默认 220）")
    p.add_argument("--x-center", type=float, help="中间槽位（默认 400）")
    p.add_argument("--x-right", type=float, help="右侧槽位（默认 580）")
    _add_machine_output_flag(p)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("plan", help="只做概念设计（选角/背景/场次表），便宜")
    _add_brief_args(p)
    p.add_argument("--out", help="输出目录（默认 projects\\<标题>）")
    p.add_argument("--mock-flaws", type=int, default=None, help=argparse.SUPPRESS)
    _add_machine_output_flag(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("compile", help="从 剧本.json 重新编译（不调 API）")
    p.add_argument("script", help="aiplay 的剧本 IR（剧本.json）")
    p.add_argument("--out", help="产出目录（默认 IR 所在目录）")
    p.add_argument("--save-dir", help="游戏文件输出目录")
    p.add_argument("--no-rebuild", action="store_true", help="只写 JSON 与文档，不写游戏文件")
    _add_machine_output_flag(p)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("web", help="启动本地网页界面")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    p.add_argument("--port", type=int, default=8756, help="端口（默认 8756）")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.add_argument("--token", help="访问口令（给了就要求 ?token=…）")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("pack", help="打包成独立工具（便携目录 + zip + 单文件 .pyz）")
    p.add_argument("--out", help="产物输出目录（默认 <产出基准>\\dist）")
    p.add_argument("--no-example", action="store_true", help="不生成 examples 离线示例")
    p.add_argument("--no-zip", action="store_true", help="不生成 zip")
    p.add_argument("--no-pyz", action="store_true", help="不生成单文件 .pyz")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过打包自检（不建议：自检会在新目录里真跑一遍）")
    p.add_argument("--public", action="store_true",
                   help="公开分发构建：抹掉 build-info.json 里的本机路径与系统信息")
    p.set_defaults(func=cmd_pack)

    return parser


def _add_machine_output_flag(parser: argparse.ArgumentParser) -> None:
    """给子命令也挂上 ``--json``。

    否则 ``gen --json`` 会被 argparse 的前缀匹配吃掉，变成 ``--json-name`` 少参数的报错。
    """
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出（机读）")


def _add_brief_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--premise", help="主题/梗概（一句话到一段话都行）")
    parser.add_argument("--premise-file", help="从文本文件读梗概（长篇设定用这个）")
    parser.add_argument("premise_rest", nargs="*", help="也可以直接跟在命令后面写梗概")
    parser.add_argument("--title", help="期望标题")
    parser.add_argument("--style", help="风格/致敬对象，例如「冷幽默的都市短篇」")
    parser.add_argument("--tone", help="情绪基调")
    parser.add_argument("--scenes", type=int, default=8, help="场次数（默认 8）")
    parser.add_argument("--cast", type=int, default=4, help="主要角色数（默认 4）")
    parser.add_argument("--duration", type=int, default=12, help="目标时长分钟（默认 12）")
    parser.add_argument("--language", default="简体中文", help="台词语言")
    parser.add_argument("--audience", help="受众")
    parser.add_argument("--constraints", help="硬性约束，例如「不要出现死亡」「全年龄」")
    parser.add_argument("--scenes-per-call", type=int, default=2,
                        help="每次 API 调用连写几场（1 最稳，2~3 更连贯更省调用次数）")


def main(argv=None) -> int:
    force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "mock_flaws", None) is None:
        args.mock_flaws = int(os.environ.get("AIPLAY_MOCK_FLAWS") or 0)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except ConfigError as exc:
        sys.stderr.write("配置错误：%s\n" % exc)
        return EXIT_CONFIG
    except APIError as exc:
        sys.stderr.write("API 错误：%s\n  建议：%s\n" % (exc, exc.hint))
        return EXIT_ERROR
    except ScriptError as exc:
        sys.stderr.write("剧本错误：%s\n" % exc)
        return EXIT_ERROR
    except AIPlayError as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断\n")
        return EXIT_ERROR
