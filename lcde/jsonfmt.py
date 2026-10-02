"""规范 JSON 格式（``format: "lcde"``）与内部模型之间的转换。

规范格式的完整定义见 ``SPEC.md``。要点：

* 导出（canonical）永远使用**数字下标**引用实体，枚举写成**名称**，浮点写成能无损回到
  float32 的最短十进制表示。
* 导入额外接受若干书写便利：枚举可用整数、``character`` 可用别名或路径、
  ``bgIndex`` 可用背景路径、``characters`` 可以用字符串简写。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from . import FORMAT_NAME, FORMAT_VERSION, __version__
from .commands import BY_NAME, ENUMS, enum_name, enum_value
from .errors import FormatError
from .model import Character, Portrait, Project, Story, win_path

__all__ = [
    "character_to_doc",
    "character_from_doc",
    "story_to_doc",
    "story_from_doc",
    "project_to_doc",
    "project_from_doc",
    "dumps",
    "loads",
    "f32_shortest",
]

KIND_CHARACTER = "character"
KIND_STORY = "story"
KIND_PROJECT = "project"


# --------------------------------------------------------------------------- #
# JSON 文本
# --------------------------------------------------------------------------- #

def f32_shortest(value: float) -> float | int:
    """找出能无损写回 float32 的最短十进制表示（让 JSON 里出现 0.6 而不是 0.6000000238418579）。"""
    packed = struct.pack("<f", value)
    for digits in range(1, 10):
        candidate = float("%.*g" % (digits, value))
        if struct.pack("<f", candidate) == packed:
            if candidate.is_integer() and abs(candidate) < 1e15:
                return int(candidate)
            return candidate
    return value


def dumps(document: dict) -> str:
    """把文档序列化成规范 JSON 文本（UTF-8、2 空格缩进、保留中文、末尾换行）。"""
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def loads(text: str) -> dict:
    """解析并校验文档外壳。"""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatError("JSON 解析失败: %s" % exc) from None
    if not isinstance(document, dict):
        raise FormatError("文档根节点必须是对象")
    _check_envelope(document)
    return document


def _check_envelope(document: dict) -> None:
    name = document.get("format")
    if name is None:
        raise FormatError('缺少 "format" 字段（应为 "lcde"）')
    if name != FORMAT_NAME:
        raise FormatError('不支持的格式 %r（本工具只处理 %r）' % (name, FORMAT_NAME))
    version = document.get("version")
    if not isinstance(version, int):
        raise FormatError('"version" 必须是整数')
    if version > FORMAT_VERSION:
        raise FormatError(
            "文档版本 %d 高于本工具支持的 %d，请升级工具" % (version, FORMAT_VERSION)
        )
    if not document.get("kind"):
        raise FormatError('缺少 "kind" 字段')


def _envelope(kind: str, **body) -> dict:
    return {"format": FORMAT_NAME, "version": FORMAT_VERSION, "kind": kind,
            "generator": "lcde %s" % __version__, **body}


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

_HEX = set("0123456789abcdefABCDEF")


def _check_color(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not set(value) <= _HEX:
        raise FormatError(
            "%s 必须是 6 位十六进制 RGB（不带 '#'），收到 %r" % (field_name, value)
        )
    return value.upper()


def character_to_doc(character: Character) -> dict:
    return _envelope(
        KIND_CHARACTER,
        id=character.char_id,
        name=character.name,
        camp=character.camp,
        nameColor=character.name_color.upper(),
        campColor=character.camp_color.upper(),
        express=[f32_shortest(v) for v in character.express],
        portraits=[
            {"image": p.image, "rect": [f32_shortest(v) for v in p.rect]}
            for p in character.portraits
        ],
        head=character.head,
    )


def character_from_doc(document: dict) -> Character:
    if document.get("kind") != KIND_CHARACTER:
        raise FormatError('kind 不是 "character"')
    char_id = document.get("id")
    if not isinstance(char_id, str) or not char_id.strip():
        raise FormatError('角色文档缺少 "id"')

    portraits_raw = document.get("portraits", [])
    if not isinstance(portraits_raw, list):
        raise FormatError('"portraits" 必须是数组')
    portraits = []
    for index, item in enumerate(portraits_raw):
        if not isinstance(item, dict):
            raise FormatError("portraits[%d] 必须是对象" % index)
        image = item.get("image", "")
        if not isinstance(image, str):
            raise FormatError("portraits[%d].image 必须是字符串" % index)
        rect = item.get("rect")
        if rect is None:
            rect = [0.0, 100.0, 90.0, 300.0]
        if not isinstance(rect, list) or len(rect) != 4:
            raise FormatError("portraits[%d].rect 必须是 4 个数字的数组" % index)
        try:
            rect = tuple(float(v) for v in rect)
        except (TypeError, ValueError):
            raise FormatError("portraits[%d].rect 含有非数字" % index) from None
        portraits.append(Portrait(image, rect))

    express = document.get("express", [0.0, 300.0])
    if not isinstance(express, list) or len(express) != 2:
        raise FormatError('"express" 必须是 2 个数字的数组')
    express = (float(express[0]), float(express[1]))

    head = document.get("head")
    if head is not None and not isinstance(head, str):
        raise FormatError('"head" 必须是字符串或 null')

    return Character(
        char_id=char_id.replace("/", "\\").strip("\\"),
        name=str(document.get("name", "")),
        camp=str(document.get("camp", "")),
        name_color=_check_color(document.get("nameColor", "DDB994"), "nameColor"),
        camp_color=_check_color(document.get("campColor", "76471A"), "campColor"),
        express=express,
        portraits=portraits,
        head=head,
        # 从 JSON 载入时，声明本身就是「期望值」；校验器会拿它与磁盘实际数量比对
        rect_count=len(portraits),
    )


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #

def _character_table(document: dict) -> tuple[list[str], dict[str, int]]:
    """返回 ``(角色路径列表, 别名/路径 → 下标)``。"""
    raw = document.get("characters", [])
    if not isinstance(raw, list):
        raise FormatError('"characters" 必须是数组')
    paths: list[str] = []
    lookup: dict[str, int] = {}
    for index, item in enumerate(raw):
        alias = None
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict):
            path = item.get("path")
            alias = item.get("alias")
            if not isinstance(path, str) or not path:
                raise FormatError("characters[%d] 缺少 path" % index)
        else:
            raise FormatError("characters[%d] 必须是字符串或 {path, alias} 对象" % index)
        paths.append(path.replace("/", "\\"))
        for key in filter(None, (alias, path, win_path(path).name, win_path(path).stem)):
            lookup.setdefault(key, index)
    return paths, lookup


def _resolve_character(value: Any, lookup: dict[str, int], where: str) -> int:
    if isinstance(value, bool):
        raise FormatError("%s 的 character 不能是布尔值" % where)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value in lookup:
            return lookup[value]
        raise FormatError(
            "%s 引用了未登记的角色 %r；已在 characters 里登记的有：%s"
            % (where, value, ", ".join(sorted(lookup)) or "（空）")
        )
    raise FormatError("%s 的 character 必须是下标或别名，收到 %r" % (where, value))


def _resolve_background(value: Any, backgrounds: list[str], where: str) -> int:
    if isinstance(value, bool):
        raise FormatError("%s 的 bgIndex 不能是布尔值" % where)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.replace("/", "\\")
        wanted = win_path(normalized).name
        for index, path in enumerate(backgrounds):
            if path == normalized or win_path(path).name == wanted:
                return index
        raise FormatError(
            "%s 引用了未在 backgrounds 里登记的背景 %r" % (where, value)
        )
    raise FormatError("%s 的 bgIndex 必须是下标或路径，收到 %r" % (where, value))


def story_to_doc(story: Story) -> dict:
    return _envelope(
        KIND_STORY,
        title=story.title,
        backgrounds=list(story.backgrounds),
        characters=[{"path": path} for path in story.characters],
        commands=[_command_to_doc(c) for c in story.commands],
    )


def _command_to_doc(command: dict) -> dict:
    name = command.get("type")
    spec = BY_NAME.get(name)
    if spec is None:
        raise FormatError("未知命令类型: %r" % name)
    out: dict[str, Any] = {"type": name}
    if spec.group:
        out["commands"] = [_command_to_doc(sub) for sub in command.get("commands") or []]
    for field_spec in spec.fields:
        value = command.get(field_spec.name, field_spec.default)
        if field_spec.enum is not None:
            out[field_spec.name] = enum_name(field_spec.enum, int(value))
        elif field_spec.kind == "f32":
            out[field_spec.name] = f32_shortest(float(value))
        elif field_spec.kind == "f64":
            out[field_spec.name] = float(value)
        elif field_spec.kind == "bool":
            out[field_spec.name] = bool(value)
        elif field_spec.kind == "decimal":
            out[field_spec.name] = str(value)
        else:
            out[field_spec.name] = value
    return out


def story_from_doc(document: dict) -> Story:
    if document.get("kind") != KIND_STORY:
        raise FormatError('kind 不是 "story"')
    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FormatError('剧本文档缺少 "title"')

    backgrounds_raw = document.get("backgrounds", [])
    if not isinstance(backgrounds_raw, list) or not all(isinstance(b, str) for b in backgrounds_raw):
        raise FormatError('"backgrounds" 必须是字符串数组')
    backgrounds = [b.replace("/", "\\") for b in backgrounds_raw]

    characters, lookup = _character_table(document)

    commands_raw = document.get("commands", [])
    if not isinstance(commands_raw, list):
        raise FormatError('"commands" 必须是数组')
    commands = [
        _command_from_doc(command, backgrounds, lookup, "commands[%d]" % index, 0)
        for index, command in enumerate(commands_raw)
    ]
    return Story(title=title, backgrounds=backgrounds, characters=characters, commands=commands)


def _command_from_doc(command, backgrounds, lookup, where, depth) -> dict:
    if depth > 64:
        raise FormatError("%s 的命令嵌套超过 64 层" % where)
    if not isinstance(command, dict):
        raise FormatError("%s 必须是对象" % where)
    name = command.get("type")
    spec = BY_NAME.get(name)
    if spec is None:
        extra = ""
        if name in ("Act", "CFade"):
            extra = "（该命令存在于引擎但不在命令表中，无法写入剧本）"
        raise FormatError("%s 的类型 %r 不是可用命令%s" % (where, name, extra))

    out: dict[str, Any] = {"type": name}
    known = {"type"}
    if spec.group:
        subs = command.get("commands", [])
        if not isinstance(subs, list):
            raise FormatError("%s.commands 必须是数组" % where)
        out["commands"] = [
            _command_from_doc(sub, backgrounds, lookup, "%s.commands[%d]" % (where, i), depth + 1)
            for i, sub in enumerate(subs)
        ]
        known.add("commands")

    for field_spec in spec.fields:
        key = field_spec.name
        known.add(key)
        if key not in command or command[key] is None:
            out[key] = field_spec.default
            continue
        value = command[key]
        if field_spec.ref == "character":
            out[key] = _resolve_character(value, lookup, where)
        elif field_spec.ref == "bg":
            out[key] = _resolve_background(value, backgrounds, where)
        elif field_spec.enum is not None:
            out[key] = enum_value(field_spec.enum, value)
        else:
            out[key] = value

    unknown = sorted(set(command) - known)
    if unknown:
        raise FormatError(
            "%s (%s) 含未知字段: %s；该命令的字段是 %s"
            % (where, name, ", ".join(unknown), ", ".join(spec.field_names) or "（无）")
        )
    return out


# --------------------------------------------------------------------------- #
# 工程（整个存档目录）
# --------------------------------------------------------------------------- #

def project_to_doc(project: Project, *, include_settings: bool = True) -> dict:
    doc = _envelope(
        KIND_PROJECT,
        saveDir=str(project.save_dir),
        characters=[character_to_doc(c) for c in project.load_all_characters()],
        stories=[story_to_doc(s) for s in project.load_all_stories()],
    )
    if include_settings:
        settings = project.read_settings()
        if settings is not None:
            doc["settings"] = settings
    return doc


def project_from_doc(document: dict) -> tuple[list[Character], list[Story]]:
    if document.get("kind") != KIND_PROJECT:
        raise FormatError('kind 不是 "project"')
    characters = [character_from_doc(item) for item in document.get("characters", [])]
    stories = [story_from_doc(item) for item in document.get("stories", [])]
    return characters, stories


def enumerate_enum_names() -> dict[str, list[str]]:
    """给 CLI / 文档用：列出所有可用的枚举名。"""
    return {name: sorted(members) for name, members in ENUMS.items()}
