"""产出物渲染：给「人」看的那几份文件。

生成器除了写出引擎要的 JSON，还会落这几份可读文件——它们才是能拿去开会的东西：

* ``raw/剧本raw.txt`` —— 人读剧本（△动作 / 角色台词 / 【字幕】 / ［分镜重点］），
  和手写项目 ``年夜一局`` 的 raw 文件同一格式，方便对照和转交。
* ``立绘清单.md``     —— 交给美术的规格表：每个表情的出处、尺寸、裁剪框。
* ``分镜备注.md``     —— 引擎表达不了的东西（运镜、光效）+ 音频事件需求清单。
* ``生成报告.md``     —— 本次生成用了哪个模型、多少 token、哪些地方被自动修正过。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import sys

from .bridge import BACKGROUND_SIZE, HEAD_SIZE, PORTRAIT_SIZE, STAGE_SIZE
from .ir import Screenplay
from .paths import is_repo_mode

__all__ = ["render_raw_script", "render_character_sheet", "render_notes", "render_report"]

#: 报告里给出的命令必须跟**当前运行形态**一致：仓库里是 ``tools\autoLCDE.py``，
#: 独立包里是根目录的 ``autoLCDE.py``，冻结成 exe 后就是 ``autoLCDE.exe``。
#: 否则用户照抄会找不到文件。冻结版里没有独立的内核入口，所以 ``_LCDE_ENTRY`` 为空，
#: 报告会省掉「用工具构建」那一块。
if getattr(sys, "frozen", False):
    _AUTOLCDE_ENTRY = "autoLCDE.exe"
    _LCDE_ENTRY = ""
elif is_repo_mode():
    _AUTOLCDE_ENTRY = r"tools\autoLCDE.py"
    _LCDE_ENTRY = r"..\..\tools\lcde.py"
else:
    _AUTOLCDE_ENTRY = "autoLCDE.py"
    _LCDE_ENTRY = "lcde.py"


# --------------------------------------------------------------------------- #
# 一、人读剧本
# --------------------------------------------------------------------------- #

def render_raw_script(play: Screenplay) -> str:
    """渲染成人读剧本（与手写项目的 ``raw/剧本raw.txt`` 同一风格）。"""
    stats = play.scene_stats()
    lines: list[str] = [
        play.title,
        play.logline or "",
        "预计时长：约 %d 分钟" % max(1, round(stats["spoken"] * 4.5 / 60)),
        "",
        "整理说明：",
        "1. △ 为动作/画面提示（引擎里演成无名字框的旁白）。",
        "2. 【角色·字幕】为字幕型角色的台词，只出名字框、不需配音。",
        "3. ［分镜重点］为运镜/光效提示，引擎演不出来，只作后期参考。",
        "4. 方括号 ⟨⟩ 内是本工具补的演出指令，转交剧本时可以删掉。",
        "",
        "一、人物",
        "",
    ]
    for member in play.cast:
        tag = "（字幕角色，不上屏）" if member.subtitles else ""
        lines.append("* %s：%s%s%s"
                     % (member.id, member.camp or "—",
                        "　表情：" + " / ".join(member.faces), tag))
        if member.notes:
            lines.append("    %s" % member.notes)
    lines += ["", "二、场景", ""]
    for bg in play.backgrounds:
        lines.append("* %s（名称条：%s）%s" % (bg.name, bg.label or bg.name, bg.notes or ""))
    lines += ["", "三、场次总览", ""]
    for scene in play.scenes:
        lines.append("场%d：%s —— %s%s"
                     % (scene.index, scene.title, scene.summary or "",
                        "（%s）" % scene.label if scene.label else ""))
    lines += ["", "四、剧本正文", ""]

    for scene in play.scenes:
        lines += ["", "场%d：%s（%s）" % (scene.index, scene.title, scene.background), ""]
        for line in scene.lines:
            lines.append(_raw_line(line, play))
        for note in scene.notes:
            lines.append("［分镜重点］%s" % note)
        lines.append("")

    lines += ["（全剧终）", ""]
    if play.tone:
        lines += ["五、基调与表演提示", "", play.tone, ""]
    return "\n".join(lines).rstrip() + "\n"


def _raw_line(line, play: Screenplay) -> str:
    kind, data = line.kind, line.data
    if kind == "narr":
        return "△%s" % data.get("text", "")
    if kind == "say":
        face = "（%s）" % data["face"] if data.get("face") else ""
        return "%s%s：%s" % (data.get("who", ""), face, data.get("text", ""))
    if kind == "board":
        voice = play.board_voice
        return "【%s·字幕】%s" % (voice.id if voice else "旁白", data.get("text", ""))
    if kind == "sfx":
        return "⟨音效：%s⟩" % data.get("cue", "")
    if kind == "bgm":
        cue = data.get("cue")
        return "⟨音乐：%s⟩" % (cue if cue else "停")
    if kind == "enter":
        return "⟨%s 进入 · %s · %s⟩" % (data.get("who"), data.get("slot"), data.get("face"))
    if kind == "exit":
        return "⟨%s 退出⟩" % data.get("who")
    if kind == "exitAll":
        return "⟨全部退场⟩"
    if kind == "move":
        return "⟨%s 走到 %s⟩" % (data.get("who"), data.get("slot"))
    if kind == "face":
        return "⟨%s 换成 %s⟩" % (data.get("who"), data.get("face"))
    if kind == "front":
        return "⟨%s 前置%s⟩" % (data.get("who"), "（压暗他人）" if data.get("hide") else "")
    if kind == "undim":
        return "⟨%s 恢复亮度⟩" % data.get("who")
    if kind == "shake":
        return "⟨%s 震动⟩" % data.get("who")
    if kind == "pause":
        return "⟨停顿 %.1fs⟩" % float(data.get("seconds") or 0)
    if kind == "wait":
        return "⟨等待点击⟩"
    if kind == "bg":
        return "⟨切背景：%s⟩" % data.get("name")
    if kind == "shot":
        return "［分镜重点］%s" % data.get("text", "")
    if kind == "raw":
        return "⟨原生命令：%s⟩" % (data.get("command") or {}).get("type", "")
    return ""


# --------------------------------------------------------------------------- #
# 二、立绘清单（交美术）
# --------------------------------------------------------------------------- #

def render_character_sheet(play: Screenplay) -> str:
    usage = _face_usage(play)
    lines = [
        "# 《%s》立绘 / 背景 / 音频清单" % play.title,
        "",
        "本文件由 `autoLCDE` 从剧本自动生成，交给美术与音频用。",
        "**替换正式素材时保持同名同数量即可，剧本不需要改。**",
        "",
        "## 一、尺寸规格",
        "",
        "| 类别 | 源图尺寸 | 说明 |",
        "| --- | --- | --- |",
        "| 立绘 | %d × %d | 显示高度按舞台 %s 高换算；宽:高 = 1:2 |"
        % (PORTRAIT_SIZE[0], PORTRAIT_SIZE[1], STAGE_SIZE[1]),
        "| 头像 | %d × %d | 只在编辑器角色选择界面用 |" % HEAD_SIZE,
        "| 背景 | %d × %d | 舞台长宽比 %.3f:1，比例不符会留黑边或拉伸 |"
        % (BACKGROUND_SIZE[0], BACKGROUND_SIZE[1], STAGE_SIZE[0] / STAGE_SIZE[1]),
        "",
        "改尺寸的话，需要删掉角色的 `Rect` 文件、用游戏内编辑器重新适配裁剪框。",
        "",
        "## 二、角色与立绘",
        "",
    ]
    for member in play.cast:
        badge = "　（字幕角色，不上屏，但仍需要一张占位立绘）" if member.subtitles else ""
        lines += [
            "### %s（%s）%s" % (member.name, member.camp or "—", badge),
            "",
            "名字色 `%s`　阵营色 `%s`" % (member.name_color, member.camp_color),
            "",
        ]
        if member.notes:
            lines += [member.notes, ""]
        lines += [
            "| face | 文件名 | 表情 | 出现次数 | 出处（场次） |",
            "| --- | --- | --- | --- | --- |",
        ]
        for index, face in enumerate(member.faces):
            name = "%02d_%s.png" % (index + 1, face)
            count, scenes = usage.get((member.id, face), (0, []))
            where = "、".join("场%d" % s for s in sorted(scenes)) or "—（未被使用）"
            lines.append("| %d | `%s` | %s | %d | %s |" % (index, name, face, count, where))
        lines.append("")

    lines += ["## 三、背景", "", "| 文件名 | 名称条 | 画面提示 | 出现场次 |", "| --- | --- | --- | --- |"]
    for bg in play.backgrounds:
        scenes = [str(scene.index) for scene in play.scenes if scene.background == bg.name]
        lines.append("| `%s` | %s | %s | %s |"
                     % (bg.name, bg.label or bg.name, bg.notes or "—",
                        "、".join("场%s" % s for s in scenes) or "—"))
    lines += ["", "## 四、音频事件（FMOD）", "",
              "当前存档没有配置 `.bank`，所以**静默不播放**；配好事件后自动生效。", "",
              "### BGM", "", "| key | 事件路径 | 用途 |", "| --- | --- | --- |"]
    for cue in play.bgm:
        lines.append("| `%s` | `%s` | %s |" % (cue.key, cue.path, cue.usage or "—"))
    lines += ["", "### SFX", "", "| key | 事件路径 | 用途 |", "| --- | --- | --- |"]
    for cue in play.sfx:
        lines.append("| `%s` | `%s` | %s |" % (cue.key, cue.path, cue.usage or "—"))
    lines.append("")
    return "\n".join(lines)


def _face_usage(play: Screenplay) -> dict:
    """``(角色, 表情) -> (次数, {场次})``。"""
    usage: dict = defaultdict(lambda: [0, set()])
    for scene in play.scenes:
        for line in scene.lines:
            if line.kind in ("say", "enter", "face") and line.get("face"):
                entry = usage[(line.get("who"), line.get("face"))]
                entry[0] += 1
                entry[1].add(scene.index)
    return usage


# --------------------------------------------------------------------------- #
# 三、分镜备注
# --------------------------------------------------------------------------- #

def render_notes(play: Screenplay) -> str:
    shots = [(scene, line.get("text")) for scene in play.scenes
             for line in scene.lines if line.kind == "shot"]
    scene_notes = [(scene, note) for scene in play.scenes for note in scene.notes]
    lines = [
        "# 《%s》分镜与制作备注" % play.title,
        "",
        "本文件由 `autoLCDE` 自动生成，收录**引擎表达不了**的内容，供美术/音频/后期参考。",
        "LCDE 没有运镜、快切、光效叠加，这些想法只能在动画版或后期剪辑里实现；",
        "游戏内以旁白 + 背景/立绘切换近似表达。",
        "",
        "## 一、分镜重点（%d 处）" % (len(shots) + len(scene_notes)),
        "",
    ]
    if not shots and not scene_notes:
        lines += ["（本剧本没有提出运镜/光效要求）", ""]
    for scene in play.scenes:
        entries = [text for item, text in shots if item is scene]
        entries += [text for item, text in scene_notes if item is scene]
        if not entries:
            continue
        lines.append("**场%d · %s**" % (scene.index, scene.title))
        lines.append("")
        for text in entries:
            lines.append("* %s" % text)
        lines.append("")

    lines += [
        "## 二、音频需求",
        "",
        "| 类型 | 事件路径 | 用途 |",
        "| --- | --- | --- |",
    ]
    for cue in play.bgm:
        lines.append("| BGM | `%s` | %s |" % (cue.path, cue.usage or "—"))
    for cue in play.sfx:
        lines.append("| SFX | `%s` | %s |" % (cue.path, cue.usage or "—"))

    lines += [
        "",
        "## 三、改编取舍（引擎限制）",
        "",
        "1. **动作行**：引擎没有动画，所有 △ 动作被编译为无名字框的旁白。",
        "2. **字幕角色**：%s" % (
            "由 `%s` 承担，用较冷的名字色与常规角色区分。" % play.board_voice.id
            if play.board_voice else "本剧本没有字幕角色，所有字幕都当作旁白。"),
        "3. **运镜/光效**：见本文件第一节，不进游戏。",
        "4. **分支选项**：引擎不支持分支，剧本是纯线性的。",
        "",
        "## 四、演出统计",
        "",
    ]
    for name, count in _line_histogram(play).most_common():
        lines.append("* %s：%d" % (name, count))
    lines.append("")
    return "\n".join(lines)


_LINE_LABELS = {
    "narr": "旁白/动作", "say": "台词", "board": "字幕", "sfx": "音效", "bgm": "音乐切换",
    "enter": "角色进入", "exit": "角色退出", "exitAll": "全部退场", "move": "角色移动",
    "face": "换表情", "front": "前置/压暗", "undim": "恢复亮度", "shake": "震动",
    "pause": "停顿", "wait": "等待点击", "bg": "场内换背景", "shot": "分镜备注", "raw": "原生命令",
}


def _line_histogram(play: Screenplay) -> Counter:
    return Counter(line.kind for scene in play.scenes for line in scene.lines)


# --------------------------------------------------------------------------- #
# 四、生成报告
# --------------------------------------------------------------------------- #

def render_report(*, play: Screenplay, result, settings, options, artifacts: dict,
                  issues, elapsed: float, stages: list, repairs: list,
                  usage_text: str = "", json_name: str = "") -> str:
    """本次运行的体检报告：模型、用量、被自动修正的地方、校验结果、产出物清单。"""
    stats = result.stats
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    infos = [issue for issue in issues if issue.severity == "info"]

    lines = [
        "# 《%s》生成报告" % play.title,
        "",
        "> 由 `autoLCDE` 自动生成。本文件记录**这次生成是怎么来的**，"
        "包括模型、用量、被自动修正过的内容与引擎校验结果。",
        "",
        "## 一、本次运行",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 模型 | `%s` |" % (settings.model or "—"),
        "| 提供方 | %s |" % settings.provider,
        "| 接口 | `%s` |" % (settings.base_url or "—"),
        "| 密钥 | %s（%s） |" % (settings.masked_key() or "—", settings.key_source or "—"),
        "| 主题 | %s |" % (options.premise or "—").replace("|", "\\|").replace("\n", " "),
        "| 风格 | %s |" % (options.style or options.tone or "—"),
        "| 场次 / 角色 / 背景 | %d / %d / %d |"
        % (stats["scenes"], stats["cast"], stats["backgrounds"]),
        "| 命令条数 | %d 条（其中台词/旁白/字幕 %d 句） |"
        % (stats["commands"], stats["spoken"]),
        "| 粗估时长 | 约 %s 分钟 |" % stats["duration_estimate"],
        "| 耗时 | %.1f 秒 |" % elapsed,
        "| token 用量 | %s |" % (usage_text or "—"),
        "",
        "## 二、阶段记录",
        "",
        "| 阶段 | 说明 |",
        "| --- | --- |",
    ]
    for stage in stages:
        lines.append("| %s | %s |" % (stage.get("name", ""), stage.get("detail", "")))

    lines += ["", "## 三、自动修正过的内容（%d 处）" % len(result.warnings), ""]
    if result.warnings:
        lines.append("模型输出与引擎要求不一致的地方，工具已经就地修好。"
                     "这些**不影响运行**，但能看出模型的偏好，改提示词时可以参考：")
        lines.append("")
        for message in result.warnings:
            lines.append("* %s" % message)
    else:
        lines.append("模型输出与引擎要求完全一致，没有需要修正的地方。")
    lines.append("")

    lines += ["## 四、%s 校验结果" % "lcde",
              "",
              "| 级别 | 数量 |", "| --- | --- |",
              "| 错误 | %d |" % len(errors),
              "| 警告 | %d |" % len(warnings),
              "| 提示 | %d |" % len(infos),
              ""]
    if repairs:
        lines += ["回喂重写：共 %d 轮（把校验器的原话交回模型重写问题场次）。" % len(repairs), ""]
        for item in repairs:
            lines.append("* 第 %d 轮：重写场次 %s" % (item["round"], item["scenes"]))
        lines.append("")
    if errors:
        lines += ["**仍有错误（需要人工处理）**：", ""]
        for issue in errors:
            lines.append("* `%s` %s" % (issue.code, issue))
        lines.append("")
    if warnings:
        lines += ["警告明细：", ""]
        for issue in warnings:
            lines.append("* `%s` %s" % (issue.code, issue))
        lines.append("")

    lines += ["## 五、产出物", "", "| 文件 | 说明 |", "| --- | --- |"]
    for name, description in artifacts.items():
        lines.append("| `%s` | %s |" % (name, description))

    lines += [
        "",
        "## 六、怎么装进游戏",
        "",
        "存档目录是 `%USERPROFILE%\\AppData\\LocalLow\\Soalin\\LCDE`，"
        "把本目录 `save/` 下的 `Character/`、`BG/`、`Dialog/` 合并进去即可：",
        "",
        "```powershell",
        "$src = \"%s\"" % (Path(options.out_dir) / "save"),
        "$dst = \"$env:USERPROFILE\\AppData\\LocalLow\\Soalin\\LCDE\"",
        "robocopy $src $dst /E",
        "```",
        "",
    ]
    if _LCDE_ENTRY:
        lines += [
            "或者用工具构建（会自动备份被覆盖的文件）：",
            "",
            "```powershell",
            "python %s --save-dir \"$env:USERPROFILE\\AppData\\LocalLow\\Soalin\\LCDE\" `"
            % _LCDE_ENTRY,
            "       project build %s --force" % (json_name or "剧本.json"),
            "```",
            "",
        ]
    lines += [
        "## 七、下一步",
        "",
        "1. 读 `raw/剧本raw.txt` 改台词与节奏；改完重跑编译 **不需要再调 API**：",
        "   ```powershell",
        "   python %s compile \"%s\"" % (_AUTOLCDE_ENTRY, Path(options.out_dir) / "剧本.json"),
        "   ```",
        "2. 结构要大改（加场、换角色）就重跑生成，或者手工改 `%s` 里的 `scenes`。"
        % (json_name or "剧本.json"),
        "3. 替换正式美术：保持文件名与数量不变直接覆盖图片即可。",
        "",
    ]
    return "\n".join(lines)
