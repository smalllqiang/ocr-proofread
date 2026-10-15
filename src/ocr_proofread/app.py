"""主窗口：打开图像 / 文件夹、切换上一张下一张、缩放、ROI 标注与排序、OCR、状态持久化。

ROI 图元的挂载点与生命周期约定见 :mod:`ocr_proofread.image_view`；
"每张图一份标注"的存取见 :mod:`ocr_proofread.annotations`；
OCR 引擎与后台识别任务见 :mod:`ocr_proofread.ocr`。

跨图状态的唯一持有者是这里：视图只持有当前这一张图，切图时本模块负责把
上一张的标注落盘、把下一张的标注装回场景（见 :meth:`MainWindow._show`）。
识别任务也在这里收口：结果先在子线程算出来，再写回图元并随标注一起落盘。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, QThread
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
)

from .annotations import RoiStore
from .export import (
    EXPORT_NAME_FILTER,
    EXPORT_SUFFIX,
    build_text,
    default_export_path,
    write_export,
)
from .image_view import ImageView
from .images import IMAGE_NAME_FILTER, collect_images, is_image_file, load_qimage
from .ocr import (
    DEFAULT_MODEL,
    OCR_MODELS,
    OcrEngine,
    OcrError,
    OcrTask,
    create_engine,
    crop_roi,
    is_known_model,
    model_label,
)
from .text_box import (
    DEFAULT_FONT_SIZE,
    DEFAULT_TEXT_DIRECTION,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    TextDirection,
)

if TYPE_CHECKING:  # 只用于类型标注，避免运行时多一层依赖
    from .annotations import Roi
    from .roi_item import RoiItem

APP_NAME = "OCR 校对工具"

#: 判断两个矩形是不是同一处（OCR 结果兜底合并时用，允许浮点误差）
_RECT_TOLERANCE = 0.5


class _OcrJob(QObject):
    """一次识别任务的现场，同时是跨线程信号的落点。

    ``OcrTask`` 在子线程里发信号，而下面这些槽属于住在界面线程的 ``_OcrJob``，
    Qt 因此会把它们排队回界面线程执行——界面状态自始至终只有界面线程在改。
    （直接把信号接到 lambda 上是不行的：没有 QObject 接收者，槽会在发信号的
    子线程里跑。）
    """

    def __init__(
        self,
        window: MainWindow,
        path: Path,
        model: str,
        items: list[RoiItem],
        rois: list[Roi],
        task: OcrTask,
        thread: QThread,
    ) -> None:
        super().__init__(window)  # 父对象在界面线程，本对象也归属界面线程
        self.window = window
        self.path = path
        self.model = model
        self.items = items
        self.rois = rois
        self.task = task
        self.thread = thread
        task.progressed.connect(self._relay_progress)
        task.finished.connect(self._relay_finished)
        task.finished.connect(thread.quit)

    def _relay_progress(self, done: int, total: int) -> None:
        self.window._on_ocr_progressed(self, done, total)

    def _relay_finished(self, cancelled: bool) -> None:
        self.window._on_ocr_finished(self, cancelled)


def _same_rect(left, right) -> bool:
    """两个矩形是否基本上是同一处（坐标存盘会四舍五入到 2 位小数）。"""
    return (
        abs(left.x() - right.x()) <= _RECT_TOLERANCE
        and abs(left.y() - right.y()) <= _RECT_TOLERANCE
        and abs(left.width() - right.width()) <= _RECT_TOLERANCE
        and abs(left.height() - right.height()) <= _RECT_TOLERANCE
    )


def _table_cell(text: str, *, editable: bool = False) -> QTableWidgetItem:
    """OCR 结果面板里的单元格；只有识别文本那一列允许编辑。"""
    cell = QTableWidgetItem(text)
    if not editable:
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return cell


class _RoiTextDelegate(QStyledItemDelegate):
    """识别文本列的显示代理：没识别过（空串）显示成「—」，双击编辑时是空串。

    底层数据只存一份文本，占位符只在绘制时出现，所以"清空文本"不会被写进「—」。
    """

    def displayText(self, value, locale) -> str:  # noqa: N802 (Qt 命名)
        return str(value) if value else "—"


class MainWindow(QMainWindow):
    def __init__(self, *, ocr_factory: Callable[[str], OcrEngine] | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.setAcceptDrops(True)

        self._paths: list[Path] = []
        self._index = -1
        self._image_size = QSize()
        self._settings = QSettings("ocr-proofread", "ocr-proofread")
        self._roi_store = RoiStore()
        self._text_direction = self._restore_text_direction()
        self._text_font_size = self._restore_text_font_size()

        # OCR：引擎按模型名字缓存，模型很重，不能每识别一次就加载一遍
        self._ocr_factory = ocr_factory or create_engine
        self._ocr_engines: dict[str, OcrEngine] = {}
        self._ocr_model = self._restore_ocr_model()
        self._ocr_job: _OcrJob | None = None
        self._panel_items: list[RoiItem] = []

        self.view = ImageView(self)
        self.view.set_text_direction(self._text_direction)
        self.view.set_text_font_size(self._text_font_size)
        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.view.roisChanged.connect(self._update_roi_label)
        self.view.textFontChanged.connect(self._on_text_font_changed)
        self.view.textFontLimitReached.connect(self._on_text_font_limit)
        self.setCentralWidget(self.view)
        # 空格是"按住"的修饰键，得知道按下与松开两个时刻；焦点可能不在视图上
        # （刚点过右侧结果面板），所以装在应用级过滤，见 :meth:`MainWindow.eventFilter`
        QApplication.instance().installEventFilter(self)

        self._label_file = QLabel("未打开图像 —— Ctrl+Shift+O 打开文件夹")
        self._label_roi = QLabel("")
        self._label_zoom = QLabel("")
        status = self.statusBar()
        status.addWidget(self._label_file, 1)
        status.addPermanentWidget(self._label_roi)
        status.addPermanentWidget(self._label_zoom)

        self._create_actions()
        self._create_ocr_panel()
        self._create_menus()
        self._create_toolbar()
        self._restore_settings()
        self._update_actions()

    # ------------------------------------------------------------------ 动作

    def _make_action(
        self,
        text: str,
        *,
        shortcuts: list[str] | None = None,
        slot=None,
        tip: str | None = None,
        checkable: bool = False,
    ) -> QAction:
        action = QAction(text, self)
        if shortcuts:
            action.setShortcuts([QKeySequence(s) for s in shortcuts])
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        if tip:
            action.setStatusTip(tip)
        action.setCheckable(checkable)
        if slot is not None:
            action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _create_actions(self) -> None:
        self.act_open_files = self._make_action(
            "导入图像(&F)…",
            shortcuts=["Ctrl+O"],
            slot=self._open_files,
            tip="选择一个或多个图像文件",
        )
        self.act_open_folder = self._make_action(
            "打开文件夹(&D)…",
            shortcuts=["Ctrl+Shift+O"],
            slot=self._open_folder,
            tip="打开一个文件夹，按文件名自然顺序浏览其中的图像",
        )
        self.act_close = self._make_action("关闭当前图像", slot=self._close_image)
        self.act_quit = self._make_action(
            "退出(&Q)", shortcuts=["Ctrl+Q"], slot=self.close, tip="退出程序"
        )

        self.act_prev = self._make_action(
            "上一张(&P)",
            shortcuts=["Left", "A", "PgUp", "Backspace"],
            slot=self.show_prev,
            tip="上一张图像",
        )
        self.act_next = self._make_action(
            "下一张(&N)",
            shortcuts=["Right", "D", "PgDown"],
            slot=self.show_next,
            tip="下一张图像",
        )
        self.act_first = self._make_action(
            "第一张", shortcuts=["Home"], slot=self.show_first, tip="跳到第一张"
        )
        self.act_last = self._make_action(
            "最后一张", shortcuts=["End"], slot=self.show_last, tip="跳到最后一张"
        )

        self.act_zoom_in = self._make_action(
            "放大",
            shortcuts=["Ctrl+=", "Ctrl++"],
            slot=self.view.zoom_in,
            tip="放大（也可直接滚轮）",
        )
        self.act_zoom_out = self._make_action(
            "缩小", shortcuts=["Ctrl+-"], slot=self.view.zoom_out, tip="缩小（也可直接滚轮）"
        )
        self.act_fit = self._make_action(
            "适应窗口",
            shortcuts=["Ctrl+0", "F"],
            slot=self.view.fit_to_window,
            tip="整张图缩放至窗口内",
        )
        self.act_actual = self._make_action(
            "实际大小 (100%)",
            shortcuts=["Ctrl+1"],
            slot=self.view.reset_zoom,
            tip="一个图像像素对应一个屏幕像素",
        )

        self.act_delete_roi = self._make_action(
            "删除选中标注(&D)",
            shortcuts=["Delete"],
            slot=self._delete_selected_rois,
            tip="删除当前选中的矩形标注",
        )
        self.act_clear_rois = self._make_action(
            "清空当前图标注(&C)",
            slot=self._clear_rois,
            tip="删除当前图像上的全部矩形标注",
        )
        self.act_sort_rois = self._make_action(
            "ROI 排序模式(&O)",
            shortcuts=["Ctrl+Shift+S"],
            slot=self._toggle_sort_rois,
            tip="进入后按点击顺序给 ROI 编号（1、2、3…），Esc 退出",
            checkable=True,
        )
        self.act_save_rois = self._make_action(
            "保存标注(&S)",
            shortcuts=["Ctrl+S"],
            slot=self._save_rois,
            tip="把当前图像的标注写入图像旁边的 .roi.json 文件",
        )

        self.act_export_text = self._make_action(
            "导出文本(&T)",
            shortcuts=["Ctrl+E"],
            slot=self._export_text,
            tip="把已打开图像的 ROI 文本导出到图像文件夹里的 <文件夹名>.txt",
        )
        self.act_export_text_as = self._make_action(
            "导出文本到(&A)…",
            shortcuts=["Ctrl+Shift+E"],
            slot=self._export_text_as,
            tip="选择导出文件的位置，内容与「导出文本」相同",
        )

        self.act_ocr_selected = self._make_action(
            "识别选中标注(&S)",
            shortcuts=["Ctrl+R"],
            slot=self._ocr_selected,
            tip="用当前 OCR 模型识别选中的矩形标注",
        )
        self.act_ocr_all = self._make_action(
            "识别全部标注(&A)",
            shortcuts=["Ctrl+Shift+R"],
            slot=self._ocr_all,
            tip="用当前 OCR 模型识别当前图像上的全部标注",
        )

    def _create_ocr_panel(self) -> None:
        """右侧的 OCR 结果面板：一行一个 ROI，点行选中对应的框，双击文本可改。"""
        self._panel = QTableWidget(0, 3, self)
        self._panel.setHorizontalHeaderLabels(["#", "位置 / 尺寸", "识别文本"])
        self._panel.verticalHeader().setVisible(False)
        # 只有识别文本一列是可编辑的（单元格自身的 ItemIsEditable 标志决定），
        # 双击（或 F2）进入编辑，改完写回图元并落盘
        self._panel.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._panel.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._panel.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._panel.setWordWrap(True)  # 识别文本宁可换行也不要被省略号截掉
        header = self._panel.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._panel.setItemDelegateForColumn(2, _RoiTextDelegate(self._panel))
        self._panel.itemSelectionChanged.connect(self._on_panel_selection_changed)
        self._panel.itemChanged.connect(self._on_panel_item_changed)

        dock = QDockWidget("OCR 结果", self)
        dock.setObjectName("ocr_panel")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setWidget(self._panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.resizeDocks([dock], [380], Qt.Orientation.Horizontal)  # 给识别文本留出宽度
        self._ocr_dock = dock

        # 交给 Qt 的 toggleViewAction，菜单勾选状态与浮动/关闭按钮自动同步
        self.act_toggle_ocr_panel = dock.toggleViewAction()
        self.act_toggle_ocr_panel.setText("识别结果面板(&R)")
        self.act_toggle_ocr_panel.setStatusTip("显示或隐藏右侧的 OCR 结果列表")

        self.view.roisChanged.connect(self._refresh_ocr_panel)
        self.view.roisChanged.connect(self._update_actions)
        self.view.roiOrderChanged.connect(self._on_roi_order_changed)
        self.view.sortModeChanged.connect(self._on_sort_mode_changed)
        self.view.sortPicked.connect(self._on_sort_picked)
        self.view.roiTextChanged.connect(self._on_roi_text_changed)
        self.view.selectionChanged.connect(self._sync_panel_selection)
        self.view.selectionChanged.connect(self._update_actions)

    def _create_menus(self) -> None:
        bar = self.menuBar()

        menu_file = bar.addMenu("文件(&F)")
        menu_file.addAction(self.act_open_files)
        menu_file.addAction(self.act_open_folder)
        menu_file.addSeparator()
        menu_file.addAction(self.act_close)
        menu_file.addSeparator()
        menu_file.addAction(self.act_quit)

        menu_view = bar.addMenu("视图(&V)")
        menu_view.addAction(self.act_zoom_in)
        menu_view.addAction(self.act_zoom_out)
        menu_view.addSeparator()
        menu_view.addAction(self.act_fit)
        menu_view.addAction(self.act_actual)
        menu_view.addSeparator()
        menu_direction = menu_view.addMenu("文本框排版方向(&T)")
        self._direction_actions: dict[TextDirection, QAction] = {}
        for direction in TextDirection:
            action = QAction(direction.label, self)
            action.setCheckable(True)
            action.setChecked(direction is self._text_direction)
            action.setStatusTip(f"选中的标注旁，文本框按{direction.label}显示")
            action.triggered.connect(
                lambda _checked=False, value=direction: self._set_text_direction(value)
            )
            menu_direction.addAction(action)
            self._direction_actions[direction] = action

        menu_browse = bar.addMenu("浏览(&B)")
        menu_browse.addAction(self.act_prev)
        menu_browse.addAction(self.act_next)
        menu_browse.addSeparator()
        menu_browse.addAction(self.act_first)
        menu_browse.addAction(self.act_last)

        menu_roi = bar.addMenu("标注(&R)")
        menu_roi.addAction(self.act_delete_roi)
        menu_roi.addAction(self.act_clear_rois)
        menu_roi.addSeparator()
        menu_roi.addAction(self.act_sort_rois)
        menu_roi.addSeparator()
        menu_roi.addAction(self.act_save_rois)

        menu_export = bar.addMenu("导出(&E)")
        menu_export.addAction(self.act_export_text)
        menu_export.addAction(self.act_export_text_as)

        menu_ocr = bar.addMenu("OCR(&O)")
        menu_ocr.addAction(self.act_ocr_selected)
        menu_ocr.addAction(self.act_ocr_all)
        menu_ocr.addSeparator()
        menu_model = menu_ocr.addMenu("识别模型(&M)")
        self._model_actions: dict[str, QAction] = {}
        for model in OCR_MODELS:
            action = QAction(model.label, self)
            action.setCheckable(True)
            action.setChecked(model.name == self._ocr_model)
            action.setStatusTip(f"用 {model.label} 识别")
            action.triggered.connect(
                lambda _checked=False, name=model.name: self._set_ocr_model(name)
            )
            menu_model.addAction(action)
            self._model_actions[model.name] = action
        menu_ocr.addSeparator()
        menu_ocr.addAction(self.act_toggle_ocr_panel)

        menu_help = bar.addMenu("帮助(&H)")
        menu_help.addAction(
            self._make_action("快捷键(&K)", slot=self._show_shortcuts, tip="查看快捷键列表")
        )

    def _create_toolbar(self) -> None:
        bar = self.addToolBar("主工具栏")
        bar.setMovable(False)
        bar.addAction(self.act_open_folder)
        bar.addAction(self.act_open_files)
        bar.addSeparator()
        bar.addAction(self.act_prev)
        bar.addAction(self.act_next)
        bar.addSeparator()
        bar.addAction(self.act_zoom_out)
        bar.addAction(self.act_zoom_in)
        bar.addAction(self.act_fit)
        bar.addAction(self.act_actual)
        bar.addSeparator()
        bar.addAction(self.act_ocr_selected)
        bar.addAction(self.act_ocr_all)

    # ------------------------------------------------------------------ 打开

    def _start_dir(self) -> str:
        remembered = self._settings.value("last_dir", "", type=str)
        if remembered and Path(remembered).is_dir():
            return remembered
        return str(Path.home())

    def _open_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "导入图像", self._start_dir(), IMAGE_NAME_FILTER
        )
        if files:
            self.open_paths([Path(f) for f in files])

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择图像文件夹", self._start_dir())
        if not folder:
            return
        paths = collect_images(folder)
        if not paths:
            QMessageBox.information(
                self, "没有找到图像", f"该文件夹下没有受支持的图像文件：\n{folder}"
            )
            return
        self.open_paths(paths)

    def open_paths(self, paths: list[Path], index: int = 0) -> None:
        """打开一组图像，并显示第 ``index`` 张。"""
        paths = [Path(p) for p in paths]
        if not paths:
            return
        self._cancel_ocr()  # 先停下正在跑的识别，并把已出的结果写回
        self._flush_rois()  # 再送走上一组里当前这张图的标注
        self._paths = paths
        self._index = -1
        self._remember_dir(paths[0].parent)
        self._show(index)

    def open_from_args(self, args: list[str]) -> None:
        """处理命令行参数：文件夹按顺序收集，文件按给定顺序打开。"""
        collected: list[Path] = []
        for raw in args:
            path = Path(raw).expanduser()
            if path.is_dir():
                collected.extend(collect_images(path))
            elif path.is_file() and is_image_file(path):
                collected.append(path)
        if collected:
            self.open_paths(collected)

    # ------------------------------------------------------------------ 浏览

    def _show(self, index: int) -> None:
        if not self._paths:
            return
        self._cancel_ocr()  # 识别到一半就切图：先停下，把已有结果写回上一张
        index = max(0, min(index, len(self._paths) - 1))
        path = self._paths[index]

        error: str | None = None
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            image: QImage | None = load_qimage(path)
        except (ValueError, OSError) as exc:
            image, error = None, str(exc)
        finally:
            QGuiApplication.restoreOverrideCursor()

        if image is None:
            self.statusBar().showMessage(f"无法打开 {path.name}：{error}", 6000)
            return

        self._flush_rois()  # 离开上一张图之前先保存它的标注
        self._index = index
        self._image_size = image.size()
        self.view.set_image(image)
        self.view.set_rois(self._roi_store.get(path))  # 装回这张图的标注
        self._update_status()
        self._update_actions()

    def show_prev(self) -> None:
        if self._index > 0:
            self._show(self._index - 1)

    def show_next(self) -> None:
        if 0 <= self._index < len(self._paths) - 1:
            self._show(self._index + 1)

    def show_first(self) -> None:
        if self._paths:
            self._show(0)

    def show_last(self) -> None:
        if self._paths:
            self._show(len(self._paths) - 1)

    def _close_image(self) -> None:
        self._cancel_ocr()
        self._flush_rois()
        self._paths = []
        self._index = -1
        self._image_size = QSize()
        self.view.set_image(None)
        self._refresh_ocr_panel()  # 图元已经随场景一起没了，面板也要清空
        self._update_status()
        self._update_actions()

    # ------------------------------------------------------------------ 标注

    def _current_path(self) -> Path | None:
        if self._index < 0 or self._index >= len(self._paths):
            return None
        return self._paths[self._index]

    def _flush_rois(self) -> None:
        """把场景里的标注写回当前这张图；没有图像时什么都不做。

        每次切图、重新打开文件夹、关闭窗口都会调用；:class:`RoiStore` 只在内容
        真的变了才落盘，所以没标注过的图不会多出文件。开头先收掉正在就地编辑的
        文本框，正在改的字才不会随着场景一起消失。
        """
        self.view.commit_text_edit()
        path = self._current_path()
        if path is None or not self.view.has_image:
            return
        try:
            self._roi_store.set(path, self.view.rois())
        except OSError as exc:
            self.statusBar().showMessage(f"保存 {path.name} 的标注失败：{exc}", 8000)

    def _save_rois(self) -> None:
        """Ctrl+S：立刻把当前图的标注写盘（平时切图会自动保存）。"""
        path = self._current_path()
        if path is None:
            return
        self.view.commit_text_edit()  # 正在手改的文本也要一起存下去
        try:
            self._roi_store.set(path, self.view.rois())
        except OSError as exc:
            self.statusBar().showMessage(f"保存 {path.name} 的标注失败：{exc}", 8000)
            return
        count = self.view.roi_count()
        self.statusBar().showMessage(f"已保存 {path.name} 的 {count} 个标注", 4000)

    def _delete_selected_rois(self) -> None:
        count = self.view.delete_selected_rois()
        if count:
            self.statusBar().showMessage(f"已删除 {count} 个标注", 4000)
        else:
            self.statusBar().showMessage("没有选中的标注", 3000)

    def _clear_rois(self) -> None:
        count = self.view.roi_count()
        if not count:
            self.statusBar().showMessage("当前图没有标注", 3000)
            return
        self.view.clear_rois()
        self.statusBar().showMessage(
            f"已清空当前图的 {count} 个标注（切图或 Ctrl+S 时写入文件）", 5000
        )

    def _update_actions(self) -> None:
        has_prev = self._index > 0
        has_next = 0 <= self._index < len(self._paths) - 1
        self.act_prev.setEnabled(has_prev)
        self.act_first.setEnabled(has_prev)
        self.act_next.setEnabled(has_next)
        self.act_last.setEnabled(has_next)
        has_image = self._index >= 0
        self.act_close.setEnabled(has_image)
        self.act_zoom_in.setEnabled(has_image)
        self.act_zoom_out.setEnabled(has_image)
        self.act_fit.setEnabled(has_image)
        self.act_actual.setEnabled(has_image)
        self.act_delete_roi.setEnabled(has_image)
        self.act_clear_rois.setEnabled(has_image)
        self.act_save_rois.setEnabled(has_image)
        self.act_export_text.setEnabled(has_image)
        self.act_export_text_as.setEnabled(has_image)

        busy = self._ocr_job is not None
        items = self.view.roi_items() if has_image else []
        self.act_sort_rois.setEnabled(bool(items))
        self.act_ocr_all.setEnabled(bool(items) and not busy)
        self.act_ocr_selected.setEnabled(
            any(item.isSelected() for item in items) and not busy
        )

    # -------------------------------------------------------------- ROI 排序

    def _toggle_sort_rois(self) -> None:
        """Ctrl+Shift+S：进入 / 退出 ROI 排序模式（只作用于当前这张图）。"""
        if self._current_path() is None:
            self.act_sort_rois.setChecked(False)
            self.statusBar().showMessage("还没有打开图像", 3000)
            return
        self.view.set_sort_mode(self.act_sort_rois.isChecked())

    def _on_sort_mode_changed(self, enabled: bool) -> None:
        """视图进出排序模式后，同步菜单勾选与状态栏提示。"""
        self.act_sort_rois.setChecked(enabled)
        if enabled:
            count = self.view.roi_count()
            self.statusBar().showMessage(
                f"ROI 排序模式：按点击顺序给这 {count} 个标注编号（1、2、3…），Esc 退出",
                8000,
            )
        else:
            self.statusBar().showMessage("已退出 ROI 排序模式", 3000)
        self._update_actions()

    def _on_sort_picked(self, item: RoiItem, number: int) -> None:
        """每点中一个标注就报一下进度；点到已经排过号的框时说明原因。"""
        if number:
            total = self.view.roi_count()
            self.statusBar().showMessage(
                f"已编号 {number}/{total}：这个标注排在第 {number} 位（Esc 退出排序模式）",
                5000,
            )
        else:
            self.statusBar().showMessage(f"这个标注已经排在第 {item.order()} 位", 3000)

    def _on_roi_order_changed(self) -> None:
        """顺序变了：结果面板跟着重排，并立刻写盘（排一次序不该等切图才保存）。"""
        self._refresh_ocr_panel()
        self._flush_rois()

    # ------------------------------------------------------------------ 导出

    def _export_folder(self) -> Path | None:
        """默认导出目录：当前这批图像所在的文件夹（来自不同文件夹时用第一个）。

        打开文件夹时这批图都在这个文件夹里；导入若干图像时就是**第一个图像**所在
        的文件夹。没有打开图像、或这个文件夹已经不在了，返回 ``None``。
        """
        if not self._paths:
            return None
        folder = self._paths[0].parent
        return folder if folder.is_dir() else None

    def _export_text(self) -> None:
        """Ctrl+E：导出文本到图像文件夹里的 ``<文件夹名>.txt``，不弹保存对话框。"""
        folder = self._export_folder()
        if folder is None:
            self._warn_export_unavailable()
            return
        self._export_to(default_export_path(folder))

    def _export_text_as(self) -> None:
        """Ctrl+Shift+E：先弹保存对话框；用户没选就退回，不作任何提示。"""
        folder = self._export_folder()
        if folder is None and not self._paths:
            self._warn_export_unavailable()
            return
        if folder is not None:  # 文件夹还在：默认位置与文件名不变
            start = default_export_path(folder)
        else:  # 文件夹没了：退到上次用过的目录，文件名只能自己起一个
            start = Path(self._start_dir()) / f"export{EXPORT_SUFFIX}"
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出文本到", str(start), EXPORT_NAME_FILTER
        )
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:  # 只填了名字时补上 .txt，填了别的后缀就听用户的
            path = path.with_suffix(EXPORT_SUFFIX)
        self._export_to(path)

    def _warn_export_unavailable(self) -> None:
        """默认导出位置拿不到时的报错（没有打开图像 / 文件夹已经不在了）。"""
        if not self._paths:
            message = "还没有打开图像，先打开文件夹或导入图像再导出。"
        else:
            message = (
                "找不到图像所在的文件夹，无法确定默认的导出位置：\n"
                f"{self._paths[0].parent}\n\n"
                "可以改用「导出 → 导出文本到」自己选一个位置。"
            )
        QMessageBox.warning(self, "无法导出", message)

    def _export_to(self, path: Path) -> None:
        """把当前这批图像的 ROI 文本拼好写进 ``path``。"""
        self._flush_rois()  # 当前图还没落盘的改动先写回，导出的就是最新内容
        pages = self._export_pages()
        if not pages:
            self.statusBar().showMessage("没有可导出的标注：已打开的图都没有 ROI", 5000)
            return
        try:
            write_export(path, build_text(pages))
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"写入 {path} 失败：\n{exc}")
            return
        count = sum(len(texts) for _, texts in pages)
        self.statusBar().showMessage(
            f"已导出 {len(pages)} 张图、{count} 个标注的文本到 {path}", 8000
        )

    def _export_pages(self) -> list[tuple[str, list[str]]]:
        """按工具里的图像顺序收集要导出的内容：没有 ROI 的图整张跳过。

        读的是 :class:`RoiStore`（内存缓存 + 边车文件）：没有边车文件的图和"有文件但
        一条记录都没有"的图一样，这里读出来都是空列表，于是被整张跳过。当前这张图
        的改动由 :meth:`_export_to` 开头的 ``_flush_rois()`` 先写回，所以拿到的是最新内容。
        """
        pages: list[tuple[str, list[str]]] = []
        for path in self._paths:
            rois = self._roi_store.get(path)
            if not rois:
                continue
            pages.append((path.name, [roi.text for roi in rois]))
        return pages

    # -------------------------------------------------------------- 文本框排版

    def _restore_text_direction(self) -> TextDirection:
        """从设置里取文本框的排版方向；名字已经不认识时退回默认的横排。"""
        stored = self._settings.value(
            "text_direction", DEFAULT_TEXT_DIRECTION.value, type=str
        )
        return TextDirection.from_name(stored)

    def _set_text_direction(self, direction: TextDirection) -> None:
        """切换标注旁文本框的排版方向（记进 QSettings，下次启动继续用）。"""
        self._text_direction = direction
        self._settings.setValue("text_direction", direction.value)
        self.view.set_text_direction(direction)
        self._sync_text_direction_actions()
        self.statusBar().showMessage(f"文本框排版方向：{direction.label}", 4000)

    def _sync_text_direction_actions(self) -> None:
        for value, action in self._direction_actions.items():
            action.setChecked(value is self._text_direction)

    def _restore_text_font_size(self) -> float:
        """从设置里取文本框字号；超出允许范围就夹回范围。"""
        stored = self._settings.value("text_font_size", DEFAULT_FONT_SIZE, type=float)
        return max(min(stored, MAX_FONT_SIZE), MIN_FONT_SIZE)

    def _on_text_font_changed(self, size: float) -> None:
        """在文本框上滚轮改了字号：记进设置并提示一下。"""
        self._settings.setValue("text_font_size", size)
        self.statusBar().showMessage(f"文本框字号：{size:g} pt", 3000)

    def _on_text_font_limit(self, size: float) -> None:
        self.statusBar().showMessage(
            f"已经是这个框放得下的最大字号（{size:g} pt），再大就排不下了", 4000
        )

    # -------------------------------------------------------------------- OCR

    def _restore_ocr_model(self) -> str:
        """从设置里取上次用的模型；设置里的名字已经不认识时退回默认模型。"""
        stored = self._settings.value("ocr_model", DEFAULT_MODEL, type=str)
        return stored if is_known_model(stored) else DEFAULT_MODEL

    def _set_ocr_model(self, name: str) -> None:
        """切换识别模型（记进 QSettings，下次启动继续用）。"""
        if not is_known_model(name) or name == self._ocr_model:
            self._sync_ocr_model_actions()
            return
        self._ocr_model = name
        self._settings.setValue("ocr_model", name)
        self._sync_ocr_model_actions()
        self.statusBar().showMessage(f"识别模型已切换为 {model_label(name)}", 4000)

    def _sync_ocr_model_actions(self) -> None:
        for name, action in self._model_actions.items():
            action.setChecked(name == self._ocr_model)

    def _engine_for(self, model: str) -> OcrEngine:
        """按模型名字取引擎（带缓存）；引擎很重，整个程序生命周期只加载一次。"""
        engine = self._ocr_engines.get(model)
        if engine is None:
            engine = self._ocr_factory(model)
            self._ocr_engines[model] = engine
        return engine

    def _ocr_selected(self) -> None:
        self._start_ocr(selected_only=True)

    def _ocr_all(self) -> None:
        self._start_ocr(selected_only=False)

    def _start_ocr(self, *, selected_only: bool) -> None:
        """对选中的（或全部的）ROI 起一个后台识别任务。"""
        path = self._current_path()
        image = self.view.image()
        if path is None or image is None or self._ocr_job is not None:
            return
        self.view.commit_text_edit()  # 正在手改的文本先收下来，别被识别结果冲掉

        items = [
            item
            for item in self.view.roi_items()
            if not selected_only or item.isSelected()
        ]
        if not items:
            self.statusBar().showMessage(
                "没有选中的标注" if selected_only else "当前图没有标注", 4000
            )
            return

        # 裁剪在界面线程做（很快），推理才丢给子线程；图块是 QImage 副本，跨线程安全
        targets: list[RoiItem] = []
        crops: list[QImage] = []
        for item in items:
            roi = item.to_roi()
            try:
                crops.append(crop_roi(image, roi))
            except OcrError as exc:
                self.statusBar().showMessage(
                    f"跳过 {(roi.x, roi.y, roi.width, roi.height)}：{exc}", 6000
                )
                continue
            targets.append(item)
        if not crops:
            return

        task = OcrTask(self._engine_for(self._ocr_model), crops)
        thread = QThread(self)
        task.moveToThread(thread)
        job = _OcrJob(
            window=self,
            path=path,
            model=self._ocr_model,
            items=targets,
            rois=[item.to_roi() for item in targets],
            task=task,
            thread=thread,
        )
        thread.started.connect(task.run)
        thread.finished.connect(thread.deleteLater)

        self._ocr_job = job
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self.statusBar().showMessage(
            f"正在识别 {len(crops)} 个标注（{model_label(self._ocr_model)}）…"
        )
        self._update_actions()
        thread.start()

    def _on_ocr_progressed(self, job: _OcrJob, done: int, total: int) -> None:
        if job is not self._ocr_job:
            return
        self.statusBar().showMessage(
            f"正在识别 {done}/{total}（{model_label(job.model)}）…"
        )

    def _on_ocr_finished(self, job: _OcrJob, cancelled: bool) -> None:
        if job is not self._ocr_job:
            return  # 已经被 _cancel_ocr 收尾过了
        self._finalize_ocr(job, cancelled=cancelled)

    def _cancel_ocr(self) -> None:
        """切图 / 关窗 / 重新打开前调用：停掉识别，把已经识别出来的结果落盘。

        当前这一张图块的推理没法中断，所以这里会等它跑完（通常不到一秒），换来的是
        "不会有结果悄悄丢掉"。
        """
        job = self._ocr_job
        if job is not None:
            job.task.cancel()
            self._finalize_ocr(job, cancelled=True)

    def _finalize_ocr(self, job: _OcrJob, *, cancelled: bool) -> None:
        """收尾：停线程、把结果写回标注并落盘、报告成败。"""
        self._ocr_job = None
        job.thread.quit()
        job.thread.wait(5000)
        QGuiApplication.restoreOverrideCursor()
        self._update_actions()

        if self._current_path() == job.path:
            self.view.cancel_text_edit()  # 识别结果说了算，别让在改的旧文本盖回来
            applied = 0
            for index, item in enumerate(job.items):
                text = job.task.results.get(index)
                if text is None or item.scene() is None:
                    continue  # 取消前已被删除的图元就跳过
                item.set_text(text)
                applied += 1
            if applied:
                self._flush_rois()  # 识别结果随标注一起写进边车文件
            self._refresh_ocr_panel()
            self._update_roi_label()
        else:  # 图像已经切走时（正常流程下不会发生）按几何并回磁盘
            applied = self._merge_ocr_text(
                job.path,
                [(job.rois[index], text) for index, text in job.task.results.items()],
            )

        failed = len(job.task.errors)
        if applied:
            message = f"已识别 {applied} 个标注（{model_label(job.model)}），已写入文件"
            if cancelled:
                message += "（识别已取消，已完成的部分保留）"
            if failed:
                message += f"，{failed} 个失败"
            self.statusBar().showMessage(message, 8000)
        elif failed:
            message = next(iter(job.task.errors.values()))
            self.statusBar().showMessage(f"识别失败：{message}", 10000)
            QMessageBox.warning(self, "OCR 失败", message)
        elif cancelled:
            self.statusBar().showMessage("已取消识别", 4000)
        else:
            self.statusBar().showMessage("没有识别出文本", 4000)

        job.deleteLater()  # 连同 task 里的图块一起放掉，别攒着占内存

    def _merge_ocr_text(self, path: Path, results: list[tuple[Roi, str]]) -> int:
        """把识别结果按矩形位置并回某张图已落盘的标注，返回并入条数。

        正常路径下结果都由场景里的图元带走（见 :meth:`_finalize_ocr`），这里是
        "图已经切走、结果才到"的兜底：几何对不上的结果宁可丢掉也不乱写。
        """
        rois = self._roi_store.get(path)
        merged = 0
        for snapshot, text in results:
            for position, roi in enumerate(rois):
                if _same_rect(roi.as_rect(), snapshot.as_rect()):
                    rois[position] = roi.with_text(text)
                    merged += 1
                    break
        if merged:
            try:
                self._roi_store.set(path, rois)
            except OSError as exc:
                self.statusBar().showMessage(f"保存 {path.name} 的识别结果失败：{exc}", 8000)
                return 0
        return merged

    # ------------------------------------------------------------ OCR 结果面板

    def _refresh_ocr_panel(self) -> None:
        """把当前图的 ROI 与识别文本重填进面板（顺序与场景里的 ROI 顺序一致）。"""
        self._panel_items = self.view.roi_items()
        self._panel.blockSignals(True)  # 重填过程中不要触发"点行选中图元"
        try:
            self._panel.setRowCount(len(self._panel_items))
            for row, item in enumerate(self._panel_items):
                roi = item.to_roi()
                # 第一列显示这个框在本张图上的顺序（排序模式排的就是它），行号只是位置
                self._panel.setItem(row, 0, _table_cell(str(item.order())))
                self._panel.setItem(
                    row,
                    1,
                    _table_cell(
                        f"{roi.x:.0f}, {roi.y:.0f}  {roi.width:.0f}×{roi.height:.0f}"
                    ),
                )
                text = item.text()
                cell = _table_cell(text, editable=True)
                cell.setToolTip(text)
                self._panel.setItem(row, 2, cell)
            self._panel.resizeRowsToContents()  # 换行后的行高
        finally:
            self._panel.blockSignals(False)
        self._sync_panel_selection()

    def _sync_panel_selection(self) -> None:
        """场景里选中了哪个 ROI，面板就高亮哪一行。

        这里现取场景里的图元，而不是用 :attr:`_panel_items`：清空场景时会连带发出
        ``selectionChanged``，那份缓存的图元已经作废，碰不得。
        """
        items = self.view.roi_items()
        selected = [item for item in items if item.isSelected()]
        row = items.index(selected[0]) if selected else -1
        self._panel.blockSignals(True)
        try:
            if row < 0:
                self._panel.clearSelection()
            else:
                self._panel.selectRow(row)
        finally:
            self._panel.blockSignals(False)

    def _on_panel_selection_changed(self) -> None:
        """点面板某一行 → 选中场景里对应的 ROI。"""
        rows = self._panel.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if not 0 <= row < len(self._panel_items):
            return
        item = self._panel_items[row]
        if not item.isSelected():
            self.view.select_roi_item(item)

    def _on_panel_item_changed(self, cell: QTableWidgetItem) -> None:
        """在结果面板里双击改完文本：写回对应的图元并立刻落盘。

        这里不回填面板（内容已经是用户刚输入的那份），只同步图元、状态栏与文件，
        免得在表格自己的 ``itemChanged`` 回调里重造正在编辑的单元格。
        """
        if cell.column() != 2:
            return
        row = cell.row()
        if not 0 <= row < len(self._panel_items):
            return
        item = self._panel_items[row]
        if item.scene() is None:
            return  # 图元已经随切图消失了
        text = cell.text()
        if text == item.text():
            return
        item.set_text(text)
        self._flush_rois()
        self._update_roi_label()

    def _on_roi_text_changed(self) -> None:
        """画布上双击改完文本：立刻落盘并刷新面板与状态栏。

        与识别结果一样"不用等切图就在文件里"，因为文本是用户手工敲的，丢了没法重来。
        """
        self._flush_rois()
        self._refresh_ocr_panel()
        self._update_roi_label()

    # ------------------------------------------------------------------ 状态

    def _update_status(self) -> None:
        if self._index < 0 or not self._paths:
            self._label_file.setText("未打开图像 —— Ctrl+Shift+O 打开文件夹")
            self._label_zoom.setText("")
            self.setWindowTitle(APP_NAME)
            self._update_roi_label()
            return
        path = self._paths[self._index]
        self._label_file.setText(
            f"[{self._index + 1}/{len(self._paths)}]  {path.name}"
            f"    {self._image_size.width()}×{self._image_size.height()}    {path.parent}"
        )
        self.setWindowTitle(f"{path.name} — {APP_NAME}")
        self._update_roi_label()

    def _update_roi_label(self) -> None:
        if self._index < 0:
            self._label_roi.setText("")
            return
        items = self.view.roi_items()
        recognized = sum(1 for item in items if item.text())
        text = f"标注 {len(items)}"
        if recognized:
            text += f" / 已识别 {recognized}"
        self._label_roi.setText(text)

    def _on_zoom_changed(self, scale: float) -> None:
        self._label_zoom.setText(f"缩放 {scale * 100:.0f}%")

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "快捷键",
            "打开\n"
            "  Ctrl+Shift+O / 工具栏「打开文件夹」   打开文件夹\n"
            "  Ctrl+O                              导入图像文件\n"
            "  Ctrl+Q                              退出\n"
            "\n"
            "浏览\n"
            "  ← / A / PageUp / Backspace          上一张\n"
            "  → / D / PageDown                    下一张\n"
            "  Home / End                          第一张 / 最后一张\n"
            "\n"
            "缩放\n"
            "  滚轮 / Ctrl+滚轮                     以鼠标为中心缩放\n"
            "  Ctrl+= / Ctrl+-                     放大 / 缩小\n"
            "  Ctrl+0 或 F                         适应窗口\n"
            "  Ctrl+1                              实际大小 (100%)\n"
            "\n"
            "标注\n"
            "  左键在图像上拖动                     拉出一个矩形标注\n"
            "  左键拖动图元 / 拖动手柄              移动 / 缩放标注\n"
            "  双击图元 或 双击结果面板的文本       编辑该 ROI 的识别文本\n"
            "                                        （Esc 放弃，Ctrl+Enter 或点开别处采纳）\n"
            "  Delete                              删除选中的标注\n"
            "  Ctrl+S                              保存当前图的标注\n"
            "  Ctrl+Shift+S                        进入 / 退出 ROI 排序模式\n"
            "                                        进入后按点击顺序给 ROI 编号 1、2、3…\n"
            "                                        只作用于当前这张图，Esc 退出\n"
            "  Esc                                 放弃正在拉的矩形 / 取消选中 / 退出排序模式\n"
            "  选中标注时，旁边的小框显示这个框的识别文本\n"
            "    横排贴上下、竖排贴左右；光标放在框上滚滚轮可以调字号\n"
            "  「视图 → 文本框排版方向」切换横排 / 竖排（记进设置）\n"
            "  标注自动保存在图像旁的 <文件名>.roi.json，切图时自动保存与恢复\n"
            "\n"
            "导出\n"
            "  Ctrl+E                              导出文本到图像文件夹里的 <文件夹名>.txt\n"
            "  Ctrl+Shift+E                        选择导出位置（弹出保存对话框）\n"
            "  导出已打开图像的全部 ROI 文本，按图像顺序与 ROI 顺序排列；\n"
            "  没有 ROI 的图整张跳过，识别文本原样输出（空白就是空行）\n"
            "\n"
            "OCR\n"
            "  Ctrl+R                              识别选中的标注\n"
            "  Ctrl+Shift+R                        识别当前图的全部标注\n"
            "  OCR → 识别模型                       切换识别模型（当前只有 manga-ocr）\n"
            "  识别结果挂在对应的 ROI 上，随标注一起写进 <文件名>.roi.json\n"
            "\n"
            "鼠标\n"
            "  中键拖动                             平移画布\n"
            "  空格按住 + 左键拖动                  平移画布（抓手，不新建矩形）\n"
            "  Shift+滚轮 / Alt+滚轮                水平 / 垂直滚动",
        )

    # ------------------------------------------------------------------ 键盘

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt 命名)
        """把空格键的按下 / 松开转成视图的抓手模式。

        空格是"按住才生效"的修饰键，而焦点可能在结果面板或对话框上（视图收不到
        ``keyPressEvent``），所以这里在应用级过滤，只读不改：事件照常向下传递，
        别的控件该用空格还照用。
        """
        kind = event.type()
        if kind == QEvent.Type.WindowDeactivate:
            self.view.set_hand_mode(False)  # 失焦时收不到 KeyRelease，别把抓手卡住
        elif kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            pressed = kind == QEvent.Type.KeyPress
            if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
                self.view.set_hand_mode(pressed)
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ 拖放

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._acceptable_urls(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        collected: list[Path] = []
        for url in urls:
            path = Path(url.toLocalFile())
            if path.is_dir():
                collected.extend(collect_images(path))
            elif path.is_file() and is_image_file(path):
                collected.append(path)
        if collected:
            self.open_paths(collected)
            event.acceptProposedAction()

    @staticmethod
    def _acceptable_urls(event) -> bool:
        if not event.mimeData().hasUrls():
            return False
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir() or is_image_file(path):
                return True
        return False

    # ------------------------------------------------------------------ 状态持久化

    def _remember_dir(self, folder: Path) -> None:
        self._settings.setValue("last_dir", str(folder))

    def _restore_settings(self) -> None:
        geometry = self._settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._cancel_ocr()  # 关窗前先停掉识别，让已经识别出来的结果落盘
        self._flush_rois()  # 关窗前保存当前图的标注
        self._settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("ocr-proofread")
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("ocr-proofread")

    window = MainWindow()
    window.show()
    if argv[1:]:
        window.open_from_args(argv[1:])
    return app.exec()
