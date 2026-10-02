"""校验规则 —— 把「引擎会在哪里崩、哪里显示不对」提前查出来。

规则编号对应 ``SPEC.md`` 的「校验规则」一节。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .commands import BY_NAME, ENUMS
from .errors import FormatError
from .model import (
    IMAGE_EXTENSIONS,
    Character,
    Project,
    Story,
    find_head,
    scan_portrait_images,
    win_path,
)

__all__ = ["Issue", "validate_character", "validate_story", "validate_project", "has_errors"]

ERROR = "error"
WARNING = "warning"
INFO = "info"

_HEX6 = re.compile(r"^[0-9A-Fa-f]{6}$")

#: 会读取「屏幕立绘列表」下标的命令
_PORTRAIT_READERS = ("CChange", "CMove", "CJump", "CShake", "CFront", "CEmo", "COut")
#: 会改变「屏幕立绘列表」的命令
_PORTRAIT_MUTATORS = ("CIn", "COut")

#: 约定的「旁白」角色下标：任何 ≥ 角色数的值都不显示名字框，统一用这个
NARRATION_INDEX = 0xFFFF


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    target: str
    location: str
    message: str

    def __str__(self) -> str:
        mark = {"error": "✗", "warning": "!", "info": "·"}.get(self.severity, "?")
        where = " @ %s" % self.location if self.location else ""
        return "%s [%s] %s%s: %s" % (mark, self.code, self.target, where, self.message)


def has_errors(issues) -> bool:
    return any(i.severity == ERROR for i in issues)


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

def validate_character(character: Character, project: Project | None = None) -> list[Issue]:
    target = "char:%s" % (character.char_id or "?")
    issues: list[Issue] = []

    def add(severity, code, location, message):
        issues.append(Issue(severity, code, target, location, message))

    if not character.char_id:
        add(ERROR, "C1", "id", "角色必须放在 Character\\ 的子目录里（否则头像与立绘路径会失效）")

    if not character.found:
        add(ERROR, "C2", "Config.ch", "角色配置文件不存在；引擎会显示为「读取失败」")
        return issues

    for field in ("name", "camp"):
        if not getattr(character, field):
            add(WARNING, "C3", field, "%s 为空，界面会留白" % field)
    if character.name in ("???",) or character.camp in ("???",):
        add(INFO, "C4", "name/camp", "仍是新建模板的占位符 ???")

    for field in ("name_color", "camp_color"):
        value = getattr(character, field)
        if not _HEX6.match(value or ""):
            add(ERROR, "C5", field, "必须是 6 位十六进制 RGB（不带 #），当前为 %r" % value)

    for index, value in enumerate(character.express):
        if not math.isfinite(value):
            add(ERROR, "C6", "express[%d]" % index, "不是有限数: %r" % value)

    # 立绘文件 vs Rect 项数
    # 从 JSON 载入的角色没有 directory，这里用 project 补出来，
    # 这样「JSON 声明 vs 磁盘实际」也能被检查（规则 C9 / C10）。
    directory = character.directory
    if directory is None and project is not None and character.char_id:
        directory = Path(project.save_dir) / "Character" / character.char_id
    if directory is not None:
        on_disk = scan_portrait_images(directory)
        has_rect = (directory / "Rect").is_file()
        if not has_rect:
            add(ERROR, "C7", "Rect", "Rect 文件不存在；引擎打开角色时会抛异常")
        else:
            declared = character.rect_count or len(character.portraits)
            if declared != len(on_disk):
                add(
                    ERROR, "C9", "portraits",
                    "Rect 记录 %d 个矩形，但目录里有 %d 张图片（%s）；"
                    "数量必须严格相等，否则立绘会错位"
                    % (declared, len(on_disk), ", ".join(on_disk) or "无"),
                )
        declared_images = [p.image for p in character.portraits]
        missing = [name for name in declared_images
                   if name and not (directory / name).is_file()]
        if missing:
            add(ERROR, "C8", "portraits", "以下立绘文件不存在: %s" % ", ".join(missing))
        if declared_images and declared_images != on_disk:
            add(
                ERROR, "C10", "portraits",
                "声明顺序 %s 与目录枚举顺序 %s 不一致；用 `lcde char fix-order` 修正"
                % (declared_images, on_disk),
            )
        if not on_disk:
            add(WARNING, "C11", "portraits", "角色没有任何立绘，CIn/CChange 会失效")

        # 头像
        heads_root = Path(project.save_dir) / "Character" / "Heads" if project else None
        if heads_root is not None:
            head = find_head(heads_root / character.char_id)
            if character.head and not head:
                add(WARNING, "C12", "head", "声明的头像 %r 在 Heads 下找不到" % character.head)
            elif not head:
                add(INFO, "C13", "head", "没有头像文件（Character\\Heads\\%s%s）"
                    % (character.char_id, IMAGE_EXTENSIONS[0]))

    # 立绘扩展名一致性（信息）
    bad_ext = [p.image for p in character.portraits
               if p.image and Path(p.image).suffix.lower() not in IMAGE_EXTENSIONS]
    if bad_ext:
        add(INFO, "C14", "portraits", "以下文件扩展名不在引擎的图片列表内: %s" % ", ".join(bad_ext))

    return issues


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #

def validate_story(story: Story, project: Project | None = None,
                   character_cache: dict[str, Character] | None = None) -> list[Issue]:
    target = "story:%s" % (story.title or "?")
    issues: list[Issue] = []
    cache = character_cache if character_cache is not None else {}

    def add(severity, code, location, message):
        issues.append(Issue(severity, code, target, location, message))

    if not story.title or any(ch in story.title for ch in "\\/:*?\"<>|"):
        add(ERROR, "S1", "title", "标题不能为空，也不能含路径分隔符或非法字符: %r" % story.title)

    # 角色表
    for index, path in enumerate(story.characters):
        location = "characters[%d]" % index
        if "\\" not in path and "/" not in path:
            add(ERROR, "S2", location,
                "%r 缺少子目录；必须是形如 `角色目录\\Config.ch` 的相对路径" % path)
        if not path.lower().endswith(".ch"):
            add(WARNING, "S3", location, "%r 不以 .ch 结尾" % path)
        if project is None:
            continue
        key = path.replace("/", "\\")
        if key not in cache:
            try:
                cache[key] = Character.load(project.save_dir, key)
            except FormatError as exc:
                add(ERROR, "S4", location, "无法读取角色 %r: %s" % (path, exc))
                continue
        character = cache[key]
        if not character.found:
            add(ERROR, "S4", location, "角色文件不存在: Character\\%s" % key)

    # 背景表
    for index, path in enumerate(story.backgrounds):
        if win_path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            add(WARNING, "S5", "backgrounds[%d]" % index,
                "%r 不是引擎支持的图片扩展名 %s" % (path, " ".join(IMAGE_EXTENSIONS)))
        if project is not None:
            full = Path(project.save_dir) / "BG" / path
            if not full.is_file():
                add(WARNING, "S6", "backgrounds[%d]" % index,
                    "背景文件不存在: BG\\%s（运行时会显示纯黑）" % path)

    # 字段级 + 立绘下标模拟
    state: list[str] = []          # 屏幕上的立绘实例：记录其角色路径（可能为空）
    _walk(story, story.commands, (), state, story, add, cache, 0)

    if not story.commands:
        add(INFO, "S7", "commands", "剧本没有任何命令")

    # 旁白数量汇总（一句一条太吵，这里只报总数）
    narration = sum(
        1 for _, command in story.iter_commands()
        if command.get("type") == "Say"
        and command.get("character", 0) >= len(story.characters)
    )
    if narration:
        add(INFO, "S23", "commands",
            "含 %d 句旁白（character = %d，不显示名字框）" % (narration, NARRATION_INDEX))
    return issues


def _character_of(story: Story, index: int) -> str | None:
    if 0 <= index < len(story.characters):
        return story.characters[index].replace("/", "\\")
    return None


def _portraits_of(cache, char_path) -> int | None:
    if char_path is None:
        return None
    character = cache.get(char_path)
    if character is None or not character.found:
        return None
    return len(character.portraits)


def _walk(story: Story, commands, base_path, state, story_ref, add, cache, depth):
    if depth > 64:
        add(ERROR, "S8", _loc(base_path), "命令嵌套过深")
        return
    for index, command in enumerate(commands):
        path = base_path + (index,)
        location = _loc(path)
        name = command.get("type")
        spec = BY_NAME.get(name)
        if spec is None:
            add(ERROR, "S9", location, "未知命令类型 %r（索引 %s）" % (name, command.get("_index")))
            continue

        # 枚举值检查
        for field_spec in spec.fields:
            if field_spec.enum is not None:
                value = command.get(field_spec.name)
                if isinstance(value, int) and value not in ENUMS[field_spec.enum].values():
                    add(WARNING, "S10", "%s.%s" % (location, field_spec.name),
                        "%s 的取值 %d 不在已知成员内（%s）"
                        % (field_spec.enum, value,
                           ", ".join("%s=%d" % (k, v) for k, v in ENUMS[field_spec.enum].items())))
            if field_spec.kind == "f32" and isinstance(command.get(field_spec.name), (int, float)):
                if not math.isfinite(float(command[field_spec.name])):
                    add(ERROR, "S11", "%s.%s" % (location, field_spec.name), "不是有限数")

        if name in ("Queue", "Synchronization"):
            subs = command.get("commands") or []
            if not subs:
                add(WARNING, "S12", location, "%s 没有子命令" % name)
            if name == "Synchronization":
                mutators = [s.get("type") for s in subs if s.get("type") in _PORTRAIT_MUTATORS]
                if mutators:
                    add(WARNING, "S13", location,
                        "Synchronization 内出现 %s；并发执行时屏幕立绘下标的结果不确定"
                        % ", ".join(sorted(set(mutators))))
            _walk(story, subs, path, state, story_ref, add, cache, depth + 1)
            continue

        if name == "CIn":
            char_index = command.get("character", 0)
            char_path = _character_of(story, char_index)
            if char_path is None:
                add(ERROR, "S14", "%s.character" % location,
                    "角色下标 %s 超出 characters（共 %d 个）" % (char_index, len(story.characters)))
            _check_face(add, cache, char_path, command.get("face", 0), location + ".face")
            state.append(char_path if char_path is not None else "?")
            continue

        if name == "COut":
            portrait = command.get("portrait", 0)
            if portrait >= len(state):
                add(ERROR, "S15", "%s.portrait" % location,
                    "下标 %d 越界：此时屏幕上有 %d 个立绘（引擎会抛异常）" % (portrait, len(state)))
            else:
                state.pop(portrait)
            continue

        if name in _PORTRAIT_READERS:
            portrait = command.get("portrait", 0)
            if portrait >= len(state):
                add(ERROR, "S16", "%s.portrait" % location,
                    "下标 %d 越界：此时屏幕上有 %d 个立绘（引擎会抛异常）" % (portrait, len(state)))
            if name == "CChange":
                target_char = state[portrait] if portrait < len(state) else None
                _check_face(add, cache, target_char, command.get("face", 0), location + ".face")
            continue

        if name == "Say":
            char_index = command.get("character", 0)
            # 65535 是约定的「旁白」写法（read.md / SPEC.md），不逐条提示；
            # 只报看起来是写错了的越界下标。
            if char_index != NARRATION_INDEX and char_index >= len(story.characters):
                add(WARNING, "S17", "%s.character" % location,
                    "角色下标 %s ≥ characters 数量 %d，会被当成无名旁白；"
                    "若是有意为之请写 65535" % (char_index, len(story.characters)))
            if not command.get("conversation"):
                add(WARNING, "S18", "%s.conversation" % location, "对话内容为空")
            continue

        if name == "BGChange":
            bg = command.get("bgIndex", 0)
            if bg >= len(story.backgrounds):
                add(ERROR, "S19", "%s.bgIndex" % location,
                    "背景下标 %d 超出 backgrounds（共 %d 个）" % (bg, len(story.backgrounds)))
            continue

        if name in ("BGM", "SFX"):
            path_value = command.get("path") or ""
            if path_value and not path_value.startswith("event:"):
                add(WARNING, "S20", "%s.path" % location,
                    "%r 不像 FMOD 事件路径（通常形如 event:/BGM/xxx）" % path_value)
            continue


def _check_face(add, cache, char_path, face, location):
    count = _portraits_of(cache, char_path)
    if count is None:
        return
    if count == 0:
        add(WARNING, "S21", location, "角色 %s 没有立绘，face 无效" % char_path)
    elif isinstance(face, int) and face >= count:
        add(ERROR, "S22", location,
            "face %d 超出角色 %s 的立绘数量 %d" % (face, char_path, count))


def _loc(path) -> str:
    return "commands[%s]" % "][".join(str(p) for p in path) if path else "commands"


# --------------------------------------------------------------------------- #
# 工程
# --------------------------------------------------------------------------- #

def validate_project(project: Project, *, include_stories: bool = True) -> list[Issue]:
    issues: list[Issue] = []
    if not project.exists():
        issues.append(Issue(ERROR, "P1", "project", "", "存档目录不存在: %s" % project.save_dir))
        return issues

    characters: dict[str, Character] = {}
    for char_id in project.character_ids():
        try:
            character = Character.load(project.save_dir, char_id)
        except FormatError as exc:
            issues.append(Issue(ERROR, "P2", "char:%s" % char_id, "", str(exc)))
            continue
        characters[char_id.replace("/", "\\")] = character
        issues.extend(validate_character(character, project))

    if include_stories:
        cache: dict[str, Character] = {}
        for title in project.story_titles():
            try:
                story = Story.load(project.save_dir, title)
            except FormatError as exc:
                issues.append(Issue(ERROR, "P3", "story:%s" % title, "", str(exc)))
                continue
            issues.extend(validate_story(story, project, cache))

    return issues
