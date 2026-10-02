"""编译层：IR（:mod:`aiplay.ir`）→ LCDE 规范文档。

这里等价于手写项目里那个 ``build.py``，只是**数据驱动**：角色、背景、音频、
场次都来自模型（或用户改过的 ``剧本.json``），不写死在代码里。

最关键的一件事是**立绘下标**。``portrait`` 指的是「屏幕上现在第几个立绘」，
随 ``CIn`` 追加、``COut`` 移除而整体前移——手工数必错。所以这里移植了
``build.py`` 里的状态机：写剧本时只说「谁」，下标由 :class:`Compiler` 现算，
因此生成出来的命令流**下标永远合法**（``lcde validate`` 的 S15/S16 不会误报）。

另一件容易踩的坑：引擎的 ``CChange`` 会把立绘位置重置成 ``rect[face]`` 的坐标
（``Portrait.ChangePortrait`` 同时写 anchoredPosition），所以换表情后必须紧跟一条
瞬时 ``CMove`` 还原横坐标，否则角色会跳回默认位置。``set_face()`` 自动补这一条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import GENERATOR
from .bridge import (
    BY_NAME,
    FORMAT_NAME,
    FORMAT_VERSION,
    PORTRAIT_SIZE,
    Character,
    Portrait,
    Story,
    character_to_doc,
    coerce_value,
    fit_rect,
    place_for_center_x,
    story_to_doc,
)
from .ir import Background, CastMember, Screenplay, sanitize_title

__all__ = ["CompileOptions", "CompileResult", "compile_screenplay", "screenplay_to_document"]

#: 与 ``build.py`` 一致的中性立绘裁剪框（400×800 源图正好填满 800×358.98 的舞台）。
NEUTRAL_RECT = tuple(fit_rect(*PORTRAIT_SIZE))


@dataclass
class CompileOptions:
    """演出参数。默认值来自手写项目 ``build.py`` 的实测取值。"""

    #: 立绘槽位 → 舞台横向中心（0..800，单位是舞台像素）
    stage_targets: dict = field(default_factory=lambda: {"left": 220.0, "center": 400.0,
                                                         "right": 580.0})
    fade_in: float = 0.45          # 立绘进入淡入
    fade_out: float = 0.4          # 立绘退出淡出
    scene_fade: float = 0.5        # 场景过色
    beat: float = 0.35             # 无台词停顿默认值
    narration_index: int = 65535   # Say.character 取此值 → 不显示名字框（旁白）
    wait_after_say: bool = True    # 每句台词后自动补 WaitClick
    auto_clear_on_scene_change: bool = True
    board_cue: str | None = "board"   # 字幕前播这个 SFX（音频表里没有就不播）
    portrait_rect: tuple = NEUTRAL_RECT

    def x_for(self, slot: str, explicit: float | None = None) -> float:
        """槽位 → ``CIn.place`` / ``CMove.position`` 的取值。"""
        if explicit is not None:
            return float(explicit)
        center = self.stage_targets.get(slot, self.stage_targets.get("center", 400.0))
        return place_for_center_x(float(center), self.portrait_rect[0])


@dataclass
class CompileResult:
    document: dict
    warnings: list[str]
    #: ``scene_of_command[i]`` = 第 i 条顶层命令属于第几个场次（0 基）
    scene_of_command: list[int]
    stats: dict

    @property
    def commands(self) -> list[dict]:
        return self.document["stories"][0]["commands"]


# --------------------------------------------------------------------------- #
# 编译器
# --------------------------------------------------------------------------- #

class Compiler:
    """把一部 :class:`Screenplay` 编译成命令流，并跟踪屏幕上的立绘。"""

    def __init__(self, play: Screenplay, options: CompileOptions | None = None) -> None:
        self.play = play
        self.options = options or CompileOptions()
        self.commands: list[dict] = []
        self.warnings: list[str] = []
        self._screen: list[dict] = []          # [{who, x, face}]
        self._scene = 0
        self.scene_of_command: list[int] = []
        self._bg = 0

        self._cast_index = {member.id: index for index, member in enumerate(play.cast)}
        self._faces = {member.id: list(member.faces) for member in play.cast}
        self._bg_index = {bg.name: index for index, bg in enumerate(play.backgrounds)}
        self._bgm = {cue.key: cue.path for cue in play.bgm}
        self._sfx = {cue.key: cue.path for cue in play.sfx}

    # -- 记账 -------------------------------------------------------------- #
    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def emit(self, command: dict, *, scene: int | None = None) -> None:
        """规范化并追加一条命令（字段名写错会在这里就暴露）。"""
        spec = BY_NAME.get(command.get("type"))
        if spec is None:
            self.warn("丢弃未知命令 %r" % command.get("type"))
            return
        out: dict = {"type": spec.name}
        known = set(spec.field_names)
        for key in command:
            if key not in known and key != "type":
                self.warn("%s 没有字段 %r，已忽略" % (spec.name, key))
        for spec_field in spec.fields:
            out[spec_field.name] = coerce_value(
                spec_field, command.get(spec_field.name, spec_field.default))
        self.commands.append(out)
        self.scene_of_command.append(self._scene if scene is None else scene)

    # -- 屏幕状态 ---------------------------------------------------------- #
    def _pos(self, who: str) -> int:
        for index, item in enumerate(self._screen):
            if item["who"] == who:
                return index
        raise KeyError(who)

    def on_screen(self, who: str) -> bool:
        return any(item["who"] == who for item in self._screen)

    def _face_index(self, who: str, face: str) -> int:
        faces = self._faces.get(who) or ["常态"]
        if face not in faces:
            self.warn("角色 %s 没有表情「%s」，已用「%s」" % (who, face, faces[0]))
            face = faces[0]
        return faces.index(face)

    # -- 演出 -------------------------------------------------------------- #
    def scene(self, scene) -> None:
        self._scene = max(0, int(scene.index) - 1)
        if self.options.auto_clear_on_scene_change and self._screen:
            self.exit_all(self.options.fade_out)
        index = self._bg_index.get(scene.background, 0)
        self._bg = index
        self.emit({"type": "BGChange", "bgIndex": index})
        if scene.label:
            self.emit({"type": "BGName", "name": scene.label})
        if scene.bgm is not None:                     # None = 不改动当前 BGM
            self.bgm(scene.bgm or None)

    def set_background(self, name: str) -> None:
        if name not in self._bg_index:
            self._bg_index[name] = len(self.play.backgrounds)
            self.play.backgrounds.append(Background(name, name.rsplit(".", 1)[0], ""))
        self._bg = self._bg_index[name]
        self.emit({"type": "BGChange", "bgIndex": self._bg})

    def flash(self, r: float = 1.0, g: float = 1.0, b: float = 1.0,
              time: float | None = None) -> None:
        self.emit({"type": "BGFade", "bgIndex": self._bg, "r": r, "g": g, "b": b,
                   "time": self.options.scene_fade if time is None else time})

    def bgm(self, cue: str | None) -> None:
        path = "" if cue is None else (self._bgm.get(cue) or _event_path(cue, "BGM"))
        self.emit({"type": "BGM", "path": path})

    def sfx(self, cue: str) -> None:
        path = self._sfx.get(cue) or _event_path(cue, "SFX")
        self.emit({"type": "SFX", "path": path})

    def enter(self, who: str, face: str, slot: str = "center",
              x: float | None = None, time: float | None = None) -> int:
        if self.on_screen(who):
            self.warn("%s 已经在台上，重复的 enter 已改为走位" % who)
            self.move(who, slot, x, 0.0)
            self.set_face(who, face)
            return self._pos(who)
        place = self.options.x_for(slot, x)
        self.emit({"type": "CIn", "character": self._cast_index[who],
                   "face": self._face_index(who, face), "place": place,
                   "time": self.options.fade_in if time is None else float(time)})
        self._screen.append({"who": who, "x": place, "face": face})
        return len(self._screen) - 1

    def exit(self, who: str, time: float | None = None) -> None:
        index = self._pos(who)
        self.emit({"type": "COut", "portrait": index,
                   "time": self.options.fade_out if time is None else float(time)})
        self._screen.pop(index)

    def exit_all(self, time: float | None = None) -> None:
        while self._screen:
            self.exit(self._screen[-1]["who"], time)

    def move(self, who: str, slot: str = "center", x: float | None = None,
             time: float = 0.0) -> None:
        index = self._pos(who)
        position = self.options.x_for(slot, x)
        self.emit({"type": "CMove", "portrait": index, "position": position,
                   "time": float(time), "ease": "Linear"})
        self._screen[index]["x"] = position

    def set_face(self, who: str, face: str) -> None:
        index = self._pos(who)
        if self._screen[index]["face"] == face:
            return
        self.emit({"type": "CChange", "portrait": index, "face": self._face_index(who, face)})
        # CChange 会重置立绘位置，必须补一条瞬时 CMove 还原（见模块开头说明）
        self.emit({"type": "CMove", "portrait": index, "position": self._screen[index]["x"],
                   "time": 0.0, "ease": "Linear"})
        self._screen[index]["face"] = face

    def shake(self, who: str, strength: float = 10.0) -> None:
        self.emit({"type": "CShake", "duration": 0.4, "strengthX": float(strength),
                   "strengthY": float(strength), "randomness": 20.0, "vibrato": 12,
                   "portrait": self._pos(who), "fadeOut": True})

    def front(self, who: str, hide: bool = True) -> None:
        self.emit({"type": "CFront", "portrait": self._pos(who), "hideOthers": bool(hide)})

    def say(self, who: str | None, text: str, *, face: str | None = None) -> None:
        if who is not None and face and self.on_screen(who):
            self.set_face(who, face)
        index = self.options.narration_index if who is None else self._cast_index[who]
        self.emit({"type": "Say", "character": index, "conversation": text,
                   "hideOthers": False, "horizontal": "Center", "vertical": "Middle"})
        if self.options.wait_after_say:
            self.emit({"type": "WaitClick"})

    def board(self, text: str) -> None:
        voice = self.play.board_voice
        if voice is not None and self.options.board_cue and self.options.board_cue in self._sfx:
            self.sfx(self.options.board_cue)
        if voice is None:
            self.warn("没有字幕型角色，字幕已作为旁白呈现：%s" % text[:20])
            self.say(None, text)
        else:
            self.say(voice.id, text)

    def pause(self, seconds: float | None = None) -> None:
        self.emit({"type": "WaitSecond", "skipable": True,
                   "time": self.options.beat if seconds is None else float(seconds)})

    # -- 主流程 ------------------------------------------------------------ #
    def run(self) -> None:
        for scene in self.play.scenes:
            self.scene(scene)
            for line in scene.lines:
                self._emit_line(line)

    def _emit_line(self, line) -> None:
        kind = line.kind
        data = line.data

        if kind == "narr":
            self.say(None, data.get("text") or "")
        elif kind == "say":
            self.say(data.get("who"), data.get("text") or "", face=data.get("face"))
        elif kind == "board":
            self.board(data.get("text") or "")
        elif kind == "sfx":
            if data.get("cue"):
                self.sfx(data["cue"])
        elif kind == "bgm":
            self.bgm(data.get("cue") or None)
        elif kind == "enter":
            who = data.get("who")
            if who in self._cast_index:
                self.enter(who, data.get("face") or self._faces[who][0],
                           data.get("slot") or "center", data.get("x"),
                           data.get("time"))
        elif kind == "exit":
            who = data.get("who")
            if who in self._cast_index and self.on_screen(who):
                self.exit(who, data.get("time"))
        elif kind == "exitAll":
            self.exit_all(data.get("time"))
        elif kind == "move":
            who = data.get("who")
            if who in self._cast_index:
                if not self.on_screen(who):
                    self.enter(who, self._faces[who][0], data.get("slot") or "center",
                               data.get("x"))
                self.move(who, data.get("slot") or "center", data.get("x"),
                          data.get("time") or 0.0)
        elif kind == "face":
            who = data.get("who")
            if who in self._cast_index and self.on_screen(who):
                self.set_face(who, data.get("face") or self._faces[who][0])
        elif kind == "front":
            who = data.get("who")
            if who in self._cast_index and self.on_screen(who):
                self.front(who, bool(data.get("hide", True)))
        elif kind == "undim":
            who = data.get("who")
            if who in self._cast_index and self.on_screen(who):
                self.front(who, False)
        elif kind == "shake":
            who = data.get("who")
            if who in self._cast_index and self.on_screen(who):
                self.shake(who, float(data.get("strength") or 10.0))
        elif kind == "pause":
            self.pause(data.get("seconds"))
        elif kind == "wait":
            self.emit({"type": "WaitClick"})
        elif kind == "bg":
            if data.get("name"):
                self.set_background(data["name"])
        elif kind == "shot":
            pass                                   # 只进分镜备注，不进命令流
        elif kind == "raw":
            command = dict(data.get("command") or {})
            spec = BY_NAME.get(command.get("type"))
            if spec is None:
                self.warn("raw 命令的类型不存在：%r" % command.get("type"))
                return
            unknown = [key for key in command if key != "type"
                       and key not in set(spec.field_names)]
            if unknown:
                self.warn("raw 命令 %s 的多余字段已忽略：%s"
                          % (spec.name, ", ".join(sorted(unknown))))
                for key in unknown:
                    command.pop(key, None)
            self.emit(command)
        else:                                      # pragma: no cover - IR 已过滤
            self.warn("认不出的演出 %r，已跳过" % kind)


def _event_path(cue: str, kind: str) -> str:
    cue = str(cue or "")
    if cue.startswith("event:"):
        return cue
    return "event:/%s/%s" % (kind.upper(), cue.strip("/"))


# --------------------------------------------------------------------------- #
# 文档组装
# --------------------------------------------------------------------------- #

def _portraits(member: CastMember, rect) -> list[Portrait]:
    """表情名 → 文件名；``NN_`` 定宽前缀保证「目录字典序 = face 顺序」。"""
    portraits = []
    for index, face in enumerate(member.faces):
        name = re.sub(r"\s+", "", face) or "表情%d" % (index + 1)
        portraits.append(Portrait("%02d_%s.png" % (index + 1, name), tuple(rect)))
    return portraits


def build_characters(play: Screenplay, options: CompileOptions) -> list[Character]:
    characters = []
    for member in play.cast:
        characters.append(Character(
            char_id=member.id,
            name=member.name or member.id,
            camp=member.camp,
            name_color=member.name_color,
            camp_color=member.camp_color,
            express=(0.0, 300.0),
            portraits=_portraits(member, options.portrait_rect),
            head="%s.png" % member.id,
        ))
    return characters


def compile_screenplay(play: Screenplay, options: CompileOptions | None = None,
                       *, save_dir: str | Path | None = None) -> CompileResult:
    """编译成 LCDE 规范文档（``kind: project``）。"""
    options = options or CompileOptions()
    compiler = Compiler(play, options)
    compiler.run()

    characters = build_characters(play, options)
    story = Story(
        title=sanitize_title(play.title),
        backgrounds=[bg.name for bg in play.backgrounds],
        # 必须是「角色目录\Config.ch」，裸的 Config.ch 会让引擎算不出立绘目录
        characters=["%s\\Config.ch" % member.id for member in play.cast],
        commands=compiler.commands,
    )

    document = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "kind": "project",
        "generator": GENERATOR,
        "characters": [character_to_doc(character) for character in characters],
        "stories": [story_to_doc(story)],
    }
    if save_dir is not None:
        document["saveDir"] = str(save_dir)

    stats = play.scene_stats()
    stats["commands"] = len(compiler.commands)
    stats["waits"] = sum(1 for c in compiler.commands if c["type"] == "WaitClick")
    stats["duration_estimate"] = _estimate_duration(compiler.commands, play)

    return CompileResult(document=document, warnings=list(play.warnings) + compiler.warnings,
                         scene_of_command=list(compiler.scene_of_command), stats=stats)


def _estimate_duration(commands: list[dict], play: Screenplay) -> float:
    """按「每句台词 ~4.5 秒 + 显式停顿」粗估时长（分钟）。"""
    spoken = sum(1 for command in commands if command["type"] == "Say")
    pauses = sum(float(command.get("time") or 0) for command in commands
                 if command["type"] == "WaitSecond")
    return round((spoken * 4.5 + pauses) / 60.0, 1)


def screenplay_to_document(play: Screenplay, options: CompileOptions | None = None,
                           *, save_dir: str | Path | None = None) -> CompileResult:
    """:func:`compile_screenplay` 的别名（语义更直白）。"""
    return compile_screenplay(play, options, save_dir=save_dir)
