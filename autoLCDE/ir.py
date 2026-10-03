"""剧本中间表示（IR）—— 模型输出与引擎要求之间的缓冲层。

模型（哪怕是很好的模型）都会有这些毛病：字段名起得不一样、表情名写成
「微笑」而角色表里叫「笑」、引用了不存在的背景、把旁白写成某人的台词、
往正文里塞运镜。直接在编译期崩掉是没用的——那只会浪费一次 API 调用。

所以这一层统一做：**别名归一 → 引用解析 → 越界降级 → 记录 warning**。
能安全修的都修掉并如实记账；修不了的（比如 ``raw`` 手写命令）
留给 :mod:`autoLCDE.compile` 原样透传，最终由 ``lcde validate`` 兜底。

IR 可以直接序列化成 ``剧本.json``（字段名与提示词里描述的一致），
用户手工改完再 ``autoLCDE compile`` 即可，不必重调 API。
"""

from __future__ import annotations

import colorsys
import hashlib
import re
from dataclasses import dataclass, field

from .errors import ScriptError

__all__ = [
    "CastMember", "Background", "AudioCue", "Line", "Scene", "Screenplay",
    "build_screenplay", "sanitize_file_name", "sanitize_color", "derive_color",
    "SLOTS", "LINE_KINDS",
]

# --------------------------------------------------------------------------- #
# 常量表
# --------------------------------------------------------------------------- #

#: 立绘位置上档：槽位名 → 舞台横向中心（0..800），由 CLI/配置覆盖。
SLOTS = ("left", "center", "right")

_SLOT_ALIASES = {
    "left": "left", "l": "left", "左": "left", "左位": "left", "左侧": "left", "左边": "left",
    "center": "center", "centre": "center", "c": "center", "mid": "center", "middle": "center",
    "中": "center", "中位": "center", "中间": "center", "正中": "center",
    "right": "right", "r": "right", "右": "right", "右位": "right", "右侧": "right", "右边": "right",
    "off": "off", "none": "off", "": "off", "离屏": "off", "不上屏": "off",
}

#: 每种 line 允许的字段（其余字段会被丢掉并记账）。
LINE_KINDS = {
    "narr": ("text",),
    "say": ("who", "text", "face"),
    "board": ("text",),
    "sfx": ("cue",),
    "bgm": ("cue",),
    "enter": ("who", "face", "slot", "x", "time"),
    "exit": ("who", "time"),
    "exitAll": ("time",),
    "move": ("who", "slot", "x", "time"),
    "face": ("who", "face"),
    "front": ("who", "hide"),
    "undim": ("who",),
    "shake": ("who", "strength"),
    "pause": ("seconds",),
    "wait": (),
    "bg": ("name",),
    "shot": ("text",),
    "raw": ("command",),
}

_KIND_ALIASES = {
    # 旁白 / 动作
    "narr": "narr", "narration": "narr", "action": "narr", "desc": "narr",
    "description": "narr", "旁白": "narr", "动作": "narr", "画面": "narr",
    # 台词
    "say": "say", "dialogue": "say", "line": "say", "speech": "say",
    "台词": "say", "对话": "say",
    # 字幕 / 系统音
    "board": "board", "subtitle": "board", "system": "board", "hud": "board",
    "字幕": "board", "系统": "board", "提示": "board",
    # 音频
    "sfx": "sfx", "sound": "sfx", "se": "sfx", "音效": "sfx",
    "bgm": "bgm", "music": "bgm", "音乐": "bgm", "背景音乐": "bgm",
    # 立绘
    "enter": "enter", "cin": "enter", "appear": "enter", "进入": "enter", "上屏": "enter",
    "exit": "exit", "cout": "exit", "leave": "exit", "退出": "exit", "离场": "exit",
    "exitall": "exitAll", "exit_all": "exitAll", "clear": "exitAll", "全部退出": "exitAll",
    "move": "move", "cmove": "move", "移动": "move", "走位": "move",
    "face": "face", "cchange": "face", "expression": "face", "表情": "face",
    "front": "front", "cfront": "front", "前置": "front", "压暗": "front",
    "undim": "undim", "restore": "undim", "还原": "undim", "恢复": "undim",
    "shake": "shake", "cshake": "shake", "震动": "shake",
    # 节奏与背景
    "pause": "pause", "wait_second": "pause", "waitsecond": "pause", "停顿": "pause", "等待秒数": "pause",
    "wait": "wait", "waitclick": "wait", "click": "wait", "等待点击": "wait", "等点击": "wait",
    "bg": "bg", "background": "bg", "bgchange": "bg", "背景": "bg", "切换背景": "bg",
    # 备注与逃生口
    "shot": "shot", "note": "shot", "camera": "shot", "分镜": "shot", "镜头": "shot", "备注": "shot",
    "raw": "raw", "command": "raw", "原生": "raw", "原始命令": "raw",
}

_ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_ILLEGAL_TITLE = re.compile(r'[\\/:*?"<>|]')
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")

#: 去掉模型爱加的「老周：」前缀
_LEADING_SPEAKER = re.compile(r"^\s*[【\[]?[^：:【】\[\]]{1,12}[】\]]?\s*[：:]\s*")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def sanitize_file_name(text: str, *, fallback: str = "未命名") -> str:
    """把名字收拾成能当文件名用的样子（引擎对 ``\\ / : * ? " < > |`` 一律不接受）。"""
    cleaned = _ILLEGAL_NAME.sub("", str(text or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or fallback


def sanitize_title(text: str, *, fallback: str = "未命名剧本") -> str:
    """剧本标题：去非法字符，限长（引擎直接用文件名）。"""
    cleaned = _ILLEGAL_TITLE.sub("", str(text or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:60]


def sanitize_color(value, *, fallback_key: str = "", default: str = "D8D8D8") -> str:
    """颜色统一成 6 位大写十六进制（**不带** ``#``，带 # 引擎会解析失败）。"""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return "".join("%02X" % max(0, min(255, int(c))) for c in value[:3])
        except (TypeError, ValueError):
            value = None
    if isinstance(value, str):
        found = _HEX.match(value.strip())
        if found:
            return found.group(1).upper()
    if fallback_key:
        return derive_color(fallback_key)
    return default.upper()


def derive_color(key: str, *, index: int = 0) -> str:
    """由字符串稳定派生一个柔和的名字色（不同角色一眼能分开）。"""
    digest = hashlib.md5(("%s#%d" % (key, index)).encode("utf-8")).digest()
    hue = (digest[0] / 255.0 + index * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.74, 0.42)
    return "%02X%02X%02X" % (int(red * 255), int(green * 255), int(blue * 255))


def _text(value, *, strip_speaker: str = "") -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    if strip_speaker:
        stripped = _LEADING_SPEAKER.sub("", text, count=1)
        if stripped and stripped != text and text.startswith(strip_speaker):
            text = stripped
    return text


def _first_of(mapping: dict, *names):
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是", "真", "on")


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_slot(value) -> str:
    """槽位名归一；认不出来返回 ``off``（调用方通常当作 center 处理）。"""
    if value is None:
        return "off"
    key = str(value).strip().lower()
    return _SLOT_ALIASES.get(key, "off")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class CastMember:
    id: str
    name: str = ""
    camp: str = ""
    name_color: str = ""
    camp_color: str = ""
    faces: list[str] = field(default_factory=list)
    subtitles: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "camp": self.camp,
            "nameColor": self.name_color, "campColor": self.camp_color,
            "faces": list(self.faces), "subtitles": self.subtitles,
            "notes": self.notes,
        }


@dataclass
class Background:
    name: str
    label: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "notes": self.notes}


@dataclass
class AudioCue:
    key: str
    path: str = ""
    usage: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "path": self.path, "usage": self.usage}


@dataclass
class Line:
    kind: str
    data: dict = field(default_factory=dict)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        out = {"kind": self.kind}
        out.update({k: v for k, v in self.data.items() if v is not None})
        return out


