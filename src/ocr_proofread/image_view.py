"""QGraphicsView 子类：显示单张图像，负责缩放、平移与 ROI 框选。

``set_image()`` 会清空整个场景，ROI 图元随之消失；切图后需要由上层重新调用
:meth:`ImageView.set_rois`。视图不保存跨图状态，只持有"当前这一张图"。
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from .annotations import Roi
from .roi_item import MIN_SIZE, RoiHandle, RoiItem

#: 缩放上下限，避免滚轮把图缩到看不见或者撑爆
MIN_SCALE = 0.02
MAX_SCALE = 40.0

#: 鼠标滚轮 / 快捷键单步缩放倍率
ZOOM_STEP = 1.25


class ImageView(QGraphicsView):
    """一张图 + 缩放 + 平移 + ROI 框选。

    交互约定：

    * 滚轮 / Ctrl+滚轮 —— 以鼠标位置为锚点缩放
    * Shift+滚轮 —— 水平滚动，Alt+滚轮 —— 垂直滚动
    * 中键拖动 —— 平移画布
    * 左键在图像空白处拖动 —— 拉出一个新矩形；落在已有图元上时交给场景，
      即选中 / 拖动 / 拖手柄缩放
    * Esc —— 放弃正在拉的矩形，或取消选中
    """

    #: 当前缩放比例，1.0 表示 100%
    zoomChanged = Signal(float)

    #: 场景里的 ROI 数量发生变化（新增、删除、整批替换）时发出；移动与缩放不发
    roisChanged = Signal()

    #: 场景里的选中状态变化（选中 / 取消选中 / 改选另一个）时发出
    selectionChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._scene.selectionChanged.connect(self.selectionChanged)

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
        self._image: QImage | None = None
        self._scale = 1.0
        self._auto_fit = True  # 窗口大小变化时是否自动重新适应窗口
        self._panning = False
        self._pan_anchor = QPointF()
        self._drawing: RoiItem | None = None  # 正在左键拉出的矩形
        self._draw_origin = QPointF()

    # ------------------------------------------------------------------ 图像

    @property
    def has_image(self) -> bool:
        return self._item is not None

    @property
    def scale_factor(self) -> float:
        return self._scale

    def image(self) -> QImage | None:
        """当前显示的图像（图像像素）；没有图像时是 ``None``。

        底图同时以 ``QPixmap`` 挂在场景里，这里保留原 :class:`QImage` 是为了按 ROI
        裁剪出 OCR 图块。
        """
        return self._image

    def set_image(self, image: QImage | None) -> None:
        """替换当前显示的图像；传入 ``None`` 表示清空。

        注意：这里会清空整个场景，其中的 ROI 图元会一并被移除。切图后由上层
        重新调用 :meth:`set_rois` 恢复该图的标注。
        """
        self._scene.clear()
        self._item = None
        self._image = None
        self._scale = 1.0
        self._panning = False
        self._drawing = None
        self.resetTransform()
        if image is None or image.isNull():
            self._scene.setSceneRect(0, 0, 0, 0)
            self.zoomChanged.emit(self._scale)
            return

        item = QGraphicsPixmapItem(QPixmap.fromImage(image))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(-1.0)  # 永远垫在 ROI 图元下面
        self._scene.addItem(item)
        self._scene.setSceneRect(item.boundingRect())
        self._item = item
        self._image = image
        self.fit_to_window()

    # -------------------------------------------------------------------- ROI

    def image_rect(self) -> QRectF:
        """当前图像在场景中的矩形；没有图像时是空矩形。"""
        return QRectF(self._item.boundingRect()) if self._item is not None else QRectF()

    def roi_items(self) -> list[RoiItem]:
        """场景里的 ROI 图元，按从上到下、从左到右排序（保证存盘顺序稳定）。"""

        def sort_key(item: RoiItem) -> tuple[float, float]:
            roi = item.to_roi()
            return (roi.y, roi.x)

        items = [item for item in self._scene.items() if isinstance(item, RoiItem)]
        items.sort(key=sort_key)
        return items

    def rois(self) -> list[Roi]:
        """当前图像的全部标注（图像像素坐标）。"""
        return [item.to_roi() for item in self.roi_items()]

    def roi_count(self) -> int:
        return len(self.roi_items())

    def set_rois(self, rois: Iterable[Roi]) -> None:
        """清掉现有 ROI，换成给定的这一批；由上层在切图后调用。"""
        for item in self.roi_items():
            self._scene.removeItem(item)
        for roi in rois:
            self._add_roi_item(roi.as_rect(), roi.text)
        self.roisChanged.emit()

    def clear_rois(self) -> None:
        """删除当前图像上的所有 ROI。"""
        items = self.roi_items()
        for item in items:
            self._scene.removeItem(item)
        if items:
            self.roisChanged.emit()

    def delete_selected_rois(self) -> int:
        """删除选中的 ROI，返回删掉的数量。"""
        selected = [item for item in self.roi_items() if item.isSelected()]
        for item in selected:
            self._scene.removeItem(item)
        if selected:
            self.roisChanged.emit()
        return len(selected)

    def select_roi_item(self, item: RoiItem, *, center: bool = True) -> None:
        """只选中给定的 ROI 图元（可选把它挪到视口中间）。

        OCR 结果面板点某一行时走这里，场景 → 面板的反向同步走 :attr:`selectionChanged`。
        """
        for other in self.roi_items():
            other.setSelected(other is item)
        if center:
            self.centerOn(item)

    def _add_roi_item(self, rect: QRectF, text: str = "") -> RoiItem:
        bounds = self.image_rect()
        item = RoiItem(rect, bounds=bounds if not bounds.isEmpty() else None, text=text)
        self._scene.addItem(item)
        return item

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
        if event.button() == Qt.MouseButton.LeftButton and self._begin_roi(event.position()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drawing is not None:
            self._resize_drawing(event.position())
            event.accept()
            return
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
        if event.button() == Qt.MouseButton.LeftButton and self._drawing is not None:
            self._commit_drawing()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            if self._drawing is not None:
                self._cancel_drawing()
                event.accept()
                return
            if self._scene.selectedItems():
                self._scene.clearSelection()
                event.accept()
                return
        super().keyPressEvent(event)

    # ---------------------------------------------------------------- 框选细节

    def _begin_roi(self, viewport_pos: QPointF) -> bool:
        """判断这次左键按下要不要开始拉新矩形。

        落在已有图元（含手柄）上时返回 ``False``，交给场景做选中/移动/缩放；
        落在图像之外或还没有图像时不响应。
        """
        if self._item is None:
            return False
        if isinstance(self.itemAt(viewport_pos.toPoint()), (RoiItem, RoiHandle)):
            return False
        scene_pos = self.mapToScene(viewport_pos.toPoint())
        if not self.image_rect().contains(scene_pos):
            return False

        self._scene.clearSelection()
        self._draw_origin = scene_pos
        self._drawing = self._add_roi_item(QRectF(scene_pos, scene_pos))
        return True

    def _resize_drawing(self, viewport_pos: QPointF) -> None:
        if self._drawing is None:
            return
        current = self.mapToScene(viewport_pos.toPoint())
        rect = QRectF(self._draw_origin, current).normalized()
        self._drawing.setRect(rect.intersected(self.image_rect()))

    def _commit_drawing(self) -> None:
        """松开左键：够大的矩形留下并选中，太小的（误点击）直接丢弃。"""
        item, self._drawing = self._drawing, None
        if item is None:
            return
        rect = item.rect()
        if rect.width() < MIN_SIZE or rect.height() < MIN_SIZE:
            self._scene.removeItem(item)
            return
        item.setSelected(True)
        self.roisChanged.emit()

    def _cancel_drawing(self) -> None:
        if self._drawing is None:
            return
        self._scene.removeItem(self._drawing)
        self._drawing = None
