"""autoLCDE —— 接外部大模型 API 的 LCDE 剧本生成器。

一句话：给它一个主题，它调外部 API（DeepSeek / OpenAI / 任意 OpenAI 兼容服务）
写出剧本，编译成 LCDE 规范 JSON，生成占位素材与游戏文件，再用 ``lcde validate``
自检；有硬错误就把错误回喂给模型重写。

流水线：

    主题 → ①概念（选角/背景/音频/场次表） → ②分场剧本（IR）
         → ③编译成 LCDE 规范 JSON + 人读剧本 + 立绘清单 + 分镜备注
         → ④占位素材 → ⑤写游戏文件 → ⑥validate（有错则回到 ② 局部重写）

用法见 ``tools/autoLCDE.py --help``；网页界面用 ``python tools/autoLCDE.py web``。

1.1.0 起由 ``aiplay`` 更名为 ``autoLCDE``（命令名、包目录、配置文件、环境变量前缀
都换了：``AIPLAY_*`` → ``AUTOLCDE_*``，``aiplay.config.json`` → ``autoLCDE.config.json``）。
"""

__version__ = "1.1.0"

#: 写进规范文档 ``generator`` 字段的标识。
GENERATOR = "autoLCDE %s" % __version__

from .errors import AutoLCDEError, APIError, ConfigError, ScriptError  # noqa: E402

__all__ = [
    "__version__",
    "GENERATOR",
    "AutoLCDEError",
    "APIError",
    "ConfigError",
    "ScriptError",
]
