"""LCDE 调取工具 —— 读写 Soalin/LCDE 的角色与剧本文件。

格式定义见仓库根目录的 ``SPEC.md``（规范 JSON 格式）与 ``read.md``（游戏二进制格式）。
"""

__version__ = "1.0.0"

#: 规范 JSON 格式的版本号。写入文档时使用，读取时校验主版本兼容性。
FORMAT_NAME = "lcde"
FORMAT_VERSION = 1

from .errors import LcdeError, FormatError, ValidationError  # noqa: E402

__all__ = [
    "__version__",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "LcdeError",
    "FormatError",
    "ValidationError",
]
