"""OCR 校对 / 图像 ROI 标注工具。

当前提供图像浏览：打开文件夹或导入图像、缩放、上下张切换。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["main", "__version__"]


def main() -> None:
    """``ocr-proofread`` 命令的入口。"""
    from .app import main as run

    raise SystemExit(run())
