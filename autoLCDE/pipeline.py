"""编排：一条命令跑完「生成 → 编译 → 占位素材 → 游戏文件 → 校验 → 回喂重写」。

流程与失败处理：

    ①概念   concept   一次调用，产出选角/背景/音频/场次表
    ②分场   scene     每 N 场一次调用，带上一场结尾做接戏
    ③编译             IR → LCDE 规范 JSON（立绘下标由状态机现算，永远合法）
    ④素材             placeholder 生成占位图 + 写 Config.ch / Rect / Dialog
    ⑤校验             lcde.validate（只校验本次生成的实体，不打扰别的剧本）
    ⑥回喂   repair    有硬错误 → 把校验器原话交回模型，只重写报错的那几场，再回到 ③

第 ⑤ 步的报错会被翻译成「第几场」：校验器的 ``commands[i]`` 下标经编译期记录的
``scene_of_command`` 映射回场次，因此修复提示词里带上的是**完整的那一场原文**，
模型不用猜自己错在哪。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .art import render_character_sheet, render_notes, render_raw_script, render_report
from .bridge import (
    Character,
    Project,
    Story,
    dumps,
    generate_for_document,
    has_errors,
    project_from_doc,
    validate_character,
    validate_story,
)
from .compile import CompileOptions, CompileResult, compile_screenplay
from .errors import ScriptError
from .ir import Screenplay, build_screenplay, sanitize_title
from .llm import extract_json, make_client
from .prompts import Brief, concept_messages, concept_summary, repair_messages, scene_messages

__all__ = ["RunOptions", "RunResult", "Pipeline", "compile_existing"]


# --------------------------------------------------------------------------- #
# 参数与结果
# --------------------------------------------------------------------------- #

@dataclass
class RunOptions:
    out_dir: Path
    premise: str
    title: str = ""
    style: str = ""
    tone: str = ""
    language: str = "简体中文"
    audience: str = ""
    constraints: str = ""
    scenes: int = 8
    cast: int = 4
    duration: int = 12
    #: 每次 API 调用连续写几场（1 最稳，2~3 更连贯更省调用次数）
    scenes_per_call: int = 2
    #: 是否生成占位素材并写出游戏文件、跑校验
    build_files: bool = True
    placeholder: bool = True
    #: 校验失败时最多回喂重写几轮
    repair_rounds: int = 2
    #: 除错误外，把这些警告码也当作需要修复（例如 S20 音频路径不像 FMOD 事件）
    repair_warning_codes: tuple = ()
    save_dir: Path | None = None
    json_name: str = ""
    #: 复用已有的概念表（``plan`` 的产物），跳过概念阶段的 API 调用
    concept_path: Path | None = None
    stage_targets: dict = field(default_factory=lambda: {"left": 220.0, "center": 400.0,
                                                         "right": 580.0})
    force_placeholder: bool = False
    #: 调试用：让 mock 故意产出非法命令，验证「校验→回喂」闭环
    mock_flaws: int = 0
    #: 只算不写（仍然会调用 API）
    dry_run: bool = False
    verbose: bool = False

    @property
    def brief(self) -> Brief:
        return Brief(premise=self.premise, title=self.title, style=self.style, tone=self.tone,
                     scenes=self.scenes, cast=self.cast, duration=self.duration,
                     language=self.language, audience=self.audience,
                     constraints=self.constraints)


@dataclass
class RunResult:
    ok: bool
    out_dir: Path
    play: Screenplay | None = None
    compile_result: CompileResult | None = None
    issues: list = field(default_factory=list)
    repairs: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    elapsed: float = 0.0
    usage_text: str = ""
    save_dir: Path | None = None
    error: str = ""

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.severity == "warning"]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

class Pipeline:
    """一次生成的完整过程。``log``/``event`` 是给 CLI 与网页界面用的回调。"""

    def __init__(self, settings, options: RunOptions, *, log=None, event=None) -> None:
        self.settings = settings
        self.options = options
        self._log = log or (lambda level, message: None)
        self._event = event or (lambda name, payload: None)
        self.cancelled = False
        self.client = None
        self._seen_warnings: set = set()
        self._started = time.time()

    # -- 回调 -------------------------------------------------------------- #
    def log(self, level: str, message: str) -> None:
        self._log(level, message)

    def stage(self, name: str, detail: str, result: RunResult) -> None:
        result.stages.append({"name": name, "detail": detail})
        self.log("stage", "%s —— %s" % (name, detail))
        self._event("stage", {"name": name, "detail": detail})

    def _check_cancel(self) -> None:
        if self.cancelled:
            raise ScriptError("已取消")

    # -- 入口 -------------------------------------------------------------- #
    def run(self) -> RunResult:
        started = time.time()
        self._started = started
        options = self.options
        result = RunResult(ok=False, out_dir=Path(options.out_dir))

        try:
            if self.client is None:                # 允许外部注入（测试/自定义客户端）
                self.client = make_client(
                    self.settings, log=self.log,
                    **({"flaws": options.mock_flaws} if self.settings.is_mock else {}))
            brief = options.brief
            self.log("info", "主题：%s" % brief.premise)
            self.log("info", "模型：%s（%s）" % (self.settings.model, self.settings.provider))

            # ① 概念
            self._check_cancel()
            concept = self._concept(brief, result)

            # ② 分场
            self._check_cancel()
            payloads = self._scenes(concept, brief, result)

            # ③ 组装 + 编译 + 落盘
            play = self._assemble(concept, payloads)
            result.play = play
            self.log("info", "剧本《%s》：%d 场 / %d 角色 / %d 背景"
                     % (play.title, len(play.scenes), len(play.cast), len(play.backgrounds)))

            self._compile_and_write(play, result)

            # ④⑤⑥ 素材 / 游戏文件 / 校验 / 回喂
            if options.build_files:
                self._build_and_validate(result)
                self._repair_loop(result)
                self._write_report(result)

            result.ok = not has_errors(result.issues) or not options.build_files
        except ScriptError as exc:
            result.error = str(exc)
            self.log("error", str(exc))
        except Exception as exc:                              # noqa: BLE001
            if options.verbose:
                raise
            result.error = "%s: %s" % (type(exc).__name__, exc)
            self.log("error", result.error)
        finally:
            result.elapsed = time.time() - started
            if self.client is not None:
                result.usage_text = self.client.usage.summary()
                self._event("usage", {"text": result.usage_text})
            self._event("done", {"ok": result.ok, "error": result.error,
                                 "elapsed": result.elapsed})
        return result

    # -- ① 概念 ------------------------------------------------------------ #
    def _concept(self, brief: Brief, result: RunResult) -> dict:
        if self.options.concept_path:
            return self._concept_from_file(result)

        self.stage("概念设计", "选角 / 背景 / 音频 / 场次表", result)
        text = self.client.chat(concept_messages(brief), stage="concept",
                                meta={"scenes": brief.scenes})
        concept = extract_json(text, expect="object")
        if not isinstance(concept, dict):
            raise ScriptError("概念阶段的输出不是 JSON 对象")
        title = concept.get("title") or brief.title or brief.premise[:12]
        scenes = concept.get("scenes") or []
        self.log("info", "《%s》：%d 个角色，%d 张背景，%d 场"
                 % (title, len(concept.get("cast") or []),
                    len(concept.get("backgrounds") or []), len(scenes)))

        if self.options.dry_run:
            path = Path(self.options.out_dir) / "概念.json"
            self.log("info", "[dry-run] 概念已生成，不落盘（本应写到 %s）" % path)
        else:
            out = Path(self.options.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "概念.json").write_text(
                json.dumps(concept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return concept

    # -- ② 分场 ------------------------------------------------------------ #
    def _concept_from_file(self, result: RunResult) -> dict:
        """复用 ``autoLCDE plan`` 产出的概念表：审过的骨架不该再让模型重掷一次。"""
        path = Path(self.options.concept_path)
        if not path.is_file():
            raise ScriptError("找不到概念文件：%s" % path)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ScriptError("概念文件不是合法 JSON：%s" % exc) from exc
        if not isinstance(data, dict) or not data.get("scenes"):
            raise ScriptError("概念文件里没有 scenes：%s" % path)
        self.stage("概念设计", "复用 %s（跳过 API 调用）" % path.name, result)
        self.log("info", "复用《%s》：%d 个角色，%d 张背景，%d 场"
                 % (data.get("title", "?"), len(data.get("cast") or []),
                    len(data.get("backgrounds") or []), len(data.get("scenes") or [])))
        if not self.options.dry_run:
            out = Path(self.options.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "概念.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return data

    def _scenes(self, concept: dict, brief: Brief, result: RunResult) -> list[dict]:
        outlines = [item for item in (concept.get("scenes") or []) if isinstance(item, dict)]
        if not outlines:
            raise ScriptError("概念阶段没有给出场次表（scenes 为空）")

        concept_text = concept_summary(concept)
        per_call = max(1, int(self.options.scenes_per_call))
        payloads: list[dict] = []
        tail = ""

        total = len(outlines)
        for start in range(0, total, per_call):
            self._check_cancel()
            chunk = outlines[start:start + per_call]
            indexes = list(range(start, start + len(chunk)))
            self.stage("分场写作", "第 %d–%d 场 / 共 %d 场"
                       % (start + 1, start + len(chunk), total), result)
            text = self.client.chat(
                scene_messages(brief, concept_text, chunk, total=total, start=start,
                               previous_tail=tail),
                stage="scene",
                meta={"scene_indices": indexes, "scene_total": total},
            )
            data = extract_json(text, expect="any")
            scenes = _flatten_scenes(data)
            if not scenes:
                raise ScriptError("第 %d 场：模型没有返回可用的场景对象" % (start + 1))
            for scene in scenes:
                payloads.append(scene)
                self.log("info", "  场%d 完成：%d 条演出"
                         % (len(payloads), len(scene.get("lines") or [])))
            tail = _tail_of(scenes[-1])
        return payloads

    # -- ③ 组装 + 编译 ------------------------------------------------------ #
    def _assemble(self, concept: dict, payloads: list[dict]) -> Screenplay:
        fallback = self.options.title or self.options.premise[:12] or "未命名剧本"

        def warn(message: str) -> None:
            if message in self._seen_warnings:      # 回喂重写会重新归一化全场，去重
                return
            self._seen_warnings.add(message)
            self.log("warn", "自动修正：%s" % message)

        return build_screenplay(concept, payloads, fallback_title=fallback, warn=warn)

    def _compile_options(self) -> CompileOptions:
        return CompileOptions(stage_targets=dict(self.options.stage_targets))

    def _compile_and_write(self, play: Screenplay, result: RunResult) -> None:
        self.stage("编译", "IR → LCDE 规范 JSON", result)
        out = Path(self.options.out_dir)
        save_dir = Path(self.options.save_dir) if self.options.save_dir else out / "save"
        result.save_dir = save_dir

        compiled = compile_screenplay(play, self._compile_options(), save_dir=save_dir)
        result.compile_result = compiled

        title = compiled.document["stories"][0]["title"]
        json_name = self.options.json_name or ("%s.json" % title)

        artifacts = {
            json_name: "LCDE 规范文档（project：角色 + 剧本，可被 lcde 直接构建）",
            "剧本.json": "剧本中间表示（IR），可手工编辑后用 autoLCDE compile 重新编译",
            "raw/剧本raw.txt": "人读剧本（△动作 / 台词 / 字幕 / 分镜重点）",
            "立绘清单.md": "交给美术的立绘、背景、头像规格表",
            "分镜备注.md": "引擎表达不了的运镜与音频需求清单",
            "生成报告.md": "本次生成的模型、用量、自动修正与校验结果",
        }
        result.artifacts = artifacts

        if self.options.dry_run:
            self.log("info", "[dry-run] 将写出：%s 等 %d 个文件"
                     % (json_name, len(artifacts) + 1))
            return

        out.mkdir(parents=True, exist_ok=True)
        (out / "raw").mkdir(parents=True, exist_ok=True)
        (out / json_name).write_text(dumps(compiled.document), encoding="utf-8")
        (out / "剧本.json").write_text(
            json.dumps(play.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "raw" / "剧本raw.txt").write_text(render_raw_script(play), encoding="utf-8")
        (out / "立绘清单.md").write_text(render_character_sheet(play), encoding="utf-8")
        (out / "分镜备注.md").write_text(render_notes(play), encoding="utf-8")
        self.log("info", "已写出 %s / 剧本.json / 立绘清单.md / 分镜备注.md" % json_name)

    # -- ④⑤ 素材 + 游戏文件 + 校验 ------------------------------------------ #
    def _write_game_files(self, document: dict, save_dir: Path) -> list[Path]:
        return write_game_files(document, save_dir, out_dir=Path(self.options.out_dir),
                                dry_run=self.options.dry_run, log=self.log)

    def _validate_entities(self, document: dict, save_dir: Path) -> list:
        return validate_entities(document, save_dir)

    def _build_and_validate(self, result: RunResult) -> None:
        options = self.options
        document = result.compile_result.document
        save_dir = result.save_dir
        if options.dry_run:
            self.log("info", "[dry-run] 跳过占位素材、游戏文件与校验")
            return

        if options.placeholder:
            self.stage("占位素材", "生成立绘 / 头像 / 背景占位图", result)
            written, skipped = generate_for_document(
                document, save_dir, force=options.force_placeholder)
            self.log("info", "占位图：新写 %d 张，跳过已存在 %d 张" % (len(written), len(skipped)))

        self.stage("写游戏文件", "Character/ + BG/ + Dialog/", result)
        written = self._write_game_files(document, save_dir)
        self.log("info", "已写出 %d 个游戏文件（目录：%s）" % (len(written), save_dir))

        self.stage("引擎校验", "lcde validate：模拟运行时立绘状态", result)
        result.issues = self._validate_entities(document, save_dir)
        errors = [issue for issue in result.issues if issue.severity == "error"]
        warnings = [issue for issue in result.issues if issue.severity == "warning"]
        self.log("info" if not errors else "warn",
                 "校验：%d 错误 / %d 警告" % (len(errors), len(warnings)))
        for issue in result.issues:
            self.log("warn" if issue.severity != "info" else "debug", "  %s" % issue)
        self._event("issues", {"errors": len(errors), "warnings": len(warnings)})

    # -- ⑥ 回喂重写 --------------------------------------------------------- #
    def _repairable_issues(self, result: RunResult) -> list:
        wanted = set(self.options.repair_warning_codes or ())
        return [issue for issue in result.issues
                if issue.severity == "error"
                or (issue.severity == "warning" and issue.code in wanted)]

    def _scenes_of_issues(self, result: RunResult, issues: list) -> list[int]:
        mapping = result.compile_result.scene_of_command
        indexes: set[int] = set()
        for issue in issues:
            for raw in _location_indexes(issue.location):
                if 0 <= raw < len(mapping):
                    indexes.add(mapping[raw])
        return sorted(indexes)

    def _repair_loop(self, result: RunResult) -> None:
        rounds = max(0, int(self.options.repair_rounds))
        for round_index in range(1, rounds + 1):
            issues = self._repairable_issues(result)
            if not issues:
                return
            scenes = self._scenes_of_issues(result, issues)
            if not scenes:
                self.log("warn", "校验报错不在任何场次的命令上（可能是角色/文件层面的问题），"
                                 "跳过回喂重写")
                return
            self._check_cancel()
            self.stage("回喂重写", "第 %d 轮：重写场次 %s"
                       % (round_index, "、".join(str(s + 1) for s in scenes)), result)
            concept_text = concept_summary(result.play)
            payloads = [scene.to_dict() for scene in result.play.scenes]
            repaired: list[int] = []
            for scene_index in scenes:
                scene_issues = [str(issue) for issue in issues]
                text = self.client.chat(
                    repair_messages(concept_text, payloads[scene_index], scene_issues,
                                    position=scene_index, total=len(payloads)),
                    stage="repair",
                    meta={"scene_indices": [scene_index], "scene_total": len(payloads)},
                )
                data = extract_json(text, expect="any")
                fresh = _flatten_scenes(data)
                if not fresh:
                    self.log("warn", "第 %d 场重写没有返回内容，保留原样" % (scene_index + 1))
                    continue
                payloads[scene_index] = fresh[0]
                repaired.append(scene_index + 1)
            if not repaired:
                return
            result.repairs.append({"round": round_index,
                                   "scenes": "、".join(str(s) for s in repaired),
                                   "issues": [str(issue) for issue in issues]})

            play = self._assemble(result.play.concept, payloads)
            result.play = play
            self._compile_and_write(play, result)
            self._build_and_validate(result)

    # -- 报告 -------------------------------------------------------------- #
    def _write_report(self, result: RunResult) -> None:
        if self.options.dry_run or result.play is None:
            return
        usage_text = self.client.usage.summary() if self.client is not None else ""
        json_name = self.options.json_name or "%s.json" % sanitize_title(result.play.title)
        report = render_report(
            play=result.play, result=result.compile_result, settings=self.settings,
            options=self.options, artifacts=result.artifacts, issues=result.issues,
            elapsed=time.time() - self._started, stages=result.stages,
            repairs=result.repairs, usage_text=usage_text, json_name=json_name,
        )
        path = Path(self.options.out_dir) / "生成报告.md"
        path.write_text(report, encoding="utf-8")
        self.log("info", "已写出 生成报告.md")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _flatten_scenes(data) -> list[dict]:
    """把模型返回的各种外壳拆成「场景对象列表」。

    接受：``{"scene": {...}}`` / ``{"scenes": [...]}`` / 直接一个场景 / 一个数组。
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("scene"), dict):
        return [data["scene"]]
    if isinstance(data.get("scenes"), list):
        return [item for item in data["scenes"] if isinstance(item, dict)]
    if "lines" in data or "background" in data:
        return [data]
    # 有些模型会包一层 {"场次1": {...}, "场次2": {...}}
    nested = [value for value in data.values() if isinstance(value, dict) and "lines" in value]
    if nested:
        return nested
    return []


