"""autoLCDE 的异常类型。

分三类，方便调用方按「该重试还是该改配置」区别处理：

* :class:`ConfigError` —— 配置/密钥问题，重试无用，要用户改；
* :class:`APIError` —— 网络或服务端问题，``retryable`` 为真时值得重试；
* :class:`ScriptError` —— 模型输出无法解析成剧本结构。
"""

from __future__ import annotations


class AutoLCDEError(Exception):
    """autoLCDE 的错误基类。"""


class ConfigError(AutoLCDEError):
    """配置或密钥问题（缺 key、base_url 不合法等）。"""


class APIError(AutoLCDEError):
    """调用外部 API 失败。

    ``retryable`` 不给时按状态码推断：网络层错误（没有状态码）与限流、5xx 值得重试，
    4xx 参数/权限类错误重试多少次都一样。
    """

    _RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})

    def __init__(self, message: str, *, status: int | None = None,
                 body: str = "", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = (status is None or status in self._RETRY_STATUS) \
            if retryable is None else bool(retryable)

    @property
    def hint(self) -> str:
        """按状态码给出可执行的处理建议。"""
        if self.status in (401, 403):
            return "密钥无效或无权限：检查 api key 是否填对、是否开通了该模型"
        if self.status == 404:
            return "base_url 或 model 写错了（base_url 通常形如 https://api.deepseek.com/v1）"
        if self.status == 429:
            return "触发限流或余额不足：降并发/稍后重试，或检查账户余额"
        if self.status and self.status >= 500:
            return "服务端故障：稍后重试"
        if self.status is None:
            return "网络不可达：检查代理、防火墙或 base_url"
        return "检查请求参数（模型名、max_tokens、response_format 支持情况）"


class ScriptError(AutoLCDEError):
    """模型返回的内容不是可用的剧本结构。"""

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw
