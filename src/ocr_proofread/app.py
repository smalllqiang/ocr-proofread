"""主窗口：打开图像 / 文件夹、切换上一张下一张、缩放、状态持久化。

标注图元的挂载点与生命周期约定见 :mod:`ocr_proofread.image_view`。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
)

from .image_view import ImageView
from .images import IMAGE_NAME_FILTER, collect_images, is_image_file, load_qimage

APP_NAME = "OCR 校对工具"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.setAcceptDrops(True)

        self._paths: list[Path] = []
        self._index = -1
        self._image_size = QSize()
        self._settings = QSettings("ocr-proofread", "ocr-proofread")

        self.view = ImageView(self)
        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.setCentralWidget(self.view)

        self._label_file = QLabel("未打开图像 —— Ctrl+Shift+O 打开文件夹")
        self._label_zoom = QLabel("")
        status = self.statusBar()
        status.addWidget(self._label_file, 1)
        status.addPermanentWidget(self._label_zoom)

        self._create_actions()
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

        self._index = index
        self._image_size = image.size()
        self.view.set_image(image)
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
        self._paths = []
        self._index = -1
        self._image_size = QSize()
        self.view.set_image(None)
        self._update_status()
        self._update_actions()

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

    # ------------------------------------------------------------------ 状态

    def _update_status(self) -> None:
        if self._index < 0 or not self._paths:
            self._label_file.setText("未打开图像 —— Ctrl+Shift+O 打开文件夹")
            self._label_zoom.setText("")
            self.setWindowTitle(APP_NAME)
            return
        path = self._paths[self._index]
        self._label_file.setText(
            f"[{self._index + 1}/{len(self._paths)}]  {path.name}"
            f"    {self._image_size.width()}×{self._image_size.height()}    {path.parent}"
        )
        self.setWindowTitle(f"{path.name} — {APP_NAME}")

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
