"""主窗口：打开图像 / 文件夹、切换上一张下一张、缩放、ROI 标注、OCR、状态持久化。

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

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread
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
    QTableWidget,
    QTableWidgetItem,
)

from .annotations import RoiStore
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


def _table_cell(text: str) -> QTableWidgetItem:
    """OCR 结果面板里的只读单元格。"""
    cell = QTableWidgetItem(text)
    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return cell


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

        # OCR：引擎按模型名字缓存，模型很重，不能每识别一次就加载一遍
        self._ocr_factory = ocr_factory or create_engine
        self._ocr_engines: dict[str, OcrEngine] = {}
        self._ocr_model = self._restore_ocr_model()
        self._ocr_job: _OcrJob | None = None
        self._panel_items: list[RoiItem] = []

        self.view = ImageView(self)
        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.view.roisChanged.connect(self._update_roi_label)
        self.setCentralWidget(self.view)

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
            shortcuts=["Right", "D", "PgDown", "Space"],
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
        self.act_save_rois = self._make_action(
            "保存标注(&S)",
            shortcuts=["Ctrl+S"],
            slot=self._save_rois,
            tip="把当前图像的标注写入图像旁边的 .roi.json 文件",
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
        """右侧的 OCR 结果面板：一行一个 ROI，点行选中对应的框。"""
        self._panel = QTableWidget(0, 3, self)
        self._panel.setHorizontalHeaderLabels(["#", "位置 / 尺寸", "识别文本"])
        self._panel.verticalHeader().setVisible(False)
        self._panel.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._panel.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._panel.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._panel.setWordWrap(True)  # 识别文本宁可换行也不要被省略号截掉
        header = self._panel.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._panel.itemSelectionChanged.connect(self._on_panel_selection_changed)

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
        menu_roi.addAction(self.act_save_rois)

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
        真的变了才落盘，所以没标注过的图不会多出文件。
        """
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

        busy = self._ocr_job is not None
        items = self.view.roi_items() if has_image else []
        self.act_ocr_all.setEnabled(bool(items) and not busy)
        self.act_ocr_selected.setEnabled(
            any(item.isSelected() for item in items) and not busy
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
        """把当前图的 ROI 与识别文本重填进面板（顺序与场景里的 ROI 一致）。"""
        self._panel_items = self.view.roi_items()
        self._panel.blockSignals(True)  # 重填过程中不要触发"点行选中图元"
        try:
            self._panel.setRowCount(len(self._panel_items))
            for row, item in enumerate(self._panel_items):
                roi = item.to_roi()
                self._panel.setItem(row, 0, _table_cell(str(row + 1)))
                self._panel.setItem(
                    row,
                    1,
                    _table_cell(
                        f"{roi.x:.0f}, {roi.y:.0f}  {roi.width:.0f}×{roi.height:.0f}"
                    ),
                )
                text = item.text()
                cell = _table_cell(text or "—")
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
            "  → / D / PageDown / Space            下一张\n"
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
            "  Delete                              删除选中的标注\n"
            "  Ctrl+S                              保存当前图的标注\n"
            "  Esc                                 放弃正在拉的矩形 / 取消选中\n"
            "  标注自动保存在图像旁的 <文件名>.roi.json，切图时自动保存与恢复\n"
            "\n"
            "OCR\n"
            "  Ctrl+R                              识别选中的标注\n"
            "  Ctrl+Shift+R                        识别当前图的全部标注\n"
            "  OCR → 识别模型                       切换识别模型（当前只有 manga-ocr）\n"
            "  识别结果挂在对应的 ROI 上，随标注一起写进 <文件名>.roi.json\n"
            "\n"
            "鼠标\n"
            "  中键拖动                             平移画布\n"
            "  Shift+滚轮 / Alt+滚轮                水平 / 垂直滚动",
        )

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
