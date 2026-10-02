"""OpenAI 兼容的 ``/chat/completions`` 客户端（纯标准库）。

只做三件事，但每件都做到位：

* **说话**：``chat(messages, stage=...)`` 返回模型正文（字符串）。
* **抽 JSON**：模型爱把 JSON 包在 ``` 围栏里、或前面加一句「好的，这是剧本：」。
  :func:`extract_json` 能把这些剥掉，并且容忍尾随逗号这类常见瑕疵。
* **记账**：累计 token 用量；服务端没回报用量时用字数粗估，报告里会标注。

重试策略：429 / 5xx / 网络错误 → 指数退避重试（``settings.retries`` 次）；
401/403/404 → 直接抛出并给处理建议，重试没有意义。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import Settings
from .errors import APIError, ConfigError, ScriptError

__all__ = ["Usage", "ChatClient", "make_client", "extract_json"]

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}

#: 一次请求里最多重试几次「服务端嫌 response_format 不支持」这类 400。
_FALLBACK_ON_BAD_REQUEST = ("response_format", "json_object", "json mode")


@dataclass
class Usage:
    """token 用量。``estimated`` 为真表示服务端没给用量、这是按字数估的。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False
    #: 按阶段分的用量：{"concept": (calls, prompt, completion), ...}
    by_stage: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, stage: str, prompt: int, completion: int, estimated: bool) -> None:
        self.calls += 1
        self.prompt_tokens += max(0, int(prompt or 0))
        self.completion_tokens += max(0, int(completion or 0))
        self.estimated = self.estimated or estimated
        calls, pt, ct = self.by_stage.get(stage or "?", (0, 0, 0))
        self.by_stage[stage or "?"] = (calls + 1, pt + int(prompt or 0), ct + int(completion or 0))

    def summary(self) -> str:
        mark = "（估算）" if self.estimated else ""
        return ("%d 次调用，输入 %d / 输出 %d tokens，合计 %d%s"
                % (self.calls, self.prompt_tokens, self.completion_tokens,
                   self.total_tokens, mark))


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other / 3.5) + 1


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _strip_fences(text: str) -> str:
    blocks = _FENCE.findall(text)
    if blocks:
        # 取最长的那个块：模型偶尔会先贴一小段示例再贴正文
        return max(blocks, key=len).strip()
    return text.strip()