def _tail_of(scene: dict, count: int = 6) -> str:
    """取一场的结尾几句，供下一场接戏。"""
    lines = []
    for item in (scene.get("lines") or [])[-count:]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "")
        if kind in ("say", "narr", "board"):
            who = item.get("who") or ("旁白" if kind == "narr" else "字幕")
            lines.append("%s：%s" % (who, item.get("text") or ""))
    return "\n".join(lines)


def _location_indexes(location: str) -> list[int]:
    """``commands[3][1].portrait`` → ``[3, 1]``。"""
    indexes = []
    for chunk in str(location or "").replace("commands", " ").split("]"):
        digits = chunk.strip(" [].")
        if digits.isdigit():
            indexes.append(int(digits))
    return indexes


def write_game_files(document: dict, save_dir, *, out_dir=None, dry_run: bool = False,
                     log=None) -> list[Path]:
    """把规范文档写成游戏文件（``Config.ch`` / ``Rect`` / ``Dialog\\<title>``）。

    先删后写，避开 ``lcde`` 每次 ``--force`` 都留一个 ``.bak-<时间戳>`` 的行为；
    本次运行**首次**覆盖某个文件时，会往 ``<out_dir>/backup-<时间戳>/`` 留一份原件。
    这样反复重编译不会攒出一堆备份，但原件仍然找得回来。
    """
    log = log or (lambda level, message: None)
    save_dir = Path(save_dir)
    characters, stories = project_from_doc(document)
    backup_root = Path(out_dir or save_dir) / ("backup-%s" % time.strftime("%Y%m%d_%H%M%S"))
    written: list[Path] = []

    def backup_once(path: Path) -> None:
        if dry_run or not path.exists():
            return
        try:
            target = backup_root / path.relative_to(save_dir)
        except ValueError:                                   # pragma: no cover
            target = backup_root / path.name
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        log("debug", "已备份 %s → %s" % (path, target))

    for character in characters:
        directory = save_dir / "Character" / character.char_id
        for path in (directory / "Config.ch", directory / "Rect"):
            backup_once(path)
            path.unlink(missing_ok=True)
        written += character.save(save_dir)
    for story in stories:
        path = save_dir / "Dialog" / story.title
        backup_once(path)
        path.unlink(missing_ok=True)
        written.append(story.save(save_dir))
    return written


