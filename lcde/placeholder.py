"""占位素材生成器 —— 在还没有美术资源时，让剧本能立刻在游戏里跑起来。

生成三类图片：

* 立绘 ``Character\\<id>\\<图片名>``    —— 默认 400×800
* 头像 ``Character\\Heads\\<id>.png``  —— 默认 200×200
* 背景 ``BG\\<路径>``                  —— 默认 1920×1080

**PNG 由本模块自己编码**（``zlib`` + ``struct``，标准库），因此不依赖 Pillow。
若环境里有 Pillow，会在图上叠加中文标签；没有则退化为纯色 + 几何标记，文件名仍然能区分。

占位图的 ``rect`` 用**游戏编辑器自己的自动适配公式**计算（见 read.md §3.3）：

    s = 400 / 图片高度
    rect = (center.x * s, center.y * s - 50, size.x * s, size.y * s)

这样生成的裁剪数据与游戏编辑器为同一张图生成的完全一致；替换成正式美术后，
删掉 ``Rect`` 文件再用编辑器打开角色，编辑器会按新图重新自动适配。
"""

from __future__ import annotations

import colorsys
import hashlib
import os
import struct
import zlib
from pathlib import Path

from .errors import LcdeError
from .model import IMAGE_EXTENSIONS, win_path

__all__ = [
    "PORTRAIT_SIZE",
    "HEAD_SIZE",
    "BACKGROUND_SIZE",
    "STAGE_SIZE",
    "ROOT_HALF_WIDTH",
    "editor_rect",
    "fit_rect",
    "place_for_center_x",
    "parse_color",
    "color_for",
    "placeholder_png",
    "generate_for_document",
]

PORTRAIT_SIZE = (400, 800)
HEAD_SIZE = (200, 200)
#: 背景要按**舞台长宽比 800:359 ≈ 2.228:1** 出图：
#: ``BG0`` 的 ``m_PreserveAspect = 1``（等比缩放，比例不符会留黑边），
#: 而交叉淡化后的 ``BG1`` 是 ``0``（直接拉伸）—— 只有比例对得上，两者才一致。
BACKGROUND_SIZE = (1600, 718)

#: 立绘舞台：``backParent.parent`` 的尺寸（engine 的 Portrait 实例就挂在这里）。
#: 取自 ``Resources/Dialog.prefab``：``Parent`` 的 sizeDelta = 800 × 358.98，
#: 上层 ``Mask`` 是 RectMask2D，所以**立绘会被裁到这块 800×359 的舞台内**。
STAGE_SIZE = (800.0, 358.98)

#: Portrait 根节点的尺寸（prefab 默认 187.2362 × 321.2513，取宽的一半）。
#: ``Portrait.MoveTo`` 改的是**根节点**的 anchoredPosition.x，根节点锚点 = 舞台左下角，
#: 而 ``ChangePortrait`` 改的是**子节点 Image** 的 anchoredPosition = ``rect.x/y``。
#: 因此可见图像的横坐标 = ``place - ROOT_HALF_WIDTH + rect.x``（以舞台左边缘为 0）。
ROOT_HALF_WIDTH = 93.6181

#: 引擎编辑器自动适配用的显示高度（``Save.LoadCharacterEditor`` 里的 400）
DISPLAY_HEIGHT = 400.0


def editor_rect(width: int, height: int) -> tuple[float, float, float, float]:
    """复刻游戏编辑器的立绘裁剪自动适配公式（read.md §3.3）。

    ``f = 400/高度``；``(中心 × f)`` 作为位置，``(尺寸 × f)`` 作为大小，再把 y 减 50。

    注意：显示高度恒为 400，而舞台只有 358.98 高，所以这个公式产出的立绘
    **底部 50px 会被 RectMask2D 裁掉**（y 从 -50 到 350）。这是引擎自身的行为。
    """
    scale = DISPLAY_HEIGHT / float(height)
    return (
        round(width / 2.0 * scale, 3),
        round(height / 2.0 * scale - 50.0, 3),
        round(width * scale, 3),
        round(height * scale, 3),
    )


