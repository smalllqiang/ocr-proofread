"""可交互的矩形 ROI 图元。

一个 :class:`RoiItem` 就是场景里的一个矩形：可选中、可整体拖动，被选中时八个
手柄可以改尺寸。手柄用 ``ItemIgnoresTransformations`` 绘制，因此不论画布缩放到
多少，看上去都是同样大的小方块。

坐标约定见 :mod:`ocr_proofread.image_view`：场景单位 = 图像像素，原点在图像左上角。
图元只用平移（不改 ``pos`` 之外的变换），所以 :meth:`RoiItem.to_roi` 可以直接
``mapRectToScene`` 得到图像坐标。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem

from .annotations import Roi

#: 手柄的屏幕边长（像素），不随缩放变化
HANDLE_SIZE = 9.0

#: 矩形的最小边长（图像像素），避免被拖成一条线
MIN_SIZE = 3.0

_EDGE_COLOR = QColor("#1e88e5")
_EDGE_COLOR_HOVER = QColor("#42a5f5")
_EDGE_COLOR_SELECTED = QColor("#ff6d00")
_FILL_COLOR = QColor(30, 136, 229, 38)
_HANDLE_COLOR = QColor("#ffffff")


class HandleRole(Enum):
    """八个缩放手柄。"""

    TOP_LEFT = "top_left"
    TOP = "top"
    TOP_RIGHT = "top_right"
    RIGHT = "right"
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM = "bottom"
    BOTTOM_LEFT = "bottom_left"
    LEFT = "left"

    @property
    def factors(self) -> tuple[float, float]:
        """手柄锚点在矩形内的相对位置，取值 0 / 0.5 / 1。"""
        return _ROLE_FACTORS[self]

    @property
    def cursor(self) -> Qt.CursorShape:
        if self in _FDIAG_ROLES:
            return Qt.CursorShape.SizeFDiagCursor
        if self in _BDIAG_ROLES:
            return Qt.CursorShape.SizeBDiagCursor
        if self in _VERTICAL_ROLES:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeHorCursor


_ROLE_FACTORS: dict[HandleRole, tuple[float, float]] = {
    HandleRole.TOP_LEFT: (0.0, 0.0),
    HandleRole.TOP: (0.5, 0.0),
    HandleRole.TOP_RIGHT: (1.0, 0.0),
    HandleRole.RIGHT: (1.0, 0.5),
    HandleRole.BOTTOM_RIGHT: (1.0, 1.0),
    HandleRole.BOTTOM: (0.5, 1.0),
    HandleRole.BOTTOM_LEFT: (0.0, 1.0),
    HandleRole.LEFT: (0.0, 0.5),
}

# 拖动某个手柄时，矩形的哪几条边跟着走
_LEFT_ROLES = frozenset({HandleRole.TOP_LEFT, HandleRole.LEFT, HandleRole.BOTTOM_LEFT})
_RIGHT_ROLES = frozenset({HandleRole.TOP_RIGHT, HandleRole.RIGHT, HandleRole.BOTTOM_RIGHT})
_TOP_ROLES = frozenset({HandleRole.TOP_LEFT, HandleRole.TOP, HandleRole.TOP_RIGHT})
_BOTTOM_ROLES = frozenset({HandleRole.BOTTOM_LEFT, HandleRole.BOTTOM, HandleRole.BOTTOM_RIGHT})

_FDIAG_ROLES = frozenset({HandleRole.TOP_LEFT, HandleRole.BOTTOM_RIGHT})
_BDIAG_ROLES = frozenset({HandleRole.TOP_RIGHT, HandleRole.BOTTOM_LEFT})
_VERTICAL_ROLES = frozenset({HandleRole.TOP, HandleRole.BOTTOM})


class RoiHandle(QGraphicsRectItem):
    """父图元边角上的缩放手柄。

    鼠标拖动时不改变自身坐标，只把"相对按下点的位移"交给父图元换算成新的矩形，
    因此缩放过程中父图元的 ``pos`` 不变，坐标换算始终稳定。
    """

    def __init__(self, parent: RoiItem, role: HandleRole) -> None:
        super().__init__(parent)
        self.role = role
        self._dragging = False
        self._press_scene = QPointF()
        self._start_rect = QRectF()

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        half = HANDLE_SIZE / 2
        self.setRect(-half, -half, HANDLE_SIZE, HANDLE_SIZE)
        self.setPen(QPen(_EDGE_COLOR_SELECTED, 1.0))
        self.setBrush(QBrush(_HANDLE_COLOR))
        self.setCursor(role.cursor)
        self.setZValue(2.0)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        parent = self.parentItem()
        if event.button() != Qt.MouseButton.LeftButton or not isinstance(parent, RoiItem):
            event.ignore()
            return
        self._dragging = True
        self._press_scene = event.scenePos()
        self._start_rect = QRectF(parent.rect())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        parent = self.parentItem()
        if not self._dragging or not isinstance(parent, RoiItem):
            event.ignore()
            return
        delta = parent.mapFromScene(event.scenePos()) - parent.mapFromScene(self._press_scene)
        parent.resize_by(self.role, delta, self._start_rect)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        event.accept()


class RoiItem(QGraphicsRectItem):
    """场景里一个可交互的矩形 ROI。

    ``bounds`` 是允许的范围（一般是图像矩形，图像像素坐标）；给定时，整体拖动与
    手柄缩放都被夹在范围内，图元不会被拖到图像外面。

    图元还挂着一份 OCR 文本（:meth:`text` / :meth:`set_text`），随 :meth:`to_roi`
    一起交给上层落盘；它不参与几何运算，只是"识别结果跟着这个框走"。
    """

    def __init__(
        self,
        rect: QRectF,
        *,
        bounds: QRectF | None = None,
        text: str = "",
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(QRectF(rect).normalized(), parent)
        self._bounds = QRectF(bounds) if bounds is not None else None
        self._hovered = False
        self._text = text

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(1.0)
        self.setBrush(QBrush(_FILL_COLOR))
        self._apply_tooltip()
        self._apply_pen()

        self._handles = [RoiHandle(self, role) for role in HandleRole]
        self.set_handles_visible(False)
        self._sync_handles()

    # ------------------------------------------------------------------ 数据

    def to_roi(self) -> Roi:
        """当前几何（含 ``pos`` 偏移）与 OCR 文本转成图像坐标的 :class:`Roi`。"""
        return Roi.from_rect(self.mapRectToScene(self.rect()), self._text)

    def text(self) -> str:
        """这个 ROI 最近一次的 OCR 结果；空串表示还没识别过。"""
        return self._text

    def set_text(self, text: str) -> None:
        """写入 OCR 结果（同时刷新提示气泡）。"""
        self._text = text
        self._apply_tooltip()

    # ------------------------------------------------------------------ 交互

    def resize_by(self, role: HandleRole, delta: QPointF, start_rect: QRectF) -> None:
        """按手柄拖动量算出新矩形。

        ``delta`` / ``start_rect`` 都是图元坐标：``start_rect`` 是按下瞬间的矩形，
        ``delta`` 是相对按下点的累计位移。越界或小于 :data:`MIN_SIZE` 的结果会被丢弃
        （保留上一次有效形状），因此拖过头不会把矩形翻面。
        """
        left, top = start_rect.left(), start_rect.top()
        right, bottom = start_rect.right(), start_rect.bottom()
        if role in _LEFT_ROLES:
            left += delta.x()
        if role in _RIGHT_ROLES:
            right += delta.x()
        if role in _TOP_ROLES:
            top += delta.y()
        if role in _BOTTOM_ROLES:
            bottom += delta.y()

        if self._bounds is not None:
            left = max(left, self._bounds.left())
            top = max(top, self._bounds.top())
            right = min(right, self._bounds.right())
            bottom = min(bottom, self._bounds.bottom())

        if right - left < MIN_SIZE or bottom - top < MIN_SIZE:
            return
        self.setRect(QRectF(left, top, right - left, bottom - top))
        self._sync_handles()

    def set_handles_visible(self, visible: bool) -> None:
        """手柄只在该图元被选中时出现，避免未选中时误抓手柄。"""
        for handle in self._handles:
            handle.setVisible(visible)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.set_handles_visible(bool(value))
            self._apply_pen()
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self._clamp_position(value)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._apply_pen()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._apply_pen()
        super().hoverLeaveEvent(event)

    # ------------------------------------------------------------------ 内部

    def _sync_handles(self) -> None:
        rect = self.rect()
        for handle in self._handles:
            fx, fy = handle.role.factors
            handle.setPos(rect.left() + fx * rect.width(), rect.top() + fy * rect.height())

    def _apply_tooltip(self) -> None:
        hint = "拖动移动，拖动方块手柄缩放，Delete 删除"
        self.setToolTip(f"{self._text}\n\n{hint}" if self._text else hint)

    def _apply_pen(self) -> None:
        if self.isSelected():
            color = _EDGE_COLOR_SELECTED
        elif self._hovered:
            color = _EDGE_COLOR_HOVER
        else:
            color = _EDGE_COLOR
        # 宽度 0 表示"装饰性画笔"：不管画布怎么缩放，边框永远是 1 个屏幕像素
        self.setPen(QPen(color, 0))

    def _clamp_position(self, pos: QPointF) -> QPointF:
        """把整体拖动的落点夹在 ``bounds`` 内。"""
        if self._bounds is None:
            return pos
        bounds = self._bounds
        moved = self.rect().translated(pos)
        dx = dy = 0.0
        if moved.left() < bounds.left():
            dx = bounds.left() - moved.left()
        elif moved.right() > bounds.right():
            dx = bounds.right() - moved.right()
        if moved.top() < bounds.top():
            dy = bounds.top() - moved.top()
        elif moved.bottom() > bounds.bottom():
            dy = bounds.bottom() - moved.bottom()
        if dx == 0.0 and dy == 0.0:
            return pos
        return QPointF(pos.x() + dx, pos.y() + dy)
