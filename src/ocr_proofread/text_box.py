"""贴在选中的 ROI 旁边的文本小框。

框里画的是这个 ROI 的识别文本。位置和尺寸都由本模块自己判断：

* **排版方向决定贴在哪个轴**：横排（横書き）贴标注的上下，竖排（縦書き）贴左右。
* **尺寸尽量与标注对齐**：余量够、文字排得下时，横排的框宽 = 标注的屏幕宽、
  竖排的框高 = 标注的屏幕高；排不下（标注太窄/太矮、或整段文字放不进余量）时退回
  按文字自然大小排（横排上限 :data:`MAX_LINE_WIDTH`、竖排上限 :data:`MAX_COLUMN_HEIGHT`）。
* **字号可以滚轮调**：光标在框上滚滚轮改字号（:data:`FONT_SIZE_STEP` 一档），
  默认值就是可调到的最小值；再大就放不下时拒绝，见 :meth:`RoiTextBox.fits_font_size`。

框是 :class:`~ocr_proofread.roi_item.RoiItem` 的子图元，且带
``ItemIgnoresTransformations``：位置锚在标注的角上，因此跟着标注一起移动、缩放、
删除，而自身的尺寸按屏幕像素算，不随画布缩放变化——2% 和 4000% 下字号都一样。

框不知道画布缩放，也不知道视口在哪：这些由 ``ImageView`` 每次重算时喂进来
（:meth:`RoiTextBox.apply_placement`），图元与框都不去反问视图。

竖排是按字符逐格摆的（原文里的换行就是换列），不做日语竖排里"ー、（）"这类需要
旋转的细节；识别结果以日文/中文为主，逐格摆已经够用。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

#: 框与 ROI 之间的空隙、框内文本与边框之间的留白（屏幕像素，不随画布缩放）
GAP = 8.0
PADDING = 7.0
BORDER_RADIUS = 4.0

#: 没有视图信息（或对齐不了）时的文本区上限（屏幕像素）
MAX_LINE_WIDTH = 240.0
MAX_COLUMN_HEIGHT = 220.0

#: 一个框最多画这么多字，再多就用省略号收尾，免得长文本把画面铺满
MAX_CHARS = 300

#: 没有识别文本时框里显示的提示
EMPTY_HINT = "（未识别）"

#: 字号（pt）：默认值同时是可调到的最小值；上限还会被"框放不放得下"进一步压低
DEFAULT_FONT_SIZE = 10.0
MIN_FONT_SIZE = DEFAULT_FONT_SIZE
MAX_FONT_SIZE = 72.0

#: 滚轮一档调多少 pt
FONT_SIZE_STEP = 1.0

_HORIZONTAL_FLAGS = (
    int(Qt.AlignmentFlag.AlignLeft)
    | int(Qt.AlignmentFlag.AlignTop)
    | int(Qt.TextFlag.TextWordWrap)
)
_CENTER_FLAGS = int(Qt.AlignmentFlag.AlignCenter)

_BACKGROUND = QColor("#fffdf5")
_BORDER_COLOR = QColor("#ff6d00")
_TEXT_COLOR = QColor("#212121")
_HINT_COLOR = QColor("#9e9e9e")

#: 浮点比较的容差（屏幕像素 / pt）
_EPS = 1e-9


class TextDirection(Enum):
    """文本框里文字的排版方向，同时决定框贴在标注的哪个轴。"""

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"

    @property
    def label(self) -> str:
        """菜单与提示里用的名字：中日排版术语 + 直观说明。"""
        return _DIRECTION_LABELS[self]

    @property
    def vertical(self) -> bool:
        return self is TextDirection.VERTICAL

    @classmethod
    def from_name(cls, name: object) -> TextDirection:
        """按存进设置的名字取方向；名字不认识时回退到默认的横排。"""
        if isinstance(name, str):
            for direction in cls:
                if direction.value == name:
                    return direction
        return DEFAULT_TEXT_DIRECTION


#: 默认排版方向：横排（横書き）
DEFAULT_TEXT_DIRECTION = TextDirection.HORIZONTAL

_DIRECTION_LABELS = {
    TextDirection.HORIZONTAL: "横排 / 横書き（左→右排布，上→下换行）",
    TextDirection.VERTICAL: "竖排 / 縦書き（上→下排布，右→左换列）",
}


# ---------------------------------------------------------------------- 排版


def _display_text(text: str) -> tuple[str, bool]:
    """把原始文本整理成要画的字符串，返回（文本，是不是"没识别"的提示）。"""
    stripped = (text or "").strip()
    if not stripped:
        return EMPTY_HINT, True
    if len(stripped) > MAX_CHARS:
        return stripped[:MAX_CHARS].rstrip() + "…", False
    return stripped, False


def _measure_lines(display: str, metrics: QFontMetricsF, inner_width: float) -> QSizeF:
    """横排文本在 ``inner_width`` 宽度里折行后要占的地方。"""
    rect = metrics.boundingRect(
        QRectF(0.0, 0.0, max(inner_width, 1.0), 100000.0), _HORIZONTAL_FLAGS, display
    )
    return QSizeF(max(rect.width(), 1.0), max(rect.height(), 1.0))


def _layout_columns(
    display: str, metrics: QFontMetricsF, inner_height: float
) -> tuple[list[list[str]], QSizeF]:
    """竖排分列：一列从上往下填满 ``inner_height`` 再换列，原文的换行也换列。

    返回每一列的字符与单格尺寸；格子取正方（宽 = 高），这样中日文字逐格对齐，
    拉丁字母虽然不会被转 90°，摆出来也不会挤在一起。
    """
    cell_height = max(metrics.height(), 1.0)
    per_column = max(1, int(inner_height // cell_height))
    columns: list[list[str]] = []
    for paragraph in display.split("\n"):
        column: list[str] = []
        for char in paragraph:
            column.append(char)
            if len(column) >= per_column:
                columns.append(column)
                column = []
        if column:
            columns.append(column)
    widest = max(
        (metrics.horizontalAdvance(char) for char in display if char != "\n"), default=0.0
    )
    return columns, QSizeF(max(widest, cell_height), cell_height)


def _columns_size(columns: list[list[str]], cell: QSizeF) -> QSizeF:
    """竖排分列后的文本区尺寸。"""
    rows = max((len(column) for column in columns), default=1)
    return QSizeF(cell.width() * max(len(columns), 1), cell.height() * rows)


def _measure_text(
    *,
    display: str,
    metrics: QFontMetricsF,
    direction: TextDirection,
    cross: float | None,
    viewport: QSizeF,
) -> tuple[QSizeF, list[list[str]], QSizeF]:
    """算出文本区尺寸（不含留白）与竖排分列。

    ``cross`` 是希望对齐的尺寸（横排 = 标注的屏幕宽，竖排 = 标注的屏幕高）。
    能排得下就用它撑满，排不下或没有视图信息时退回文字自然尺寸。
    """
    if direction.vertical:
        natural_columns, natural_cell = _layout_columns(display, metrics, MAX_COLUMN_HEIGHT)
        inner = (cross - 2 * PADDING) if cross is not None else 0.0
        if (
            cross is not None
            and inner >= metrics.height()
            and (viewport.isEmpty() or cross <= viewport.height())
        ):
            columns, cell = _layout_columns(display, metrics, inner)
            return QSizeF(_columns_size(columns, cell).width(), inner), columns, cell
        return _columns_size(natural_columns, natural_cell), natural_columns, natural_cell

    inner = (cross - 2 * PADDING) if cross is not None else 0.0
    if cross is not None and inner > 0 and (viewport.isEmpty() or cross <= viewport.width()):
        size = _measure_lines(display, metrics, inner)
        if size.width() <= inner + 0.5:  # 折得动：至少放得下一个字
            return QSizeF(inner, size.height()), [], QSizeF()
    size = _measure_lines(display, metrics, MAX_LINE_WIDTH)
    return size, [], QSizeF()


def _fits_in(
    size: QSizeF, *, direction: TextDirection, room: float, viewport: QSizeF
) -> bool:
    """框（含留白）放不放得下：排版轴上有余量，横轴上不超出视口。"""
    if viewport.isEmpty():
        return True
    if direction.vertical:
        return size.width() <= room + 0.5 and size.height() <= viewport.height() + 0.5
    return size.height() <= room + 0.5 and size.width() <= viewport.width() + 0.5


class RoiTextBox(QGraphicsItem):
    """贴在 ROI 旁边的文本小框（作为 ``RoiItem`` 的子图元）。

    本地坐标的单位就是屏幕像素（见 ``ItemIgnoresTransformations``），原点放在
    标注的角上，内容按 :meth:`set_after` 决定往哪个方向铺。视图通过
    :meth:`apply_placement` 把"对齐尺寸、余量、视口"喂进来，框据此决定尺寸。
    """

    def __init__(
        self,
        *,
        direction: TextDirection = DEFAULT_TEXT_DIRECTION,
        font_size: float = DEFAULT_FONT_SIZE,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(3.0)  # 压在标注与手柄上面
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)  # 不抢画布的鼠标操作

        self._font = QFont()
        self._font.setPointSizeF(min(max(font_size, MIN_FONT_SIZE), MAX_FONT_SIZE))
        self._metrics = QFontMetricsF(self._font)
        self._direction = direction
        self._text = ""
        self._display = EMPTY_HINT
        self._hint = True
        self._after = True  # 横排 = 标注下方，竖排 = 标注右侧
        self._content = QRectF()
        self._columns: list[list[str]] = []
        self._cell = QSizeF(1.0, 1.0)

        # 视图喂进来的现场：对齐尺寸（ROI 屏幕宽/高）、排版轴上的余量、视口大小
        self._placed = False
        self._cross: float | None = None
        self._room = 0.0
        self._viewport = QSizeF()
        self._relayout()

    # ------------------------------------------------------------------ 数据

    def text(self) -> str:
        """这个框要显示的原始文本（空串表示还没识别过）。"""
        return self._text

    def set_text(self, text: str) -> None:
        text = text or ""
        if text == self._text:
            return
        self._text = text
        self._relayout()
        self.update()

    def direction(self) -> TextDirection:
        return self._direction

    def set_direction(self, direction: TextDirection) -> None:
        if direction is self._direction:
            return
        self._direction = direction
        self._relayout()
        self.update()

    def font_size(self) -> float:
        """当前字号（pt）。"""
        return self._font.pointSizeF()

    def set_font_size(self, size: float) -> None:
        size = max(min(float(size), MAX_FONT_SIZE), MIN_FONT_SIZE)
        if abs(size - self._font.pointSizeF()) < _EPS:
            return
        self._font.setPointSizeF(size)
        self._metrics = QFontMetricsF(self._font)
        self._relayout()
        self.update()

    def after(self) -> bool:
        """内容铺在标注的"后面"一侧吗：横排 = 下方，竖排 = 右侧。"""
        return self._after

    def set_after(self, after: bool) -> None:
        if after == self._after:
            return
        self._after = after
        self._relayout()
        self.update()

    def display_text(self) -> str:
        """实际画出来的文本：空文本换成提示、超长截断加省略号。"""
        return self._display

    def content_rect(self) -> QRectF:
        """框（含边框）在本地坐标里的矩形，单位是屏幕像素。"""
        return QRectF(self._content)

    # ------------------------------------------------------------ 视图接线

    def apply_placement(
        self, *, cross: float | None, room: float, viewport: QSizeF
    ) -> None:
        """视图告诉它：要对齐的尺寸、排版轴上的余量、视口大小，然后重算尺寸。"""
        self._placed = True
        self._cross = cross if cross is not None and cross > 0 else None
        self._room = max(float(room), 0.0)
        self._viewport = QSizeF(viewport)
        self._relayout()
        self.update()

    def fits_font_size(self, size: float) -> bool:
        """换成 ``size`` 号字后，整段文字还放得下吗？（滚轮调字号的判据）"""
        if size > MAX_FONT_SIZE:
            return False
        if not self._placed or abs(size - self.font_size()) < _EPS:
            return True
        probe = QFont(self._font)
        probe.setPointSizeF(size)
        total, _, _ = self._layout(self._display, QFontMetricsF(probe))
        return _fits_in(
            total,
            direction=self._direction,
            room=self._room,
            viewport=self._viewport,
        )

    # -------------------------------------------------------------- 绘制用

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt 命名)
        return self._content.adjusted(-1.0, -1.0, 1.0, 1.0)  # 留 1 像素给画笔

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(_BORDER_COLOR, 1.0))
        painter.setBrush(QBrush(_BACKGROUND))
        painter.drawRoundedRect(self._content, BORDER_RADIUS, BORDER_RADIUS)

        area = self._content.adjusted(PADDING, PADDING, -PADDING, -PADDING)
        painter.setFont(self._font)
        painter.setPen(QPen(_HINT_COLOR if self._hint else _TEXT_COLOR))
        if self._direction.vertical:
            self._paint_vertical(painter, area)
        else:
            painter.drawText(area, _HORIZONTAL_FLAGS, self._display)
        painter.restore()

    def _paint_vertical(self, painter: QPainter, area: QRectF) -> None:
        cell_w, cell_h = self._cell.width(), self._cell.height()
        for index, column in enumerate(self._columns):
            # 第一列贴最右，后面的列一列一列往左排
            left = area.right() - (index + 1) * cell_w
            for row, char in enumerate(column):
                cell = QRectF(left, area.top() + row * cell_h, cell_w, cell_h)
                painter.drawText(cell, _CENTER_FLAGS, char)

    # ------------------------------------------------------------------ 内部

    def _layout(self, display: str, metrics: QFontMetricsF) -> tuple[QSizeF, list, QSizeF]:
        """按当前方向与现场尺寸排一遍版，返回（含留白的框尺寸, 列, 单格）。"""
        cross = self._cross
        if self._hint:
            cross = None  # 提示文字不值得撑满标注的宽度
        text_size, columns, cell = _measure_text(
            display=display,
            metrics=metrics,
            direction=self._direction,
            cross=cross,
            viewport=self._viewport,
        )
        total = QSizeF(text_size.width() + 2 * PADDING, text_size.height() + 2 * PADDING)
        return total, columns, cell

    def _relayout(self) -> None:
        """重算要画的文本、框的大小，以及在本地坐标里的位置。"""
        self._display, self._hint = _display_text(self._text)
        size, columns, cell = self._layout(self._display, self._metrics)
        self._columns, self._cell = columns, cell
        if self._direction.vertical:
            left = GAP if self._after else -GAP - size.width()
            content = QRectF(left, 0.0, size.width(), size.height())
        else:
            top = GAP if self._after else -GAP - size.height()
            content = QRectF(0.0, top, size.width(), size.height())
        if content != self._content:
            self.prepareGeometryChange()  # boundingRect 要变了，先跟场景打招呼
            self._content = content
