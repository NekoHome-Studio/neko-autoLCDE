""".NET ``BinaryWriter`` / ``BinaryReader`` 的等价实现。

剧本与 ``Rect`` 全部使用小端序；字符串使用 7-bit 变长长度前缀 + UTF-8。
字段与字节序的对应关系见 ``read.md`` 第 4.3 节。
"""

from __future__ import annotations

import struct
from decimal import Decimal

from .errors import FormatError

__all__ = ["BinWriter", "BinReader", "encode_7bit", "decode_7bit", "decimal_to_net", "net_to_decimal"]


# --------------------------------------------------------------------------- #
# 7-bit 变长整数（BinaryWriter.Write7BitEncodedInt）
# --------------------------------------------------------------------------- #

def encode_7bit(value: int) -> bytes:
    if value < 0:
        raise FormatError("7-bit 长度不能为负: %r" % value)
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_7bit(data: bytes, offset: int = 0) -> tuple[int, int]:
    """返回 ``(值, 新的偏移)``。"""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise FormatError("读取 7-bit 长度时数据提前结束")
        if shift > 35:
            raise FormatError("7-bit 长度过长，数据可能已损坏")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


# --------------------------------------------------------------------------- #
# .NET decimal（16 字节：lo / mid / hi / flags，各 4 字节小端）
# --------------------------------------------------------------------------- #

_FLAG_UNUSED = 0x7F00FFFF  # bits 0-15, 24-30 必须为 0


def decimal_to_net(value) -> bytes:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    sign, digits, exponent = d.as_tuple()
    if not isinstance(exponent, int):
        raise FormatError("不支持 NaN / Infinity 形式的 decimal: %r" % value)
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    scale = -exponent
    if scale < 0:
        coefficient *= 10 ** (-scale)
        scale = 0
    if scale > 28:
        raise FormatError("decimal 小数位超过 28 位: %r" % value)
    if coefficient >= 1 << 96:
        raise FormatError("decimal 数值超出 96 位整数范围: %r" % value)
    lo = coefficient & 0xFFFFFFFF
    mid = (coefficient >> 32) & 0xFFFFFFFF
    hi = (coefficient >> 64) & 0xFFFFFFFF
    flags = (scale << 16) | (0x80000000 if sign else 0)
    return struct.pack("<IIII", lo, mid, hi, flags)


def net_to_decimal(raw: bytes) -> Decimal:
    if len(raw) != 16:
        raise FormatError("decimal 需要 16 字节，实际 %d" % len(raw))
    lo, mid, hi, flags = struct.unpack("<IIII", raw)
    if flags & _FLAG_UNUSED:
        raise FormatError("decimal flags 含有非法位: 0x%08X" % flags)
    scale = (flags >> 16) & 0xFF
    if scale > 28:
        raise FormatError("decimal 小数位超过 28 位: %d" % scale)
    coefficient = lo | (mid << 32) | (hi << 64)
    sign = 1 if flags & 0x80000000 else 0
    # 用十进制元组直接构造：scaleb()/Decimal(int) 会受 context 的 28 位精度影响，
    # 对 96 位整数（最多 29 位十进制）会静默丢精度。
    digits = tuple(int(ch) for ch in str(coefficient))
    return Decimal((sign, digits, -scale))


# --------------------------------------------------------------------------- #
# 写入器
# --------------------------------------------------------------------------- #

class BinWriter:
    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    # -- 定长数值 ---------------------------------------------------------- #
    def u8(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<B", v & 0xFF)
        return self

    def i8(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<b", v)
        return self

    def u16(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<H", v & 0xFFFF)
        return self

    def i16(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<h", v)
        return self

    def i32(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<i", v)
        return self

    def u32(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<I", v & 0xFFFFFFFF)
        return self

    def i64(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<q", v)
        return self

    def u64(self, v: int) -> "BinWriter":
        self._buf += struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)
        return self

    def f32(self, v: float) -> "BinWriter":
        self._buf += struct.pack("<f", v)
        return self

    def f64(self, v: float) -> "BinWriter":
        self._buf += struct.pack("<d", v)
        return self

    def bool(self, v) -> "BinWriter":
        self._buf.append(1 if v else 0)
        return self

    def char(self, v) -> "BinWriter":
        """``BinaryWriter.Write(char)`` —— 按 UTF-8 编码，无长度前缀。"""
        text = v if isinstance(v, str) else chr(int(v))
        if len(text) != 1:
            raise FormatError("char 必须是单个字符: %r" % v)
        encoded = text.encode("utf-8")
        if len(encoded) > 3:
            raise FormatError("char 超出 BMP 范围: %r" % v)
        self._buf += encoded
        return self

    def str(self, v: str) -> "BinWriter":
        """``BinaryWriter.Write(string)`` —— 7-bit 长度 + UTF-8。"""
        if v is None:
            v = ""
        encoded = str(v).encode("utf-8")
        self._buf += encode_7bit(len(encoded))
        self._buf += encoded
        return self

    def decimal(self, v) -> "BinWriter":
        self._buf += decimal_to_net(v)
        return self

    def bytes(self) -> bytes:
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------- #
# 读取器
# --------------------------------------------------------------------------- #

class BinReader:
    __slots__ = ("_data", "offset")

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self._data = data
        self.offset = offset

    # -- 状态 -------------------------------------------------------------- #
    @property
    def position(self) -> int:
        return self.offset

    @property
    def remaining(self) -> int:
        return len(self._data) - self.offset

    @property
    def eof(self) -> bool:
        return self.offset >= len(self._data)

    def _need(self, count: int) -> int:
        if self.offset + count > len(self._data):
            raise FormatError(
                "数据提前结束：需要 %d 字节，偏移 %d 处仅剩 %d 字节"
                % (count, self.offset, len(self._data) - self.offset)
            )
        start = self.offset
        self.offset += count
        return start

    # -- 定长数值 ---------------------------------------------------------- #
    def u8(self) -> int:
        return self._data[self._need(1)]

    def i8(self) -> int:
        return struct.unpack_from("<b", self._data, self._need(1))[0]

    def u16(self) -> int:
        return struct.unpack_from("<H", self._data, self._need(2))[0]

    def i16(self) -> int:
        return struct.unpack_from("<h", self._data, self._need(2))[0]

    def i32(self) -> int:
        return struct.unpack_from("<i", self._data, self._need(4))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self._data, self._need(4))[0]

    def i64(self) -> int:
        return struct.unpack_from("<q", self._data, self._need(8))[0]

    def u64(self) -> int:
        return struct.unpack_from("<Q", self._data, self._need(8))[0]

    def f32(self) -> float:
        return struct.unpack_from("<f", self._data, self._need(4))[0]

    def f64(self) -> float:
        return struct.unpack_from("<d", self._data, self._need(8))[0]

    def bool(self) -> bool:
        return self.u8() != 0

    def char(self) -> str:
        start = self.offset
        for size in (1, 2, 3):
            if self.remaining < size:
                break
            try:
                text = self._data[start:start + size].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(text) == 1:
                self.offset = start + size
                return text
        raise FormatError("偏移 %d 处的 char 不是合法 UTF-8 单字符" % start)

    def str(self) -> str:
        length, self.offset = decode_7bit(self._data, self.offset)
        start = self._need(length)
        return self._data[start:start + length].decode("utf-8")

    def decimal(self) -> Decimal:
        start = self._need(16)
        return net_to_decimal(self._data[start:start + 16])