@dataclass
class Scene:
    index: int = 1
    title: str = ""
    background: str = ""
    label: str = ""
    bgm: str | None = None
    summary: str = ""
    beats: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "index": self.index, "title": self.title,
            "background": self.background, "label": self.label,
            "summary": self.summary, "beats": list(self.beats),
            "notes": list(self.notes),
            "lines": [line.to_dict() for line in self.lines],
        }
        # None = 不改动当前 BGM（不写这个键）；"" = 显式停止；其余 = 切到该 BGM
        if self.bgm is not None:
            out["bgm"] = self.bgm
        return out


@dataclass
class Screenplay:
    title: str
    logline: str = ""
    tone: str = ""
    cast: list[CastMember] = field(default_factory=list)
    backgrounds: list[Background] = field(default_factory=list)
    bgm: list[AudioCue] = field(default_factory=list)
    sfx: list[AudioCue] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 概念阶段的原样输出，便于排查/续写
    concept: dict = field(default_factory=dict)

    # -- 查询 -------------------------------------------------------------- #
    @property
    def board_voice(self) -> CastMember | None:
        """字幕型角色（``subtitles: true``）；没有就返回 None（字幕降级为旁白）。"""
        for member in self.cast:
            if member.subtitles:
                return member
        return None

    def cast_index(self, who: str) -> int | None:
        target = str(who or "").strip()
        for index, member in enumerate(self.cast):
            if target == member.id or target == member.name:
                return index
        for index, member in enumerate(self.cast):
            if target and (target in member.id or target in member.name):
                return index
        return None

    def scene_stats(self) -> dict:
        says = sum(1 for scene in self.scenes for line in scene.lines
                   if line.kind in ("say", "narr", "board"))
        return {
            "scenes": len(self.scenes),
            "cast": len(self.cast),
            "backgrounds": len(self.backgrounds),
            "lines": sum(len(scene.lines) for scene in self.scenes),
            "spoken": says,
        }

    # -- 序列化 ------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "format": "autolcde-script",
            "version": 1,
            "kind": "screenplay",
            "title": self.title,
            "logline": self.logline,
            "tone": self.tone,
            "cast": [member.to_dict() for member in self.cast],
            "backgrounds": [bg.to_dict() for bg in self.backgrounds],
            "audio": {
                "bgm": [cue.to_dict() for cue in self.bgm],
                "sfx": [cue.to_dict() for cue in self.sfx],
            },
            "scenes": [scene.to_dict() for scene in self.scenes],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Screenplay":
        """从 ``剧本.json`` 读回（``compile`` / ``resume`` 用，不需要 API）。"""
        if not isinstance(data, dict):
            raise ScriptError("剧本 JSON 顶层必须是对象")
        cast = [CastMember(
            id=str(item.get("id") or item.get("name") or "").strip(),
            name=str(item.get("name") or item.get("id") or "").strip(),
            camp=str(item.get("camp") or "").strip(),
            name_color=sanitize_color(item.get("nameColor") or item.get("name_color")),
            camp_color=sanitize_color(item.get("campColor") or item.get("camp_color")),
            faces=[str(f) for f in _as_list(item.get("faces"))] or ["常态"],
            subtitles=_as_bool(item.get("subtitles") or item.get("subtitle")),
            notes=str(item.get("notes") or ""),
        ) for item in _as_list(data.get("cast"))]
        backgrounds = [Background(
            name=str(item.get("name") if isinstance(item, dict) else item),
            label=str(item.get("label") or "") if isinstance(item, dict) else "",
            notes=str(item.get("notes") or "") if isinstance(item, dict) else "",
        ) for item in _as_list(data.get("backgrounds"))]
        audio = data.get("audio") or {}
        bgm = [AudioCue(str(c.get("key") or ""), str(c.get("path") or ""), str(c.get("usage") or ""))
               for c in _as_list(audio.get("bgm")) if isinstance(c, dict)]
        sfx = [AudioCue(str(c.get("key") or ""), str(c.get("path") or ""), str(c.get("usage") or ""))
               for c in _as_list(audio.get("sfx")) if isinstance(c, dict)]
        scenes = [Scene(
            index=int(scene.get("index") or position + 1),
            title=str(scene.get("title") or ""),
            background=str(scene.get("background") or ""),
            label=str(scene.get("label") or ""),
            bgm=("" if scene.get("bgm") in (None, "") else str(scene["bgm"]))
                if "bgm" in scene else None,
            summary=str(scene.get("summary") or ""),
            beats=[str(b) for b in _as_list(scene.get("beats"))],
            notes=[str(n) for n in _as_list(scene.get("notes"))],
            lines=[_line_from_dict(item) for item in _as_list(scene.get("lines"))],
        ) for position, scene in enumerate(_as_list(data.get("scenes"))) if isinstance(scene, dict)]
        return cls(
            title=str(data.get("title") or "未命名剧本"),
            logline=str(data.get("logline") or ""),
            tone=str(data.get("tone") or ""),
            cast=cast, backgrounds=backgrounds, bgm=bgm, sfx=sfx, scenes=scenes,
            warnings=[str(w) for w in _as_list(data.get("warnings"))],
            concept=dict(data.get("concept") or {}),
        )


