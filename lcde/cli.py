"""``lcde`` 命令行：调取、检查、导出、生成 LCDE 的角色与剧本。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import FORMAT_VERSION, __version__
from .commands import BY_NAME, COMMANDS, ENUMS, UNUSABLE, enum_name
from .errors import FormatError, LcdeError, ValidationError
from .jsonfmt import (
    character_from_doc,
    character_to_doc,
    dumps,
    loads,
    project_from_doc,
    project_to_doc,
    story_from_doc,
    story_to_doc,
)
from .model import (
    DEFAULT_CAMP,
    DEFAULT_CAMP_COLOR,
    DEFAULT_EXPRESS,
    DEFAULT_NAME,
    DEFAULT_NAME_COLOR,
    DEFAULT_RECT,
    IMAGE_EXTENSIONS,
    Character,
    Portrait,
    Project,
    Story,
    default_save_dir,
    scan_portrait_images,
)
from .validate import ERROR, has_errors, validate_character, validate_project, validate_story

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2


# --------------------------------------------------------------------------- #
# 输出小工具
# --------------------------------------------------------------------------- #

def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _is_wide(ch: str) -> bool:
    import unicodedata

    return unicodedata.east_asian_width(ch) in ("W", "F")


def _ewidth(text: str) -> int:
    """按终端显示宽度估算（CJK 记 2 列）。"""
    return sum(2 if _is_wide(ch) else 1 for ch in text)


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max([_ewidth(h)] + [_ewidth(r[i]) for r in rows]) if rows else _ewidth(h)
              for i, h in enumerate(headers)]

    def render(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell + " " * (widths[i] - _ewidth(cell)))
        return "  ".join(parts).rstrip()

    lines = [render(headers), render(["-" * w for w in widths])]
    lines += [render(r) for r in rows]
    return "\n".join(lines)


def _fmt_number(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


# --------------------------------------------------------------------------- #
# 命令：env
# --------------------------------------------------------------------------- #

def cmd_env(args) -> int:
    project = Project(args.save_dir)
    if args.json:
        _out(dumps({
            "format": "lcde", "version": FORMAT_VERSION, "kind": "env",
            "toolVersion": __version__,
            "saveDir": str(project.save_dir),
            "saveDirExists": project.exists(),
            "imageExtensions": list(IMAGE_EXTENSIONS),
            "commandCount": len(COMMANDS),
            "unusableCommands": list(UNUSABLE),
        }))
        return EXIT_OK

    _out("lcde %s   （规范格式版本 %d）" % (__version__, FORMAT_VERSION))
    _out("存档目录 : %s" % project.save_dir)
    _out("目录状态 : %s" % ("存在" if project.exists() else "不存在（游戏还没运行过？）"))
    if project.exists():
        _out("            Character/ %d 个角色" % len(project.character_ids()))
        _out("            BG/        %d 张背景" % len(project.background_paths()))
        _out("            Dialog/    %d 个剧本" % len(project.story_titles()))
        settings = project.read_settings()
        if settings:
            _out("Setting.json: %s" % json.dumps(settings, ensure_ascii=False))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 命令：char
# --------------------------------------------------------------------------- #

def cmd_char_list(args) -> int:
    project = Project(args.save_dir)
    ids = project.character_ids()
    if args.json:
        _out(dumps({"format": "lcde", "version": FORMAT_VERSION, "kind": "char-list",
                    "characters": [character_to_doc(project.load_character(c)) for c in ids]}))
        return EXIT_OK
    if not ids:
        _out("（没有找到任何角色）")
        return EXIT_OK
    rows = []
    for char_id in ids:
        character = project.load_character(char_id)
        rows.append([
            char_id,
            character.name or "（空）",
            character.camp or "（空）",
            str(len(character.portraits)),
            "有" if character.head else "无",
            "#" + character.name_color,
        ])
    _out(table(rows, ["ID", "名字", "阵营", "立绘数", "头像", "名字色"]))
    return EXIT_OK


def cmd_char_show(args) -> int:
    project = Project(args.save_dir)
    character = project.load_character(args.id)
    if args.json:
        _out(dumps(character_to_doc(character)))
        return EXIT_OK if character.found else EXIT_INVALID

    _out("角色 %s" % character.char_id)
    _out("  配置文件 : %s" % (character.ch_file or "（不存在）"))
    if not character.found:
        _out("")
        _out("⚠ 配置文件不存在（引擎会回退为「读取失败」的占位角色）。")
        _out("  查找位置: %s" % (character.directory or "?"))
        return EXIT_INVALID
    _out("  目录     : %s" % (character.directory or "?"))
    _out("  名字     : %s" % (character.name or "（空）"))
    _out("  阵营     : %s" % (character.camp or "（空）"))
    _out("  名字颜色 : #%s" % character.name_color)
    _out("  阵营颜色 : #%s" % character.camp_color)
    _out("  express  : (%s, %s)" % (_fmt_number(character.express[0]),
                                    _fmt_number(character.express[1])))
    _out("  头像     : %s" % (character.head or "（无）"))
    _out("  剧本内引用该角色的写法: %s" % character.ch_relative_path)
    _out("")
    if character.portraits:
        rows = [[str(i), p.image or "（空）",
                 ", ".join(_fmt_number(v) for v in p.rect)] for i, p in enumerate(character.portraits)]
        _out(table(rows, ["face", "图片", "rect(x, y, w, h)"]))
    else:
        _out("（没有立绘）")

    if character.directory and character.directory.is_dir():
        on_disk = scan_portrait_images(character.directory)
        if [p.image for p in character.portraits] != on_disk:
            _out("")
            _out("注意：目录枚举顺序 %s 与上面声明的不一致。" % on_disk)
    return EXIT_OK


def cmd_char_export(args) -> int:
    project = Project(args.save_dir)
    character = project.load_character(args.id)
    if not character.found:
        raise LcdeError(
            "角色配置文件不存在: %s（不导出占位内容；用 `char show` 查看详情）"
            % (character.directory or args.id)
        )
    text = dumps(character_to_doc(character))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _out("已导出 -> %s" % args.output)
    else:
        _out(text)
    return EXIT_OK


def cmd_char_new(args) -> int:
    char_id = args.id.replace("/", "\\")
    directory = Path(args.save_dir) / "Character" / char_id
    images = scan_portrait_images(directory)
    character = Character(
        char_id=char_id,
        name=args.name or DEFAULT_NAME,
        camp=args.camp or DEFAULT_CAMP,
        name_color=DEFAULT_NAME_COLOR,
        camp_color=DEFAULT_CAMP_COLOR,
        express=DEFAULT_EXPRESS,
        portraits=[Portrait(image, DEFAULT_RECT) for image in images],
    )
    text = dumps(character_to_doc(character))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _out("已生成模板 -> %s" % args.output)
    else:
        _out(text)
    if images:
        _out("# 已按目录现有图片填入 %d 个立绘（顺序 = 引擎枚举顺序）" % len(images))
    return EXIT_OK


_ORDER_PREFIX = re.compile(r"^\d{4}_")


def _order_base(name: str) -> str:
    """去掉 fix-order 加的 ``NNNN_`` 前缀，用于幂等匹配。"""
    return _ORDER_PREFIX.sub("", name)


def cmd_char_fix_order(args) -> int:
    """把立绘文件重命名成 ``0000_`` / ``0001_`` …，让目录枚举顺序固定下来。

    目录枚举顺序就是引擎眼里的 ``face`` 索引，而它是文件系统给的、不保证稳定。
    本命令把它冻结成一个显式的数字前缀。

    * 不给 ``--order-from``：冻结**当前**枚举顺序（保持与现有 ``Rect`` 的对齐关系）。
    * 给 ``--order-from <json>``：按该角色文档里 ``portraits[].image`` 的顺序重排。
    """
    project = Project(args.save_dir)
    character = project.load_character(args.id)
    if character.directory is None or not character.directory.is_dir():
        raise LcdeError("角色目录不存在: %s" % character.directory)

    on_disk = scan_portrait_images(character.directory)
    if not on_disk:
        raise LcdeError("角色目录里没有立绘图片")

    if args.order_from:
        document = loads(Path(args.order_from).read_text(encoding="utf-8-sig"))
        wanted = [p.image for p in character_from_doc(document).portraits]
        if not wanted:
            raise LcdeError("%s 里没有声明任何立绘" % args.order_from)
        remaining = list(on_disk)
        ordered: list[str] = []
        for name in wanted:
            base = _order_base(name)
            match = next((f for f in remaining if f == name or _order_base(f) == base), None)
            if match is None:
                raise LcdeError("文档声明的立绘 %r 在目录里找不到（目录: %s）"
                                % (name, ", ".join(on_disk)))
            remaining.remove(match)
            ordered.append(match)
        if remaining:
            raise LcdeError("目录里还有文档未声明的立绘: %s（请先在文档里补全）"
                            % ", ".join(remaining))
    else:
        ordered = list(on_disk)

    plan = []
    for index, name in enumerate(ordered):
        new_name = "%04d_%s" % (index, _order_base(name))
        if new_name != name:
            plan.append((name, new_name))

    if not plan:
        _out("顺序已经一致，无需修正。")
        return EXIT_OK

    _out("将把 %d 个立绘重命名以固定 face 顺序：" % len(ordered))
    for old, new in plan:
        _out("  %s  ->  %s" % (old, new))
    if args.dry_run:
        _out("（--dry-run，未做修改）")
        return EXIT_OK
    if not args.yes:
        raise LcdeError("这是不可逆的重命名操作；确认后加 --yes 执行")

    # 两阶段重命名，避免中间名冲突
    staged = []
    for old, new in plan:
        src = character.directory / old
        tmp = character.directory / (".lcde_tmp_" + old)
        src.rename(tmp)
        staged.append((tmp, new))
    for tmp, new in staged:
        tmp.rename(character.directory / new)

    _out("完成。图片名已变化，请重新导出 JSON 并把 portraits[].image 更新为新文件名。")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 命令：story
# --------------------------------------------------------------------------- #

def cmd_story_list(args) -> int:
    project = Project(args.save_dir)
    titles = project.story_titles()
    if args.json:
        _out(dumps({"format": "lcde", "version": FORMAT_VERSION, "kind": "story-list",
                    "stories": [story_to_doc(project.load_story(t)) for t in titles]}))
        return EXIT_OK
    if not titles:
        _out("（没有找到任何剧本）")
        return EXIT_OK
    rows = []
    for title in titles:
        story = project.load_story(title)
        size = story.path.stat().st_size if story.path else 0
        rows.append([title, str(len(story.backgrounds)), str(len(story.characters)),
                     str(len(story.commands)), "%d B" % size])
    _out(table(rows, ["标题", "背景", "角色", "顶层命令", "大小"]))
    return EXIT_OK


def _describe_command(command: dict, project: Project, story: Story, indent: str) -> list[str]:
    name = command.get("type")
    spec = BY_NAME.get(name)
    if spec is None:
        return ["%s? %s" % (indent, name)]
    title = spec.title
    parts = []
    for field_spec in spec.fields:
        value = command.get(field_spec.name, field_spec.default)
        label = field_spec.label or field_spec.name
        if field_spec.ref == "character":
            index = value
            if isinstance(index, int) and index < len(story.characters):
                path = story.characters[index].replace("/", "\\")
                try:
                    who = Character.load(project.save_dir, path).name
                except LcdeError:
                    who = "?"
                parts.append("%s=%s(%s)" % (label, index, who))
            else:
                parts.append("%s=%s(旁白)" % (label, index))
        elif field_spec.ref == "bg":
            index = value
            path = story.backgrounds[index] if isinstance(index, int) and index < len(story.backgrounds) else "?"
            parts.append("%s=%s(%s)" % (label, index, path))
        elif field_spec.kind == "bool":
            if value:
                parts.append(label)
        elif field_spec.enum is not None:
            parts.append("%s=%s" % (label, enum_name(field_spec.enum, int(value))))
        elif field_spec.kind == "str":
            text = str(value)
            if len(text) > 40:
                text = text[:37] + "…"
            parts.append("%s=%s" % (label, text))
        else:
            parts.append("%s=%s" % (label, _fmt_number(value)))
    head = "%s%s %s" % (indent, title, name)
    lines = [head + ("  " + "  ".join(parts) if parts else "")]
    if spec.group:
        for sub in command.get("commands") or []:
            lines += _describe_command(sub, project, story, indent + "    ")
    return lines


def cmd_story_show(args) -> int:
    project = Project(args.save_dir)
    story = project.load_story(args.title)
    if args.json:
        _out(dumps(story_to_doc(story)))
        return EXIT_OK

    _out("剧本 %s" % story.title)
    _out("  文件 : %s" % (story.path or "?"))
    _out("  背景表 (%d):" % len(story.backgrounds))
    for index, path in enumerate(story.backgrounds):
        exists = (Path(project.save_dir) / "BG" / path).is_file()
        _out("    [%d] %s%s" % (index, path, "" if exists else "   ← 文件不存在"))
    _out("  角色表 (%d):" % len(story.characters))
    for index, path in enumerate(story.characters):
        try:
            character = Character.load(project.save_dir, path)
            state = "%s / %s, 立绘 %d" % (character.name, character.camp, len(character.portraits))
        except LcdeError as exc:
            state = "读取失败: %s" % exc
        _out("    [%d] %s   (%s)" % (index, path, state))
    _out("  命令 (%d):" % len(story.commands))
    for command in story.commands:
        for line in _describe_command(command, project, story, "    "):
            _out(line)
    return EXIT_OK


def cmd_story_export(args) -> int:
    project = Project(args.save_dir)
    story = project.load_story(args.title)
    text = dumps(story_to_doc(story))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _out("已导出 -> %s" % args.output)
    else:
        _out(text)
    return EXIT_OK


def cmd_story_new(args) -> int:
    story = Story(title=args.title)
    _out(dumps(story_to_doc(story)))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 命令：project
# --------------------------------------------------------------------------- #

def cmd_project_export(args) -> int:
    project = Project(args.save_dir)
    document = project_to_doc(project)
    if args.split:
        out_dir = Path(args.split)
        out_dir.mkdir(parents=True, exist_ok=True)
        for character in project.load_all_characters():
            target = out_dir / ("char_%s.json" % character.char_id.replace("\\", "__"))
            target.write_text(dumps(character_to_doc(character)), encoding="utf-8")
            _out("  -> %s" % target)
        for story in project.load_all_stories():
            target = out_dir / ("story_%s.json" % story.title)
            target.write_text(dumps(story_to_doc(story)), encoding="utf-8")
            _out("  -> %s" % target)
        return EXIT_OK
    text = dumps(document)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _out("已导出 %d 个角色 / %d 个剧本 -> %s"
             % (len(document["characters"]), len(document["stories"]), args.output))
    else:
        _out(text)
    return EXIT_OK


def cmd_project_build(args) -> int:
    path = Path(args.input)
    if not path.is_file():
        raise LcdeError("找不到输入文件: %s" % path)
    document = loads(path.read_text(encoding="utf-8-sig"))
    kind = document.get("kind")

    if kind == "project":
        characters, stories = project_from_doc(document)
    elif kind == "character":
        characters, stories = [character_from_doc(document)], []
    elif kind == "story":
        characters, stories = [], [story_from_doc(document)]
    else:
        raise LcdeError("不支持的 kind: %r" % kind)

    project = Project(args.save_dir)
    written: list[Path] = []
    if args.dry_run:
        for character in characters:
            directory = project.save_dir / "Character" / character.char_id
            written.append(directory / "Config.ch")
            written.append(directory / "Rect")
        for story in stories:
            written.append(project.dialog_dir / story.title)
        _out("--dry-run：将写出 %d 个文件" % len(written))
        for item in written:
            _out("  %s%s" % (item, "" if item.exists() else "   (新文件)"))
        return EXIT_OK

    for character in characters:
        written += character.save(project.save_dir, force=args.force)
    for story in stories:
        written.append(story.save(project.save_dir, force=args.force))

    _out("已写出 %d 个文件：" % len(written))
    for item in written:
        _out("  %s" % item)
    if not args.force:
        _out("（未覆盖任何已存在的文件；如需覆盖请加 --force）")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 命令：validate
# --------------------------------------------------------------------------- #

def cmd_validate(args) -> int:
    project = Project(args.save_dir)
    if args.what in ("char", "story") and not args.value:
        raise LcdeError("`validate %s` 需要给出%s"
                        % (args.what, "角色 ID" if args.what == "char" else "剧本标题"))
    issues = []
    if args.what == "project":
        issues = validate_project(project)
    elif args.what == "char":
        issues = validate_character(project.load_character(args.value), project)
    else:
        story = project.load_story(args.value)
        issues = validate_story(story, project)

    if args.json:
        _out(dumps({
            "format": "lcde", "version": FORMAT_VERSION, "kind": "validation",
            "issues": [i.__dict__ for i in issues],
            "summary": {
                "error": sum(1 for i in issues if i.severity == ERROR),
                "warning": sum(1 for i in issues if i.severity == "warning"),
                "info": sum(1 for i in issues if i.severity == "info"),
            },
        }))
    else:
        for issue in issues:
            if args.quiet and issue.severity != ERROR:
                continue
            _out(str(issue))
        errors = sum(1 for i in issues if i.severity == ERROR)
        warnings = sum(1 for i in issues if i.severity == "warning")
        _out("")
        _out("检查完成：%d 个错误，%d 个警告" % (errors, warnings))

    if has_errors(issues):
        return EXIT_INVALID
    if args.strict and any(i.severity == "warning" for i in issues):
        return EXIT_INVALID
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 命令：schema / commands
# --------------------------------------------------------------------------- #

def cmd_schema(args) -> int:
    schema_path = Path(__file__).resolve().parent.parent / "schema" / "lcde.schema.json"
    if not schema_path.is_file():
        raise LcdeError("找不到 schema 文件: %s" % schema_path)
    _out(schema_path.read_text(encoding="utf-8").rstrip("\n"))
    return EXIT_OK


def cmd_commands(args) -> int:
    if args.json:
        _out(dumps({
            "format": "lcde", "version": FORMAT_VERSION, "kind": "command-table",
            "commands": [
                {
                    "index": spec.index, "type": spec.name, "title": spec.title,
                    "group": spec.group,
                    "fields": [
                        {"name": f.name, "kind": f.kind, "label": f.label,
                         "enum": f.enum, "ref": f.ref}
                        for f in spec.fields
                    ],
                }
                for spec in COMMANDS
            ],
            "enums": {k: dict(v) for k, v in ENUMS.items()},
            "unusable": list(UNUSABLE),
        }))
        return EXIT_OK

    rows = []
    for spec in COMMANDS:
        fields = ", ".join(
            "%s:%s%s%s" % (f.name, f.kind,
                           "[%s]" % f.enum if f.enum else "",
                           "<%s>" % f.ref if f.ref else "")
            for f in spec.fields
        ) or ("子命令" if spec.group else "（无字段）")
        rows.append([str(spec.index), spec.name, spec.title, fields])
    _out(table(rows, ["idx", "类名", "中文名", "字段（顺序即写入顺序）"]))
    _out("")
    _out("不可用（存在但不在命令表内）: %s" % ", ".join(UNUSABLE))
    return EXIT_OK


def cmd_char_refit(args) -> int:
    """按各张立绘的**实际像素尺寸**重算 ``Rect``。

    引擎的显示尺寸是按 ``rect.width = 源图宽 × (显示高度 / 源图高)`` 换算的，
    而 ``rect`` 是逐个 face 存的。所以换掉美术资源（尤其是改了长宽比）之后
    必须重算，否则会被拉伸变形。

    默认按「正好填满 800×358.98 的舞台」计算，可以用 ``--display-height`` 改小留出余量。
    """
    from .images import image_size
    from .model import RECT_NAME, _backup
    from .placeholder import fit_rect

    project = Project(args.save_dir)
    character = project.load_character(args.id)
    if not character.found or character.directory is None:
        raise LcdeError("角色不存在或没有立绘: %s" % args.id)

    images = scan_portrait_images(character.directory)
    if not images:
        raise LcdeError("角色目录里没有图片: %s" % character.directory)

    display = args.display_height
    rows = []
    rects = []
    for index, name in enumerate(images):
        path = character.directory / name
        width, height = image_size(path)
        rect = fit_rect(width, height, display)
        rects.append(rect)
        old = character.portraits[index].rect if index < len(character.portraits) else None
        rows.append([str(index), name, "%d×%d" % (width, height),
                     "%.1f × %.1f" % (rect[2], rect[3]),
                     "不变" if old == rect else ("新" if old is None else "**改动**")])

    if args.json:
        out = {"format": "lcde", "version": FORMAT_VERSION, "kind": "refit",
               "character": character.char_id, "directory": str(character.directory),
               "rects": [{"image": n, "rect": list(r)} for n, r in zip(images, rects)]}
        if args.dry_run:
            _out(dumps(out))
            return EXIT_OK
        _write_rect(character.directory, character.express, rects)
        _out(dumps(out))
        return EXIT_OK

    _out("角色 %s —— 按实际图片尺寸重算 Rect" % character.char_id)
    _out("  目录 : %s" % character.directory)
    _out("  舞台 : 800 × 358.98；显示高度 %s" % (("%.2f" % display) if display else "358.98（填满）"))
    _out(table(rows, ["face", "图片", "源图尺寸", "显示尺寸", "状态"]))
    if args.dry_run:
        _out("")
        _out("（--dry-run，未写入）")
        return EXIT_OK

    _backup(character.directory / RECT_NAME)
    _write_rect(character.directory, character.express, rects)
    _out("")
    _out("已写入 %s" % (character.directory / RECT_NAME))
    return EXIT_OK


def _write_rect(directory, express, rects) -> None:
    from .binary import BinWriter
    from .model import RECT_NAME

    writer = BinWriter()
    writer.f32(express[0]).f32(express[1])
    for rect in rects:
        for value in rect:
            writer.f32(value)
    (Path(directory) / RECT_NAME).write_bytes(writer.bytes())


def cmd_placeholder(args) -> int:
    """按规范文档生成占位素材（立绘 / 头像 / 背景）。"""
    from .placeholder import generate_for_document

    path = Path(args.input)
    if not path.is_file():
        raise LcdeError("找不到输入文件: %s" % path)
    document = loads(path.read_text(encoding="utf-8-sig"))
    palette = None
    if args.palette:
        palette_path = Path(args.palette)
        if not palette_path.is_file():
            raise LcdeError("找不到配色文件: %s" % palette_path)
        palette = json.loads(palette_path.read_text(encoding="utf-8-sig"))
        if not isinstance(palette, dict):
            raise LcdeError("配色文件必须是一个 JSON 对象：{\"char:金笠\": \"#C9A227\", ...}")

    written, skipped = generate_for_document(
        document, args.save_dir, force=args.force, dry_run=args.dry_run,
        only=args.only, palette=palette)

    if args.json:
        _out(dumps({
            "format": "lcde", "version": FORMAT_VERSION, "kind": "placeholders",
            "written": [str(p) for p in written],
            "skipped": [str(p) for p in skipped],
            "dryRun": bool(args.dry_run),
        }))
        return EXIT_OK

    verb = "将生成" if args.dry_run else "已生成"
    _out("%s %d 个占位素材%s" % (verb, len(written), "（--dry-run）" if args.dry_run else "："))
    for item in written:
        _out("  %s" % item)
    if skipped:
        _out("")
        _out("跳过 %d 个已存在的文件（加 --force 覆盖）：" % len(skipped))
        for item in skipped:
            _out("  %s" % item)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcde",
        description="LCDE / Soalin 角色与剧本的调取、检查与生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  lcde env\n"
            "  lcde char list\n"
            "  lcde char show Hero --json\n"
            "  lcde char export Hero -o hero.json\n"
            "  lcde story show 序章\n"
            "  lcde story export 序章 -o story.json\n"
            "  lcde project export -o project.json\n"
            "  lcde project build project.json --dry-run\n"
            "  lcde validate\n"
            "  lcde commands\n"
        ),
    )
    parser.add_argument("--version", action="version", version="lcde " + __version__)
    parser.add_argument("--save-dir", default=None,
                        help="存档目录（默认 %%USERPROFILE%%\\AppData\\LocalLow\\Soalin\\LCDE）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机读）")

    # 让 --save-dir / --json 也能写在子命令之后。
    # 用 SUPPRESS 作为默认值，这样未被指定时不会覆盖顶层解析出来的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--save-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出（机读）")

    sub = parser.add_subparsers(dest="group", required=True, parser_class=argparse.ArgumentParser)

    sub.add_parser("env", parents=[common], help="显示存档目录与环境信息").set_defaults(func=cmd_env)
    sub.add_parser("commands", parents=[common],
                   help="打印命令表与枚举").set_defaults(func=cmd_commands)
    sub.add_parser("schema", parents=[common],
                   help="打印规范格式的 JSON Schema").set_defaults(func=cmd_schema)

    char = sub.add_parser("char", parents=[common],
                          help="角色操作").add_subparsers(dest="action", required=True)
    char.add_parser("list", parents=[common], help="列出所有角色").set_defaults(func=cmd_char_list)

    p = char.add_parser("show", parents=[common], help="显示角色详情")
    p.add_argument("id", help="角色目录或 .ch 路径，如 Hero 或 Hero\\Config.ch")
    p.set_defaults(func=cmd_char_show)

    p = char.add_parser("export", parents=[common], help="导出为规范 JSON")
    p.add_argument("id")
    p.add_argument("-o", "--output", help="写到文件（默认 stdout）")
    p.set_defaults(func=cmd_char_export)

    p = char.add_parser("new", parents=[common], help="按目录现有立绘生成角色模板 JSON")
    p.add_argument("id")
    p.add_argument("--name", help="角色名")
    p.add_argument("--camp", help="阵营")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_char_new)

    p = char.add_parser("fix-order", parents=[common], help="重命名立绘以固定 face 顺序")
    p.add_argument("id")
    p.add_argument("--order-from", metavar="JSON",
                   help="按该角色文档的 portraits 顺序重排（默认冻结当前枚举顺序）")
    p.add_argument("--dry-run", action="store_true", help="只预览")
    p.add_argument("--yes", action="store_true", help="确认执行不可逆的重命名")
    p.set_defaults(func=cmd_char_fix_order)

    p = char.add_parser("refit", parents=[common],
                        help="按各张立绘的实际像素尺寸重算 Rect（换素材后必做）")
    p.add_argument("id")
    p.add_argument("--display-height", type=float, default=None,
                   help="立绘的显示高度（舞台 358.98，默认正好填满）")
    p.add_argument("--dry-run", action="store_true", help="只显示计划，不写入")
    p.set_defaults(func=cmd_char_refit)

    story = sub.add_parser("story", parents=[common],
                           help="剧本操作").add_subparsers(dest="action", required=True)
    story.add_parser("list", parents=[common], help="列出所有剧本").set_defaults(func=cmd_story_list)

    p = story.add_parser("show", parents=[common], help="以人类可读形式显示剧本")
    p.add_argument("title")
    p.set_defaults(func=cmd_story_show)

    p = story.add_parser("export", parents=[common], help="导出为规范 JSON")
    p.add_argument("title")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_story_export)

    p = story.add_parser("new", parents=[common], help="输出一个空剧本模板 JSON")
    p.add_argument("title")
    p.set_defaults(func=cmd_story_new)

    proj = sub.add_parser("project", parents=[common],
                          help="整目录操作").add_subparsers(dest="action", required=True)

    p = proj.add_parser("export", parents=[common], help="导出全部角色与剧本")
    p.add_argument("-o", "--output")
    p.add_argument("--split", metavar="DIR", help="改为每个实体一个文件，写到 DIR")
    p.set_defaults(func=cmd_project_export)

    p = proj.add_parser("build", parents=[common], help="从规范 JSON 写回游戏文件")
    p.add_argument("input", help="JSON 文件（kind = project / character / story）")
    p.add_argument("--dry-run", action="store_true", help="只显示将写出的文件")
    p.add_argument("--force", action="store_true", help="允许覆盖（自动备份为 *.bak-<时间戳>）")
    p.set_defaults(func=cmd_project_build)

    p = sub.add_parser("placeholder", parents=[common],
                       help="按规范文档生成占位立绘 / 头像 / 背景")
    p.add_argument("input", help="JSON 文件（kind = project / character / story）")
    p.add_argument("--only", choices=["all", "portraits", "heads", "backgrounds"],
                   default="all", help="只生成某一类")
    p.add_argument("--palette", metavar="JSON",
                   help='配色覆盖，如 {"char:金笠":"#C9A227","bg:客栈房·夜.png":"#2A2118"}')
    p.add_argument("--dry-run", action="store_true", help="只列出将生成的文件")
    p.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p.set_defaults(func=cmd_placeholder)

    p = sub.add_parser("validate", parents=[common], help="检查一致性与潜在崩溃")
    p.add_argument("what", nargs="?", choices=["project", "char", "story"], default="project")
    p.add_argument("value", nargs="?", help="char 时为角色 ID，story 时为标题")
    p.add_argument("--strict", action="store_true", help="有警告也返回失败码")
    p.add_argument("--quiet", action="store_true", help="只显示错误")
    p.set_defaults(func=cmd_validate)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "save_dir", None) is None:
        args.save_dir = default_save_dir()
    else:
        args.save_dir = Path(args.save_dir)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return args.func(args)
    except ValidationError as exc:
        for message in exc.messages:
            sys.stderr.write("错误: %s\n" % message)
        return EXIT_INVALID
    except FormatError as exc:
        sys.stderr.write("格式错误: %s\n" % exc)
        return EXIT_ERROR
    except LcdeError as exc:
        sys.stderr.write("错误: %s\n" % exc)
        return EXIT_ERROR
    except BrokenPipeError:  # 管道被下游关闭（例如 | head）
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