def _match_bracket(text: str, start: int) -> str | None:
    """从 ``text[start]`` 的括号开始做配对扫描（跳过字符串里的括号与转义）。"""
    opening = text[start]
    closing = {"{": "}", "[": "]"}[opening]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _repair_json(text: str) -> str:
    """修掉最常见的两种瑕疵：尾随逗号、```外的说明文字。"""
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def extract_json(text: str, *, expect: str = "object"):
    """从模型回复里抽出 JSON 并 ``json.loads``。

    ``expect`` ∈ ``object|array|any``，用于在同时出现 ``{}`` 和 ``[]`` 时选对那个。
    """
    if text is None:
        raise ScriptError("模型返回为空")
    body = _strip_fences(text)
    candidates: list[str] = []

    # 先用「最外层的括号」扫一遍，再退化为直接解析
    for index, ch in enumerate(body):
        if ch in "{[":
            chunk = _match_bracket(body, index)
            if chunk:
                candidates.append(chunk)
            break

    for opener, closer in (("{", "}"), ("[", "]")):
        first = body.find(opener)
        last = body.rfind(closer)
        if first != -1 and last > first:
            candidates.append(body[first:last + 1])

    candidates.append(body)

    errors: list[str] = []
    for chunk in candidates:
        for attempt in (chunk, _repair_json(chunk)):
            try:
                value = json.loads(attempt)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if expect == "object" and not isinstance(value, dict):
                errors.append("期望 JSON 对象，得到 %s" % type(value).__name__)
                continue
            if expect == "array" and not isinstance(value, list):
                errors.append("期望 JSON 数组，得到 %s" % type(value).__name__)
                continue
            return value

    head = body[:400].replace("\n", "\\n")
    raise ScriptError("模型没有返回可解析的 JSON（%s）；开头是：%s"
                      % (errors[0] if errors else "未知原因", head), raw=text)


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class ChatClient:
    """OpenAI 兼容的聊天客户端。"""

    def __init__(self, settings: Settings, log=None) -> None:
        if not settings.endpoint:
            raise ConfigError("base_url 为空，无法调用 API")
        self.settings = settings
        self.log = log or (lambda *_a, **_k: None)
        self.usage = Usage()
        self._context = ssl.create_default_context()
        self._json_mode = settings.json_mode

    # -- 底层 -------------------------------------------------------------- #
    def _payload(self, messages, temperature, max_tokens, json_object) -> dict:
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_object and self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.settings.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.settings.api_key,
                "Accept": "application/json",
                "User-Agent": "lcde-aiplay/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout,
                                        context=self._context) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:                                   # noqa: BLE001
                pass
            message = _error_message(detail) or str(exc)
            raise APIError("HTTP %s: %s" % (exc.code, message), status=exc.code,
                           body=detail, retryable=exc.code in RETRY_STATUS)
        except urllib.error.URLError as exc:
            raise APIError("网络错误: %s" % exc.reason, retryable=True) from exc
        except TimeoutError as exc:
            raise APIError("请求超时（%.0fs）" % self.settings.timeout, retryable=True) from exc

        try:
            return json.loads(raw)
        except ValueError as exc:
            raise APIError("服务端返回的不是 JSON：%s" % raw[:300]) from exc

    # -- 对外 -------------------------------------------------------------- #
    def chat(self, messages, *, stage: str = "", temperature=None,
             max_tokens=None, json_object: bool = True, meta=None) -> str:
        """发一次对话请求，返回正文。

        ``stage`` 只用于日志与用量归类。``meta`` 是给离线 mock 用的结构化上下文
        （场次下标等），真实服务端不需要，这里显式忽略——但**签名必须收下它**，
        否则流水线一调用就 TypeError。
        """
        payload = self._payload(messages, temperature, max_tokens, json_object)
        attempts = max(1, int(self.settings.retries))
        last: APIError | None = None

        for attempt in range(1, attempts + 1):
            try:
                data = self._post(payload)
                break
            except APIError as exc:
                # 有些中转服务不认 response_format，去掉再试一次
                if (exc.status == 400 and "response_format" in payload
                        and any(word in (exc.body or "").lower()
                                for word in _FALLBACK_ON_BAD_REQUEST)):
                    self.log("warn", "服务端不支持 JSON 模式，改为普通文本模式重试")
                    payload.pop("response_format", None)
                    self._json_mode = False
                    continue
                last = exc
                if not exc.retryable or attempt >= attempts:
                    raise
                wait = min(30.0, 1.6 ** attempt)
                self.log("warn", "第 %d/%d 次失败（%s），%.1fs 后重试"
                         % (attempt, attempts, exc, wait))
                time.sleep(wait)
        else:                                                   # pragma: no cover
            raise last or APIError("请求失败")

        text, usage = _read_response(data)
        if usage is None:
            prompt = estimate_tokens(json.dumps(messages, ensure_ascii=False))
            completion = estimate_tokens(text)
            self.usage.add(stage, prompt, completion, estimated=True)
        else:
            self.usage.add(stage, usage.get("prompt_tokens", 0),
                           usage.get("completion_tokens", 0), estimated=False)
        self.log("debug", "[%s] 收到 %d 字（累计 %s）"
                 % (stage or "chat", len(text), self.usage.summary()))
        return text

    def ping(self) -> str:
        """用最小请求验证密钥与模型可用。返回模型的一句话回复。"""
        text = self.chat(
            [{"role": "user", "content": "只回复两个字：可用"}],
            stage="ping", temperature=0.0, max_tokens=16, json_object=False,
        )
        return text.strip()


def _read_response(data: dict) -> tuple[str, dict | None]:
    if not isinstance(data, dict):
        raise APIError("服务端返回结构异常：%s" % str(data)[:200])
    if "error" in data and data["error"]:
        err = data["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise APIError("服务端报错：%s" % message, status=None)
    choices = data.get("choices") or []
    if not choices:
        raise APIError("服务端没有返回 choices：%s" % json.dumps(data, ensure_ascii=False)[:300])
    choice = choices[0]
    message = choice.get("message") or {}
    text = message.get("content")
    if text is None:
        # 有些实现把正文放在 text 字段，或只给了 reasoning_content
        text = choice.get("text") or message.get("reasoning_content")
    if not text:
        raise APIError("模型返回了空正文（finish_reason=%s）" % choice.get("finish_reason"))
    return text, data.get("usage")


def _error_message(body: str) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300]
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return err
    return str(data.get("message") or body[:300])


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def make_client(settings: Settings, log=None, **kwargs):
    """按 provider 造客户端；``mock`` 走内置离线实现（``kwargs`` 透传给构造函数）。"""
    if settings.is_mock:
        from .mock import MockClient
        return MockClient(settings, log=log, **kwargs)
    return ChatClient(settings, log=log)