def fit_rect(width: int, height: int,
             display_height: float = None) -> tuple[float, float, float, float]:
    """按「正好填满舞台高度」换算裁剪矩形，避免被 RectMask2D 裁掉。

    与 :func:`editor_rect` 同一套等比缩放，只是把显示高度从 400 改成舞台高度，
    并且不加那 −50 的偏移。
    """
    display = display_height if display_height is not None else STAGE_SIZE[1]
    scale = display / float(height)
    return (
        round(width / 2.0 * scale, 3),
        round(height / 2.0 * scale, 3),
        round(width * scale, 3),
        round(height * scale, 3),
    )


def place_for_center_x(center_x: float, rect_x: float) -> float:
    """求把立绘中心放到舞台横向 ``center_x`` 处所需的 ``CIn.place`` / ``CMove.position``。

    ``center_x`` 以舞台**左边缘**为 0（舞台宽 800）。
    """
    return round(center_x + ROOT_HALF_WIDTH - rect_x, 3)


_CJK_FONTS = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #

def parse_color(value) -> tuple[int, int, int]:
    """接受 ``"#RRGGBB"``、``"RRGGBB"`` 或 ``[r, g, b]``，返回 RGB 三元组。"""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(c))) for c in value)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
    raise LcdeError("无法解析颜色: %r（应为 #RRGGBB 或 [r,g,b]）" % (value,))


