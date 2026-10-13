"""无界面冒烟测试：不需要 pytest，直接 ``uv run python tests/smoke_test.py``。

覆盖第一步的功能：文件夹扫描与自然排序、打开/切换图像、缩放范围、
快捷键绑定，以及坏图不让程序崩溃。

在无显示器的环境下会自动使用 offscreen 平台插件。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ocr_proofread.app import MainWindow  # noqa: E402
from ocr_proofread.image_view import MAX_SCALE, MIN_SCALE  # noqa: E402
from ocr_proofread.images import collect_images, load_qimage, natural_key  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


def make_image(path: Path, width: int, height: int, color: str) -> None:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path)), f"写入测试图失败: {path}"


def build_fixture(root: Path) -> None:
    for index, name in enumerate(["img1.png", "img2.png", "img10.png"]):
        make_image(root / name, 200 + index * 10, 100, "#3355aa")
    make_image(root / "photo.JPG", 320, 240, "#aa5533")
    (root / "notes.txt").write_text("not an image", encoding="utf-8")
    hidden = root / ".hidden.png"
    make_image(hidden, 10, 10, "#000000")
    nested = root / "sub"
    nested.mkdir()
    make_image(nested / "deep.png", 50, 50, "#00aa00")
    (root / "broken.png").write_bytes(b"definitely not a png")


def test_collect(root: Path) -> None:
    names = [p.name for p in collect_images(root)]
    check(
        "扫描文件夹：只收图像、忽略隐藏文件与子目录",
        names == ["broken.png", "img1.png", "img2.png", "img10.png", "photo.JPG"],
        str(names),
    )
    nested = [p.name for p in collect_images(root, recursive=True)]
    check("递归扫描包含子目录", "deep.png" in nested, str(nested))
    check("自然排序：img2 < img10", natural_key("img2.png") < natural_key("img10.png"))
    check("自然排序与大小写无关", natural_key("A2.png") == natural_key("a2.png"))


def test_navigation(window: MainWindow, root: Path) -> None:
    image_root = root
    paths = [p for p in collect_images(image_root) if p.name != "broken.png"]
    window.open_paths(paths)
    check("打开文件夹后停在第一张", window._index == 0, f"index={window._index}")
    check("场景里有图像", window.view.has_image)
    check("初始状态：整张图完整显示在视口内", fits_in_viewport(window),
          f"scale={window.view.scale_factor:.4f}")
    check("状态栏显示序号与分辨率", "1/4" in window._label_file.text(), window._label_file.text())
    check("上一张在第一张时禁用", not window.act_prev.isEnabled())

    window.show()
    QTest.qWaitForWindowExposed(window)

    QTest.keyClick(window, Qt.Key.Key_Right)
    check("→ 切到下一张", window._index == 1, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_D)
    check("D 切到下一张", window._index == 2, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_PageDown)
    check("PageDown 切到下一张", window._index == 3, f"index={window._index}")
    check("最后一张时下一张禁用", not window.act_next.isEnabled())
    check("状态栏跟随到 4/4", "4/4" in window._label_file.text(), window._label_file.text())

    QTest.keyClick(window, Qt.Key.Key_Left)
    check("← 切到上一张", window._index == 2, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_A)
    check("A 切到上一张", window._index == 1, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_PageUp)
    QTest.keyClick(window, Qt.Key.Key_Backspace)
    check("PageUp/Backspace 连按回到第一张", window._index == 0, f"index={window._index}")

    QTest.keyClick(window, Qt.Key.Key_End)
    check("End 跳到最后一张", window._index == 3, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_Home)
    check("Home 跳到第一张", window._index == 0, f"index={window._index}")
    QTest.keyClick(window, Qt.Key.Key_Space)
    check("Space 切到下一张", window._index == 1, f"index={window._index}")

    # 切换后每张图都应重新适应窗口（整图落在视口内，小图允许被放大铺满）
    check("切图后重新适应窗口：整图仍在视口内", fits_in_viewport(window),
          f"scale={window.view.scale_factor:.4f}")


def test_zoom(window: MainWindow) -> None:
    window.view.fit_to_window()
    fit_scale = window.view.scale_factor
    window.view.zoom_in()
    check("放大：倍率变大", window.view.scale_factor > fit_scale,
          f"{fit_scale:.3f} -> {window.view.scale_factor:.3f}")
    window.view.zoom_out()
    window.view.zoom_out()
    check("缩小：倍率变小", window.view.scale_factor < fit_scale,
          f"scale={window.view.scale_factor:.3f}")

    window.view.reset_zoom()
    check("Ctrl+1 实际大小为 100%", abs(window.view.scale_factor - 1.0) < 1e-9,
          f"scale={window.view.scale_factor:.3f}")

    for _ in range(40):
        window.view.zoom_in()
    check("放大有上限", window.view.scale_factor <= MAX_SCALE,
          f"scale={window.view.scale_factor:.2f}")
    for _ in range(120):
        window.view.zoom_out()
    check("缩小有下限", window.view.scale_factor >= MIN_SCALE,
          f"scale={window.view.scale_factor:.4f}")

    # 滚轮缩放（模拟滚轮事件走真实的事件处理）
    window.view.reset_zoom()
    before = window.view.scale_factor
    QTest.mouseMove(window.view.viewport(), QPoint(60, 40))
    _send_wheel(window, 120)
    check("滚轮向上放大", window.view.scale_factor > before,
          f"{before:.3f} -> {window.view.scale_factor:.3f}")
    _send_wheel(window, -120)
    check("滚轮向下缩小", abs(window.view.scale_factor - before) < 1e-6,
          f"scale={window.view.scale_factor:.3f}")

    window.view.fit_to_window()
    check("适应窗口：整图可见", fits_in_viewport(window),
          f"scale={window.view.scale_factor:.3f}")

    # 快捷键绑定是否真的挂在 QAction 上
    check("Ctrl+= 绑定到放大", "Ctrl+=" in _shortcut_texts(window.act_zoom_in),
          str(_shortcut_texts(window.act_zoom_in)))
    check("Ctrl+- 绑定到缩小", "Ctrl+-" in _shortcut_texts(window.act_zoom_out))
    check("Ctrl+0 绑定到适应窗口", "Ctrl+0" in _shortcut_texts(window.act_fit))
    check("Ctrl+1 绑定到实际大小", "Ctrl+1" in _shortcut_texts(window.act_actual))
    check("Ctrl+Shift+O 绑定到打开文件夹",
          "Ctrl+Shift+O" in _shortcut_texts(window.act_open_folder))


def _shortcut_texts(action) -> list[str]:
    return [seq.toString() for seq in action.shortcuts()]


def fits_in_viewport(window: MainWindow, tolerance: int = 3) -> bool:
    """图像当前映射到视口里的矩形是否完整落在视口内。"""
    view = window.view
    item_rect = view.scene().itemsBoundingRect()
    if item_rect.isEmpty():
        return False
    shown = view.mapFromScene(item_rect).boundingRect()
    viewport = view.viewport().rect().adjusted(-tolerance, -tolerance, tolerance, tolerance)
    return viewport.contains(shown)


def _send_wheel(window: MainWindow, delta: int) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent

    event = QWheelEvent(
        QPointF(60, 40),
        QPointF(60, 40),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(window.view.viewport(), event)
    QApplication.processEvents()


def test_broken_image(window: MainWindow, root: Path) -> None:
    try:
        load_qimage(root / "broken.png")
    except ValueError:
        check("损坏的图像会抛出 ValueError", True)
    else:
        check("损坏的图像会抛出 ValueError", False, "没有抛异常")

    window.open_paths([root / "img1.png", root / "broken.png"])
    window.show_last()  # 最后一张是 broken.png
    check("跳到坏图不崩溃且保留原图", window._index == 0 and window.view.has_image,
          f"index={window._index} has_image={window.view.has_image}")
    check("坏图给出状态栏提示", "无法打开" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    with tempfile.TemporaryDirectory(prefix="ocr-proofread-smoke-") as tmp:
        root = Path(tmp)
        build_fixture(root)

        test_collect(root)

        window = MainWindow()
        test_navigation(window, root)
        test_zoom(window)
        test_broken_image(window, root)
        window.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：" + "; ".join(FAILURES))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
