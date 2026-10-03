"""提示词工程：把「引擎能表达什么」翻译成模型能照做的简报。

三个阶段各有一套提示词：

* ``concept`` —— 选角、背景表、音频表、场次表（大纲）。这一步决定全片骨架。
* ``scene``   —— 逐场写演出条目。模型只负责「编排」，引用必须落在概念表里。
* ``repair``  —— 校验失败时，把 ``lcde validate`` 的原话回喂，要求只重写出问题的那几场。

三套提示词都要求**纯 JSON**，并且把 JSON 结构写成显式契约（而不是让模型猜）。
引擎能力的描述刻意写成「能做到 / 做不到」两栏——模型最常犯的错是写运镜和动画，
而那两样 LCDE 完全没有。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ir import Screenplay

__all__ = ["Brief", "concept_messages", "scene_messages", "repair_messages",
           "render_brief", "concept_summary"]


# --------------------------------------------------------------------------- #
# 用户输入
# --------------------------------------------------------------------------- #

@dataclass
class Brief:
    """用户给的一句话需求。``premise`` 是唯一必填项。"""

    premise: str = ""
    title: str = ""
    style: str = ""
    tone: str = ""
    scenes: int = 8
    cast: int = 4
    duration: int = 12
    language: str = "简体中文"
    audience: str = ""
    constraints: str = ""
    lines_per_scene: str = ""

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value not in ("", None)}


def render_brief(brief: Brief) -> str:
    lines = ["【创作需求】", "主题/梗概：%s" % brief.premise.strip()]
    if brief.title:
        lines.append("期望标题：%s（可以改得更好）" % brief.title)
    if brief.style:
        lines.append("风格/致敬对象：%s" % brief.style)
    if brief.tone:
        lines.append("情绪基调：%s" % brief.tone)
    lines.append("篇幅：%d 场，约 %d 分钟" % (brief.scenes, brief.duration))
    lines.append("主要角色数：%d 个左右（可含 1 个只出字幕的角色）" % brief.cast)
    lines.append("语言：%s" % brief.language)
    if brief.audience:
        lines.append("受众：%s" % brief.audience)
    if brief.lines_per_scene:
        lines.append("每场篇幅：%s" % brief.lines_per_scene)
    else:
        per = max(18, int(brief.duration * 60 / max(1, brief.scenes) / 4.5))
        lines.append("每场篇幅：约 %d 条演出条目（够 %d 分钟全场铺开）" % (per + 10, brief.duration))
    if brief.constraints:
        lines.append("硬性约束：%s" % brief.constraints)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 引擎能力（三个阶段共用）
# --------------------------------------------------------------------------- #

ENGINE_SECTION = """\
【目标引擎：LCDE / Soalin】
你写的不是小说、不是分镜脚本，而是**一台没有动画的 Unity 对话引擎**能演出的东西。

能做到的（全部手段，没有别的）：
· 背景：切换背景、背景淡入/淡出（过色转场）、背景名称条、背景平移/缩放/震动
· 立绘：进入（淡入）、退出（淡出）、横向移动、换表情、震动、前置并压暗其他角色
· 文字：带名字框的台词；不带名字框的旁白；一个只出名字框的「字幕型角色」（如广播/系统提示）
· 音频：BGM 切换/停止、按 FMOD 事件路径播放音效
· 节奏：等待点击、等待固定秒数

做不到的（**绝对不要写进演出条目**）：
· 运镜、镜头推拉摇移、快切、蒙太奇、慢动作、分屏
· 逐帧动画、特效、粒子、光效叠加、调色、字幕动画
· 分支选项、变量、条件、存档、成就、数值
· 语音、口型
· 同时 >2 个角色同屏说话时的精细调度（舞台上可以有多个立绘，但没有层级动画）

同屏立绘用槽位描述：left / center / right（最多三个位置）。
连续两句台词之间**不要**写重复的进入；角色在台上就直接说话。
运镜、光效这类「引擎演不出来但导演想知道」的想法，写进场景的 notes 里，不要写进 lines。
"""


# --------------------------------------------------------------------------- #
# 一、概念
# --------------------------------------------------------------------------- #

_CONCEPT_CONTRACT = """\
【输出格式】只输出一个 JSON 对象，不要任何解释文字、不要 markdown 围栏。

