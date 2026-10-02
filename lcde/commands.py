"""命令表：``Save.commandType`` 的镜像，以及命令的二进制编解码。

表顺序即 ``typeIndex``，**不可更改**（见 read.md 4.5）。

字段顺序即 ``Save.SaveCommand`` 里
``type.GetFields(Public | Instance).OrderBy(f => f.MetadataToken)`` 的结果 ——
MetadataToken 是字段在元数据 Field 表里的行号，**基类的行号小于派生类**，
所以顺序是「基类字段在前，派生类字段在后」，同一声明类内按声明顺序。
不要用 ``Type.GetFields()`` 的默认返回顺序，那是「派生类在前」，对继承字段正好相反。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .binary import BinReader, BinWriter
from .errors import FormatError

__all__ = [
    "ENUMS",
    "FieldSpec",
    "CommandSpec",
    "COMMANDS",
    "BY_NAME",
    "BY_INDEX",
    "encode_command",
    "decode_command",
    "coerce_value",
    "enum_name",
    "enum_value",
]

# --------------------------------------------------------------------------- #
# 枚举（数值取自 Unity.TextMeshPro.dll / DOTween.dll 的 Constant 表）
# --------------------------------------------------------------------------- #

ENUMS: dict[str, dict[str, int]] = {
    "HorizontalAlignmentOptions": {
        "Left": 1, "Center": 2, "Right": 4, "Justified": 8, "Flush": 16, "Geometry": 32,
    },
    "VerticalAlignmentOptions": {
        "Top": 256, "Middle": 512, "Bottom": 1024,
        "Baseline": 2048, "Geometry": 4096, "Capline": 8192,
    },
    "Ease": {
        "Unset": 0, "Linear": 1,
        "InSine": 2, "OutSine": 3, "InOutSine": 4,
        "InQuad": 5, "OutQuad": 6, "InOutQuad": 7,
        "InCubic": 8, "OutCubic": 9, "InOutCubic": 10,
        "InQuart": 11, "OutQuart": 12, "InOutQuart": 13,
        "InQuint": 14, "OutQuint": 15, "InOutQuint": 16,
        "InExpo": 17, "OutExpo": 18, "InOutExpo": 19,
        "InCirc": 20, "OutCirc": 21, "InOutCirc": 22,
        "InElastic": 23, "OutElastic": 24, "InOutElastic": 25,
        "InBack": 26, "OutBack": 27, "InOutBack": 28,
        "InBounce": 29, "OutBounce": 30, "InOutBounce": 31,
        "Flash": 32, "InFlash": 33, "OutFlash": 34, "InOutFlash": 35,
        "INTERNAL_Zero": 36, "INTERNAL_Custom": 37,
    },
}

_ENUM_REVERSE = {name: {v: k for k, v in members.items()} for name, members in ENUMS.items()}

#: 数值 → 名称时，同名不同值的项（TMPro 的 Geometry 在两个枚举里都有）不会冲突，
#: 因为一个字段只属于一个枚举。


def enum_value(enum_name: str, value: Any) -> int:
    """把名称或数值统一成整数枚举值。"""
    members = ENUMS[enum_name]
    if isinstance(value, bool):
        raise FormatError("%s 不支持布尔值" % enum_name)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value in members:
            return members[value]
        # 允许 "3" 这种数字字符串
        try:
            return int(value, 0)
        except ValueError:
            raise FormatError(
                "%s 没有名为 %r 的成员，可选：%s"
                % (enum_name, value, ", ".join(members))
            ) from None
    raise FormatError("%s 的值必须是名称或整数，收到 %r" % (enum_name, value))


def enum_name(enum_name_: str, value: int) -> Any:
    """整数枚举值 → 名称；没有精确匹配时原样返回整数（保持无损）。"""
    exact = _ENUM_REVERSE[enum_name_].get(value)
    return exact if exact is not None else value


# --------------------------------------------------------------------------- #
# 字段与命令描述
# --------------------------------------------------------------------------- #

#: 参考类型：字段指向剧本/角色中的某个实体
REFS = ("character", "bg", "portrait", "face", "emo")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str                       # 线上类型：u8/u16/i32/f32/bool/str/...
    label: str = ""                 # 编辑器里的中文标签
    enum: str | None = None         # 枚举名
    ref: str | None = None          # 引用类型（见 REFS）
    default: Any = 0

    @property
    def is_numeric(self) -> bool:
        return self.kind not in ("str", "bool", "char", "decimal")


@dataclass(frozen=True)
class CommandSpec:
    index: int
    name: str                       # C# 类名
    title: str                      # 编辑器中文名
    group: bool = False             # 是否 GroupCommand
    fields: tuple[FieldSpec, ...] = ()

    def field(self, name: str) -> FieldSpec | None:
        for spec in self.fields:
            if spec.name == name:
                return spec
        return None

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


def _f(name, kind, label, **kw):
    return FieldSpec(name, kind, label, **kw)


F32 = "f32"
I32 = "i32"
BOOL = "bool"
STR = "str"

COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(0, "Say", "说话", fields=(
        _f("character", "u16", "角色", ref="character"),
        _f("conversation", STR, "对话内容", default=""),
        _f("hideOthers", BOOL, "隐藏其他角色"),
        # 默认「文本框内上下左右居中」。
        # 注意引擎里这两个字段的默认值是 0，而 0 **不是** TMPro 的合法对齐成员
        # （VerticalAlignmentOptions 只有 256/512/1024/2048/4096/8192），
        # 所以省略字段时给显式的 Center/Middle 比给 0 更安全。
        _f("horizontal", I32, "水平对齐", enum="HorizontalAlignmentOptions",
           default=ENUMS["HorizontalAlignmentOptions"]["Center"]),
        _f("vertical", I32, "垂直对齐", enum="VerticalAlignmentOptions",
           default=ENUMS["VerticalAlignmentOptions"]["Middle"]),
    )),
    CommandSpec(1, "Synchronization", "同步", group=True),
    CommandSpec(2, "Queue", "队列", group=True),
    CommandSpec(3, "WaitClick", "等待点击"),
    CommandSpec(4, "WaitSecond", "等待秒数", fields=(
        _f("skipable", BOOL, "可跳过"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(5, "CChange", "角色立绘", fields=(
        _f("portrait", "u8", "立绘", ref="portrait"),
        _f("face", "u8", "表情", ref="face"),
    )),
    CommandSpec(6, "CIn", "角色进入", fields=(
        _f("character", "u16", "角色", ref="character"),
        _f("face", "u8", "表情", ref="face"),
        _f("place", F32, "位置"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(7, "CJump", "角色跳跃", fields=(
        _f("high", F32, "高度"),
        _f("time", F32, "时长"),
        _f("vibrato", I32, "震动频率"),
        _f("portrait", "u8", "立绘", ref="portrait"),
    )),
    CommandSpec(8, "CMove", "角色移动", fields=(
        _f("portrait", "u8", "立绘", ref="portrait"),
        _f("position", F32, "位置"),
        _f("time", F32, "时长"),
        _f("ease", I32, "缓动方式", enum="Ease", default=1),
    )),
    CommandSpec(9, "COut", "角色退出", fields=(
        _f("portrait", "u16", "立绘", ref="portrait"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(10, "CShake", "角色震动", fields=(
        _f("duration", F32, "持续时间"),
        _f("strengthX", F32, "横向强度"),
        _f("strengthY", F32, "纵向强度"),
        _f("randomness", F32, "随机度"),
        _f("vibrato", I32, "震动频率"),
        _f("portrait", "u8", "立绘", ref="portrait"),
        _f("fadeOut", BOOL, "淡出"),
    )),
    CommandSpec(11, "BGChange", "切换背景", fields=(
        _f("bgIndex", "u16", "背景编号", ref="bg"),
    )),
    # 注意：字段顺序按 MetadataToken 升序 = **基类字段在前**。
    # BGFade 系列继承自 BGChange，所以继承来的 bgIndex 排在自身字段之前。
    # （Save.SaveCommand 里显式 OrderBy(f => f.MetadataToken)，不是 GetFields() 的
    #   默认「派生类在前」顺序 —— 两者对继承字段正好相反。）
    CommandSpec(12, "BGFade", "背景淡入淡出", fields=(
        _f("bgIndex", "u16", "背景编号", ref="bg"),
        _f("r", F32, "红色"), _f("g", F32, "绿色"), _f("b", F32, "蓝色"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(13, "BGFadeBG", "背景渐变", fields=(
        _f("bgIndex", "u16", "背景编号", ref="bg"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(14, "BGFadeIn", "背景淡入", fields=(
        _f("bgIndex", "u16", "背景编号", ref="bg"),
        _f("r", F32, "红色"), _f("g", F32, "绿色"), _f("b", F32, "蓝色"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(15, "BGFadeOut", "背景淡出", fields=(
        _f("bgIndex", "u16", "背景编号", ref="bg"),
        _f("r", F32, "红色"), _f("g", F32, "绿色"), _f("b", F32, "蓝色"),
        _f("time", F32, "时长"),
    )),
    CommandSpec(16, "BGName", "背景名称", fields=(
        _f("name", STR, "名称", default=""),
    )),
    CommandSpec(17, "BGMove", "背景移动", fields=(
        _f("targetX", F32, "目标 X"), _f("targetY", F32, "目标 Y"),
        _f("duration", F32, "持续时间"), _f("withCharacter", BOOL, "跟随角色"),
    )),
    CommandSpec(18, "BGScale", "背景缩放", fields=(
        _f("targetX", F32, "目标 X"), _f("targetY", F32, "目标 Y"),
        _f("duration", F32, "持续时间"), _f("withCharacter", BOOL, "跟随角色"),
    )),
    CommandSpec(19, "BGShakePos", "背景位置震动", fields=(
        _f("duration", F32, "持续时间"), _f("strengthX", F32, "横向强度"),
        _f("strengthY", F32, "纵向强度"), _f("vibrato", I32, "震动频率"),
        _f("randomness", F32, "随机度"), _f("fadeOut", BOOL, "淡出"),
        _f("random", BOOL, "随机"), _f("withCharacter", BOOL, "跟随角色"),
    )),
    CommandSpec(20, "BGShakeRotation", "背景旋转震动", fields=(
        _f("duration", F32, "持续时间"), _f("strength", F32, "强度"),
        _f("vibrato", I32, "震动频率"), _f("randomness", F32, "随机度"),
        _f("fadeOut", BOOL, "淡出"), _f("random", BOOL, "随机"),
        _f("withCharacter", BOOL, "跟随角色"),
    )),
    CommandSpec(21, "BGShakeScale", "背景缩放震动", fields=(
        _f("duration", F32, "持续时间"), _f("strengthX", F32, "横向强度"),
        _f("strengthY", F32, "纵向强度"), _f("vibrato", I32, "震动频率"),
        _f("randomness", F32, "随机度"), _f("fadeOut", BOOL, "淡出"),
        _f("random", BOOL, "随机"), _f("withCharacter", BOOL, "跟随角色"),
    )),
    CommandSpec(22, "BGM", "背景音乐", fields=(
        _f("path", STR, "路径", default=""),
    )),
    CommandSpec(23, "SFX", "音效", fields=(
        _f("path", STR, "路径", default=""),
    )),
    CommandSpec(24, "CFront", "角色前置", fields=(
        _f("portrait", "u8", "立绘", ref="portrait"),
        _f("hideOthers", BOOL, "隐藏其他角色"),
    )),
    CommandSpec(25, "CEmo", "角色表情", fields=(
        _f("portrait", "u8", "立绘", ref="portrait"),
        _f("emo", "u8", "表情", ref="emo"),
    )),
)

BY_NAME: dict[str, CommandSpec] = {c.name: c for c in COMMANDS}
BY_INDEX: dict[int, CommandSpec] = {c.index: c for c in COMMANDS}

#: 存在但不能用于剧本的类（不在 commandType 表里）
UNUSABLE = ("Act", "CFade")

GROUP_KINDS = ("Synchronization", "Queue")


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #

def coerce_value(spec: FieldSpec, value: Any) -> Any:
    """把 JSON 里的值规范成写入器需要的 Python 值。"""
    if spec.enum is not None:
        return enum_value(spec.enum, value)
    if spec.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(value)
    if spec.kind == "str":
        return "" if value is None else str(value)
    if spec.kind == "char":
        if isinstance(value, int):
            return chr(value)
        if not isinstance(value, str) or len(value) != 1:
            raise FormatError("%s.%s 需要单个字符，收到 %r" % (spec.name, spec.kind, value))
        return value
    if spec.kind == "decimal":
        return value
    if isinstance(value, bool):
        raise FormatError("字段 %s 需要数值，收到布尔值" % spec.name)
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise FormatError("字段 %s 需要数值，收到 %r" % (spec.name, value)) from None
    if spec.kind.startswith("f"):
        return float(value)
    if spec.kind.startswith("u") or spec.kind.startswith("i"):
        ivalue = int(value)
        size = int(spec.kind[1:]) * 8
        if spec.kind.startswith("u"):
            if not 0 <= ivalue < (1 << size):
                raise FormatError("字段 %s 超出 %s 范围: %d" % (spec.name, spec.kind, ivalue))
        else:
            if not -(1 << (size - 1)) <= ivalue < (1 << (size - 1)):
                raise FormatError("字段 %s 超出 %s 范围: %d" % (spec.name, spec.kind, ivalue))
        return ivalue
    raise FormatError("未知字段类型: %s" % spec.kind)


def _write_field(writer: BinWriter, spec: FieldSpec, value: Any) -> None:
    # 字段 kind 与 BinWriter 的方法名一一对应
    getattr(writer, spec.kind)(coerce_value(spec, value))


def _read_field(reader: BinReader, spec: FieldSpec) -> Any:
    # 内部表示刻意保持"线上格式"：枚举就是整数。
    # 名称 ↔ 整数的转换只发生在规范 JSON 层（jsonfmt）与显示层（cli）。
    return getattr(reader, spec.kind)()


def encode_command(writer: BinWriter, command: dict) -> None:
    """把一个命令 dict 写入 writer。

    ``command`` 形如 ``{"type": "Say", "character": 0, ...}``；
    组命令带 ``"commands"`` 子命令列表。
    """
    name = command.get("type")
    spec = BY_NAME.get(name)
    if spec is None:
        if name in UNUSABLE:
            raise FormatError("%s 不在引擎的命令表里，无法写入剧本" % name)
        raise FormatError("未知命令类型: %r" % name)
    writer.u16(spec.index)
    if spec.group:
        subs = command.get("commands") or []
        if len(subs) > 0xFFFF:
            raise FormatError("%s 的子命令超过 65535 条" % name)
        writer.u16(len(subs))
        for sub in subs:
            encode_command(writer, sub)
    for field_spec in spec.fields:
        key = field_spec.name
        if key in command and command[key] is not None:
            value = command[key]
        else:
            value = field_spec.default
        _write_field(writer, field_spec, value)


def decode_command(reader: BinReader, depth: int = 0) -> dict:
    """从 reader 读出一个命令 dict。"""
    if depth > 64:
        raise FormatError("命令嵌套超过 64 层，数据可能已损坏")
    index = reader.u16()
    spec = BY_INDEX.get(index)
    if spec is None:
        raise FormatError(
            "typeIndex %d 不在 0..%d 范围内（数据损坏或版本不匹配）"
            % (index, len(COMMANDS) - 1)
        )
    command: dict[str, Any] = {"type": spec.name}
    if spec.group:
        count = reader.u16()
        command["commands"] = [decode_command(reader, depth + 1) for _ in range(count)]
    for field_spec in spec.fields:
        command[field_spec.name] = _read_field(reader, field_spec)
    return command