def _line_from_dict(item) -> Line:
    if not isinstance(item, dict):
        return Line("narr", {"text": str(item)})
    raw = dict(item)
    kind = str(raw.pop("kind", raw.pop("type", "narr"))).strip().lower()
    kind = _KIND_ALIASES.get(kind, kind)
    data = {}
    for field_name in LINE_KINDS.get(kind, ()):
        for candidate in _FIELD_ALIASES.get(field_name, (field_name,)):
            if candidate in raw:
                data[field_name] = raw[candidate]
                break
    return Line(kind, data)


_FIELD_ALIASES = {
    "text": ("text", "conversation", "content", "line", "台词", "内容"),
    "who": ("who", "character", "speaker", "name", "角色", "说话人"),
    "face": ("face", "expression", "表情"),
    "cue": ("cue", "key", "path", "sound", "音频"),
    "slot": ("slot", "place", "position", "side", "位置"),
    "x": ("x", "center_x", "px"),
    "time": ("time", "duration", "seconds", "时长"),
    "hide": ("hide", "hideOthers", "hide_others", "压暗"),
    "strength": ("strength", "strengthX", "amount", "强度"),
    "seconds": ("seconds", "time", "duration", "时长"),
    "name": ("name", "background", "bg", "背景"),
    "command": ("command", "payload", "cmd", "命令"),
}


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

