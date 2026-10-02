"""角色与剧本的文件模型：发现存档目录、读取、写出。

路径约定（与引擎一致，见 read.md 第 2、3、4 节）：

* ``<save_dir>/Character/<角色目录>/<名字>.ch``  角色配置
* ``<save_dir>/Character/<角色目录>/Rect``       立绘裁剪数据
* ``<save_dir>/Character/Heads/<角色目录>.<ext>`` 头像
* ``<save_dir>/BG/<任意相对路径>``               背景图
* ``<save_dir>/Dialog/<标题>``                   剧本（无扩展名）
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .binary import BinReader, BinWriter
from .commands import decode_command, encode_command
from .errors import FormatError, ValidationError

__all__ = [
    "IMAGE_EXTENSIONS",
    "BACKUP_DIR",
    "default_save_dir",
    "scan_portrait_images",
    "find_ch_file",
    "find_head",
    "win_path",
    "Project",
    "Portrait",
    "Character",
    "Story",
]

#: 引擎认可的图片扩展名（顺序即头像扩展名尝试顺序，见 read.md 3.5）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")

DEFAULT_CH_NAME = "Config.ch"
RECT_NAME = "Rect"

# 引擎的默认值（Save.LoadCharacter 的容错分支与 CreateCharacterUI 的新建模板）
DEFAULT_NAME = "???"
DEFAULT_CAMP = "???"
DEFAULT_NAME_COLOR = "DDB994"
DEFAULT_CAMP_COLOR = "76471A"
DEFAULT_EXPRESS = (0.0, 300.0)
DEFAULT_RECT = (0.0, 100.0, 90.0, 300.0)


def default_save_dir() -> Path:
    """``Application.persistentDataPath``：``%USERPROFILE%\\AppData\\LocalLow\\Soalin\\LCDE``。"""
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(base) / "AppData" / "LocalLow" / "Soalin" / "LCDE"


def _norm_relative(rel: str) -> str:
    """把剧本里存的相对路径统一成反斜杠形式（Windows / Path.GetRelativePath 的写法）。"""
    return rel.replace("/", "\\").strip("\\")


def win_path(rel: str) -> PureWindowsPath:
    """把游戏内的相对路径（反斜杠）当成 Windows 路径解析。

    用 ``PureWindowsPath`` 而不是 ``Path``：后者在 Windows 之外不会把 ``\\`` 当分隔符，
    ``.name`` / ``.stem`` / ``.suffix`` 都会算错。
    """
    return PureWindowsPath(rel.replace("/", "\\"))


def _split_reference(reference: str) -> tuple[str, str | None]:
    """把 ``Hero`` 或 ``Hero\\Config.ch`` 拆成 ``(角色目录, .ch 文件名或 None)``。"""
    normalized = _norm_relative(reference)
    if normalized.lower().endswith(".ch"):
        parent = normalized.rsplit("\\", 1)[0] if "\\" in normalized else ""
        filename = normalized.rsplit("\\", 1)[-1]
        return parent, filename
    return normalized, None


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def scan_portrait_images(directory: Path) -> list[str]:
    """按**运行时引擎看到的顺序**列出立绘，返回相对角色目录的路径。

    引擎（``Save.LoadCharacter``）：

    .. code-block:: csharp

        Directory.EnumerateFiles(d, "*.*", SearchOption.AllDirectories)
            .Where(file => allowedImageExtensions.Contains(Path.GetExtension(file).ToLower()))
            .OrderBy(file => file)

    —— **递归**（子目录里的图片也算）且**按完整路径字符串排序**，
    所以顺序是确定的字典序，不是「文件系统随便给的顺序」。
    用 ``01_``、``02_`` 这类定宽数字前缀命名即可稳定控制。

    .. note::

       编辑器路径 ``Save.LoadCharacterEditor`` **没有** ``OrderBy``，
       用的是原始枚举顺序。两者不一致时，编辑器里调好的 ``Rect`` 在运行时会对错图 ——
       这是引擎自身的不一致，所以**务必用数字前缀命名**。
    """
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            full = Path(dirpath) / name
            if full.suffix.lower() in IMAGE_EXTENSIONS:
                found.append(full)
    # 引擎按完整路径排序；同前缀下与按相对路径排序等价
    found.sort(key=lambda p: str(p))
    return [str(p.relative_to(directory)).replace(os.sep, "\\") for p in found]


def find_ch_file(directory: Path) -> Path | None:
    """找出角色目录里的 ``.ch`` 文件：优先 ``Config.ch``，否则取唯一的 ``*.ch``。"""
    preferred = directory / DEFAULT_CH_NAME
    if preferred.is_file():
        return preferred
    candidates = sorted(p for p in directory.glob("*.ch") if p.is_file())
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise FormatError(
            "目录 %s 里有多个 .ch 文件：%s（请保留一个，或命名为 Config.ch）"
            % (directory, ", ".join(p.name for p in candidates))
        )
    return None


def find_head(directory: Path) -> Path | None:
    """按引擎的扩展名顺序找出头像。``directory`` 是 ``Character/Heads`` 下的相对目录。"""
    for ext in IMAGE_EXTENSIONS:
        candidate = directory.with_name(directory.name + ext)
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

@dataclass
class Portrait:
    image: str                                        # 立绘文件名（相对角色目录）
    rect: tuple[float, float, float, float]           # 裁剪 x, y, width, height


@dataclass
class Character:
    char_id: str                                      # 角色目录，相对 Character\，如 "Hero" 或 "A\\Hero"
    name: str = DEFAULT_NAME
    camp: str = DEFAULT_CAMP
    name_color: str = DEFAULT_NAME_COLOR              # 6 位十六进制，不含 '#'
    camp_color: str = DEFAULT_CAMP_COLOR
    express: tuple[float, float] = DEFAULT_EXPRESS
    portraits: list[Portrait] = field(default_factory=list)
    head: str | None = None                           # 头像文件名，仅作信息记录
    #: ``Rect`` 文件里的裁剪矩形个数。与 ``len(portraits)`` 不同：
    #: 前者来自文件长度，后者来自目录里的图片数量 —— 校验器会比对二者。
    rect_count: int = 0

    #: 由加载过程填充，不参与 JSON 序列化
    ch_file: Path | None = None
    directory: Path | None = None
    found: bool = True                                # False 表示配置文件不存在（引擎会容错）

    # -- 便捷属性 ---------------------------------------------------------- #
    @property
    def ch_relative_path(self) -> str:
        """剧本的 ``characters`` 里应填的字符串，如 ``Hero\\Config.ch``。"""
        filename = self.ch_file.name if self.ch_file else DEFAULT_CH_NAME
        return _norm_relative("%s\\%s" % (self.char_id, filename)) if self.char_id else filename

    @property
    def head_relative(self) -> str:
        return self.char_id

    def to_dict(self) -> dict:
        return {
            "id": self.char_id,
            "name": self.name,
            "camp": self.camp,
            "nameColor": self.name_color,
            "campColor": self.camp_color,
            "express": list(self.express),
            "portraits": [{"image": p.image, "rect": list(p.rect)} for p in self.portraits],
            "head": self.head,
        }

    # -- 加载 -------------------------------------------------------------- #
    @classmethod
    def load(cls, save_dir: Path, reference: str) -> "Character":
        """``reference`` 可以是角色目录（``Hero``）或 ``.ch`` 文件路径（``Hero\\Config.ch``）。

        两者都接受，因为剧本里存的是 ``.ch`` 文件路径，而工具的其他地方用目录。
        """
        char_id, ch_hint = _split_reference(reference)
        directory = Path(save_dir) / "Character" / char_id
        ch_file = (directory / ch_hint) if ch_hint else find_ch_file(directory)
        if ch_file is not None and not ch_file.is_file():
            ch_file = find_ch_file(directory)

        if ch_file is None:
            head = find_head(Path(save_dir) / "Character" / "Heads" / char_id)
            return cls(
                char_id=char_id,
                name="读取失败",
                camp="读取失败",
                portraits=[Portrait("", DEFAULT_RECT)],
                head=head.name if head else None,
                directory=directory,
                found=False,
            )

        text = ch_file.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        while len(lines) < 4:
            lines.append("")
        # 注意：**不**去掉开头的 '#'。引擎会自己补 '#'，文件里若已带 '#' 就会解析失败；
        # 这里保留原样是为了让校验器（规则 C5）能报出来。
        name, camp = lines[0], lines[1]
        name_color = lines[2].strip()
        camp_color = lines[3].strip()

        rect_file = directory / RECT_NAME
        express, rects = _read_rect(rect_file)
        images = scan_portrait_images(directory)
        portraits = [
            Portrait(image, rects[i] if i < len(rects) else DEFAULT_RECT)
            for i, image in enumerate(images)
        ]

        head = find_head(Path(save_dir) / "Character" / "Heads" / char_id)
        return cls(
            char_id=char_id,
            name=name,
            camp=camp,
            name_color=name_color,
            camp_color=camp_color,
            express=express,
            portraits=portraits,
            head=head.name if head else None,
            rect_count=len(rects),
            ch_file=ch_file,
            directory=directory,
        )

    # -- 写出 -------------------------------------------------------------- #
    def save(self, save_dir: Path, *, force: bool = False) -> list[Path]:
        """写出 ``.ch`` 与 ``Rect``。返回写出的文件路径列表。

        已存在的文件在 ``force=False`` 时会报错以避免误覆盖。
        """
        if not self.char_id:
            raise ValidationError("角色缺少 id")
        directory = Path(save_dir) / "Character" / self.char_id
        ch_file = self.ch_file or (directory / DEFAULT_CH_NAME)
        rect_file = directory / RECT_NAME

        if not force:
            for path in (ch_file, rect_file):
                if path.exists():
                    raise ValidationError(
                        "%s 已存在；加 --force 覆盖（会自动备份为 *.bak-<时间戳>）" % path
                    )

        directory.mkdir(parents=True, exist_ok=True)

        body = "\r\n".join([
            self.name,
            self.camp,
            self.name_color.lstrip("#").upper(),
            self.camp_color.lstrip("#").upper(),
        ]) + "\r\n"
        _backup(ch_file)
        ch_file.write_bytes(body.encode("utf-8"))

        writer = BinWriter()
        writer.f32(self.express[0]).f32(self.express[1])
        for portrait in self.portraits:
            for value in portrait.rect:
                writer.f32(value)
        _backup(rect_file)
        rect_file.write_bytes(writer.bytes())

        self.ch_file = ch_file
        self.directory = directory
        return [ch_file, rect_file]


def _read_rect(path: Path) -> tuple[tuple[float, float], list[tuple[float, float, float, float]]]:
    """读取 ``Rect``：2 个 float 的 express + N×4 个 float 的裁剪矩形。"""
    if not path.is_file():
        return DEFAULT_EXPRESS, []
    raw = path.read_bytes()
    if len(raw) < 8 or (len(raw) - 8) % 16:
        raise FormatError(
            "%s 长度 %d 不是 8 + 16N，无法解析（应恰好为 2 个 express float + N×4 个矩形 float）"
            % (path, len(raw))
        )
    reader = BinReader(raw)
    express = (reader.f32(), reader.f32())
    rects = []
    while reader.remaining >= 16:
        rects.append((reader.f32(), reader.f32(), reader.f32(), reader.f32()))
    return express, rects


#: 覆盖前的备份目录名。必须是**子目录**：
#: 引擎用 ``Directory.EnumerateFiles(<存档>\Dialog)``（非递归）列出全部剧本，
#: 且把该目录里的**任何**文件都当成一个剧本条目 —— 备份放在旁边会变成重复条目。
BACKUP_DIR = ".lcde-backup"


def _backup(path: Path) -> Path | None:
    """把即将被覆盖的文件备份到同级的 ``.lcde-backup/`` 子目录。"""
    if not path.exists():
        return None
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = path.parent / BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / ("%s.bak-%s" % (path.name, stamp))
    shutil.copy2(path, backup)
    return backup


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #

@dataclass
class Story:
    title: str
    backgrounds: list[str] = field(default_factory=list)     # 相对 BG\，含扩展名
    characters: list[str] = field(default_factory=list)      # 相对 Character\，含 .ch
    commands: list[dict] = field(default_factory=list)
    path: Path | None = None

    # -- 加载 -------------------------------------------------------------- #
    @classmethod
    def load(cls, save_dir: Path, title: str) -> "Story":
        path = Path(save_dir) / "Dialog" / title
        if not path.is_file():
            raise FormatError("剧本不存在: %s" % path)
        return cls.from_bytes(path.read_bytes(), title=title, path=path)

    @classmethod
    def from_bytes(cls, raw: bytes, *, title: str = "?", path: Path | None = None) -> "Story":
        reader = BinReader(raw)
        background_count = reader.u16()
        backgrounds = [reader.str() for _ in range(background_count)]
        character_count = reader.u16()
        characters = [reader.str() for _ in range(character_count)]
        command_count = reader.u16()
        commands = [decode_command(reader) for _ in range(command_count)]
        if reader.remaining:
            raise FormatError(
                "解析完 %d 条命令后还剩 %d 字节，文件结构不匹配"
                % (command_count, reader.remaining)
            )
        return cls(title=title, backgrounds=backgrounds, characters=characters,
                   commands=commands, path=path)

    # -- 导出 -------------------------------------------------------------- #
    def to_bytes(self) -> bytes:
        if len(self.backgrounds) > 0xFFFF or len(self.characters) > 0xFFFF:
            raise ValidationError("背景或角色数量超过 65535")
        if len(self.commands) > 0xFFFF:
            raise ValidationError("命令数量超过 65535")
        writer = BinWriter()
        writer.u16(len(self.backgrounds))
        for value in self.backgrounds:
            writer.str(value)
        writer.u16(len(self.characters))
        for value in self.characters:
            writer.str(value)
        writer.u16(len(self.commands))
        for command in self.commands:
            encode_command(writer, command)
        return writer.bytes()

    def save(self, save_dir: Path, *, force: bool = False, title: str | None = None) -> Path:
        title = title or self.title
        if not title:
            raise ValidationError("剧本缺少 title")
        directory = Path(save_dir) / "Dialog"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / title
        if path.exists() and not force:
            raise ValidationError("%s 已存在；加 --force 覆盖（会自动备份）" % path)
        if force:
            _backup(path)
        path.write_bytes(self.to_bytes())
        self.path = path
        self.title = title
        return path

    def iter_commands(self, path: tuple[int, ...] = ()):
        """深度优先遍历所有命令（含组命令的子命令），产出 ``(路径, 命令)``。"""
        for index, command in enumerate(self.commands):
            here = path + (index,)
            yield here, command
            if command.get("type") in ("Queue", "Synchronization"):
                for sub_path, sub in _iter_nested(command, here):
                    yield sub_path, sub


def _iter_nested(command: dict, path: tuple[int, ...]):
    for index, sub in enumerate(command.get("commands") or []):
        here = path + (index,)
        yield here, sub
        if sub.get("type") in ("Queue", "Synchronization"):
            for deeper in _iter_nested(sub, here):
                yield deeper


# --------------------------------------------------------------------------- #
# 存档目录
# --------------------------------------------------------------------------- #

class Project:
    """一个存档目录（``Application.persistentDataPath``）。"""

    def __init__(self, save_dir: Path) -> None:
        self.save_dir = Path(save_dir)

    # -- 目录 -------------------------------------------------------------- #
    @property
    def character_dir(self) -> Path:
        return self.save_dir / "Character"

    @property
    def bg_dir(self) -> Path:
        return self.save_dir / "BG"

    @property
    def dialog_dir(self) -> Path:
        return self.save_dir / "Dialog"

    @property
    def debug_dir(self) -> Path:
        return self.save_dir / "Debug"

    @property
    def settings_file(self) -> Path:
        return self.save_dir / "Setting.json"

    def exists(self) -> bool:
        return self.save_dir.is_dir()

    # -- 枚举 -------------------------------------------------------------- #
    def character_ids(self) -> list[str]:
        """列出角色目录（含 ``.ch`` 的子目录）的相对路径。"""
        root = self.character_dir
        if not root.is_dir():
            return []
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if here.name == "Heads" and here.parent == root:
                dirnames[:] = []
                continue
            if any(name.lower().endswith(".ch") and (here / name).is_file() for name in filenames):
                found.append(str(here.relative_to(root)).replace("/", "\\"))
        return sorted(found)

    def story_titles(self) -> list[str]:
        root = self.dialog_dir
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_file())

    def background_paths(self) -> list[str]:
        root = self.bg_dir
        if not root.is_dir():
            return []
        return sorted(
            str(p.relative_to(root)).replace("/", "\\")
            for p in root.rglob("*") if p.is_file()
        )

    # -- 批量 -------------------------------------------------------------- #
    def load_character(self, char_id: str) -> Character:
        return Character.load(self.save_dir, char_id)

    def load_story(self, title: str) -> Story:
        return Story.load(self.save_dir, title)

    def load_all_characters(self) -> list[Character]:
        return [Character.load(self.save_dir, cid) for cid in self.character_ids()]

    def load_all_stories(self) -> list[Story]:
        return [Story.load(self.save_dir, title) for title in self.story_titles()]

    def read_settings(self) -> dict | None:
        path = self.settings_file
        if not path.is_file():
            return None
        import json

        return json.loads(path.read_text(encoding="utf-8-sig"))
