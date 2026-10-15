"""可交互的矩形 ROI 图元。

一个 :class:`RoiItem` 就是场景里的一个矩形：可选中、可整体拖动，被选中时八个
手柄可以改尺寸。手柄用 ``ItemIgnoresTransformations`` 绘制，因此不论画布缩放到
多少，看上去都是同样大的小方块。

图元旁边还挂着一个文本小框（:class:`~ocr_proofread.text_box.RoiTextBox`），只在
自己被选中时出现，显示这个框的识别文本；它是本图元的子图元，所以跟着标注一起
移动、缩放、删除，不需要额外的簿记。

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
from .text_box import (
    DEFAULT_FONT_SIZE,
    DEFAULT_TEXT_DIRECTION,
    RoiTextBox,
    TextDirection,
)

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
        text_direction: TextDirection = DEFAULT_TEXT_DIRECTION,
        text_font_size: float = DEFAULT_FONT_SIZE,
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

        self._text_box = RoiTextBox(
            direction=text_direction, font_size=text_font_size, parent=self
        )
        self._text_box.set_text(text)
        self._text_box.setVisible(False)  # 只有被选中的标注才露出来
        self._text_box_after: bool | None = None  # 视图给的贴边方向；None = 按兜底规则
        self._handles = [RoiHandle(self, role) for role in HandleRole]
        self.set_handles_visible(False)
        self._sync_handles()
        self._sync_text_box_anchor()

    # ------------------------------------------------------------------ 数据

    def to_roi(self) -> Roi:
        """当前几何（含 ``pos`` 偏移）与 OCR 文本转成图像坐标的 :class:`Roi`。"""
        return Roi.from_rect(self.mapRectToScene(self.rect()), self._text)

    def text(self) -> str:
        """这个 ROI 最近一次的 OCR 结果；空串表示还没识别过。"""
        return self._text

    def set_text(self, text: str) -> None:
        """写入 OCR 结果（同时刷新提示气泡与旁边的文本框）。"""
        self._text = text
        self._text_box.set_text(text)
        self._apply_tooltip()

    def text_box(self) -> RoiTextBox:
        """旁边那个显示识别文本的小框（只在选中时可见）。"""
        return self._text_box

    def set_text_direction(self, direction: TextDirection) -> None:
        """换文本框的排版方向（横排 / 竖排）；全局设定，由上层统一设置。

        方向同时决定框贴在哪个轴（横排贴上下、竖排贴左右），所以换方向后要重新
        贴边；视图在场时紧接着会用 :meth:`update_text_box` 给出准确的一侧。
        """
        self._text_box.set_direction(direction)
        self._text_box_after = None  # "上下"与"左右"的含义变了，交回自动判断
        self._sync_text_box_anchor()

    def text_box_after(self) -> bool | None:
        """文本框贴在标注的"后面"一侧吗；``None`` 表示还没被视图定过。"""
        return self._text_box_after

    def update_text_box(
        self, *, roi_screen: QRectF, viewport: QRectF, font_size: float
    ) -> None:
        """视图把"标注在屏幕上的位置 + 视口 + 字号"喂进来，重算框的形状与贴边。

        视图是唯一知道画布缩放与视口大小的地方；图元自己不去问视图。横排贴上下、
        竖排贴左右，贴哪一侧看余量：能整框放下的一侧优先，其次余量大的一侧。
        """
        box = self._text_box
        box.set_font_size(font_size)
        vertical = box.direction().vertical
        if vertical:
            cross = roi_screen.height()
            room_before = roi_screen.left() - viewport.left()
            room_after = viewport.right() - roi_screen.right()
        else:
            cross = roi_screen.width()
            room_before = roi_screen.top() - viewport.top()
            room_after = viewport.bottom() - roi_screen.bottom()
        box.apply_placement(
            cross=cross, room=max(room_before, room_after), viewport=viewport.size()
        )

        extent = box.content_rect().width() if vertical else box.content_rect().height()
        if room_after >= extent:
            after = True
        elif room_before >= extent:
            after = False
        else:
            after = room_after >= room_before
        self._text_box_after = after
        self._sync_text_box_anchor()

    def can_enlarge_text(self, font_size: float) -> bool:
        """这个字号下文本框还放得下整段文字吗？滚轮调大字号时的判据。"""
        return self._text_box.fits_font_size(font_size)

    # ------------------------------------------------------------------ 交互

    def setRect(self, rect: QRectF) -> None:  # noqa: N802 (Qt 命名)
        """改几何时把手柄与文本框一起挪过去。

        ``QGraphicsRectItem.setRect()`` 不会通知子图元，直接调它会让手柄和文本框
        留在旧位置（画新框时最明显），所以这里统一补一次同步。
        """
        super().setRect(QRectF(rect).normalized())
        self._sync_handles()
        self._sync_text_box_anchor()

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
        self.setRect(QRectF(left, top, right - left, bottom - top))  # 顺带同步子图元

    def set_handles_visible(self, visible: bool) -> None:
        """手柄只在该图元被选中时出现，避免未选中时误抓手柄。"""
        for handle in self._handles:
            handle.setVisible(visible)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.set_handles_visible(bool(value))
            self._text_box.setVisible(bool(value))
            self._apply_pen()
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self._clamp_position(value)
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._sync_text_box_anchor()  # 挪过图像中线时兜底的贴边要重算
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

    def _sync_text_box_anchor(self) -> None:
        """把文本框贴到标注旁边。

        贴哪一侧：视图给过就听视图的（:meth:`update_text_box`），没给过就按兜底规则
        ——横排默认下方，竖排看标注落在图像的左半边还是右半边。原点放在标注的角上，
        内容往哪边铺由 :meth:`RoiTextBox.set_after` 决定，所以这里不需要知道画布缩放，
        也不会因为缩放而错位。
        """
        rect = self.rect()
        after = self._text_box_after
        if after is None:
            after = self._default_text_box_after(rect)
        self._text_box.set_after(after)
        if self._text_box.direction().vertical:
            self._text_box.setPos(rect.right() if after else rect.left(), rect.top())
        else:
            self._text_box.setPos(rect.left(), rect.bottom() if after else rect.top())

    def _default_text_box_after(self, rect: QRectF) -> bool:
        """没有视图信息时的兜底贴边：横排贴下方；竖排按标注在图像里的位置定左右。"""
        if not self._text_box.direction().vertical:
            return True
        if self._bounds is not None and not self._bounds.isEmpty():
            # 图元只平移，pos.x + 局部 x 就是场景（图像）x，不依赖它有没有进场景
            center_x = self.pos().x() + rect.center().x()
            return center_x <= self._bounds.center().x()
        return True

    def _apply_tooltip(self) -> None:
        hint = "拖动移动，拖动方块手柄缩放，双击编辑文本，Delete 删除"
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