class _Normalizer:
    def __init__(self, *, warn) -> None:
        self.warn = warn
        #: 分场阶段临时冒出来的音频事件（概念表里没有），最后并进 Screenplay
        self.extra_audio: dict[str, list[AudioCue]] = {"bgm": [], "sfx": []}

    def audio_extra(self, kind: str, cue: AudioCue) -> None:
        bucket = self.extra_audio.setdefault(kind, [])
        if all(existing.key != cue.key for existing in bucket):
            bucket.append(cue)

    # -- 角色 -------------------------------------------------------------- #
    def cast(self, items) -> list[CastMember]:
        members: list[CastMember] = []
        seen: dict[str, int] = {}
        for position, raw in enumerate(_as_list(items)):
            if isinstance(raw, str):
                raw = {"id": raw}
            if not isinstance(raw, dict):
                continue
            char_id = sanitize_file_name(
                _first_of(raw, "id", "name", "角色") or "", fallback="角色%d" % (position + 1))
            if char_id in seen:
                self.warn("角色 id 重复：%s（已跳过重复项）" % char_id)
                continue
            name = _text(_first_of(raw, "name", "display", "显示名") or char_id)
            camp = _text(_first_of(raw, "camp", "faction", "title", "阵营") or "")
            faces = [_text(f) for f in _as_list(_first_of(raw, "faces", "expressions", "表情"))
                     if _text(f)]
            if not faces:
                faces = ["常态"]
                self.warn("角色 %s 没有表情列表，已补一个「常态」" % char_id)
            deduped: list[str] = []
            for face in faces:
                face = sanitize_file_name(face, fallback="表情")
                if face not in deduped:
                    deduped.append(face)
            key = char_id
            members.append(CastMember(
                id=char_id, name=name or char_id, camp=camp,
                name_color=sanitize_color(_first_of(raw, "nameColor", "name_color", "颜色"),
                                          fallback_key="name:" + key),
                camp_color=sanitize_color(_first_of(raw, "campColor", "camp_color"),
                                          fallback_key="camp:" + key, default="8A8270"),
                faces=deduped[:12],
                subtitles=_as_bool(_first_of(raw, "subtitles", "subtitle", "isSubtitle", "字幕")),
                notes=_text(_first_of(raw, "notes", "note", "备注") or ""),
            ))
            seen[char_id] = len(members) - 1
        if not members:
            raise ScriptError("概念里没有任何角色；至少要有一个 cast 成员")
        if not any(member.subtitles for member in members):
            # 没有声明字幕角色时，把「广播/系统/字幕/旁白」这类名字自动认领
            for member in members:
                if any(word in member.id for word in ("广播", "系统", "字幕", "棋盘", "提示", "旁白")):
                    member.subtitles = True
                    self.warn("没有声明 subtitles 角色，已把「%s」当作字幕角色" % member.id)
                    break
        return members

    # -- 背景 -------------------------------------------------------------- #
    def backgrounds(self, items, extra_names=()) -> list[Background]:
        result: list[Background] = []
        seen: set[str] = set()

        def add(name, label="", notes=""):
            clean = sanitize_file_name(name, fallback="背景")
            if not clean:
                return
            if not re.search(r"\.(png|jpg|jpeg|bmp|tga|webp)$", clean, re.I):
                clean += ".png"
            if clean in seen:
                return
            seen.add(clean)
            result.append(Background(clean, _text(label) or clean.rsplit(".", 1)[0],
                                     _text(notes)))

        for raw in _as_list(items):
            if isinstance(raw, str):
                add(raw)
            elif isinstance(raw, dict):
                add(_first_of(raw, "name", "file", "path", "背景") or "",
                    _first_of(raw, "label", "display", "title", "显示名") or "",
                    _first_of(raw, "notes", "note", "备注") or "")
        for name in extra_names:
            if sanitize_file_name(name, fallback="背景") not in seen:
                add(name)
                self.warn("场景引用了概念表里没有的背景「%s」，已自动补进背景表" % name)
        return result

    # -- 音频 -------------------------------------------------------------- #
    def audio(self, raw, kind: str) -> list[AudioCue]:
        cues: list[AudioCue] = []
        seen: set[str] = set()
        for item in _as_list(raw):
            if isinstance(item, str):
                item = {"key": item, "path": item}
            if not isinstance(item, dict):
                continue
            key = _text(_first_of(item, "key", "id", "name", "名字") or "")
            path = _text(_first_of(item, "path", "event", "file", "路径") or "")
            if not key and path:
                key = path.rstrip("/").split("/")[-1]
            if not key:
                continue
            key = re.sub(r"\s+", "_", key)
            if key in seen:
                continue
            seen.add(key)
            if not path:
                path = "event:/%s/%s" % (kind.upper(), key)
            elif not path.startswith("event:"):
                path = "event:/" + path.lstrip("/")
            cues.append(AudioCue(key, path, _text(_first_of(item, "usage", "use", "用途") or "")))
        return cues