def validate_entities(document: dict, save_dir) -> list:
    """只校验本次生成的实体，不去打扰存档目录里别的剧本。"""
    project = Project(save_dir)
    issues: list = []
    cache: dict = {}
    for entry in document.get("characters", []):
        char_id = entry.get("id") or ""
        character = Character.load(save_dir, char_id)
        issues += validate_character(character, project)
        cache[char_id.replace("/", "\\")] = character
    for entry in document.get("stories", []):
        story = Story.load(save_dir, entry.get("title") or "")
        issues += validate_story(story, project, cache)
    return issues


# --------------------------------------------------------------------------- #
# 复用入口：从现成的 IR 重新编译（不调 API）
# --------------------------------------------------------------------------- #

def compile_existing(ir_path, *, save_dir=None, out_dir=None, log=None,
                     stage_targets: dict | None = None, rebuild: bool = True) -> RunResult:
    """``autoLCDE compile`` 的实现：读 ``剧本.json`` → 编译 → 写游戏文件 → 校验。

    改台词之后走这条路**不需要再调 API**，也不消耗 token。
    """
    log = log or (lambda level, message: None)
    source = Path(ir_path)
    if not source.is_file():
        raise ScriptError("找不到剧本文件：%s" % source)
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if data.get("kind") == "project":
        raise ScriptError("这是 LCDE 规范文档，不是 autoLCDE 的剧本 IR；"
                          "要用 lcde 的 `project build` 处理它")
    play = Screenplay.from_dict(data)

    out = Path(out_dir) if out_dir else source.parent
    save = Path(save_dir) if save_dir else out / "save"
    result = RunResult(ok=False, out_dir=out, save_dir=save)
    started = time.time()

    options = RunOptions(
        out_dir=out, premise=play.logline or play.title, title=play.title,
        scenes=len(play.scenes), save_dir=save,
        stage_targets=stage_targets or {"left": 220.0, "center": 400.0, "right": 580.0},
    )
    compiled = compile_screenplay(play, CompileOptions(stage_targets=options.stage_targets),
                                  save_dir=save)
    result.play = play
    result.compile_result = compiled
    title = compiled.document["stories"][0]["title"]
    json_name = "%s.json" % title

    out.mkdir(parents=True, exist_ok=True)
    (out / json_name).write_text(dumps(compiled.document), encoding="utf-8")
    (out / "raw").mkdir(parents=True, exist_ok=True)
    (out / "raw" / "剧本raw.txt").write_text(render_raw_script(play), encoding="utf-8")
    (out / "立绘清单.md").write_text(render_character_sheet(play), encoding="utf-8")
    (out / "分镜备注.md").write_text(render_notes(play), encoding="utf-8")
    log("info", "已编译 %s（%d 场 / %d 条命令）"
        % (json_name, len(play.scenes), len(compiled.document["stories"][0]["commands"])))

    result.artifacts = {
        json_name: "LCDE 规范文档",
        "剧本.json": "剧本中间表示（IR）",
        "raw/剧本raw.txt": "人读剧本",
        "立绘清单.md": "美术规格表",
        "分镜备注.md": "运镜与音频需求",
    }

    if rebuild:
        written, skipped = generate_for_document(compiled.document, save, force=False)
        log("info", "占位图：新写 %d 张，跳过 %d 张" % (len(written), len(skipped)))
        files = write_game_files(compiled.document, save, out_dir=out, log=log)
        log("info", "已写出 %d 个游戏文件 → %s" % (len(files), save))
        result.issues = validate_entities(compiled.document, save)
        for issue in result.issues:
            log("warn" if issue.severity != "info" else "debug", "  %s" % issue)

    result.elapsed = time.time() - started
    result.ok = not has_errors(result.issues)
    return result