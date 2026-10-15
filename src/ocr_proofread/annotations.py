"""ROI 标注的数据结构与持久化。

这一层不依赖任何界面控件：``Roi`` 是四个浮点数加一段可选的识别文本、一个排序号，
``RoiStore`` 负责内存缓存与 JSON 边车文件读写。存储按"一张图一份"组织，文件放在图像
旁边，命名为 ``<图像文件名>.roi.json``（``page1.png`` 对应 ``page1.png.roi.json``），
这样复制或移动图像文件夹时标注跟着走，不同文件夹里的同名图也不会互相覆盖。

坐标系与 :mod:`ocr_proofread.image_view` 一致：单位是图像像素，原点在图像左上角。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QRectF

#: 边车文件名后缀，拼在完整图像文件名之后
SIDECAR_SUFFIX = ".roi.json"

#: 标注文件格式版本，便于以后升级。
#: v1 只有几何；v2 起每条记录可以带可选的 ``text``（OCR 识别结果）；v3 起再带可选的
#: ``index``（这张图上的排序号，从 1 开始）。读盘不校验版本：缺 ``text`` 的 v1 文件
#: text 视为空串，缺 ``index`` 的 v1/v2 文件按"还没排过号"处理，由上层重新编号。
FORMAT_VERSION = 3


def _coerce_index(value: object) -> int:
    """把写盘或手改过的 ``index`` 收敛成 >= 0 的整数；认不出来就当没排过号（0）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and value.is_integer():
        return max(int(value), 0)
    return 0


@dataclass(frozen=True)
class Roi:
    """一个矩形标注，单位是图像像素，原点在图像左上角。

    ``text`` 是这个区域最近一次的 OCR 结果，空串表示还没识别过。文本不参与几何运算，
    只跟着标注一起存取；等于说"识别结果挂在 ROI 上"。

    ``index`` 是这个框在**本张图上的顺序**（1、2、3…）：初始就是框的创建顺序，之后可以
    在排序模式里按点击顺序重排。0 表示"还没有排过号"，只可能出现在读进来的老文件里，
    上层会按文件里的先后重新编号。
    """

    x: float
    y: float
    width: float
    height: float
    text: str = ""
    index: int = 0

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError("Roi 的宽高不能为负数")

    def as_rect(self) -> QRectF:
        """转成 Qt 矩形（仍是图像像素坐标）。"""
        return QRectF(self.x, self.y, self.width, self.height)

    def with_text(self, text: str) -> Roi:
        """返回一个只换了识别文本的副本（:class:`Roi` 本身不可变）。"""
        return Roi(self.x, self.y, self.width, self.height, text, self.index)

    @classmethod
    def from_rect(cls, rect: QRectF, text: str = "", index: int = 0) -> Roi:
        """从 Qt 矩形构造，负宽高会被规整。"""
        normalized = QRectF(rect).normalized()
        return cls(
            normalized.x(),
            normalized.y(),
            normalized.width(),
            normalized.height(),
            text,
            index,
        )

    def to_dict(self) -> dict[str, float | str | int]:
        """转成写盘用的字典；没识别过的标注不写 ``text``，没排过号的不写 ``index``。"""
        data: dict[str, float | str | int] = {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "w": round(self.width, 2),
            "h": round(self.height, 2),
        }
        if self.text:
            data["text"] = self.text
        if self.index > 0:
            data["index"] = self.index
        return data

    @classmethod
    def from_dict(cls, data: object) -> Roi | None:
        """解析一条记录；字段缺失、类型不对或退化的矩形返回 ``None``。"""
        if not isinstance(data, dict):
            return None
        try:
            roi = cls(
                float(data["x"]),
                float(data["y"]),
                float(data["w"]),
                float(data["h"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if roi.width <= 0 or roi.height <= 0:
            return None
        text = data.get("text", "")
        return cls(
            roi.x,
            roi.y,
            roi.width,
            roi.height,
            text if isinstance(text, str) else "",
            _coerce_index(data.get("index", 0)),
        )


def sidecar_path(image_path: str | Path) -> Path:
    """返回某张图像对应的标注文件路径。"""
    path = Path(image_path)
    return path.with_name(path.name + SIDECAR_SUFFIX)


def load_rois(image_path: str | Path) -> list[Roi]:
    """读取边车文件；文件不存在、损坏或格式不符时返回空列表。

    单个坏记录会被跳过而不是让整份文件作废——标注数据是人工产物，能救回多少是多少。
    """
    path = sidecar_path(image_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("annotations")
    if not isinstance(entries, list):
        return []
    rois: list[Roi] = []
    for entry in entries:
        roi = Roi.from_dict(entry)
        if roi is not None:
            rois.append(roi)
    return rois


def save_rois(image_path: str | Path, rois: Iterable[Roi]) -> None:
    """写入边车文件（先写临时文件再原子替换）；``rois`` 为空时删除已有文件。

    空标注不落盘，所以没标过的图不会多出文件。IO 失败会抛出 :class:`OSError`，
    由调用方决定怎么提示。
    """
    path = sidecar_path(image_path)
    entries = [roi.to_dict() for roi in rois]
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    payload = {
        "version": FORMAT_VERSION,
        "image": _image_name(path),
        "annotations": entries,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _image_name(sidecar: Path) -> str:
    """从边车文件名反推图像文件名，只用于写进文件方便人工阅读。"""
    return sidecar.name.removesuffix(SIDECAR_SUFFIX)


class RoiStore:
    """每张图一份标注：内存缓存 + 边车文件。

    读取是惰性的——``get()`` 第一次访问某张图时读盘并缓存；``set()`` 更新缓存，
    只有内容真的变了才写盘，因此反复切图不会反复碰磁盘。这里的"内容"包括每条标注的
    排序号，所以重排顺序同样会被写进文件。
    """

    def __init__(self) -> None:
        self._rois: dict[Path, list[Roi]] = {}
        self._loaded: set[Path] = set()

    @staticmethod
    def _key(image_path: str | Path) -> Path:
        return Path(image_path).resolve()

    def get(self, image_path: str | Path) -> list[Roi]:
        """取某张图的标注（副本）；首次访问时从磁盘读取。"""
        key = self._key(image_path)
        if key not in self._loaded:
            self._rois[key] = load_rois(key)
            self._loaded.add(key)
        return list(self._rois[key])

    def set(self, image_path: str | Path, rois: Iterable[Roi]) -> bool:
        """更新某张图的标注，内容变化时写盘；返回是否真的写了文件。"""
        key = self._key(image_path)
        if key not in self._loaded:
            self._rois[key] = load_rois(key)
            self._loaded.add(key)

        new = [Roi(r.x, r.y, r.width, r.height, r.text, r.index) for r in rois]
        if new == self._rois[key]:
            return False
        self._rois[key] = new
        save_rois(key, new)
        return True