def build_screenplay(concept: dict, scene_payloads, *, fallback_title: str = "未命名剧本",
                     warn=None) -> Screenplay:
    """把「概念 + 若干场次」拼成 :class:`Screenplay`，顺带把所有毛病记账。

    ``scene_payloads`` 里每项可以是 ``{"scene": {...}}``、直接是场景对象，
    或者模型偷懒给的 ``{"lines": [...]}``。
    """
    warnings: list[str] = []

    def note(message: str) -> None:
        warnings.append(message)
        if warn:
            warn(message)

    if not isinstance(concept, dict):
        raise ScriptError("概念阶段的输出不是 JSON 对象")
    normalizer = _Normalizer(warn=note)

    title = sanitize_title(_first_of(concept, "title", "name", "标题") or fallback_title,
                           fallback=fallback_title)
    cast = normalizer.cast(_first_of(concept, "cast", "characters", "roles", "角色"))

    # 场次先扫一遍背景引用，保证背景表齐全
    scene_items = []
    for payload in _as_list(scene_payloads):
        if isinstance(payload, dict) and isinstance(payload.get("scene"), dict):
            scene_items.append(payload["scene"])
        elif isinstance(payload, dict):
            scene_items.append(payload)
        elif payload is not None:
            note("忽略了一个无法识别的场次输出（%s）" % type(payload).__name__)

    referenced = [_first_of(item, "background", "bg", "背景") for item in scene_items]
    backgrounds = normalizer.backgrounds(
        _first_of(concept, "backgrounds", "background", "scenes_bg", "背景"),
        extra_names=[name for name in referenced if name])

    audio_raw = _first_of(concept, "audio", "sound", "audioCues") or {}
    if not isinstance(audio_raw, dict):
        audio_raw = {}
    bgm = normalizer.audio(_first_of(audio_raw, "bgm", "music", "BGM"), "BGM")
    sfx = normalizer.audio(_first_of(audio_raw, "sfx", "sound", "se", "SFX"), "SFX")
    bgm_keys = {cue.key for cue in bgm}
    sfx_keys = {cue.key for cue in sfx}

    # 场次大纲（概念里的；用来补台词缺失的元信息）
    outlines = {}
    for position, item in enumerate(_as_list(_first_of(concept, "scenes", "outline", "场次"))):
        if isinstance(item, dict):
            outlines[position] = item
        elif isinstance(item, str):
            outlines[position] = {"summary": item}

    scenes: list[Scene] = []
    for position, item in enumerate(scene_items):
        outline = outlines.get(position, {})
        # bgm 三态：字段缺失 → None（不改动当前音乐）；显式 null/"" → ""（停止）；
        #           给了 key → 切到该 BGM。分场没写就沿用场次大纲里的值。
        if "bgm" in item or "music" in item:
            scene_bgm = _first_of(item, "bgm", "music", "背景音乐")
            scene_bgm = "" if scene_bgm is None else str(scene_bgm)
        else:
            inherited = _first_of(outline, "bgm", "music", "背景音乐")
            scene_bgm = None if inherited is None else str(inherited)
        scene = Scene(
            index=int(_first_of(item, "index", "no", "序号") or position + 1),
            title=_text(_first_of(item, "title", "name", "标题") or outline.get("title") or
                        "第%d场" % (position + 1)),
            background=_text(_first_of(item, "background", "bg", "背景") or
                             outline.get("background") or ""),
            label=_text(_first_of(item, "label", "bgName", "背景名") or
                        outline.get("label") or ""),
            bgm=scene_bgm,
            summary=_text(_first_of(item, "summary", "梗概") or outline.get("summary") or ""),
            beats=[_text(b) for b in _as_list(_first_of(item, "beats", "节拍") or
                                              outline.get("beats"))],
            notes=[_text(n) for n in _as_list(_first_of(item, "notes", "note", "备注") or
                                              outline.get("notes"))],
        )
        scene.lines = _normalize_lines(item, position, normalizer, cast, backgrounds,
                                       bgm_keys, sfx_keys)
        scenes.append(scene)

    if not scenes:
        raise ScriptError("没有产出任何场次")

    # 分场阶段临时冒出来的 BGM/SFX 合并回音频表
    for kind, bucket in normalizer.extra_audio.items():
        target = bgm if kind == "bgm" else sfx
        for cue in bucket:
            if all(existing.key != cue.key for existing in target):
                target.append(cue)

    # 背景兜底：某个场景没给背景就用第一张，避免整场黑屏
    if backgrounds and not any(scene.background for scene in scenes):
        note("所有场次都没写背景，已统一用 %s" % backgrounds[0].name)
    for scene in scenes:
        if not scene.background:
            if backgrounds:
                scene.background = backgrounds[0].name
                note("第%d场没写背景，已用 %s 兜底" % (scene.index, scene.background))
            else:
                raise ScriptError("既没有背景表，场景也没写背景")
        if not scene.label:
            scene.label = scene.title or scene.background.rsplit(".", 1)[0]

    return Screenplay(title=title,
                      logline=_text(_first_of(concept, "logline", "一句话梗概", "premise") or ""),
                      tone=_text(_first_of(concept, "tone", "风格", "style") or ""),
                      cast=cast, backgrounds=backgrounds, bgm=bgm, sfx=sfx,
                      scenes=scenes, warnings=warnings, concept=dict(concept))


