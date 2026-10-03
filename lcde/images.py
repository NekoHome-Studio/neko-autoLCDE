"""读取图片的像素尺寸（不依赖第三方库）。

用途：替换立绘后按**实际图片尺寸**重算 ``Rect`` —— 引擎的显示尺寸是按
``rect.width = 源图宽 × (显示高度 / 源图高)`` 换算的，源图换了而 ``Rect`` 没换就会变形。

原生支持 PNG / JPEG / BMP / GIF / WebP（有损与无损两种）；其它格式若环境里有
Pillow 就交给它，否则明确报错。
"""

from __future__ import annotations

import struct
from pathlib import Path

from .errors import FormatError

__all__ = ["image_size", "SUPPORTED_SUFFIXES"]

SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")

#: JPEG 里携带尺寸的段（SOF0..SOF15，排除 DHT/JPG/DAC）
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _png(raw: bytes):
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise FormatError("不是 PNG")
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def _jpeg(raw: bytes):
    if raw[:2] != b"\xff\xd8":
        raise FormatError("不是 JPEG")
    offset = 2
    while offset + 4 <= len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        marker = raw[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = struct.unpack(">H", raw[offset + 2:offset + 4])[0]
        if marker in _JPEG_SOF:
            height, width = struct.unpack(">HH", raw[offset + 5:offset + 9])
            return width, height
        offset += 2 + length
    raise FormatError("JPEG 里找不到 SOF 段")


def _bmp(raw: bytes):
    if raw[:2] != b"BM":
        raise FormatError("不是 BMP")
    width, height = struct.unpack("<ii", raw[18:26])
    return abs(width), abs(height)


def _gif(raw: bytes):
    if raw[:6] not in (b"GIF87a", b"GIF89a"):
        raise FormatError("不是 GIF")
    width, height = struct.unpack("<HH", raw[6:10])
    return width, height


def _webp(raw: bytes):
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise FormatError("不是 WebP")
    kind = raw[12:16]
    if kind == b"VP8X":
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return width, height
    if kind == b"VP8 ":
        # 有损：帧头里 0x9d012a 之后是 14 位宽高
        pos = raw.find(b"\x9d\x01\x2a", 20)
        if pos < 0:
            raise FormatError("WebP(VP8) 帧头异常")
        width = struct.unpack("<H", raw[pos + 3:pos + 5])[0] & 0x3FFF
        height = struct.unpack("<H", raw[pos + 5:pos + 7])[0] & 0x3FFF
        return width, height
    if kind == b"VP8L":
        bits = int.from_bytes(raw[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise FormatError("未知的 WebP 子格式: %r" % kind)


def image_size(path) -> tuple[int, int]:
    """返回 ``(宽, 高)``。"""
    path = Path(path)
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    try:
        if suffix == ".png":
            return _png(raw)
        if suffix in (".jpg", ".jpeg"):
            return _jpeg(raw)
        if suffix == ".bmp":
            return _bmp(raw)
        if suffix == ".gif":
            return _gif(raw)
        if suffix == ".webp":
            return _webp(raw)
    except FormatError:
        raise
    except Exception as exc:                      # 数据损坏
        raise FormatError("读取 %s 的尺寸失败: %s" % (path.name, exc)) from None

    # 兜底：TIFF 等交给 Pillow
    try:
        from PIL import Image
    except ImportError:
        raise FormatError(
            "%s：格式 %s 需要 Pillow 才能读取尺寸（或改用 png/jpg/bmp/webp）"
            % (path.name, suffix or "未知")
        ) from None
    with Image.open(path) as image:
        return image.width, image.height
