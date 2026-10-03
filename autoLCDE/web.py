"""本地网页界面 —— 纯标准库 ``http.server``，不需要装任何前端工具链。

    python tools/autoLCDE.py web            # 然后打开 http://127.0.0.1:8756

界面做四件事：填密钥、填需求、看进度、读产出。

* 默认**只监听 127.0.0.1**；``--token`` 可以再加一道口令（URL 上带 ``?token=…``）。
* 网页里填的密钥**只留在内存**，勾了「记住」才会写进配置文件；
  任何回显都经过打码（``sk-ab…cd12``）。
* 生成在后台线程里跑，页面每秒轮询一次增量日志；随时可以取消。
* 读取产出文件走 :func:`_safe_child`，路径必须落在该次运行的产出目录内。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from . import __version__
from .config import (
    PROVIDERS,
    ConfigError,
    config_path,
    resolve_settings,
)
from .errors import AutoLCDEError
from .ir import sanitize_title
from .paths import projects_root
from .pipeline import Pipeline, RunOptions

_LOCK = threading.Lock()
_RUNS: dict[str, "RunState"] = {}
_COUNTER = [0]


# --------------------------------------------------------------------------- #
# 运行状态
# --------------------------------------------------------------------------- #

class RunState:
    def __init__(self, run_id: str, options: RunOptions, settings) -> None:
        self.id = run_id
        self.options = options
        self.settings = settings
        self.lines: list[dict] = []
        self.state = "running"
        self.summary: dict = {}
        self.started = time.time()
        self.pipeline: Pipeline | None = None
        self.lock = threading.Lock()

    def log(self, level: str, message: str) -> None:
        with self.lock:
            self.lines.append({"level": level, "text": message,
                               "t": round(time.time() - self.started, 1)})

    def snapshot(self, since: int) -> dict:
        with self.lock:
            lines = self.lines[since:]
            total = len(self.lines)
        return {
            "id": self.id, "state": self.state, "lines": lines,
            "next": total, "elapsed": round(time.time() - self.started, 1),
            "summary": self.summary,
        }

    def cancel(self) -> None:
        if self.pipeline is not None:
            self.pipeline.cancelled = True
            self.log("warn", "已请求取消，正在收尾……")


def _new_run_id() -> str:
    with _LOCK:
        _COUNTER[0] += 1
        return "run%d-%d" % (_COUNTER[0], int(time.time()))


# --------------------------------------------------------------------------- #
# 生成任务
# --------------------------------------------------------------------------- #

def _options_from_payload(payload: dict) -> RunOptions:
    premise = (payload.get("premise") or "").strip()
    if not premise:
        raise ConfigError("请填写主题/梗概")
    title = (payload.get("title") or "").strip()
    out_name = (payload.get("outputName") or "").strip()
    folder = out_name or title or premise[:12]
    out_dir = Path(payload.get("outDir") or (projects_root() / _slug(folder)))

    def number(key, default, cast=float):
        value = payload.get(key)
        if value in (None, ""):
            return default
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    targets = {
        "left": number("xLeft", 220.0),
        "center": number("xCenter", 400.0),
        "right": number("xRight", 580.0),
    }
    return RunOptions(
        out_dir=out_dir, premise=premise, title=title,
        style=(payload.get("style") or "").strip(),
        tone=(payload.get("tone") or "").strip(),
        language=(payload.get("language") or "简体中文").strip(),
        audience=(payload.get("audience") or "").strip(),
        constraints=(payload.get("constraints") or "").strip(),
        scenes=int(number("scenes", 8, int)),
        cast=int(number("cast", 4, int)),
        duration=int(number("duration", 12, int)),
        scenes_per_call=max(1, int(number("scenesPerCall", 2, int))),
        build_files=bool(payload.get("build", True)),
        placeholder=bool(payload.get("placeholder", True)),
        repair_rounds=max(0, int(number("repairRounds", 2, int))),
        repair_warning_codes=tuple(
            code.strip().upper() for code in (payload.get("repairWarnings") or "").split(",")
            if code.strip()),
        stage_targets=targets,
        mock_flaws=int(number("mockFlaws", 0, int)),
        dry_run=bool(payload.get("dryRun", False)),
    )


def _settings_from_payload(payload: dict):
    """命令行/环境/配置文件的解析结果，再用网页表单里填的覆盖。"""
    args = SimpleNamespace(
        config=None,
        provider=(payload.get("provider") or None),
        base_url=(payload.get("baseUrl") or None),
        model=(payload.get("model") or None),
        api_key=(payload.get("apiKey") or None),
        temperature=None, max_tokens=None, timeout=None, retries=None, json_mode=None,
    )
    return resolve_settings(args)


def _persist_key(settings) -> None:
    """把表单里的连接信息写回配置文件（仅当用户勾了「记住」）。"""
    path = settings.config_path or config_path(None)
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            data = {}
    data.setdefault("provider", settings.provider)
    data["provider"] = settings.provider
    if settings.base_url:
        data["baseUrl"] = settings.base_url
    if settings.model:
        data["model"] = settings.model
    if settings.api_key:
        data["apiKey"] = settings.api_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def start_run(payload: dict) -> RunState:
    settings = _settings_from_payload(payload)
    options = _options_from_payload(payload)
    run = RunState(_new_run_id(), options, settings)
    if payload.get("rememberKey") and not settings.is_mock:
        try:
            _persist_key(settings)
            run.log("info", "连接信息已写入 %s（密钥也写进去了，注意别提交到版本库）"
                    % (settings.config_path or config_path(None)))
        except OSError as exc:
            run.log("warn", "写配置文件失败：%s" % exc)

    with _LOCK:
        _RUNS[run.id] = run

    def worker() -> None:
        run.log("info", "autoLCDE %s 开始（模型 %s，密钥 %s）"
                % (__version__, settings.model or "—", settings.masked_key() or "无"))
        run.log("info", "主题：%s" % options.premise.replace("\n", " ")[:120])
        pipeline = Pipeline(settings, options, log=run.log,
                            event=lambda name, pl: run.log("debug", "事件 %s %s" % (name, pl)))
        run.pipeline = pipeline
        try:
            result = pipeline.run()
            run.summary = {
                "ok": result.ok and not result.error,
                "error": result.error,
                "title": result.play.title if result.play else None,
                "out_dir": str(result.out_dir),
                "save_dir": str(result.save_dir) if result.save_dir else None,
                "stats": result.compile_result.stats if result.compile_result else {},
                "commands": len(result.compile_result.commands) if result.compile_result else 0,
                "errors": [{"code": i.code, "message": i.message} for i in result.errors],
                "warnings": [{"code": i.code, "message": i.message} for i in result.warnings],
                "repairs": len(result.repairs),
                "usage": result.usage_text,
                "elapsed": round(result.elapsed, 1),
                "artifacts": result.artifacts,
            }
            run.state = "done" if result.ok else "failed"
        except AutoLCDEError as exc:
            run.log("error", str(exc))
            run.summary = {"ok": False, "error": str(exc)}
            run.state = "failed"
        except Exception as exc:                              # noqa: BLE001
            run.log("error", "内部错误：%s" % exc)
            run.log("debug", traceback.format_exc())
            run.summary = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            run.state = "failed"
        finally:
            run.log("info", "结束（%.1fs）" % (time.time() - run.started))

    threading.Thread(target=worker, name="autoLCDE-%s" % run.id, daemon=True).start()
    return run


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    return (sanitize_title(text or "autoLCDE项目", fallback="autoLCDE项目")
            .replace(" ", "")[:24] or "autoLCDE项目")


def _safe_child(root: Path, relative: str) -> Path | None:
    """把 ``relative`` 解析到 ``root`` 之内；越界返回 None。"""
    root = Path(root).resolve()
    candidate = (root / str(relative).replace("\\", "/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "autoLCDE/%s" % __version__
    token: str | None = None
    allow_lan = False

    # -- 工具 -------------------------------------------------------------- #
    def log_message(self, fmt, *args):                          # noqa: A003
        if os.environ.get("AUTOLCDE_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    def _authorized(self, query: dict) -> bool:
        if not self.token:
            return True
        given = (query.get("token") or [""])[0] or self.headers.get("X-AutoLCDE-Token", "")
        return given == self.token

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self):                                           # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            self._json(403, {"ok": False, "error": "口令不对"})
            return
        route = parsed.path.rstrip("/") or "/"

        if route == "/":
            self._html(INDEX_HTML.replace("__TOKEN__", self.token or ""))
            return
        if route == "/api/providers":
            self._json(200, {"providers": [
                {"name": name, "label": preset.label, "baseUrl": preset.base_url,
                 "model": preset.model, "keyEnv": list(preset.key_env), "note": preset.note}
                for name, preset in PROVIDERS.items()]})
            return
        if route == "/api/config":
            try:
                settings = resolve_settings(SimpleNamespace(
                    config=None, provider=None, base_url=None, model=None, api_key=None,
                    temperature=None, max_tokens=None, timeout=None, retries=None,
                    json_mode=None))
                body = {"settings": {
                    "provider": settings.provider, "baseUrl": settings.base_url,
                    "model": settings.model, "apiKey": settings.masked_key(),
                    "hasKey": bool(settings.api_key), "keySource": settings.key_source,
                    "endpoint": settings.endpoint,
                    "configPath": str(settings.config_path or config_path(None)),
                }}
            except AutoLCDEError as exc:
                body = {"settings": None, "error": str(exc)}
            self._json(200, body)
            return
        if route in ("/api/status", "/api/run"):
            run_id = (query.get("id") or [""])[0]
            run = _RUNS.get(run_id)
            if run is None:
                self._json(404, {"ok": False, "error": "没有这次运行"})
                return
            since = int((query.get("since") or ["0"])[0] or 0)
            self._json(200, run.snapshot(since))
            return
        if route == "/api/file":
            run_id = (query.get("id") or [""])[0]
            name = (query.get("name") or [""])[0]
            run = _RUNS.get(run_id)
            if run is None:
                self._json(404, {"ok": False, "error": "没有这次运行"})
                return
            target = _safe_child(Path(run.options.out_dir), name)
            if target is None or not target.is_file():
                self._json(404, {"ok": False, "error": "文件不存在"})
                return
            kind = mimetypes.guess_type(str(target))[0] or "text/plain"
            if kind.startswith("text/") or target.suffix.lower() in (".md", ".json", ".txt"):
                kind += "; charset=utf-8"
            self._send(200, target.read_bytes(), kind)
            return

        self._json(404, {"ok": False, "error": "没有这个接口：%s" % route})

    def do_POST(self):                                          # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            self._json(403, {"ok": False, "error": "口令不对"})
            return
        route = parsed.path.rstrip("/")
        payload = self._read_body()

        if route == "/api/generate":
            try:
                run = start_run(payload)
            except (ConfigError, AutoLCDEError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:                            # noqa: BLE001
                self._json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                return
            self._json(200, {"ok": True, "id": run.id, "outDir": str(run.options.out_dir)})
            return

        if route == "/api/cancel":
            run = _RUNS.get(payload.get("id") or "")
            if run is None:
                self._json(404, {"ok": False, "error": "没有这次运行"})
                return
            run.cancel()
            self._json(200, {"ok": True})
            return

        if route == "/api/ping":
            try:
                settings = _settings_from_payload(payload)
                from .llm import make_client
                client = make_client(settings, log=lambda *_a: None)
                reply = client.ping()
            except AutoLCDEError as exc:
                self._json(200, {"ok": False, "error": "%s（%s）" % (exc, exc.hint)})
                return
            except Exception as exc:                            # noqa: BLE001
                self._json(200, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                return
            self._json(200, {"ok": True, "reply": reply, "usage": client.usage.summary()})
            return

        self._json(404, {"ok": False, "error": "没有这个接口：%s" % route})


def serve(*, host: str = "127.0.0.1", port: int = 8756, open_browser: bool = False,
          token: str | None = None, config: str | None = None,
          provider: str | None = None) -> int:
    from .cli import force_utf8_output
    force_utf8_output()
    Handler.token = token
    Handler.allow_lan = host not in ("127.0.0.1", "localhost")

    httpd = None
    last_error = None
    for candidate in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        raise ConfigError("端口 %d~%d 都被占用了：%s" % (port, port + 11, last_error))

    url = "http://%s:%d/%s" % (host, port, ("?token=%s" % token) if token else "")
    print("autoLCDE 网页界面已启动：%s" % url)
    print("  · 生成过程在后台线程里跑，页面关掉也不影响")
    print("  · 密钥只留在内存；勾「记住」才会写进配置文件")
    print("  · Ctrl+C 结束")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autoLCDE · LCDE 剧本生成器</title>
<style>
  :root {
    --bg: #14161a; --panel: #1d2026; --panel2: #242830; --line: #313640;
    --ink: #e8e6e1; --dim: #9aa0aa; --accent: #d8b06a; --ok: #7fc08a;
    --warn: #e0b070; --err: #e07a7a; --mono: "Cascadia Mono", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.6 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--line);
           display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 17px; margin: 0; letter-spacing: .5px; }
  header .sub { color: var(--dim); font-size: 12px; }
  main { display: grid; grid-template-columns: 340px minmax(420px, 1fr) minmax(320px, 460px);
         gap: 14px; padding: 14px; align-items: start; }
  @media (max-width: 1280px) { main { grid-template-columns: 1fr; } }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
            padding: 14px; }
  section h2 { margin: 0 0 10px; font-size: 13px; color: var(--accent);
               letter-spacing: 1px; font-weight: 600; }
  label { display: block; margin: 9px 0 3px; font-size: 12px; color: var(--dim); }
  input, select, textarea { width: 100%; background: var(--panel2); color: var(--ink);
      border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px; font: inherit; }
  textarea { min-height: 92px; resize: vertical; line-height: 1.55; }
  input:focus, select:focus, textarea:focus { outline: 1px solid var(--accent);
      border-color: var(--accent); }
  .row { display: flex; gap: 8px; }
  .row > * { flex: 1; }
  .hint { color: var(--dim); font-size: 11.5px; margin-top: 4px; }
  button { background: var(--panel2); color: var(--ink); border: 1px solid var(--line);
      border-radius: 6px; padding: 8px 12px; font: inherit; cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #241d0d; border-color: var(--accent);
      font-weight: 700; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .actions { display: flex; gap: 8px; margin-top: 12px; }
  .actions button { flex: 1; }
  #log { font-family: var(--mono); font-size: 12.5px; background: #0f1114;
         border: 1px solid var(--line); border-radius: 8px; padding: 10px;
         height: 380px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }
  #log .l { display: block; }
  #log .info { color: #c9cdd4; }
  #log .stage { color: var(--accent); font-weight: 700; }
  #log .warn { color: var(--warn); }
  #log .error { color: var(--err); }
  #log .debug { color: #6d737d; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px;
           border: 1px solid var(--line); color: var(--dim); }
  .badge.running { color: var(--accent); border-color: var(--accent); }
  .badge.done { color: var(--ok); border-color: var(--ok); }
  .badge.failed { color: var(--err); border-color: var(--err); }
  .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px 12px;
           margin: 10px 0 0; font-size: 12.5px; }
  .stats div span { color: var(--dim); }
  .files { margin-top: 10px; font-size: 12.5px; }
  .files a { color: var(--accent); text-decoration: none; }
  .files a:hover { text-decoration: underline; }
  .issues { margin-top: 8px; font-size: 12.5px; max-height: 160px; overflow-y: auto; }
  .issues .e { color: var(--err); } .issues .w { color: var(--warn); }
  details { margin-top: 10px; }
  summary { cursor: pointer; color: var(--accent); font-size: 12.5px; }
  #preview { font-family: var(--mono); font-size: 12.5px; white-space: pre-wrap;
             background: #0f1114; border: 1px solid var(--line); border-radius: 8px;
             padding: 10px; max-height: 340px; overflow-y: auto; margin-top: 8px; }
  .ok { color: var(--ok); } .err { color: var(--err); }
</style>
</head>
<body>
<header>
  <h1>autoLCDE</h1>
  <span class="sub">LCDE 剧本生成器 · 接外部大模型 API · 生成 → 编译 → 占位素材 → 校验</span>
  <span id="state" class="badge">未开始</span>
</header>

<main>
  <!-- 左：连接 -->
  <section>
    <h2>① 连接</h2>
    <label>提供方</label>
    <select id="provider"></select>
    <div class="hint" id="providerNote"></div>

    <label>base_url</label>
    <input id="baseUrl" placeholder="https://api.deepseek.com/v1">
    <label>模型</label>
    <input id="model" placeholder="deepseek-chat">
    <label>API Key</label>
    <input id="apiKey" type="password" placeholder="sk-…（只留在内存）">
    <div class="hint" id="keyState">未填写；也会自动读取环境变量与配置文件。</div>
    <label style="display:flex;align-items:center;gap:6px;margin-top:10px">
      <input type="checkbox" id="rememberKey" style="width:auto"> 记住到配置文件
    </label>
    <div class="actions">
      <button id="btnPing">测试连接</button>
      <button id="btnReload">重新读取配置</button>
    </div>
    <div class="hint" id="pingResult"></div>
  </section>

  <!-- 中：需求 -->
  <section>
    <h2>② 创作需求</h2>
    <label>主题 / 梗概（必填）</label>
    <textarea id="premise" placeholder="例：四等小站的末班车十年前就停运了，可每个雪夜都有人来等。值班员老周守着一份没人来取的旧记录。"></textarea>
    <div class="row">
      <div><label>标题（可留空）</label><input id="title" placeholder="由模型起名"></div>
      <div><label>产出目录名</label><input id="outputName" placeholder="projects\&lt;名字&gt;"></div>
    </div>
    <div class="row">
      <div><label>风格 / 致敬</label><input id="style" placeholder="冷幽默的都市短篇"></div>
      <div><label>情绪基调</label><input id="tone" placeholder="克制、留白"></div>
    </div>
    <div class="row">
      <div><label>场次</label><input id="scenes" type="number" value="8" min="1" max="40"></div>
      <div><label>角色数</label><input id="cast" type="number" value="4" min="1" max="12"></div>
      <div><label>时长(分)</label><input id="duration" type="number" value="12" min="1" max="120"></div>
      <div><label>每次写几场</label><input id="scenesPerCall" type="number" value="2" min="1" max="4"></div>
    </div>
    <label>硬性约束 / 受众（可留空）</label>
    <input id="constraints" placeholder="例：全年龄；不要出现死亡；不要网络梗">
    <div class="row" style="margin-top:10px">
      <div><label>回喂重写轮数</label><input id="repairRounds" type="number" value="2" min="0" max="5"></div>
      <div><label>额外交给模型修的警告码</label><input id="repairWarnings" placeholder="如 S20,S6"></div>
    </div>
    <label style="display:flex;align-items:center;gap:6px;margin-top:10px">
      <input type="checkbox" id="build" checked style="width:auto">
      生成占位素材并写出游戏文件 + 跑引擎校验
    </label>
    <div class="actions">
      <button id="btnStart" class="primary">开始生成</button>
      <button id="btnCancel" disabled>取消</button>
    </div>
    <div class="hint">生成耗时为几分钟量级；页面关掉也没关系，后台会继续跑完。</div>
  </section>

  <!-- 右：进度与产出 -->
  <section>
    <h2>③ 进度与产出</h2>
    <div id="log"><span class="l info">等待开始……</span></div>
    <div class="stats" id="stats"></div>
    <div class="issues" id="issues"></div>
    <div class="files" id="files"></div>
    <details id="previewBox" style="display:none">
      <summary>预览产出文件</summary>
      <div id="preview"></div>
    </details>
  </section>
</main>

<script>
const TOKEN = "__TOKEN__";
const $ = (id) => document.getElementById(id);
let currentRun = null, cursor = 0, timer = null;

function api(path) { return path + (TOKEN ? (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : ""); }

function setState(text, kind) {
  const el = $("state"); el.textContent = text; el.className = "badge " + (kind || "");
}

async function loadProviders() {
  const res = await fetch(api("/api/providers"));
  const data = await res.json();
  const sel = $("provider");
  sel.innerHTML = "";
  for (const p of data.providers) {
    const opt = document.createElement("option");
    opt.value = p.name; opt.textContent = p.label;
    opt.dataset.baseUrl = p.baseUrl; opt.dataset.model = p.model;
    opt.dataset.note = (p.note || "") + (p.keyEnv.length ? "　密钥环境变量：" + p.keyEnv.join(" / ") : "");
    sel.appendChild(opt);
  }
  sel.onchange = () => {
    const opt = sel.selectedOptions[0];
    $("baseUrl").value = opt.dataset.baseUrl || "";
    $("model").value = opt.dataset.model || "";
    $("providerNote").textContent = opt.dataset.note || "";
  };
  sel.onchange();
}

async function loadConfig() {
  const res = await fetch(api("/api/config"));
  const data = await res.json();
  const s = data.settings;
  if (!s) { $("keyState").textContent = data.error || "读取配置失败"; return; }
  if (s.provider) $("provider").value = s.provider;
  $("provider").onchange();
  if (s.baseUrl) $("baseUrl").value = s.baseUrl;
  if (s.model) $("model").value = s.model;
  $("keyState").textContent = s.hasKey
    ? ("已检测到密钥 " + s.apiKey + "（来源：" + (s.keySource || "?") + "）")
    : "没有检测到密钥；请在上面填写，或设好环境变量后点「重新读取配置」。";
}

$("btnReload").onclick = loadConfig;

$("btnPing").onclick = async () => {
  $("pingResult").textContent = "正在测试……";
  const res = await fetch(api("/api/ping"), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(formPayload())
  });
  const data = await res.json();
  $("pingResult").innerHTML = data.ok
    ? '<span class="ok">✓ 连通：' + escapeHtml(data.reply) + "　" + escapeHtml(data.usage || "") + "</span>"
    : '<span class="err">✗ ' + escapeHtml(data.error) + "</span>";
};

function formPayload() {
  return {
    provider: $("provider").value,
    baseUrl: $("baseUrl").value.trim(),
    model: $("model").value.trim(),
    apiKey: $("apiKey").value.trim(),
    rememberKey: $("rememberKey").checked,
    premise: $("premise").value,
    title: $("title").value.trim(),
    outputName: $("outputName").value.trim(),
    style: $("style").value.trim(),
    tone: $("tone").value.trim(),
    scenes: $("scenes").value,
    cast: $("cast").value,
    duration: $("duration").value,
    scenesPerCall: $("scenesPerCall").value,
    constraints: $("constraints").value.trim(),
    repairRounds: $("repairRounds").value,
    repairWarnings: $("repairWarnings").value.trim(),
    build: $("build").checked
  };
}

$("btnStart").onclick = async () => {
  if (!$("premise").value.trim()) { alert("请先填写主题/梗概"); return; }
  $("btnStart").disabled = true; $("btnCancel").disabled = false;
  $("log").innerHTML = ""; cursor = 0; $("stats").innerHTML = ""; $("issues").innerHTML = "";
  $("files").innerHTML = ""; $("previewBox").style.display = "none";
  setState("启动中", "running");
  const res = await fetch(api("/api/generate"), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(formPayload())
  });
  const data = await res.json();
  if (!data.ok) {
    appendLog({level: "error", text: data.error}); setState("启动失败", "failed");
    $("btnStart").disabled = false; $("btnCancel").disabled = true; return;
  }
  currentRun = data.id;
  appendLog({level: "info", text: "产出目录：" + data.outDir});
  timer = setInterval(poll, 800);
};

$("btnCancel").onclick = async () => {
  if (!currentRun) return;
  await fetch(api("/api/cancel"), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: currentRun})
  });
  appendLog({level: "warn", text: "已请求取消……"});
};

async function poll() {
  if (!currentRun) return;
  const res = await fetch(api("/api/status?id=" + encodeURIComponent(currentRun) + "&since=" + cursor));
  if (!res.ok) return;
  const data = await res.json();
  cursor = data.next;
  (data.lines || []).forEach(appendLog);
  if (data.state !== "running") {
    clearInterval(timer); timer = null;
    $("btnStart").disabled = false; $("btnCancel").disabled = true;
    const s = data.summary || {};
    setState(data.state === "done" ? "完成" : "失败", data.state === "done" ? "done" : "failed");
    renderSummary(s);
  } else {
    setState("生成中 " + data.elapsed + "s", "running");
  }
}

function appendLog(line) {
  const box = $("log");
  const span = document.createElement("span");
  span.className = "l " + (line.level || "info");
  span.textContent = "[" + String(line.t || 0).padStart(6) + "s] " + line.text;
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

function renderSummary(s) {
  const stats = s.stats || {};
  const rows = [
    ["剧本", s.title || "—"],
    ["场次 / 角色 / 背景", (stats.scenes ?? "—") + " / " + (stats.cast ?? "—") + " / " + (stats.backgrounds ?? "—")],
    ["命令 / 台词", (s.commands ?? "—") + " / " + (stats.spoken ?? "—")],
    ["粗估时长", (stats.duration_estimate ?? "—") + " 分钟"],
    ["回喂重写", (s.repairs ?? 0) + " 轮"],
    ["用量", s.usage || "—"],
    ["游戏文件目录", s.save_dir || "—"]
  ];
  $("stats").innerHTML = rows.map(([k, v]) =>
    "<div><span>" + k + "：</span>" + escapeHtml(String(v)) + "</div>").join("");

  const errors = s.errors || [], warnings = s.warnings || [];
  $("issues").innerHTML =
    (errors.length ? errors.map(e => '<div class="e">✗ [' + e.code + "] " + escapeHtml(e.message) + "</div>").join("") : "") +
    (warnings.length ? warnings.slice(0, 12).map(w => '<div class="w">! [' + w.code + "] " + escapeHtml(w.message) + "</div>").join("") : "") +
    (s.error ? '<div class="e">✗ ' + escapeHtml(s.error) + "</div>" : "");

  const artifacts = s.artifacts || {};
  $("files").innerHTML = Object.keys(artifacts).map(name =>
    '<div>· <a href="#" data-file="' + encodeURIComponent(name) + '">' + escapeHtml(name) +
    "</a> <span style='color:var(--dim)'>" + escapeHtml(artifacts[name]) + "</span></div>").join("");
  $("files").querySelectorAll("a").forEach(a => a.onclick = (ev) => {
    ev.preventDefault(); preview(decodeURIComponent(a.dataset.file));
  });
}

async function preview(name) {
  const res = await fetch(api("/api/file?id=" + encodeURIComponent(currentRun) +
                             "&name=" + encodeURIComponent(name)));
  const text = await res.text();
  $("previewBox").style.display = "block";
  $("preview").textContent = text.length > 60000 ? text.slice(0, 60000) + "\n……（已截断）" : text;
  $("preview").scrollIntoView({behavior: "smooth", block: "nearest"});
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

loadProviders().then(loadConfig);
</script>
</body>
</html>
"""