def _normalize_lines(scene_item: dict, position: int, normalizer: _Normalizer,
                     cast: list[CastMember], backgrounds: list[Background],
                     bgm_keys: set[str], sfx_keys: set[str]) -> list[Line]:
    note = normalizer.warn
    raw_lines = _first_of(scene_item, "lines", "commands", "script", "演出", "正文")
    lines: list[Line] = []
    on_screen: list[str] = []           # 这一场内部跟踪，用于「不该有的引用」提示

    def find_cast(who: str):
        target = str(who or "").strip()
        if not target:
            return None
        for member in cast:
            if target == member.id or target == member.name:
                return member
        for member in cast:
            if target in member.id or (member.name and target in member.name):
                return member
        return None

    for raw in _as_list(raw_lines):
        if isinstance(raw, str):
            raw = {"kind": "narr", "text": raw}
        if not isinstance(raw, dict):
            continue
        raw = dict(raw)
        kind_raw = _first_of(raw, "kind", "type", "类型") or "narr"
        kind = _KIND_ALIASES.get(str(kind_raw).strip().lower(), None)
        if kind is None:
            note("第%d场：认不出的演出类型 %r，已当作旁白处理" % (position + 1, kind_raw))
            kind = "narr"
            raw.setdefault("text", str(raw.get("text") or raw.get("content") or ""))

        data: dict = {}
        for field_name in LINE_KINDS[kind]:
            for candidate in _FIELD_ALIASES.get(field_name, (field_name,)):
                if candidate in raw and raw[candidate] not in (None, ""):
                    data[field_name] = raw[candidate]
                    break

        # ---- 台词 / 字幕：解析说话人 ------------------------------------- #
        if kind == "say":
            text = _text(data.get("text"))
            member = find_cast(data.get("who"))
            if member is None:
                if text:
                    note("第%d场：台词引用了角色表里没有的「%s」，已降级为旁白"
                         % (position + 1, data.get("who")))
                    lines.append(Line("narr", {"text": text}))
                continue
            text = _text(text, strip_speaker=member.id) or _text(text, strip_speaker=member.name)
            if not text:
                continue
            data = {"who": member.id, "text": text}
            face = _first_of(raw, "face", "expression", "表情")
            if face:
                data["face"] = _face_of(member, face, position, note)
            lines.append(Line("say", data))
            continue

        if kind == "board":
            text = _text(data.get("text"))
            if text:
                lines.append(Line("board", {"text": text}))
            continue

        if kind == "narr":
            text = _text(data.get("text"))
            if text:
                lines.append(Line("narr", {"text": text}))
            continue

        if kind == "shot":
            text = _text(data.get("text"))
            if text:
                lines.append(Line("shot", {"text": text}))
            continue

        if kind in ("enter", "exit", "move", "face", "front", "undim", "shake"):
            member = find_cast(data.get("who"))
            if member is None:
                note("第%d场：%s 引用了不存在的角色「%s」，该条已删除"
                     % (position + 1, kind, data.get("who")))
                continue
            payload = {"who": member.id}
            if kind == "enter":
                payload["face"] = _face_of(member, data.get("face"), position, note)
                slot = normalize_slot(data.get("slot"))
                if slot == "off":
                    slot = "center"
                payload["slot"] = slot
                if data.get("x") is not None:
                    payload["x"] = _as_float(data["x"], 400.0)
                payload["time"] = _as_float(data.get("time"), 0.45)
                if member.id in on_screen:
                    note("第%d场：%s 已经在台上又 enter 了一次，已改为走位 + 换表情"
                         % (position + 1, member.id))
                    kind = "move"
                else:
                    on_screen.append(member.id)
            elif kind == "exit":
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上却被 exit，该条已删除" % (position + 1, member.id))
                    continue
                on_screen.remove(member.id)
                payload["time"] = _as_float(data.get("time"), 0.4)
            elif kind == "move":
                slot = normalize_slot(data.get("slot"))
                if slot == "off" and data.get("x") is None:
                    note("第%d场：%s 的走位没写位置，已按 center 处理" % (position + 1, member.id))
                    slot = "center"
                payload["slot"] = slot
                if data.get("x") is not None:
                    payload["x"] = _as_float(data["x"], 400.0)
                payload["time"] = _as_float(data.get("time"), 0.0)
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上，move 之前先补一次 enter" % (position + 1, member.id))
                    on_screen.append(member.id)
                    pre = dict(payload)
                    pre["slot"] = slot if slot != "off" else "center"
                    lines.append(Line("enter", {"who": member.id,
                                                "face": member.faces[0],
                                                "slot": pre["slot"], "time": 0.45}))
            elif kind == "face":
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上，换表情已忽略" % (position + 1, member.id))
                    continue
                payload["face"] = _face_of(member, data.get("face"), position, note)
            elif kind == "front":
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上，前置/压暗已忽略" % (position + 1, member.id))
                    continue
                payload["hide"] = _as_bool(data.get("hide"), True)
            elif kind == "undim":
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上，还原亮度已忽略" % (position + 1, member.id))
                    continue
            elif kind == "shake":
                if member.id not in on_screen:
                    note("第%d场：%s 不在台上，震动已忽略" % (position + 1, member.id))
                    continue
                payload["strength"] = _as_float(data.get("strength"), 10.0)
            lines.append(Line(kind, payload))
            continue

        if kind == "exitAll":
            lines.append(Line("exitAll", {"time": _as_float(data.get("time"), 0.4)}))
            on_screen.clear()
            continue

        if kind in ("sfx", "bgm"):
            cue = data.get("cue")
            key = str(cue).strip() if cue is not None else ""
            known = sfx_keys if kind == "sfx" else bgm_keys
            if key and key not in known:
                # 让模型随手写的路径/别名也能用：补一条音频事件出去
                note("第%d场：%s 用了概念表里没有的 %s「%s」，已自动补进音频表"
                     % (position + 1, kind, kind.upper(), key))
                known.add(key)
                path = key if key.startswith("event:") else "event:/%s/%s" % (kind.upper(), key)
                normalizer.audio_extra(kind, AudioCue(key if not key.startswith("event:") else
                                                      key.rstrip("/").split("/")[-1], path, ""))
            if kind == "bgm":
                lines.append(Line("bgm", {"cue": key or None}))
            elif key:
                lines.append(Line("sfx", {"cue": key}))
            continue

        if kind == "pause":
            lines.append(Line("pause", {"seconds": _as_float(data.get("seconds"), 0.35)}))
            continue

        if kind == "wait":
            lines.append(Line("wait", {}))
            continue

        if kind == "bg":
            name = data.get("name")
            if name:
                clean = sanitize_file_name(name, fallback="")
                if clean and not re.search(r"\.(png|jpg|jpeg|bmp|tga|webp)$", clean, re.I):
                    clean += ".png"
                known = {bg.name for bg in backgrounds}
                if clean and clean not in known:
                    note("第%d场中途切到未登记的背景「%s」，已自动补进背景表" % (position + 1, clean))
                    backgrounds.append(Background(clean, clean.rsplit(".", 1)[0], ""))
                lines.append(Line("bg", {"name": clean}))
            continue

        if kind == "raw":
            command = data.get("command")
            if isinstance(command, dict) and command.get("type"):
                lines.append(Line("raw", {"command": command}))
            else:
                note("第%d场：raw 命令格式不对（需要 {\"type\": ...}），已忽略" % (position + 1))
            continue

    return lines


def _face_of(member: CastMember, face, position: int, note) -> str:
    """表情名容错：认不出来就用第一个，并说明。"""
    if face in (None, ""):
        return member.faces[0]
    target = str(face).strip()
    if target in member.faces:
        return target
    for candidate in member.faces:
        if target in candidate or candidate in target:
            return candidate
    note("第%d场：角色 %s 没有表情「%s」，已改用「%s」"
         % (position + 1, member.id, target, member.faces[0]))
    return member.faces[0]
