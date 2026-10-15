"""导出：把这一批图像的全部 ROI 文本拼成纯文本文件。

这一层不带任何界面控件，只做两件事：按固定格式拼字符串、原子地写文件。
"导出哪些图、按什么顺序" 由调用方（``MainWindow``）决定：图像顺序就是工具里的
图像顺序，ROI 顺序就是排序号顺序，没有 ROI（连边车文件都没有）的图整张跳过。

导出格式是"文件名一段、每个 ROI 文本一段"，每段后面空一行::

    <图像文件名>

    <第一个 ROI 的文本>

    <第二个 ROI 的文本>

    <下一张图的文件名>

    ...

文本原样输出：没识别过、没填过的就是空白（只有空行），不写占位符，也不写序号与坐标。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

#: 导出文件的后缀，以及保存对话框的过滤器
EXPORT_SUFFIX = ".txt"
EXPORT_NAME_FILTER = "文本文件 (*.txt)"


def default_export_path(folder: str | Path) -> Path:
    """默认导出位置：该文件夹里的 ``<文件夹名>.txt``。

    打开文件夹时导出文件就落在被浏览的文件夹里，重新导出会直接覆盖它，不会在
    图像旁边堆出一串名字。
    """
    folder = Path(folder)
    return folder / f"{folder.name}{EXPORT_SUFFIX}"


def build_text(pages: Iterable[tuple[str, Iterable[str]]]) -> str:
    """把（图像文件名, 每个 ROI 的文本）拼成导出的纯文本。

    ``pages`` 的顺序就是导出的顺序；没有 ROI 的图应当由调用方先剔除，这里不判断。
    """
    chunks: list[str] = []
    for name, texts in pages:
        chunks.append(f"{name}\n\n")
        for text in texts:
            chunks.append(f"{text}\n\n")
    return "".join(chunks)


def write_export(path: str | Path, text: str) -> None:
    """写导出文件（先写临时文件再原子替换）；IO 失败抛 :class:`OSError`。

    和标注的边车文件一样原子替换：写到一半被打断也不会留下半份导出结果。
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
