"""QGraphicsView 子类：显示单张图像，负责缩放、平移、ROI 框选与文本就地编辑。

``set_image()`` 会清空整个场景，ROI 图元随之消失；切图后需要由上层重新调用
:meth:`ImageView.set_rois`。视图不保存跨图状态，只持有"当前这一张图"。
"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QImage,
    QPainter,
    QPixmap,
    QTextCursor,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QPlainTextEdit,
    QWidget,
)

from .annotations import Roi
from .roi_item import MIN_SIZE, RoiHandle, RoiItem

#: 缩放上下限，避免滚轮把图缩到看不见或者撑爆
MIN_SCALE = 0.02
MAX_SCALE = 40.0

#: 鼠标滚轮 / 快捷键单步缩放倍率
ZOOM_STEP = 1.25

#: 就地文本编辑器的最小尺寸（屏幕像素）；小的 ROI 也要能编辑得下去
TEXT_EDITOR_MIN_WIDTH = 160
TEXT_EDITOR_MIN_HEIGHT = 64


class _RoiTextEditor(QPlainTextEdit):
    """盖在 ROI 上的就地文本编辑器。

    编辑器是视口的子控件，坐标与尺寸都用屏幕像素，所以画布缩放到多少都不影响
    输入手感。它只负责把"编辑结束"的三种方式转成一次 ``done(commit)`` 回调：

    * ``Esc`` —— 放弃（``commit=False``）
    * ``Ctrl+Enter`` —— 采纳（``commit=True``）
    * 焦点离开（点了别处 / Tab 走）—— 采纳
    """

    def __init__(self, parent: QWidget, done: Callable[[bool], None]) -> None:
        super().__init__(parent)
        self._done = done
        self._finished = False
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)  # Tab 用来离开编辑器而不是插制表符
        self.setStyleSheet(
            "QPlainTextEdit {"
            " background: #fffdf5;"
            " border: 2px solid #ff6d00;"
            " padding: 2px;"
            " selection-background-color: #ffcc80;"
            "}"
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == Qt.Key.Key_Escape:
            self._finish(commit=False)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._finish(commit=True)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._finish(commit=True)

    def _finish(self, *, commit: bool) -> None:
        """结束编辑，保证回调只发一次（Esc 之后紧接着的 focusOut 是空操作）。"""
        if self._finished:
            return
        self._finished = True
        self._done(commit)


class ImageView(QGraphicsView):
    """一张图 + 缩放 + 平移 + ROI 框选。

    交互约定：

    * 滚轮 / Ctrl+滚轮 —— 以鼠标位置为锚点缩放
    * Shift+滚轮 —— 水平滚动，Alt+滚轮 —— 垂直滚动
    * 中键拖动 —— 平移画布
    * 空格按住 + 左键拖动 —— 平移画布（抓手模式），此时不拉新矩形、也不拖动图元
    * 左键在图像空白处拖动 —— 拉出一个新矩形；落在已有图元上时交给场景，
      即选中 / 拖动 / 拖手柄缩放
    * 双击已有图元 —— 就地编辑这个 ROI 的文本（见 :meth:`begin_text_edit`）
    * Esc —— 放弃正在拉的矩形，或取消选中
    """

    #: 当前缩放比例，1.0 表示 100%
    zoomChanged = Signal(float)

    #: 场景里的 ROI 数量发生变化（新增、删除、整批替换）时发出；移动与缩放不发
    roisChanged = Signal()

    #: 就地编辑文本被采纳时发出（放弃编辑、没有改动时不发）
    roiTextChanged = Signal()

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
        self._pan_button: Qt.MouseButton | None = None  # 正在平移时按住的键
        self._pan_anchor = QPointF()
        self._hand_mode = False  # 空格按住：左键拖动 = 平移画布
        self._drawing: RoiItem | None = None  # 正在左键拉出的矩形
        self._draw_origin = QPointF()
        self._text_editor: _RoiTextEditor | None = None  # 双击图元后盖上去的编辑器
        self._text_item: RoiItem | None = None  # 编辑器正在改的那个图元
        # 缩放后编辑器要跟着 ROI 挪，否则输入框会留在原地
        self.zoomChanged.connect(lambda _scale: self._place_text_editor())

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
        self.cancel_text_edit()  # 编辑器挂在图元上，图元要没了就先撤掉
        self._scene.clear()
        self._item = None
        self._image = None
        self._scale = 1.0
        self._pan_button = None
        self._drawing = None
        self._update_cursor()  # 切图打断平移时别把"抓紧"的光标留在屏幕上
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
        self.cancel_text_edit()
        for item in self.roi_items():
            self._scene.removeItem(item)
        for roi in rois:
            self._add_roi_item(roi.as_rect(), roi.text)
        self.roisChanged.emit()

    def clear_rois(self) -> None:
        """删除当前图像上的所有 ROI。"""
        self.cancel_text_edit()
        items = self.roi_items()
        for item in items:
            self._scene.removeItem(item)
        if items:
            self.roisChanged.emit()

    def delete_selected_rois(self) -> int:
        """删除选中的 ROI，返回删掉的数量。"""
        self.cancel_text_edit()
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

    # ---------------------------------------------------------------- 文本编辑

    def is_editing_text(self) -> bool:
        """当前是否有一个 ROI 正在就地编辑文本。"""
        return self._text_editor is not None

    def begin_text_edit(self, item: RoiItem) -> bool:
        """在给定 ROI 上盖一个就地编辑器并让它拿到焦点。

        返回是否真的开始编辑；一次只编辑一个框，已经在编辑别的框时先采纳前一个。
        文本只在结束时（:meth:`commit_text_edit`）写回图元，中途的输入不影响场景。
        """
        if item.scene() is not self._scene:
            return False
        self.commit_text_edit()
        self._scene.clearSelection()
        item.setSelected(True)

        editor = _RoiTextEditor(self.viewport(), self._finish_text_edit)
        editor.setPlainText(item.text())
        self._text_editor = editor
        self._text_item = item
        self._place_text_editor()
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = editor.textCursor()  # 光标落在末尾：接着原文改，而不是覆盖
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        return True

    def commit_text_edit(self) -> bool:
        """采纳正在编辑的文本（并发出 :attr:`roiTextChanged`）；没在编辑时返回 ``False``。

        切图 / 保存 / 关窗前由上层调用，免得"正在改的字"随场景一起消失。
        """
        if self._text_editor is None:
            return False
        self._finish_text_edit(True)
        return True

    def cancel_text_edit(self) -> bool:
        """放弃正在编辑的文本，保持原样；没在编辑时返回 ``False``。"""
        if self._text_editor is None:
            return False
        self._finish_text_edit(False)
        return True

    def _finish_text_edit(self, commit: bool) -> None:
        """收起编辑器：``commit`` 为真才把文本写回图元。

        先把 ``_text_editor`` 置空再动图元，所以这里发信号引起的重入（比如上层
        收到 ``roiTextChanged`` 后又来 ``commit_text_edit()``）会立刻返回，不会绕回来。
        """
        editor, item = self._text_editor, self._text_item
        if editor is None:
            return
        self._text_editor = None
        self._text_item = None
        text = editor.toPlainText()
        editor.hide()
        editor.deleteLater()

        if item is None or item.scene() is not self._scene:
            return  # 图元已经被删掉了（比如编辑期间切了图），只收掉编辑器
        if not commit or text == item.text():
            return
        item.set_text(text)
        self.roiTextChanged.emit()

    def _place_text_editor(self) -> None:
        """把编辑器摆到 ROI 在屏幕上的位置上，小的 ROI 用最小尺寸兜底。"""
        editor, item = self._text_editor, self._text_item
        if editor is None or item is None:
            return
        area = self.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
        viewport = self.viewport().rect()
        width = min(max(area.width(), TEXT_EDITOR_MIN_WIDTH), viewport.width())
        height = min(max(area.height(), TEXT_EDITOR_MIN_HEIGHT), viewport.height())
        x = min(max(area.left(), 0), max(viewport.width() - width, 0))
        y = min(max(area.top(), 0), max(viewport.height() - height, 0))
        editor.setGeometry(int(x), int(y), int(width), int(height))

    # ------------------------------------------------------------------ 抓手

    @property
    def hand_mode(self) -> bool:
        """是否处于抓手模式（按住空格）。

        这个状态由上层在空格按下 / 松开时设置，见 :meth:`set_hand_mode`。
        """
        return self._hand_mode

    def set_hand_mode(self, enabled: bool) -> None:
        """开关抓手模式：开启时左键拖动平移画布，不拉新矩形。

        空格是应用级按键，焦点可能不在视图上（比如刚点过右侧结果面板），视图自己听
        ``keyPressEvent`` 会漏掉这些情况；所以按键由 ``MainWindow`` 在应用级过滤后
        转发到这里（见 ``MainWindow.eventFilter``）。
        """
        if enabled == self._hand_mode:
            return
        self._hand_mode = enabled
        self._update_cursor()

    def _begin_pan(self, button: Qt.MouseButton, viewport_pos: QPointF) -> None:
        self._pan_button = button
        self._pan_anchor = viewport_pos
        self._update_cursor()

    def _end_pan(self) -> None:
        self._pan_button = None
        self._update_cursor()

    def _update_cursor(self) -> None:
        """按当前状态刷新指针：平移中 = 抓紧，空格按住 = 张手，否则默认。"""
        if self._pan_button is not None:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._hand_mode:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().unsetCursor()

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
        self._place_text_editor()  # 编辑器是屏幕坐标的，窗口变了要重新贴回 ROI
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
        # 画布上任何一次按下都先结束就地编辑：接着要移动图元或拉新矩形了
        self.commit_text_edit()
        # 空格按住时左键也是抓手，优先于"拉新矩形"和"拖动图元"
        grabbing = event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._hand_mode
        )
        if self._item is not None and grabbing:
            self._begin_pan(event.button(), event.position())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._begin_roi(event.position()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """双击已有图元 → 就地编辑它的文本；其余情况照常交给场景。"""
        if event.button() == Qt.MouseButton.LeftButton and not self._hand_mode:
            target = self.itemAt(event.position().toPoint())
            if isinstance(target, RoiHandle):  # 手柄也算所在的那个框
                target = target.parentItem()
            if isinstance(target, RoiItem) and self.begin_text_edit(target):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drawing is not None:
            self._resize_drawing(event.position())
            event.accept()
            return
        if self._pan_button is not None:
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
        if self._pan_button is not None and event.button() == self._pan_button:
            self._end_pan()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            if self.cancel_text_edit():  # 焦点不在编辑器上时的兜底
                event.accept()
                return
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
