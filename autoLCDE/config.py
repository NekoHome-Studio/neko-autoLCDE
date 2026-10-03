"""配置与密钥解析。

**密钥从不写进代码，也不打印全量。** 取值优先级（高 → 低）：

1. 命令行参数 ``--api-key`` / ``--base-url`` / ``--model`` / ``--provider``
2. 环境变量（``AUTOLCDE_API_KEY`` 或各家的 ``DEEPSEEK_API_KEY`` / ``OPENAI_API_KEY`` …）
3. 配置文件（默认 ``<仓库根>/autoLCDE.config.json``，可用 ``--config`` 或
   ``AUTOLCDE_CONFIG`` 指定）
4. 提供方预设（只含 base_url 与默认模型，**不含密钥**）

配置文件长这样（``python tools/autoLCDE.py init`` 会生成模板）::

    {
      "provider": "deepseek",
      "baseUrl": null,
      "model": null,
      "apiKey": null,
      "temperature": 1.0,
      "maxTokens": 8192,
      "timeout": 300,
      "retries": 3,
      "jsonMode": true
    }

把密钥写进文件是可行的（本地工具），但更推荐用环境变量：

    $env:DEEPSEEK_API_KEY = "sk-..."
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .paths import default_config_path, resolve_config_path

#: 默认配置文件位置（仓库模式 → 仓库根；独立包 → 包根，配置随包走）。
DEFAULT_CONFIG_PATH = default_config_path()

DEFAULT_PROVIDER = "deepseek"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    base_url: str
    model: str
    key_env: tuple[str, ...]
    note: str = ""


#: 预设只提供 base_url 与默认模型；它们都是 OpenAI 兼容的 ``/chat/completions``。
PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        "deepseek", "DeepSeek（默认）",
        "https://api.deepseek.com/v1", "deepseek-chat",
        ("DEEPSEEK_API_KEY",),
        "性价比高，中文剧本质量稳定；JSON 模式需提示词里出现「JSON」字样",
    ),
    "openai": Provider(
        "openai", "OpenAI",
        "https://api.openai.com/v1", "gpt-4o-mini",
        ("OPENAI_API_KEY",),
        "需要能访问 api.openai.com（国内通常要代理）",
    ),
    "moonshot": Provider(
        "moonshot", "月之暗面 Kimi",
        "https://api.moonshot.cn/v1", "moonshot-v1-32k",
        ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "长上下文，适合一次写很多场",
    ),
    "siliconflow": Provider(
        "siliconflow", "硅基流动",
        "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3",
        ("SILICONFLOW_API_KEY",),
        "聚合多家开源模型",
    ),
    "dashscope": Provider(
        "dashscope", "阿里百炼（通义）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
        ("DASHSCOPE_API_KEY",),
        "OpenAI 兼容模式",
    ),
    "custom": Provider(
        "custom", "自定义（OpenAI 兼容，必须给 base_url 与 model）",
        "", "",
        ("AUTOLCDE_API_KEY",),
        "自建/中转服务；base_url 要写到 /v1 这一层",
    ),
    "mock": Provider(
        "mock", "离线演示（不联网、不需要密钥）",
        "mock://local", "mock-playwright",
        (),
        "内置一段原创短剧，用来验证整条流水线",
    ),
}

#: 配置文件里允许的键（多写的键会提示，避免拼错后静默失效）。
CONFIG_KEYS = {
    "provider", "baseUrl", "base_url", "model", "apiKey", "api_key",
    "temperature", "maxTokens", "max_tokens", "timeout", "retries",
    "jsonMode", "json_mode", "defaults", "$schema",
}


@dataclass
class Settings:
    """一次运行用到的连接与采样参数。"""

    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 1.0
    max_tokens: int = 8192
    timeout: float = 300.0
    retries: int = 3
    json_mode: bool = True
    #: 配置文件里带的生成默认值（--scenes / --style 等未显式给出时使用）
    defaults: dict = field(default_factory=dict)
    #: 密钥来源，仅用于诊断输出（如 ``env:DEEPSEEK_API_KEY``）
    key_source: str = ""
    config_path: Path | None = None

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    @property
    def endpoint(self) -> str:
        """补全到 ``/chat/completions`` 的完整地址。"""
        base = (self.base_url or "").rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def masked_key(self) -> str:
        return mask_key(self.api_key)

    def describe(self) -> str:
        lines = [
            "提供方    : %s" % self.provider,
            "base_url  : %s" % (self.base_url or "（未设置）"),
            "model     : %s" % (self.model or "（未设置）"),
            "api key   : %s" % (self.masked_key() or "（未设置）"),
        ]
        if self.key_source:
            lines.append("密钥来源  : %s" % self.key_source)
        lines += [
            "temperature: %s" % self.temperature,
            "max_tokens : %s" % self.max_tokens,
            "json 模式  : %s" % ("开" if self.json_mode else "关"),
            "配置文件   : %s" % (self.config_path or "（不存在）"),
        ]
        return "\n".join(lines)


def mask_key(key: str) -> str:
    """把密钥打码成 ``sk-ab…cd12``。**任何日志都不许直接打印原值。**"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[0] + "…" + key[-1]
    return "%s…%s" % (key[:6], key[-4:])