{
  "title": "剧本名（会成为文件名，不要含 \\\\ / : * ? \" < > |）",
  "logline": "一句话梗概",
  "tone": "情绪基调与表演提示，一两句",
  "cast": [
    {
      "id": "角色目录名（中文或英文，不含路径分隔符，剧本里靠它引用）",
      "name": "对话框里显示的名字",
      "camp": "阵营/头衔，显示在名字下方的小字",
      "nameColor": "6 位十六进制 RGB，不带 #（自己挑，要能区分角色）",
      "campColor": "6 位十六进制 RGB，不带 #，比 nameColor 暗一些",
      "faces": ["表情名1", "表情名2", "表情名3"],
      "subtitles": false,
      "notes": "一句话人物设计提示（给美术看）"
    }
  ],
  "backgrounds": [
    { "name": "背景文件名.png", "label": "背景名称条上显示的短标题", "notes": "画面提示" }
  ],
  "audio": {
    "bgm": [ { "key": "英文短键", "path": "event:/BGM/xxx", "usage": "用在哪" } ],
    "sfx": [ { "key": "英文短键", "path": "event:/SFX/xxx", "usage": "用在哪" } ]
  },
  "scenes": [
    {
      "index": 1,
      "title": "场次小标题",
      "background": "必须是 backgrounds 里的 name，一字不差",
      "label": "背景名称条文字",
      "bgm": "audio.bgm 里的某个 key；本场不换音乐就写 null",
      "summary": "这一场发生了什么（2~3 句）",
      "beats": ["节拍1", "节拍2", "节拍3"],
      "notes": ["给美术/音频/后期的提示，例如运镜、光效、需要制作的事件"]
    }
  ]
}
"""


def concept_messages(brief: Brief) -> list[dict]:
    system = (
        "你是一位擅长短篇叙事的中文编剧，同时非常懂「低成本演出」——"
        "知道在没有任何动画的引擎里，怎么靠台词、停顿、立绘走位和背景切换把戏做出来。\n"
        "你的输出会被程序直接解析成 JSON，所以你必须**只输出 JSON**。\n\n"
        + ENGINE_SECTION
    )
    user = "\n".join([
        render_brief(brief),
        "",
        "【本次任务】先做**概念设计**，不要写任何具体台词。",
        "要点：",
        "1. 角色数量控制在需求左右；给每个角色 3~6 个**互不重复**的表情名，"
        "名字要能直接当文件名（2~4 个汉字或英文单词）。",
        "2. 至少留一个角色 `subtitles: true`（广播、系统、旁白装置一类），"
        "用来承接引擎播报式的信息；它不出现在舞台上，只出名字框。",
        "3. 背景数量 2~6 张，宁少勿多：引擎没有运镜，同一场景换背景反而会打断沉浸。"
        "背景名要短、能一眼分辨（如「客栈房·夜.png」）。",
        "4. 音频事件：BGM 2~4 条，SFX 4~10 条；只用 FMOD 事件路径（event:/BGM/... 、event:/SFX/...）。",
        "5. 场次表要形成清晰的起承转合：每场都必须有**一个变化**"
        "（关系变了、信息露了、抉择做了），不要写「两人聊天」这种没有推进的场。",
        "6. 每一场都要明确写在哪个背景、用什么 BGM。",
        "",
        _CONCEPT_CONTRACT,
        "",
        "再次强调：只输出那个 JSON 对象本身。",
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# 二、分场
# --------------------------------------------------------------------------- #

_LINE_CONTRACT = """\
【演出条目（lines）的写法】每一项是一个对象，`kind` 决定字段：

  {"kind": "narr",  "text": "旁白/动作描述"}                  ← 无名字框
  {"kind": "say",   "who": "角色id", "text": "台词", "face": "表情名"}
  {"kind": "board", "text": "字幕文字"}                        ← 由 subtitles 角色念出
  {"kind": "sfx",   "cue": "audio.sfx 里的 key"}
  {"kind": "bgm",   "cue": "audio.bgm 里的 key"}               ← "cue": null 表示停止音乐
  {"kind": "enter", "who": "角色id", "face": "表情名", "slot": "left|center|right"}
  {"kind": "exit",  "who": "角色id"}
  {"kind": "exitAll"}
  {"kind": "move",  "who": "角色id", "slot": "left|center|right"}
  {"kind": "face",  "who": "角色id", "face": "表情名"}
  {"kind": "front", "who": "角色id", "hide": true}             ← 前置该角色并压暗其他人
  {"kind": "undim", "who": "角色id"}                           ← 恢复亮度（front 之后必须还原）
  {"kind": "shake", "who": "角色id", "strength": 8}
  {"kind": "pause", "seconds": 0.6}
  {"kind": "wait"}                                             ← 等玩家点击
  {"kind": "bg",    "name": "背景文件名.png"}                   ← 场内换背景（慎用）
  {"kind": "shot",  "text": "运镜/光效提示"}                    ← 不会进游戏，只进分镜备注

硬性规则：
· `who` 必须是角色表里的 id（一字不差）；`face` 必须是该角色 faces 里的表情名。
· `cue`、`background` 必须是下面给出的表里的名字。表里没有的**不要用**。
· 角色第一次开口前，必须先 `enter`；说完最后一句再 `exit`（或让下一场自动清台）。
· 台词一句一条，句子要短。同一角色连续说话超过 3 句时，中间插 `pause` 或别人的一句话。
· 每 6~12 条条目里安排一次 `wait`（等玩家点击）；纯氛围段可以用 `pause` 连缀。
· 旁白（narr）用来写**看得见的具体动作**，不要写心理活动、不要写「他想起了」。
· 需要音效的地方就写 `sfx`；需要静下来就 `{"kind": "bgm", "cue": null}` 再起新曲。
"""


def concept_summary(play_or_concept) -> str:
    """把概念表压成一段紧凑的「定稿设定」，作为分场阶段的唯一事实来源。"""
    if isinstance(play_or_concept, Screenplay):
        concept = play_or_concept.concept or {}
        cast = [member.to_dict() for member in play_or_concept.cast]
        backgrounds = [bg.to_dict() for bg in play_or_concept.backgrounds]
        audio = {
            "bgm": [cue.to_dict() for cue in play_or_concept.bgm],
            "sfx": [cue.to_dict() for cue in play_or_concept.sfx],
        }
    else:
        concept = play_or_concept or {}
        cast = concept.get("cast") or []
        backgrounds = concept.get("backgrounds") or []
        audio = concept.get("audio") or {}
    fixed = {
        "title": concept.get("title"),
        "logline": concept.get("logline"),
        "tone": concept.get("tone"),
        "cast": cast,
        "backgrounds": backgrounds,
        "audio": audio,
    }
    return json.dumps(fixed, ensure_ascii=False, indent=2)


def scene_messages(brief: Brief, concept_text: str, scenes, *, total: int,
                   start: int, previous_tail: str = "") -> list[dict]:
    """一场或多场（``scenes`` 是场次大纲列表）的写作提示词。

    ``len(scenes) == 1`` 时要求输出 ``{"scene": {...}}``；多场时要求输出
    ``{"scenes": [...]}``——一次调用连写几场，接戏更紧、调用次数更少。
    """
    system = (
        "你是中文编剧，负责把已经定稿的设定写成可演出的场次。\n"
        "你只能使用给定表里的角色、表情、背景、音频；不得新增，不得改名。\n"
        "你只输出 JSON。\n\n" + ENGINE_SECTION
    )

    outlines = []
    for offset, scene in enumerate(scenes):
        item = dict(scene)
        item["场景序号"] = start + offset + 1          # 供程序与模型双方对齐序号
        outlines.append(item)
    outline_text = json.dumps(outlines if len(outlines) > 1 else outlines[0],
                              ensure_ascii=False, indent=2)

    if len(outlines) == 1:
        task = "【本场任务】第 %d / %d 场" % (start + 1, total)
        shell = "\n".join([
            "{",
            '  "scene": {',
            '    "index": %d,' % (start + 1),
            '    "title": "场次小标题（照抄大纲）",',
            '    "background": "背景文件名（照抄大纲）",',
            '    "label": "背景名称条文字",',
            '    "bgm": "audio.bgm 里的 key，或 null",',
            '    "summary": "一句话复述本场事件（与原大纲一致）",',
            '    "notes": ["运镜/光效/音频提示，每条一句"],',
            '    "lines": [ ... 演出条目，按演出顺序 ... ]',
            "  }",
            "}",
        ])
    else:
        task = ("【本场任务】第 %d–%d / %d 场（连写，注意场与场之间的接戏）"
                % (start + 1, start + len(outlines), total))
        blocks = []
        for offset in range(len(outlines)):
            blocks.append("\n".join([
                "    {",
                '      "index": %d,' % (start + offset + 1),
                '      "title": "场次小标题（照抄大纲）",',
                '      "background": "背景文件名（照抄大纲）",',
                '      "label": "背景名称条文字",',
                '      "bgm": "audio.bgm 里的 key，或 null",',
                '      "summary": "一句话复述本场事件",',
                '      "notes": ["运镜/光效/音频提示"],',
                '      "lines": [ ... 演出条目 ... ]',
                "    }",
            ]))
        shell = "{\n  \"scenes\": [\n" + ",\n".join(blocks) + "\n  ]\n}"

    user = "\n".join([
        "【定稿设定】（唯一事实来源，引用必须与之一字不差）",
        concept_text,
        "",
        task,
        outline_text,
        "",
        ("【已写好的前文结尾（接戏用）】\n%s" % previous_tail) if previous_tail else "",
        "",
        render_brief(brief) if start == 0 else "",
        _LINE_CONTRACT,
        "",
        "【输出格式】只输出一个 JSON 对象：",
        shell,
        "",
        "写作要求：",
        "1. 台词要有**潜台词**：人物说出口的话和真正想说的不一样，是这类短剧最好看的地方。",
        "2. 少用形容词，多用具体动作与物件；情绪靠节奏（pause/wait）和走位变化推进。",
        "3. 每一场都必须有推进：进来时和结束时，人物关系或信息状态要变。",
        "4. 不要复述设定、不要写角色尚未知道的信息。",
        "5. 不要出现现实世界的品牌、真人姓名、网络梗。",
        "6. 每条台词尽量 30 字以内；长信息拆成几句。",
        "7. 每场开头重新交代舞台：需要谁在场就先 enter，不要依赖上一场留下的立绘。",
        "",
        "只输出那个 JSON 对象。",
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# 三、修复
# --------------------------------------------------------------------------- #

def repair_messages(concept_text: str, scene: dict, issues: list[str], *,
                    position: int, total: int, command_hint: str = "") -> list[dict]:
    """把 ``lcde validate`` 的原文回喂，要求只重写出问题的那一场。"""
    system = (
        "你是中文编剧兼演出工程师。上一版场次在**引擎校验**里报错了，"
        "现在只重写指定的那一场，其余内容一律不要改。\n"
        "校验器模拟了引擎的运行时状态（屏幕上现在有哪些立绘、下标是否越界），"
        "它的报错是权威的。\n"
        "你只输出 JSON。\n\n" + ENGINE_SECTION
    )
    user = "\n".join([
        "【定稿设定】",
        concept_text,
        "",
        "【报错的场次原文】（第 %d / %d 场）" % (position + 1, total),
        json.dumps(scene, ensure_ascii=False, indent=2),
        "",
        "【引擎校验器的报错原文】",
        "\n".join("- %s" % issue for issue in issues),
        command_hint,
        "",
        _LINE_CONTRACT,
        "",
        "【要求】",
        "1. 只修报错指出的问题，保留原有的台词、节奏与情绪——不要把好句子改没。",
        "2. 最常见的两种错因，优先检查：",
        "   · 引用了不存在的角色 id / 表情名 / 背景名 / 音频 key；",
        "   · 立绘顺序问题：对还没 `enter` 的角色做了 `move`/`face`/`exit`，"
        "或对已经 `exit` 的角色又操作了一次。",
        "3. 输出格式与上一版完全一致（同一个 JSON 外壳，含 scene 包裹层）。",
        "",
        "只输出那个 JSON 对象。",
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