def color_for(key: str, *, saturation: float = 0.55, lightness: float = 0.45) -> tuple[int, int, int]:
    """由字符串稳定地派生一个可区分的颜色。"""
    digest = hashlib.md5(key.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return int(red * 255), int(green * 255), int(blue * 255)


# --------------------------------------------------------------------------- #
# 纯 Python PNG 编码
# --------------------------------------------------------------------------- #

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """把 RGB8 像素数据编码成 PNG。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # 每行的过滤器类型：None
        raw += rgb[y * stride:(y + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def _canvas(width: int, height: int, color) -> bytearray:
    return bytearray(bytes(color) * (width * height))


def _fill_rect(canvas: bytearray, width: int, height: int, x0, y0, x1, y1, color) -> None:
    x0 = max(0, int(x0)); x1 = min(width, int(x1))
    y0 = max(0, int(y0)); y1 = min(height, int(y1))
    row = bytes(color) * max(0, x1 - x0)
    for y in range(y0, y1):
        start = (y * width + x0) * 3
        canvas[start:start + len(row)] = row


def _shade(color, factor: float):
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _relative_luminance(color) -> float:
    red, green, blue = (c / 255.0 for c in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


# --------------------------------------------------------------------------- #
# 标签绘制（有 Pillow 就用，没有就跳过）
# --------------------------------------------------------------------------- #

def _try_load_font(size: int):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for path in _CJK_FONTS:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def _draw_labels(png: bytes, width: int, height: int, lines: list[tuple[str, int]],
                 color) -> bytes:
    """在已编码的 PNG 上叠加文字；Pillow 不可用时原样返回。"""
    try:
        import io

        from PIL import Image, ImageDraw
    except ImportError:
        return png

    image = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(image)
    fonts = [_try_load_font(size) for _, size in lines]
    if any(font is None for font in fonts):
        return png
    total = sum(size for _, size in lines) + 14 * (len(lines) - 1)
    y = (height - total) // 2
    ink = (250, 250, 250) if _relative_luminance(color) < 0.5 else (25, 25, 25)
    for (text, size), font in zip(lines, fonts):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (box[2] - box[0])) // 2 - box[0], y - box[1]),
                  text, font=font, fill=ink,
                  stroke_width=max(1, size // 24), stroke_fill=_shade(color, 0.35))
        y += size + 14
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 单个占位图
# --------------------------------------------------------------------------- #

def placeholder_png(width: int, height: int, color, lines: list[tuple[str, int]],
                    *, role: str = "") -> bytes:
    """生成一张占位图。``lines`` 是 ``[(文本, 字号), ...]``，Pillow 不可用时忽略。"""
    canvas = _canvas(width, height, color)

    # 斜纹，让它在游戏里一眼就是占位图
    stripe = _shade(color, 0.88)
    band = max(6, height // 40)
    for index, x in enumerate(range(-height, width, band * 2)):
        _fill_rect(canvas, width, height, x, 0, x + band, height, stripe)

    # 边框
    edge = max(2, min(width, height) // 100)
    border = _shade(color, 0.45)
    _fill_rect(canvas, width, height, 0, 0, width, edge, border)
    _fill_rect(canvas, width, height, 0, height - edge, width, height, border)
    _fill_rect(canvas, width, height, 0, 0, edge, height, border)
    _fill_rect(canvas, width, height, width - edge, 0, width, height, border)

    png = _encode_png(width, height, bytes(canvas))
    if lines:
        png = _draw_labels(png, width, height, lines, color)
    return png


def _label_lines(kind: str, title: str, subtitle: str, width: int, height: int):
    base = max(16, min(96, min(width, height) // 8))
    lines = [(title, base)]
    if subtitle:
        lines.append((subtitle, max(12, int(base * 0.58))))
    lines.append((kind, max(11, int(base * 0.45))))
    return lines


# --------------------------------------------------------------------------- #
# 批量生成
# --------------------------------------------------------------------------- #

def _write(path: Path, data: bytes, force: bool, written: list[Path],
           skipped: list[Path], dry_run: bool) -> None:
    if path.exists() and not force:
        skipped.append(path)
        return
    written.append(path)
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def generate_for_document(document: dict, save_dir, *, force: bool = False,
                          dry_run: bool = False, only: str = "all", palette: dict = None):
    """按规范文档生成占位素材。

    ``palette`` 可覆盖自动配色的结果，键为 ``"char:<id>"`` / ``"bg:<相对路径>"``，
    值为 ``"#RRGGBB"`` 或 ``[r, g, b]`` —— 拿到角色官方配色后填进来，
    占位图就会用接近正式美术的颜色，试跑时更容易看出效果。

    返回 ``(written, skipped)`` 两个路径列表。``only`` ∈ ``all|portraits|heads|backgrounds``。
    """
    from .jsonfmt import character_from_doc

    palette = {k: parse_color(v) for k, v in (palette or {}).items()}
    save_dir = Path(save_dir)
    kind = document.get("kind")
    if kind == "project":
        characters = [character_from_doc(item) for item in document.get("characters", [])]
        stories = document.get("stories", [])
    elif kind == "character":
        characters = [character_from_doc(document)]
        stories = []
    elif kind == "story":
        characters = []
        stories = [document]
    else:
        raise LcdeError("不支持的 kind: %r" % kind)

    written: list[Path] = []
    skipped: list[Path] = []

    for character in characters:
        color = palette.get("char:" + character.char_id) or color_for("char:" + character.char_id)
        char_dir = save_dir / "Character" / character.char_id
        for index, portrait in enumerate(character.portraits):
            if only in ("all", "portraits"):
                name = portrait.image or ("%02d.png" % (index + 1))
                png = placeholder_png(
                    *PORTRAIT_SIZE, color,
                    _label_lines("立绘 %d" % index, character.name or character.char_id,
                                 portrait.image or "", *PORTRAIT_SIZE),
                )
                _write(char_dir / name, png, force, written, skipped, dry_run)
        if only in ("all", "heads"):
            png = placeholder_png(
                *HEAD_SIZE, color,
                _label_lines("头像", character.name or character.char_id, "", *HEAD_SIZE),
            )
            parts = win_path(character.char_id).parts
            heads = save_dir / "Character" / "Heads"
            target_dir = heads.joinpath(*parts[:-1]) if len(parts) > 1 else heads
            _write(target_dir / (parts[-1] + IMAGE_EXTENSIONS[0]), png,
                   force, written, skipped, dry_run)

    for story in stories:
        for entry in story.get("backgrounds", []):
            if only not in ("all", "backgrounds"):
                continue
            rel = str(entry).replace("/", "\\")
            path = save_dir / "BG" / rel
            color = (palette.get("bg:" + rel)
                     or palette.get("bg:" + win_path(rel).name)
                     or color_for("bg:" + rel, saturation=0.30, lightness=0.30))
            label = win_path(rel).stem
            png = placeholder_png(
                *BACKGROUND_SIZE, color,
                _label_lines("背景", label, rel, *BACKGROUND_SIZE),
            )
            _write(path, png, force, written, skipped, dry_run)

    return written, skipped


def suggest_rects(images: list[str], dimensions=None) -> list:
    """给一组图片名算出编辑器公式下的 ``rect``（供手工写 JSON 时参考）。"""
    width, height = dimensions or PORTRAIT_SIZE
    return [editor_rect(width, height) for _ in images]
