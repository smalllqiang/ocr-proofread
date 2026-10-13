"""QGraphicsView 子类：显示单张图像，负责缩放与平移。

后续的 ROI 图元（矩形/多边形/文字框）都会加到 ``self.scene_`` 上，因此这里
刻意不做任何"图元会被清掉"的假设：``set_image()`` 会清空场景，后续做标注时
需要在切图时重新把该图的标注画回去。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

#: 缩放上下限，避免滚轮把图缩到看不见或者撑爆
MIN_SCALE = 0.02
MAX_SCALE = 40.0

#: 鼠标滚轮 / 快捷键单步缩放倍率
ZOOM_STEP = 1.25


class ImageView(QGraphicsView):
    """只做"一张图 + 缩放 + 平移"的视图。

    交互约定：

    * 滚轮 / Ctrl+滚轮 —— 以鼠标位置为锚点缩放
    * Shift+滚轮 —— 水平滚动，Alt+滚轮 —— 垂直滚动
    * 中键拖动 —— 平移画布（左键留给后续画 ROI）
    """

    #: 当前缩放比例，1.0 表示 100%
    zoomChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing
        )
        self.setBackgroundBrush(self.palette().window())
        self.setMouseTracking(True)

        self._item: QGraphicsPixmapItem | None = None
        self._scale = 1.0
        self._auto_fit = True  # 窗口大小变化时是否自动重新适应窗口
        self._panning = False
        self._pan_anchor = QPointF()

    # ------------------------------------------------------------------ 图像

    @property
    def has_image(self) -> bool:
        return self._item is not None

    @property
    def scale_factor(self) -> float:
        return self._scale

    def set_image(self, image: QImage | None) -> None:
        """替换当前显示的图像；传入 ``None`` 表示清空。

        注意：这里会清空整个场景，后续新增的 ROI 图元也会一并被移除。
        """
        self._scene.clear()
        self._item = None
        self._scale = 1.0
        self._panning = False
        self.resetTransform()
        if image is None or image.isNull():
            self._scene.setSceneRect(0, 0, 0, 0)
            self.zoomChanged.emit(self._scale)
            return

        item = QGraphicsPixmapItem(QPixmap.fromImage(image))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(item)
        self._scene.setSceneRect(item.boundingRect())
        self._item = item
        self.fit_to_window()

    # ------------------------------------------------------------------ 缩放

    def fit_to_window(self) -> None:
        """缩放到整张图刚好可见，并进入"跟随窗口自适应"状态。"""
        if self._item is None:
            return
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self._apply_scale(self.transform().m11(), anchor_under_mouse=False, force=True)
        self._auto_fit = True

    def zoom_in(self) -> None:
        self.zoom_by(ZOOM_STEP)

    def zoom_out(self) -> None:
        self.zoom_by(1.0 / ZOOM_STEP)

    def reset_zoom(self) -> None:
        """回到 100%（一个图像像素对应一个屏幕像素）。"""
        self._apply_scale(1.0, anchor_under_mouse=False)

    def zoom_by(self, factor: float, *, anchor_under_mouse: bool = False) -> None:
        """按倍率缩放，结果会被裁剪到 [MIN_SCALE, MAX_SCALE]。"""
        if self._item is None:
            return
        self._apply_scale(self._scale * factor, anchor_under_mouse=anchor_under_mouse)

    def _apply_scale(self, scale: float, *, anchor_under_mouse: bool, force: bool = False) -> None:
        target = min(max(scale, MIN_SCALE), MAX_SCALE)
        if not force and abs(target - self._scale) < 1e-9:
            return
        self._auto_fit = False
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
            if anchor_under_mouse
            else QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setTransform(QTransform.fromScale(target, target))
        self._scale = target
        self.zoomChanged.emit(target)

    # ------------------------------------------------------------------ 事件

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        if self._auto_fit:
            self.fit_to_window()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._item is None:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            delta = event.pixelDelta().y() or event.pixelDelta().x()
        if delta == 0:
            super().wheelEvent(event)
            return

        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
        elif modifiers & Qt.KeyboardModifier.AltModifier:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - delta)
        else:
            self.zoom_by(ZOOM_STEP ** (delta / 120.0), anchor_under_mouse=True)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._item is not None:
            self._panning = True
            self._pan_anchor = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._panning:
            delta = event.position() - self._pan_anchor
            self._pan_anchor = event.position()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - int(delta.x()))
            v_bar.setValue(v_bar.value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)