def config_path(explicit: str | os.PathLike | None = None) -> Path:
    """配置文件的解析入口（实现在 :func:`autoLCDE.paths.resolve_config_path`）。"""
    return resolve_config_path(explicit)


def load_config_file(path: Path | None) -> dict:
    """读取配置文件；不存在就返回空 dict（配置文件是可选的）。"""
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError("配置文件无法解析: %s（%s）" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象: %s" % path)
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise ConfigError("配置文件里有未知键 %s（可能是拼错了）: %s"
                          % (", ".join(unknown), path))
    return data


def _first(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _env(names) -> tuple[str | None, str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value, "env:%s" % name
    return None, ""


def resolve_settings(args=None, *, require_key: bool = True) -> Settings:
    """把「命令行 + 环境变量 + 配置文件 + 预设」合并成 :class:`Settings`。

    ``require_key=False`` 用于诊断场景（``autoLCDE doctor``）：缺密钥也照样返回，
    由调用方自己决定怎么报。
    """
    args = args if args is not None else _EmptyArgs()
    path = config_path(getattr(args, "config", None))
    cfg = load_config_file(path)

    provider_name = _first(
        getattr(args, "provider", None),
        os.environ.get("AUTOLCDE_PROVIDER"),
        cfg.get("provider"),
        DEFAULT_PROVIDER,
    )
    preset = PROVIDERS.get(provider_name)
    if provider_name not in PROVIDERS:
        raise ConfigError("未知 provider %r；可选：%s"
                          % (provider_name, ", ".join(sorted(PROVIDERS))))

    base_url = _first(getattr(args, "base_url", None),
                      os.environ.get("AUTOLCDE_BASE_URL"),
                      cfg.get("baseUrl"), cfg.get("base_url"),
                      preset.base_url if preset else None)
    model = _first(getattr(args, "model", None),
                   os.environ.get("AUTOLCDE_MODEL"),
                   cfg.get("model"),
                   preset.model if preset else None)

    key, source = _env(preset.key_env if preset else ())
    if not key:
        key, source = _env(("AUTOLCDE_API_KEY",))
    if not key:
        key = _first(getattr(args, "api_key", None))
        source = "命令行 --api-key" if key else ""
    if not key:
        key = _first(cfg.get("apiKey"), cfg.get("api_key"))
        source = ("配置文件 %s" % path) if key else ""

    def number(name, cast, env_name, fallback):
        value = _first(getattr(args, name, None), os.environ.get(env_name),
                       cfg.get(name) if name in cfg else None,
                       cfg.get(_snake(name)) if _snake(name) in cfg else None)
        if value is None:
            return fallback
        try:
            return cast(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError("参数 %s 不是合法数值: %r" % (name, value)) from exc

    json_mode = _first(getattr(args, "json_mode", None),
                       cfg.get("jsonMode"), cfg.get("json_mode"))
    if json_mode is None:
        json_mode = True

    settings = Settings(
        provider=provider_name,
        base_url=base_url or "",
        model=model or "",
        api_key=key or "",
        temperature=float(number("temperature", float, "AUTOLCDE_TEMPERATURE", 1.0)),
        max_tokens=int(number("max_tokens", int, "AUTOLCDE_MAX_TOKENS", 8192)),
        timeout=float(number("timeout", float, "AUTOLCDE_TIMEOUT", 300.0)),
        retries=int(number("retries", int, "AUTOLCDE_RETRIES", 3)),
        json_mode=bool(json_mode),
        defaults=dict(cfg.get("defaults") or {}),
        key_source=source,
        config_path=path if path.is_file() else None,
    )

    if settings.is_mock:
        return settings
    if not settings.model:
        raise ConfigError("没有指定模型：加 --model，或把 model 写进 %s" % path)
    if not settings.api_key and require_key:
        env_hint = " 或 ".join(preset.key_env) if preset and preset.key_env else "AUTOLCDE_API_KEY"
        raise ConfigError(
            "没有找到 API 密钥。三种做法任选其一：\n"
            "  1) 设环境变量：  $env:%s = \"sk-...\"\n"
            "  2) 加命令行参数：--api-key sk-...\n"
            "  3) 写进配置文件 %s 的 \"apiKey\" 字段\n"
            "（想要完全离线试用，可以加 --provider mock）" % (env_hint, path)
        )
    if not settings.base_url:
        raise ConfigError("provider=custom 时必须给出 base_url（形如 https://host/v1）")
    return settings


def _snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


class _EmptyArgs:
    """没有传命名空间时的占位（全部取默认/环境变量）。"""

    def __getattr__(self, _name):
        return None


# --------------------------------------------------------------------------- #
# 配置模板
# --------------------------------------------------------------------------- #

CONFIG_TEMPLATE = {
    "provider": "deepseek",
    "baseUrl": None,
    "model": None,
    "apiKey": None,
    "temperature": 1.0,
    "maxTokens": 8192,
    "timeout": 300,
    "retries": 3,
    "jsonMode": True,
    "defaults": {
        "scenes": 8,
        "scenesPerCall": 2,
        "cast": 4,
        "duration": 12,
    },
}


def write_config_template(path: Path, *, force: bool = False) -> Path:
    """写出配置模板（已存在且未 ``--force`` 时报错，避免覆盖用户的密钥）。"""
    path = Path(path)
    if path.exists() and not force:
        raise ConfigError("%s 已存在；要覆盖请加 --force" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path
