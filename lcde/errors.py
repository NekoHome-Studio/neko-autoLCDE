"""异常类型。"""

from __future__ import annotations


class LcdeError(Exception):
    """本工具所有错误的基类。"""


class FormatError(LcdeError):
    """文件不是合法的 LCDE 数据（二进制损坏、JSON 不合规范、字段缺失等）。"""


class ValidationError(LcdeError):
    """数据合法但违反校验规则（例如 Rect 项数与立绘数不一致）。"""

    def __init__(self, messages):
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))
