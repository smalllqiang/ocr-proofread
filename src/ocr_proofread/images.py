"""图像文件的收集与加载。

这一层不依赖任何界面控件，方便后续在标注流程里复用（例如后台预加载下一张图）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from PySide6.QtGui import QImage, QImageReader

#: 视为图像的扩展名（小写，含点）
IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".bmp",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".ppm",
        ".pgm",
        ".pbm",
        ".xpm",
    }
)

#: 给 QFileDialog 用的过滤器字符串
IMAGE_NAME_FILTER: str = (
    "图像文件 (" + " ".join(f"*{suffix}" for suffix in sorted(IMAGE_SUFFIXES)) + ");;所有文件 (*)"
)

_NUMBER_RE = re.compile(r"(\d+)")


def is_image_file(path: str | Path) -> bool:
    """判断路径是否是受支持的图像文件（只看扩展名，不检查内容）。"""
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def natural_key(text: str) -> tuple[tuple[int, object], ...]:
    """自然排序键：让 ``img2.png`` 排在 ``img10.png`` 前面。

    每个片段被包成 ``(0, 文本)`` 或 ``(1, 数字)``，保证比较时不会出现
    字符串和数字互比导致的 ``TypeError``。
    """
    parts: list[tuple[int, object]] = []
    for chunk in _NUMBER_RE.split(text):
        if not chunk:
            continue
        parts.append((1, int(chunk)) if chunk.isdigit() else (0, chunk.casefold()))
    return tuple(parts)


def collect_images(
    folder: str | Path,
    *,
    recursive: bool = False,
    suffixes: Iterable[str] = IMAGE_SUFFIXES,
) -> list[Path]:
    """扫描文件夹里的图像文件，按自然顺序返回。

    忽略隐藏文件和隐藏目录，返回绝对路径。``recursive=True`` 时递归子目录。
    """
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []

    allowed = {suffix.lower() for suffix in suffixes}
    found: list[Path] = []
    if recursive:
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            # 只跳过 root 内部的隐藏文件/目录；root 自身的父目录是否隐藏与本次扫描无关
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_file() and path.suffix.lower() in allowed:
                found.append(path)
        found.sort(key=lambda p: tuple(natural_key(part) for part in p.relative_to(root).parts))
    else:
        for path in root.iterdir():
            if path.name.startswith("."):
                continue
            if path.is_file() and path.suffix.lower() in allowed:
                found.append(path)
        found.sort(key=lambda p: natural_key(p.name))

    return [path.resolve() for path in found]


def load_qimage(path: str | Path) -> QImage:
    """读取图像，自动应用 EXIF 方向（手机拍的照片不会躺倒）。

    失败时抛出 :class:`ValueError`，消息来自 QImageReader。
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "无法解码该图像")
    return image
