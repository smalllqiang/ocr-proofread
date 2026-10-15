"""OCR：模型注册表、图块裁剪与后台识别任务。

几条约定：

* **引擎按需加载**：:class:`MangaOcrEngine` 在第一次 :meth:`~MangaOcrEngine.recognize`
  时才 import ``manga_ocr`` 并加载模型（约 400 MB，首次运行会从 HuggingFace 下载）。
  没装依赖或加载失败时抛 :class:`OcrError`，消息可以直接显示给用户，程序照常启动。
* **引擎可替换**：界面只依赖 :class:`OcrEngine` 协议，按名字从 :data:`OCR_MODELS`
  取引擎。再加一个模型只要在这里登记一条，其余代码不用动。
* **识别在子线程**：:class:`OcrTask` 在 ``QThread`` 里逐个处理图块，结果先攒在任务
  对象上再由界面线程读走；本模块只用 QtCore / QtGui，不碰任何控件。

manga-ocr 接受路径或 PIL 图像、返回识别文本，多行 / 竖排 / 带注音的文字一次搞定，
所以一个 ROI 对应一次调用。
"""

from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from PySide6.QtCore import QBuffer, QIODevice, QObject, Signal
from PySide6.QtGui import QImage

from .annotations import Roi

#: manga-ocr 模型的内部标识（也写进 QSettings）
MANGA_OCR = "manga-ocr"

#: 默认模型
DEFAULT_MODEL = MANGA_OCR


class OcrError(RuntimeError):
    """识别失败：没装依赖、模型加载不出来、图块不合法等。消息可直接给用户看。"""


class OcrEngine(Protocol):
    """一个 OCR 模型的最小接口：把一张图块变成文本。"""

    #: 模型标识，与 :data:`OCR_MODELS` 里的名字一致
    name: str

    def recognize(self, image: QImage) -> str:
        """识别一个图块（图像像素坐标裁剪出来的 :class:`QImage`）。

        识别不出内容时返回空串（模型总会"猜"出点东西），真的失败才抛 :class:`OcrError`。
        """
        ...


@dataclass(frozen=True)
class OcrModel:
    """一个可选的 OCR 模型：内部标识、菜单文字与构造函数。"""

    name: str
    label: str
    factory: Callable[[], OcrEngine]


class MangaOcrEngine:
    """manga-ocr 封装：日文漫画 / 印刷体识别，模型在首次识别时才加载。

    manga-ocr 自己会在 CUDA / MPS / CPU 里挑一个（``force_cpu=True`` 可以强制 CPU）。
    加载失败的 :class:`OcrError` 会被记住，批处理时不会为每个 ROI 重试一遍。
    """

    name = MANGA_OCR

    def __init__(self, *, force_cpu: bool = False) -> None:
        self._force_cpu = force_cpu
        self._model = None
        self._load_error: OcrError | None = None
        #: 取消识别时旧线程可能还在加载模型，加锁避免并发加载出两份
        self._load_lock = threading.Lock()

    def recognize(self, image: QImage) -> str:
        if image.isNull():
            raise OcrError("待识别的图块是空的")
        model = self._ensure_model()
        try:
            return str(model(qimage_to_pil(image))).strip()
        except OcrError:
            raise
        except Exception as exc:  # 第三方库的异常不往外裸奔，统一成 OcrError
            raise OcrError(f"manga-ocr 识别失败：{exc}") from exc

    def _ensure_model(self):
        with self._load_lock:
            if self._load_error is not None:
                raise self._load_error
            if self._model is None:
                try:
                    from manga_ocr import MangaOcr
                except ImportError as exc:
                    self._load_error = OcrError(
                        "没有安装 manga-ocr，无法识别；请先执行 uv sync 安装依赖"
                    )
                    raise self._load_error from exc
                try:
                    self._model = MangaOcr(force_cpu=self._force_cpu)
                except Exception as exc:
                    self._load_error = OcrError(f"加载 manga-ocr 模型失败：{exc}")
                    raise self._load_error from exc
            return self._model


#: 可选的 OCR 模型清单，界面的"识别模型"菜单直接照着它生成
OCR_MODELS: tuple[OcrModel, ...] = (
    OcrModel(
        name=MANGA_OCR,
        label="manga-ocr（日文 · 漫画 / 印刷体）",
        factory=MangaOcrEngine,
    ),
)


def create_engine(model: str) -> OcrEngine:
    """按名字造一个引擎；名字不在 :data:`OCR_MODELS` 里时抛 :class:`OcrError`。"""
    for entry in OCR_MODELS:
        if entry.name == model:
            return entry.factory()
    raise OcrError(f"未知的 OCR 模型：{model}")


def model_label(model: str) -> str:
    """取菜单上显示的模型名字；未知名字原样返回。"""
    for entry in OCR_MODELS:
        if entry.name == model:
            return entry.label
    return model


def is_known_model(model: str) -> bool:
    return any(entry.name == model for entry in OCR_MODELS)


def crop_roi(image: QImage, roi: Roi) -> QImage:
    """按 ROI 的几何裁出待识别图块。

    边界向外取整再夹进图像，保证 ROI 覆盖到的像素一个不少；ROI 完全落在图像外时
    抛 :class:`OcrError`。
    """
    left = max(0, math.floor(roi.x))
    top = max(0, math.floor(roi.y))
    right = min(image.width(), math.ceil(roi.x + roi.width))
    bottom = min(image.height(), math.ceil(roi.y + roi.height))
    if right <= left or bottom <= top:
        raise OcrError(f"标注不在图像范围内：{roi.as_rect()}")
    return image.copy(left, top, right - left, bottom - top)


def qimage_to_pil(image: QImage):
    """QImage → PIL 图像；manga-ocr 只收路径或 PIL 图像。

    先编码成 PNG 再交给 Pillow，省得自己处理不同 QImage 格式的扫描行对齐。
    """
    from PIL import Image

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise OcrError("无法把待识别的图块编码成 PNG")
    return Image.open(io.BytesIO(bytes(buffer.data())))


class OcrTask(QObject):
    """在子线程里按顺序识别一批图块。

    结果攒在 :attr:`results` / :attr:`errors` 上（序号 → 文本 / 错误消息），信号只用来
    刷新进度和收尾。这样即使中途被取消，已经识别出来的部分也不会丢——界面线程等
    ``QThread.wait()`` 返回后再读这两个字典即可（写入在前、信号在后）。
    """

    #: 已完成的图块数 / 总数
    progressed = Signal(int, int)

    #: 任务结束，参数是"是否被取消"
    finished = Signal(bool)

    def __init__(
        self,
        engine: OcrEngine,
        crops: list[QImage],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._crops = crops
        self._cancelled = False
        #: 序号 → 识别文本
        self.results: dict[int, str] = {}
        #: 序号 → 错误消息
        self.errors: dict[int, str] = {}

    def cancel(self) -> None:
        """请求取消；当前这一张图块识别完就停（推理本身不可中断）。"""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        """槽：由子线程的事件循环调用，不要在界面线程里直接调。"""
        total = len(self._crops)
        for index, crop in enumerate(self._crops):
            if self._cancelled:
                break
            try:
                self.results[index] = self._engine.recognize(crop)
            except OcrError as exc:
                self.errors[index] = str(exc)
            except Exception as exc:  # noqa: BLE001 (引擎是第三方代码，异常不能穿透到线程外)
                self.errors[index] = f"{type(exc).__name__}: {exc}"
            self.progressed.emit(index + 1, total)
        self.finished.emit(self._cancelled)
