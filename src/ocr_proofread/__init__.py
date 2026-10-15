"""OCR 校对 / 图像 ROI 标注工具。

打开图像文件夹、缩放浏览、在图上框出文字区域（ROI），并对选中的或全部的 ROI
跑 OCR（manga-ocr），识别结果随标注一起存进每张图的 ``.roi.json`` 边车文件。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["main", "__version__"]


def main() -> None:
    """``ocr-proofread`` 命令的入口。"""
    from .app import main as run

    raise SystemExit(run())
