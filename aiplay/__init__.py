"""aiplay —— 接外部大模型 API 的 LCDE 剧本生成器。

一句话：给它一个主题，它调外部 API（DeepSeek / OpenAI / 任意 OpenAI 兼容服务）
写出剧本，编译成 LCDE 规范 JSON，生成占位素材与游戏文件，再用 ``lcde validate``
自检；有硬错误就把错误回喂给模型重写。

流水线：

    主题 → ①概念（选角/背景/音频/场次表） → ②分场剧本（IR）
         → ③编译成 LCDE 规范 JSON + 人读剧本 + 立绘清单 + 分镜备注
         → ④占位素材 → ⑤写游戏文件 → ⑥validate（有错则回到 ② 局部重写）

用法见 ``tools/aiplay.py --help``；网页界面用 ``python tools/aiplay.py web``。
"""

__version__ = "1.0.0"

#: 写进规范文档 ``generator`` 字段的标识。
GENERATOR = "aiplay %s" % __version__

from .errors import AIPlayError, APIError, ConfigError, ScriptError  # noqa: E402

__all__ = [
    "__version__",
    "GENERATOR",
    "AIPlayError",
    "APIError",
    "ConfigError",
    "ScriptError",
]
