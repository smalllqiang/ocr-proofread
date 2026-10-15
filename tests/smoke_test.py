"""无界面冒烟测试：不需要 pytest，直接 ``uv run python tests/smoke_test.py``。

覆盖：文件夹扫描与自然排序、打开/切换图像、缩放范围、快捷键绑定、ROI 框选与
标注存取、双击编辑识别文本、OCR（裁剪 / 引擎接线 / 后台任务 / 结果落盘）
以及坏图不让程序崩溃。

OCR 部分注入一个假引擎（``MainWindow(ocr_factory=...)``），所以这里不会加载 torch，
也不会去 HuggingFace 下载模型——真正跑 manga-ocr 的手动验证见 README。

在无显示器的环境下会自动使用 offscreen 平台插件。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLineEdit  # noqa: E402

from ocr_proofread.annotations import (  # noqa: E402
    Roi,
    RoiStore,
    sidecar_path,
)
from ocr_proofread.app import MainWindow  # noqa: E402
from ocr_proofread.image_view import MAX_SCALE, MIN_SCALE  # noqa: E402
from ocr_proofread.images import collect_images, load_qimage, natural_key  # noqa: E402
from ocr_proofread.ocr import (  # noqa: E402
    MANGA_OCR,
    OCR_MODELS,
    MangaOcrEngine,
    OcrError,
    create_engine,
    crop_roi,
    is_known_model,
    model_label,
)
from ocr_proofread.roi_item import HandleRole, RoiItem  # noqa: E402
from ocr_proofread.text_box import (  # noqa: E402
    EMPTY_HINT,
    FONT_SIZE_STEP,
    GAP,
    MAX_CHARS,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    TextDirection,
)

FAILURES: list[str] = []


class FakeOcrEngine:
    """假引擎：验证"裁剪 → 子线程 → 写回图元 → 落盘"整条链路，不碰 torch。

    ``delay`` 用来把识别拖慢，好测"识别到一半切图"的取消路径。
    """

    name = "fake"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.sizes: list[tuple[int, int]] = []

    def recognize(self, image: QImage) -> str:
        if self.delay:
            time.sleep(self.delay)
        self.sizes.append((image.width(), image.height()))
        return f"识别结果-{len(self.sizes)}"


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
    check("Space 不再切图（改成空格抓手的按键）", window._index == 0,
          f"index={window._index}")
    check("「下一张」不再绑定 Space", "Space" not in _shortcut_texts(window.act_next),
          str(_shortcut_texts(window.act_next)))

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


def _send_mouse(widget, kind, pos: QPoint, button, buttons) -> None:
    """往 widget 直接送一个鼠标事件。

    QTest.mouseMove 不带按键状态，场景不会认为在拖动图元，所以这里自己造事件，
    让 ``buttons()`` 在拖动过程中保持左键按下。
    """
    event = QMouseEvent(
        kind,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()


def drag(widget, start: QPoint, end: QPoint, *, steps: int = 4) -> None:
    """按下左键从 start 拖到 end 再松开，中间分几步送 move 事件。"""
    _send_mouse(widget, QEvent.Type.MouseButtonPress, start,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    for step in range(1, steps + 1):
        point = QPoint(
            start.x() + (end.x() - start.x()) * step // steps,
            start.y() + (end.y() - start.y()) * step // steps,
        )
        _send_mouse(widget, QEvent.Type.MouseMove, point,
                    Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _send_mouse(widget, QEvent.Type.MouseButtonRelease, end,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def same_rois(left, right, tolerance: float = 0.02) -> bool:
    """两组标注是否一致（几何允许存盘四舍五入的误差，文本与排序号要求完全相同）。"""
    if len(left) != len(right):
        return False
    return all(
        abs(a.x - b.x) <= tolerance
        and abs(a.y - b.y) <= tolerance
        and abs(a.width - b.width) <= tolerance
        and abs(a.height - b.height) <= tolerance
        and a.text == b.text
        and a.index == b.index
        for a, b in zip(left, right)
    )


def inside(roi: Roi, bounds: QRectF, tolerance: float = 0.05) -> bool:
    """标注是否落在给定范围内（边界上的浮点误差不算越界）。"""
    return (
        roi.x >= bounds.left() - tolerance
        and roi.y >= bounds.top() - tolerance
        and roi.x + roi.width <= bounds.right() + tolerance
        and roi.y + roi.height <= bounds.bottom() + tolerance
    )


def test_roi_store(root: Path) -> None:
    # 用一个不会出现在浏览列表里的路径：这里的边车文件是给存储层测的，
    # 别把标注留给后面的"切图/适应窗口"测试。
    path = root / "sub" / "store_target.png"
    check("没标过的图读出来是空的", RoiStore().get(path) == [])

    store = RoiStore()
    rois = [Roi(1, 2, 30, 40), Roi(50, 60, 70, 80)]
    check("set 会写出边车文件", store.set(path, rois) is True)
    check("边车文件落在图像旁边", sidecar_path(path).is_file(), str(sidecar_path(path)))
    check("内容没变时不再写盘", store.set(path, rois) is False)
    check("新的 store 能读回同样内容", same_rois(RoiStore().get(path), rois))
    check("边车文件不会被当成图像",
          sidecar_path(path) not in collect_images(root, recursive=True))

    sidecar_path(path).write_text("{ 这不是 JSON", encoding="utf-8")
    check("损坏的标注文件按空处理", RoiStore().get(path) == [])

    bad = sidecar_path(path)
    bad.write_text('{"annotations": [{"x": 1, "y": 2}, {"x": 3, "y": 4, "w": 5, "h": 6}]}',
                   encoding="utf-8")
    check("坏记录跳过、好记录保留", RoiStore().get(path) == [Roi(3, 4, 5, 6)])

    store = RoiStore()
    store.set(path, [Roi(0, 0, 10, 10)])
    store.set(path, [])
    check("清空标注会删掉边车文件", not sidecar_path(path).exists())

    recognized = [Roi(1, 2, 30, 40, "認識された", 1), Roi(50, 60, 70, 80, "", 2)]
    store = RoiStore()
    store.set(path, recognized)
    check("识别文本随标注一起读回", same_rois(RoiStore().get(path), recognized),
          str(RoiStore().get(path)))
    raw = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    check("识别过的标注写了 text 字段", raw["annotations"][0].get("text") == "認識された",
          json.dumps(raw, ensure_ascii=False))
    check("没识别过的标注不写 text 字段", "text" not in raw["annotations"][1],
          json.dumps(raw["annotations"][1], ensure_ascii=False))
    check("边车文件版本升到 3", raw["version"] == 3, str(raw["version"]))
    check("排序号随标注一起写盘",
          [entry.get("index") for entry in raw["annotations"]] == [1, 2],
          json.dumps(raw, ensure_ascii=False))
    swapped = [Roi(1, 2, 30, 40, "認識された", 2), Roi(50, 60, 70, 80, "", 1)]
    check("只有排序号变了也会写盘", store.set(path, swapped) is True)
    check("内容完全没变时不写盘", store.set(path, swapped) is False)
    check("重排后的顺序按传入顺序落盘", same_rois(RoiStore().get(path), swapped),
          str(RoiStore().get(path)))

    # 排序号：写盘、读回、字段容错
    check("没排过号的标注不写 index 字段", "index" not in Roi(1, 2, 3, 4).to_dict(),
          json.dumps(Roi(1, 2, 3, 4).to_dict()))
    check("with_text 不丢排序号", Roi(1, 2, 3, 4, "", 7).with_text("x").index == 7)
    check("from_rect 能带上排序号", Roi.from_rect(QRectF(1, 2, 3, 4), "x", 5).index == 5)
    bad_index = Roi.from_dict({"x": 1, "y": 2, "w": 3, "h": 4, "index": "第二个"})
    check("认不出来的 index 当作没排过号", bad_index is not None and bad_index.index == 0,
          str(bad_index))
    negative = Roi.from_dict({"x": 1, "y": 2, "w": 3, "h": 4, "index": -3})
    check("负数 index 被夹成 0", negative is not None and negative.index == 0,
          str(negative))

    legacy = sidecar_path(path)
    legacy.write_text('{"version": 1, "annotations": [{"x": 1, "y": 2, "w": 3, "h": 4}]}',
                      encoding="utf-8")
    check("v1 老文件照常读入（text 为空串）", RoiStore().get(path) == [Roi(1, 2, 3, 4, "")],
          str(RoiStore().get(path)))

    # v2 老文件（有 text、没有 index）同样读得进来，排序号交给上层重编
    legacy.write_text(
        '{"version": 2, "annotations": [{"x": 1, "y": 2, "w": 3, "h": 4, "text": "旧"}]}',
        encoding="utf-8",
    )
    check("v2 老文件照常读入（没有排序号）",
          RoiStore().get(path) == [Roi(1, 2, 3, 4, "旧")], str(RoiStore().get(path)))


def test_roi_interaction(window: MainWindow, root: Path) -> None:
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)
    window.view.fit_to_window()
    QApplication.processEvents()

    viewport = window.view.viewport()
    view = window.view
    check("初始场景里没有标注", view.roi_count() == 0)

    # 左键拖出一个矩形
    center = viewport.rect().center()
    start = QPoint(center.x() - 60, center.y() - 40)
    end = QPoint(center.x() + 60, center.y() + 40)
    drag(viewport, start, end)
    check("左键拖出矩形", view.roi_count() == 1, f"count={view.roi_count()}")
    if view.roi_count() != 1:
        return

    expected = QRectF(view.mapToScene(start), view.mapToScene(end)).normalized()
    roi = view.rois()[0]
    check(
        "矩形范围与拖动一致（±1.5 图像像素）",
        abs(roi.x - expected.x()) < 1.5
        and abs(roi.y - expected.y()) < 1.5
        and abs(roi.width - expected.width()) < 1.5
        and abs(roi.height - expected.height()) < 1.5,
        f"{roi} vs {expected}",
    )
    check("新建的矩形自动选中", view.roi_items()[0].isSelected())
    check("状态栏显示标注数", "标注 1" in window._label_roi.text(), window._label_roi.text())

    # 刚画出来的框，手柄必须贴在矩形角上（setRect 会同步子图元，别堆在原点）
    drawn = view.roi_items()[0]
    bottom_right = next(h for h in drawn._handles if h.role is HandleRole.BOTTOM_RIGHT)
    corner = drawn.rect().bottomRight()
    check("新画出的框：手柄贴在矩形角上",
          abs(bottom_right.pos().x() - corner.x()) < 1e-6
          and abs(bottom_right.pos().y() - corner.y()) < 1e-6,
          f"{bottom_right.pos()} vs {corner}")
    check("新画出的框：文本框也贴在矩形边上",
          abs(drawn.text_box().pos().x() - drawn.rect().right()) < 1e-6,
          f"{drawn.text_box().pos()} vs {drawn.rect()}")

    # 单击（拖动距离过小）不应留下图元
    drag(viewport, QPoint(center.x() - 150, center.y() - 100),
         QPoint(center.x() - 149, center.y() - 100))
    check("误点击不会留下碎图元", view.roi_count() == 1, f"count={view.roi_count()}")

    # Esc 放弃正在拉的矩形（起点要避开已有图元，否则按下的是它的手柄）
    esc_start = QPoint(center.x() - 170, center.y() - 130)
    esc_end = QPoint(center.x() - 80, center.y() - 60)
    _send_mouse(viewport, QEvent.Type.MouseButtonPress, esc_start,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _send_mouse(viewport, QEvent.Type.MouseMove, esc_end,
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    _send_mouse(viewport, QEvent.Type.MouseButtonRelease, esc_end,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    check("Esc 放弃正在拉的矩形", view.roi_count() == 1, f"count={view.roi_count()}")
    check("放弃后原图元没被改动", same_rois(view.rois(), [roi]), f"{view.rois()}")

    view.roi_items()[0].setSelected(True)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    check("Esc 取消选中", not any(item.isSelected() for item in view.roi_items()))

    # 切图保存、切回恢复
    window.show_next()
    check("切到下一张时不带上一张的标注", view.roi_count() == 0)
    check("切图后边车文件已写出", sidecar_path(paths[0]).is_file(), str(sidecar_path(paths[0])))
    check("边车文件内容可读回", same_rois(RoiStore().get(paths[0]), [roi]),
          f"{RoiStore().get(paths[0])}")
    window.show_prev()
    check("切回上一张恢复标注", same_rois(view.rois(), [roi]), f"{view.rois()}")

    # 拖动图元本体
    item = view.roi_items()[0]
    item.setSelected(True)
    before = item.to_roi()
    grip = view.mapFromScene(item.mapRectToScene(item.rect()).center())
    drag(viewport, grip, QPoint(grip.x() + 30, grip.y() + 20))
    after = view.roi_items()[0].to_roi()
    check(
        "拖动图元本体可移动",
        after.x > before.x + 1 and after.y > before.y + 1,
        f"{before} -> {after}",
    )
    check(
        "移动不改变尺寸",
        abs(after.width - before.width) < 1e-6 and abs(after.height - before.height) < 1e-6,
    )

    # 拖右下角手柄缩放
    item = view.roi_items()[0]
    item.setSelected(True)
    before = item.to_roi()
    corner = view.mapFromScene(item.mapRectToScene(item.rect()).bottomRight())
    drag(viewport, corner, QPoint(corner.x() + 25, corner.y() + 25))
    after = view.roi_items()[0].to_roi()
    check(
        "拖手柄可放大",
        after.width > before.width + 1 and after.height > before.height + 1,
        f"{before} -> {after}",
    )
    check("缩放时左上角不动", abs(after.x - before.x) < 1e-6 and abs(after.y - before.y) < 1e-6)

    # 拖左上角手柄，左上角跟着走
    item = view.roi_items()[0]
    item.setSelected(True)
    before = item.to_roi()
    corner = view.mapFromScene(item.mapRectToScene(item.rect()).topLeft())
    drag(viewport, corner, QPoint(corner.x() + 15, corner.y() + 10))
    after = view.roi_items()[0].to_roi()
    check(
        "拖左上角手柄改变原点",
        after.x > before.x + 1 and after.y > before.y + 1 and after.width < before.width - 1,
        f"{before} -> {after}",
    )

    # 拖动不会跑出图像范围
    item = view.roi_items()[0]
    item.setSelected(True)
    image_rect = view.image_rect()
    grip = view.mapFromScene(item.mapRectToScene(item.rect()).center())
    drag(viewport, grip, QPoint(viewport.rect().right() - 2, viewport.rect().bottom() - 2))
    moved = view.roi_items()[0].to_roi()
    check(
        "整体拖动被夹在图像内",
        inside(moved, image_rect),
        f"{moved} vs {image_rect}",
    )

    # Ctrl+S 手动保存
    window.act_save_rois.trigger()
    check("Ctrl+S 保存后有提示", "已保存" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())
    check("保存内容与场景一致", same_rois(RoiStore().get(paths[0]), view.rois()))

    # 删除选中
    view.roi_items()[0].setSelected(True)
    window.act_delete_roi.trigger()
    check("Delete 删除选中标注", view.roi_count() == 0, f"count={view.roi_count()}")
    window.show_next()
    check("清空后边车文件被删除", not sidecar_path(paths[0]).exists())
    window.show_prev()
    check("重新切回仍是空的", view.roi_count() == 0)


def test_hand_mode(window: MainWindow, root: Path) -> None:
    """空格按住 + 左键拖动 = 平移画布：既不留新标注，也不改动已有图元。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    view = window.view
    viewport = view.viewport()
    view.set_rois([])
    view.fit_to_window()
    for _ in range(6):  # 放大到画布比视口大，滚动条才有行程
        view.zoom_in()
    QApplication.processEvents()
    h_bar = view.horizontalScrollBar()
    v_bar = view.verticalScrollBar()
    check("放大后画布可以滚动", h_bar.maximum() > 0 and v_bar.maximum() > 0,
          f"max=({h_bar.maximum()}, {v_bar.maximum()})")

    center = viewport.rect().center()
    start = QPoint(center.x() + 50, center.y() + 40)
    end = QPoint(center.x() - 50, center.y() - 40)

    # ---- 空白处按住空格拖动：只平移，不留标注
    h_bar.setValue(h_bar.maximum() // 2)
    v_bar.setValue(v_bar.maximum() // 2)
    before = (h_bar.value(), v_bar.value())

    QTest.keyPress(window, Qt.Key.Key_Space)
    check("按住空格进入抓手模式", view.hand_mode)
    check("抓手模式下指针是张手",
          viewport.cursor().shape() == Qt.CursorShape.OpenHandCursor,
          str(viewport.cursor().shape()))
    drag(viewport, start, end)
    after = (h_bar.value(), v_bar.value())
    check("空格拖动平移了画布", after[0] > before[0] and after[1] > before[1],
          f"{before} -> {after}")
    check("空格拖动不新建标注", view.roi_count() == 0, f"count={view.roi_count()}")

    # ---- 从已有图元上按下同样是平移：图元不许被拖动
    scene_center = view.mapToScene(center)
    view.set_rois([Roi(scene_center.x() - 20, scene_center.y() - 15, 40, 30)])
    item = view.roi_items()[0]
    item.setSelected(True)
    grip = view.mapFromScene(item.mapRectToScene(item.rect()).center())
    before_roi = item.to_roi()
    h_bar.setValue(h_bar.maximum() // 2)
    v_bar.setValue(v_bar.maximum() // 2)
    before = (h_bar.value(), v_bar.value())
    drag(viewport, grip, QPoint(grip.x() - 60, grip.y() - 40))
    check("空格拖动压过「拖动图元」", same_rois(view.rois(), [before_roi]), str(view.rois()))
    check("从图元上按下也在平移画布",
          h_bar.value() > before[0] and v_bar.value() > before[1],
          f"{before} -> {(h_bar.value(), v_bar.value())}")

    QTest.keyRelease(window, Qt.Key.Key_Space)
    check("松开空格退出抓手模式", not view.hand_mode)
    check("退出抓手后指针恢复默认",
          viewport.cursor().shape() == Qt.CursorShape.ArrowCursor,
          str(viewport.cursor().shape()))

    # ---- 松开空格后左键恢复成拉矩形
    view.set_rois([])
    drag(viewport, start, end)
    check("松开空格后左键恢复成拉矩形", view.roi_count() == 1, f"count={view.roi_count()}")

    # ---- 焦点不在视图上也要能进抓手（这就是按键走应用级过滤的原因）
    view.set_rois([])
    window._panel.setFocus()
    QApplication.processEvents()
    check("焦点在结果面板上", window._panel.hasFocus())
    QTest.keyPress(window, Qt.Key.Key_Space)
    check("焦点不在视图上也能进入抓手", view.hand_mode)
    grab = viewport.rect().center()
    drag(viewport, QPoint(grab.x() + 30, grab.y()), QPoint(grab.x() - 30, grab.y()))
    check("面板有焦点时空格拖动同样不留标注", view.roi_count() == 0,
          f"count={view.roi_count()}")
    QTest.keyRelease(window, Qt.Key.Key_Space)
    check("松开空格后退出抓手", not view.hand_mode)


def _double_click(widget, pos: QPoint) -> None:
    """往 widget 送一个双击事件（走真实的事件处理）。"""
    _send_mouse(
        widget,
        QEvent.Type.MouseButtonDblClick,
        pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )


def _click(widget, pos: QPoint) -> None:
    """往 widget 送一次完整的左键点击（按下 + 松开）。"""
    _send_mouse(widget, QEvent.Type.MouseButtonPress, pos,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _send_mouse(widget, QEvent.Type.MouseButtonRelease, pos,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def test_text_edit(window: MainWindow, root: Path) -> None:
    """双击图元 / 双击结果面板都能改 ROI 的 text 字段，并且立刻落盘。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    view = window.view
    viewport = view.viewport()
    view.set_rois([Roi(20, 20, 60, 30, "原始文本"), Roi(120, 50, 40, 25)])
    view.fit_to_window()
    QApplication.processEvents()

    def center_of(index: int) -> QPoint:
        item = view.roi_items()[index]
        return view.mapFromScene(item.mapRectToScene(item.rect()).center())

    check("默认不在编辑文本", not view.is_editing_text())

    # ---- 双击图元：打开、采纳、落盘、面板同步
    item = view.roi_items()[0]
    _double_click(viewport, center_of(0))
    check("双击图元打开就地编辑器", view.is_editing_text())
    if not view.is_editing_text():
        return
    check("编辑器带出原文本", view._text_editor.toPlainText() == "原始文本",
          repr(view._text_editor.toPlainText()))
    check("被双击的图元处于选中态", item.isSelected())
    area = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    check("编辑器盖在 ROI 上", view._text_editor.geometry().contains(area.center()),
          f"{view._text_editor.geometry()} vs {area}")

    view._text_editor.setPlainText("手改后的文本")
    QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    check("Ctrl+Enter 采纳并收起编辑器", not view.is_editing_text())
    check("文本写回图元", view.rois()[0].text == "手改后的文本", str(view.rois()))
    check("改完立刻写进边车文件", RoiStore().get(paths[0])[0].text == "手改后的文本",
          str(RoiStore().get(paths[0])))
    check("结果面板跟着更新", window._panel.item(0, 2).text() == "手改后的文本",
          window._panel.item(0, 2).text())
    check("状态栏统计已识别数", "已识别 1" in window._label_roi.text(),
          window._label_roi.text())

    # ---- Esc 放弃
    _double_click(viewport, center_of(0))
    check("再次双击可以继续编辑", view.is_editing_text())
    view._text_editor.setPlainText("不该保存的文本")
    QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
    check("Esc 放弃并收起编辑器", not view.is_editing_text())
    check("放弃后图元文本不变", view.rois()[0].text == "手改后的文本", str(view.rois()))
    check("放弃后文件也没被改", RoiStore().get(paths[0])[0].text == "手改后的文本",
          str(RoiStore().get(paths[0])))

    # ---- 没识别过的标注也能编辑（编辑器从空串开始）
    _double_click(viewport, center_of(1))
    check("空文本的标注也能打开编辑器", view.is_editing_text())
    check("空文本的编辑器是空的", view._text_editor.toPlainText() == "",
          repr(view._text_editor.toPlainText()))
    QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)

    # ---- Ctrl+S 会把正在编辑的文本一起收下去
    _double_click(viewport, center_of(1))
    view._text_editor.setPlainText("保存键收下的文本")
    window.act_save_rois.trigger()
    check("Ctrl+S 会先收下正在编辑的文本",
          RoiStore().get(paths[0])[1].text == "保存键收下的文本",
          str(RoiStore().get(paths[0])))
    check("Ctrl+S 后编辑器已收起", not view.is_editing_text())

    # ---- 编辑到一半切图：先收下正在敲的字
    _double_click(viewport, center_of(0))
    view._text_editor.setPlainText("切图前敲的字")
    window.show_next()
    check("切图会先收下正在编辑的文本", RoiStore().get(paths[0])[0].text == "切图前敲的字",
          str(RoiStore().get(paths[0])))
    check("切图后编辑器已收起", not view.is_editing_text())
    window.show_prev()
    check("切回来能看到改后的文本", view.rois()[0].text == "切图前敲的字",
          str(view.rois()))

    # ---- 双击空白处不打开编辑器
    empty = view.mapFromScene(view.image_rect().topLeft() + QPointF(3, 3))
    _double_click(viewport, empty)
    check("双击空白处不打开编辑器", not view.is_editing_text())

    # ---- 结果面板：识别文本列可编辑，改完同样写回图元并落盘
    cell = window._panel.item(0, 2)
    check("识别文本单元格可编辑", bool(cell.flags() & Qt.ItemFlag.ItemIsEditable))
    check("面板只在双击时进入编辑",
          bool(window._panel.editTriggers() & QAbstractItemView.EditTrigger.DoubleClicked))
    check("位置列仍然只读",
          not (window._panel.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable))
    window._panel.editItem(cell)  # 与双击该单元格走同一条路
    QApplication.processEvents()
    editor = window._panel.findChild(QLineEdit)
    check("面板编辑器带出原文本", editor is not None and editor.text() == "切图前敲的字",
          repr(editor.text()) if editor is not None else "没有打开编辑器")
    if editor is None:
        return
    editor.setText("面板改的文本")
    QTest.keyClick(editor, Qt.Key.Key_Return)  # 回车提交，等价于点开别处
    QApplication.processEvents()
    check("面板改文本会写回图元", view.rois()[0].text == "面板改的文本", str(view.rois()))
    check("面板改文本会立刻落盘", RoiStore().get(paths[0])[0].text == "面板改的文本",
          str(RoiStore().get(paths[0])))

    # ---- 面板正在编辑时收到重填（OCR 收尾、切图都会走这里）不能崩
    window._panel.editItem(window._panel.item(0, 2))
    window._refresh_ocr_panel()
    QApplication.processEvents()
    check("面板编辑中重填不崩溃", window._panel.rowCount() == view.roi_count(),
          f"{window._panel.rowCount()} vs {view.roi_count()}")

    # ---- 进入排序模式会先收下正在就地编辑的文本（跟画布上按下左键一个道理）
    view.set_rois([Roi(20, 20, 60, 30, "排序前")])
    _double_click(viewport, center_of(0))
    check("排序前先打开编辑器", view.is_editing_text())
    view._text_editor.setPlainText("排序前敲的字")
    window.act_sort_rois.trigger()
    check("进入排序模式会先收下正在编辑的文本",
          not view.is_editing_text() and view.rois()[0].text == "排序前敲的字",
          str(view.rois()))
    QTest.keyClick(view, Qt.Key.Key_Escape)  # 退出排序模式，别留给别的测试

    view.clear_rois()  # 别把标注留给后面的 OCR 测试


def _box_point(view, box) -> QPoint:
    """文本框在视口里的中心点。

    框带 ``ItemIgnoresTransformations``，屏幕上从 ``box.pos()`` 起偏移
    ``content_rect()`` 的像素，不需要经过画布缩放换算。
    """
    origin = view.mapFromScene(box.pos())
    center = box.content_rect().center()
    return QPoint(origin.x() + int(center.x()), origin.y() + int(center.y()))


def test_text_box(window: MainWindow, root: Path) -> None:
    """选中标注时旁边显示文本框：位置随排版方向、尺寸尽量与标注对齐、滚轮调字号。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    view = window.view
    viewport = view.viewport()
    window._set_text_direction(TextDirection.HORIZONTAL)  # 从默认设定开始，别受上次运行影响
    view.set_text_font_size(MIN_FONT_SIZE)
    check("默认排版方向是横排", view.text_direction is TextDirection.HORIZONTAL)
    check("默认字号就是最小字号", abs(view.text_font_size - MIN_FONT_SIZE) < 1e-9,
          str(view.text_font_size))

    view.set_rois([Roi(20, 20, 60, 30, "認識されたテキスト"), Roi(120, 50, 40, 25)])
    view.fit_to_window()
    QApplication.processEvents()

    items = view.roi_items()
    check("文本框不算标注", view.roi_count() == 2 and len(view.rois()) == 2,
          f"count={view.roi_count()}")
    check("没选中时不显示文本框", not any(item.text_box().isVisible() for item in items))

    # ---- 选中 → 出现，框里是识别文本
    item = items[0]
    item.setSelected(True)
    box = item.text_box()
    check("选中后出现文本框", box.isVisible())
    check("框里是识别文本", box.text() == "認識されたテキスト", box.text())
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()

    # ---- 横排：贴上下，宽度与标注的屏幕宽度一致
    below = abs(box.pos().y() - item.rect().bottom()) < 1e-9
    check("横排时框贴在标注的上方或下方",
          abs(box.pos().x() - item.rect().left()) < 1e-9
          and (below or abs(box.pos().y() - item.rect().top()) < 1e-9),
          f"{box.pos()} vs {item.rect()}")
    check("横排的框不压住标注",
          box.content_rect().top() >= GAP - 1e-6 if below
          else box.content_rect().bottom() <= -GAP + 1e-6,
          str(box.content_rect()))
    check("横排时框宽与标注的屏幕宽相等",
          abs(box.content_rect().width() - screen.width()) < 1.5,
          f"{box.content_rect().width()} vs {screen.width()}")

    # ---- 尺寸跟着标注走：拖手柄把标注拉宽，框宽随之对齐
    item.setSelected(True)
    roi_rect = item.mapRectToScene(item.rect())
    grip = view.mapFromScene(QPointF(roi_rect.right(), roi_rect.center().y()))
    drag(viewport, grip, QPoint(grip.x() + 60, grip.y()))
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    check("拖宽标注后框宽跟着对齐",
          abs(box.content_rect().width() - screen.width()) < 1.5,
          f"{box.content_rect().width()} vs {screen.width()}")

    # ---- 竖排：贴左右，高度与标注的屏幕高度一致
    window._set_text_direction(TextDirection.VERTICAL)
    QApplication.processEvents()
    check("切换方向后现有的框跟着换", box.direction() is TextDirection.VERTICAL)
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    after = abs(box.pos().x() - item.rect().right()) < 1e-9
    check("竖排时框贴在标注的左侧或右侧",
          abs(box.pos().y() - item.rect().top()) < 1e-9
          and (after or abs(box.pos().x() - item.rect().left()) < 1e-9),
          f"{box.pos()} vs {item.rect()}")
    check("竖排的框不压住标注",
          box.content_rect().left() >= GAP - 1e-6 if after
          else box.content_rect().right() <= -GAP + 1e-6,
          str(box.content_rect()))
    check("竖排时框高与标注的屏幕高相等",
          abs(box.content_rect().height() - screen.height()) < 1.5,
          f"{box.content_rect().height()} vs {screen.height()}")

    # ---- 标注太小 / 排不下时，退回按文字自然大小排
    view.set_rois([Roi(20, 20, 60, 4, "認識されたテキスト")])  # 竖排：高度放不下一个字
    view.fit_to_window()
    QApplication.processEvents()
    item = view.roi_items()[0]
    item.setSelected(True)
    box = item.text_box()
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    check("竖排：标注太矮时不再强行对齐高度",
          box.content_rect().height() > screen.height() + 1,
          f"{box.content_rect().height()} vs {screen.height()}")

    window._set_text_direction(TextDirection.HORIZONTAL)
    view.set_rois([Roi(20, 20, 4, 30, "認識されたテキスト")])  # 横排：宽度放不下一个字
    view.fit_to_window()
    QApplication.processEvents()
    item = view.roi_items()[0]
    item.setSelected(True)
    box = item.text_box()
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    check("横排：标注太窄时不再强行对齐宽度",
          box.content_rect().width() > screen.width() + 1,
          f"{box.content_rect().width()} vs {screen.width()}")

    # ---- 没有视图信息时（脱离视图使用）按兜底规则贴边
    standalone = RoiItem(QRectF(20, 20, 60, 30), bounds=view.image_rect(), text="認識")
    standalone_box = standalone.text_box()
    check("没有视图信息时横排默认贴下方",
          standalone_box.after() and standalone_box.content_rect().top() >= GAP - 1e-6,
          f"{standalone_box.after()} {standalone_box.content_rect()}")
    standalone.set_text_direction(TextDirection.VERTICAL)
    standalone.setRect(QRectF(150, 20, 40, 30))  # 图像右半边
    check("没有视图信息时竖排按图像左右半边贴左侧",
          not standalone_box.after() and standalone_box.content_rect().right() <= -GAP + 1e-6,
          f"{standalone_box.after()} {standalone_box.content_rect()}")

    # ---- 方向与字号都记进设置
    check("方向记进设置", window._settings.value("text_direction") == "horizontal",
          str(window._settings.value("text_direction")))
    check("菜单勾选跟着换",
          window._direction_actions[TextDirection.HORIZONTAL].isChecked()
          and not window._direction_actions[TextDirection.VERTICAL].isChecked())
    view.set_rois([Roi(20, 20, 60, 30, "あいうえお")])
    check("新画的标注沿用当前方向",
          view.roi_items()[0].text_box().direction() is TextDirection.HORIZONTAL)

    # ---- 没识别过 / 超长文本
    view.set_rois([Roi(20, 20, 60, 30)])
    item = view.roi_items()[0]
    item.setSelected(True)
    box = item.text_box()
    check("没识别过也有框，显示提示",
          box.isVisible() and box.text() == "" and box.display_text() == EMPTY_HINT,
          box.display_text())
    box.set_text("あ" * 1000)
    check("超长文本截断并加省略号",
          box.display_text().endswith("…") and len(box.display_text()) <= MAX_CHARS + 1,
          str(len(box.display_text())))

    # ---- 点 / 双击文本框
    view.set_rois([Roi(20, 20, 60, 30, "认识")])
    view.fit_to_window()
    QApplication.processEvents()
    item = view.roi_items()[0]
    item.setSelected(True)
    box = item.text_box()
    inside = _box_point(view, box)
    check("文本框挡在标注旁边（命中测试用）", view.itemAt(inside) is box,
          str(view.itemAt(inside)))

    _send_mouse(viewport, QEvent.Type.MouseButtonPress, inside,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _send_mouse(viewport, QEvent.Type.MouseButtonRelease, inside,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    check("点文本框不会在下面拉出新标注", view.roi_count() == 1, f"count={view.roi_count()}")
    check("点文本框选中它所属的标注", item.isSelected())

    _double_click(viewport, inside)
    check("双击文本框也能编辑这个标注的文本",
          view.is_editing_text() and view._text_item is item)
    QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)


def test_text_box_font(window: MainWindow, root: Path) -> None:
    """光标在文本框上滚滚轮：调字号、框跟着变，放不下就拒绝，并且记进设置。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    view = window.view
    window._set_text_direction(TextDirection.HORIZONTAL)
    view.set_text_font_size(MIN_FONT_SIZE)
    view.set_rois([Roi(20, 20, 60, 30, "認識されたテキスト")])
    view.fit_to_window()
    QApplication.processEvents()
    item = view.roi_items()[0]
    item.setSelected(True)
    box = item.text_box()
    point = _box_point(view, box)
    screen = view.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
    check("字号检查：光标落在文本框上", view.itemAt(point) is box, str(view.itemAt(point)))

    # ---- 调大一档：字号变大、框跟着变高、宽仍然和标注对齐；不缩放
    scale_before = view.scale_factor
    height_before = box.content_rect().height()
    _send_wheel(window, 120, point)
    check("框上滚滚轮改的是字号，不是缩放",
          abs(view.scale_factor - scale_before) < 1e-9
          and abs(view.text_font_size - (MIN_FONT_SIZE + FONT_SIZE_STEP)) < 1e-9,
          f"scale={view.scale_factor:.3f} size={view.text_font_size}")
    check("字号变大后框跟着变高", box.content_rect().height() > height_before,
          f"{height_before} -> {box.content_rect().height()}")
    check("调字号后依然与标注宽度对齐",
          abs(box.content_rect().width() - screen.width()) < 1.5,
          f"{box.content_rect().width()} vs {screen.width()}")
    check("字号记进设置",
          abs(window._settings.value("text_font_size", 0.0, type=float)
              - view.text_font_size) < 1e-9,
          str(window._settings.value("text_font_size")))
    check("状态栏提示字号", "字号" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())

    # ---- 一直调大到放不下为止：拒绝继续调大，并给出提示
    limits: list[float] = []
    view.textFontLimitReached.connect(limits.append)
    for _ in range(80):
        _send_wheel(window, 120, point)
    reached = view.text_font_size
    check("字号不会无上限地涨", reached < MAX_FONT_SIZE, str(reached))
    check("到上限后给出提示",
          bool(limits) and "最大字号" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())
    check("上限处再大一档就放不下了", not item.can_enlarge_text(reached + FONT_SIZE_STEP),
          str(reached))
    _send_wheel(window, 120, point)
    check("到上限后再滚一次字号也不变", abs(view.text_font_size - reached) < 1e-9,
          f"{reached} -> {view.text_font_size}")

    # ---- 调小回默认（最小）字号
    for _ in range(100):
        _send_wheel(window, -120, point)
    check("字号能调回最小（默认）字号",
          abs(view.text_font_size - MIN_FONT_SIZE) < 1e-9, str(view.text_font_size))
    _send_wheel(window, -120, point)
    check("已经是最小字号时不再变小",
          abs(view.text_font_size - MIN_FONT_SIZE) < 1e-9, str(view.text_font_size))

    # ---- 记在设置里的字号，下次启动读回来（并且会夹回允许范围）
    window._settings.setValue("text_font_size", 18.0)
    check("字号能从设置里读回", abs(window._restore_text_font_size() - 18.0) < 1e-9,
          str(window._restore_text_font_size()))
    window._settings.setValue("text_font_size", 999.0)
    check("设置里的字号超上限会被夹住",
          abs(window._restore_text_font_size() - MAX_FONT_SIZE) < 1e-9,
          str(window._restore_text_font_size()))
    window._settings.setValue("text_font_size", 1.0)
    check("设置里的字号超下限会被夹住",
          abs(window._restore_text_font_size() - MIN_FONT_SIZE) < 1e-9,
          str(window._restore_text_font_size()))

    # ---- 收尾：字号与方向都恢复默认，别把设置留给别的测试
    view.set_text_font_size(MIN_FONT_SIZE)
    window._settings.setValue("text_font_size", MIN_FONT_SIZE)
    view.clear_rois()


def test_roi_order(window: MainWindow, root: Path) -> None:
    """ROI 排序号：初始是创建顺序，排序模式按点击顺序重排，并且随标注一起落盘。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    view = window.view
    viewport = view.viewport()

    def order_of(text: str) -> int:
        return next(item.order() for item in view.roi_items() if item.text() == text)

    def point_of(text: str) -> QPoint:
        item = next(item for item in view.roi_items() if item.text() == text)
        return view.mapFromScene(item.mapRectToScene(item.rect()).center())

    # ---- 初始顺序 = 创建（读入）顺序，不是坐标顺序
    # 故意让坐标顺序与创建顺序相反：先建的在右边，后建的在左上
    view.set_rois([Roi(120, 40, 50, 25, "右边"), Roi(10, 10, 40, 20, "左边")])
    view.fit_to_window()
    QApplication.processEvents()
    check("初始顺序就是创建顺序，不按坐标重排",
          [roi.text for roi in view.rois()] == ["右边", "左边"]
          and [roi.index for roi in view.rois()] == [1, 2],
          str(view.rois()))
    check("面板第一列显示排序号",
          window._panel.item(0, 0).text() == "1"
          and window._panel.item(1, 0).text() == "2",
          f"{window._panel.item(0, 0).text()}/{window._panel.item(1, 0).text()}")
    check("面板行序跟着排序号",
          window._panel.item(0, 2).text() == "右边"
          and window._panel.item(1, 2).text() == "左边",
          f"{window._panel.item(0, 2).text()}/{window._panel.item(1, 2).text()}")

    # ---- 新画的框排在最后
    start = view.mapFromScene(QPointF(10, 75))
    drag(viewport, start, QPoint(start.x() + 40, start.y() + 30))
    check("新画的框排在最后",
          view.roi_count() == 3 and view.rois()[2].index == 3, str(view.rois()))

    # ---- 删掉中间那个之后序号重排，不留空档
    view.select_roi_item(view.roi_items()[1], center=False)
    window.act_delete_roi.trigger()
    check("删除后序号重排成连续的 1..N",
          [roi.index for roi in view.rois()] == [1, 2], str(view.rois()))

    # ---- 排序模式：按点击顺序从 1 开始编号
    view.set_rois([Roi(10, 10, 40, 20, "A"), Roi(120, 10, 40, 20, "B"),
                   Roi(10, 60, 40, 20, "C")])
    view.fit_to_window()
    QApplication.processEvents()
    check("Ctrl+Shift+S 绑定到排序模式",
          "Ctrl+Shift+S" in _shortcut_texts(window.act_sort_rois),
          str(_shortcut_texts(window.act_sort_rois)))

    window.act_sort_rois.trigger()
    check("进入排序模式（菜单 / 快捷键同一个动作）",
          view.sort_mode and window.act_sort_rois.isChecked())
    check("排序模式下每个框都露出角标",
          all(item.order_badge().isVisible() for item in view.roi_items()))
    check("进入时都还没点到（角标是 ?）",
          all(item.order_badge().is_pending() for item in view.roi_items()))
    check("排序模式下指针是十字",
          viewport.cursor().shape() == Qt.CursorShape.CrossCursor,
          str(viewport.cursor().shape()))

    _click(viewport, point_of("C"))
    check("第一个点的是 1", order_of("C") == 1, f"C={order_of('C')}")
    check("点过的框角标不再是 ?",
          not next(i for i in view.roi_items()
                   if i.text() == "C").order_badge().is_pending())
    check("状态栏报告编号进度", "已编号 1/3" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())
    check("结果面板跟着重排",
          window._panel.item(0, 2).text() == "C", window._panel.item(0, 2).text())

    _click(viewport, point_of("B"))
    _click(viewport, point_of("A"))
    check("按点击顺序排成 1、2、3",
          [item.text() for item in view.roi_items()] == ["C", "B", "A"],
          str([item.text() for item in view.roi_items()]))
    check("排序号与点击顺序一致",
          (order_of("C"), order_of("B"), order_of("A")) == (1, 2, 3),
          f"{order_of('C')},{order_of('B')},{order_of('A')}")

    # ---- 重复点已编号的框：不改动顺序，只说明一下
    _click(viewport, point_of("C"))
    check("重复点已编号的框不改动顺序",
          (order_of("C"), order_of("B"), order_of("A")) == (1, 2, 3),
          f"{order_of('C')},{order_of('B')},{order_of('A')}")
    check("重复点击时状态栏说明原因",
          "已经排在第 1 位" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())
    check("排序模式下的点击不选中框",
          not any(item.isSelected() for item in view.roi_items()))

    # ---- 空白处点击 / 拖动都不会新建或移动图元
    _click(viewport, view.mapFromScene(QPointF(180, 90)))
    check("点空白处不新建标注", view.roi_count() == 3, str(view.roi_count()))

    before = next(item for item in view.roi_items() if item.text() == "B").to_roi()
    grip = point_of("B")
    drag(viewport, grip, QPoint(grip.x() + 40, grip.y() + 20))
    after = next(item for item in view.roi_items() if item.text() == "B").to_roi()
    check("排序模式下拖不动图元",
          abs(after.x - before.x) < 1e-6 and abs(after.y - before.y) < 1e-6,
          f"{before} -> {after}")

    # ---- 双击也不进文本编辑（这个模式下左键只用来编号）
    _double_click(viewport, point_of("A"))
    check("排序模式下双击不进文本编辑", not view.is_editing_text())

    # ---- 每点一次就落盘，不用等切图
    stored = RoiStore().get(paths[0])
    check("排序结果立刻写进边车文件",
          [roi.text for roi in stored] == ["C", "B", "A"]
          and [roi.index for roi in stored] == [1, 2, 3],
          str(stored))

    # ---- Esc 退出：角标收起、指针恢复
    QTest.keyClick(view, Qt.Key.Key_Escape)
    check("Esc 退出排序模式",
          not view.sort_mode and not window.act_sort_rois.isChecked())
    check("退出后角标收起来",
          not any(item.order_badge().isVisible() for item in view.roi_items()))
    check("退出后指针恢复默认",
          viewport.cursor().shape() == Qt.CursorShape.ArrowCursor,
          str(viewport.cursor().shape()))

    # ---- 没点到的框跟在后面，保持原有相对次序
    view.set_rois([Roi(10, 10, 40, 20, "A"), Roi(70, 10, 40, 20, "B"),
                   Roi(130, 10, 40, 20, "C"), Roi(10, 60, 40, 20, "D")])
    view.fit_to_window()
    QApplication.processEvents()
    window.act_sort_rois.trigger()
    _click(viewport, point_of("C"))
    QTest.keyClick(view, Qt.Key.Key_Escape)  # 只点一个就退出
    check("没点到的框按原顺序接在后面",
          [item.text() for item in view.roi_items()] == ["C", "A", "B", "D"],
          str([item.text() for item in view.roi_items()]))

    # ---- 重新进入是从头排，不沿用上次的"已点"状态
    QTest.keyClick(window, Qt.Key.Key_S,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    check("Ctrl+Shift+S 能真正进入排序模式", view.sort_mode)
    check("重新进入时又都回到未点状态",
          all(item.order_badge().is_pending() for item in view.roi_items()))
    _click(viewport, point_of("D"))
    check("重新排的第一个又是 1", order_of("D") == 1, f"D={order_of('D')}")

    # ---- 排序模式只作用于单张图：切图自动退出，切回来顺序还在
    window.show_next()
    check("切图会自动退出排序模式",
          not view.sort_mode and not window.act_sort_rois.isChecked())
    check("新的一张没有角标残留",
          not any(item.order_badge().isVisible() for item in view.roi_items()))
    window.show_prev()
    check("切回来顺序还在",
          [item.text() for item in view.roi_items()] == ["D", "C", "A", "B"],
          str([item.text() for item in view.roi_items()]))

    view.clear_rois()  # 别把标注留给后面的 OCR 测试


def _fake_manga_ocr_run(image: QImage) -> tuple[str, dict]:
    """用假的 ``manga_ocr`` 模块走一遍 :class:`MangaOcrEngine`，验证接线。

    这样不用装 torch、也不用下载模型，就能确认"QImage 图块 → PIL 图像 → 文本"这条路径。
    """
    seen: dict = {}

    class FakeMangaOcr:
        def __init__(self, **kwargs) -> None:
            seen["kwargs"] = kwargs

        def __call__(self, img) -> str:
            seen["size"] = img.size
            seen["mode"] = img.mode
            return "  認識された テキスト \n"

    module = types.ModuleType("manga_ocr")
    module.MangaOcr = FakeMangaOcr
    saved = sys.modules.get("manga_ocr")
    sys.modules["manga_ocr"] = module
    try:
        text = MangaOcrEngine().recognize(image)
    finally:
        if saved is None:
            sys.modules.pop("manga_ocr", None)
        else:
            sys.modules["manga_ocr"] = saved
    return text, seen


def test_ocr_engine() -> None:
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))

    crop = crop_roi(image, Roi(10.4, 20.6, 30.2, 40.1))
    check("按 ROI 裁剪：边界向外取整", (crop.width(), crop.height()) == (31, 41),
          f"{crop.width()}×{crop.height()}")
    check("裁剪不会超出原图", crop.width() <= image.width() and crop.height() <= image.height())

    outside = crop_roi(image, Roi(190, 90, 50, 50))
    check("ROI 超出图像时裁剪被夹住", (outside.width(), outside.height()) == (10, 10),
          f"{outside.width()}×{outside.height()}")
    try:
        crop_roi(image, Roi(500, 500, 10, 10))
    except OcrError:
        check("ROI 完全在图像外时报错", True)
    else:
        check("ROI 完全在图像外时报错", False, "没有抛 OcrError")

    check("模型清单里有 manga-ocr", is_known_model(MANGA_OCR) and MANGA_OCR in
          [model.name for model in OCR_MODELS], str([model.name for model in OCR_MODELS]))
    check("模型有给菜单用的名字", model_label(MANGA_OCR) != MANGA_OCR, model_label(MANGA_OCR))
    try:
        create_engine("不存在的模型")
    except OcrError:
        check("未知模型名会报错", True)
    else:
        check("未知模型名会报错", False, "没有抛 OcrError")

    text, seen = _fake_manga_ocr_run(QImage(60, 30, QImage.Format.Format_RGB32))
    check("manga-ocr 接线：图块按 PIL 图像传入", seen.get("size") == (60, 30), str(seen))
    check("manga-ocr 接线：识别文本去掉首尾空白", text == "認識された テキスト", repr(text))


def wait_for_ocr(window: MainWindow, timeout_ms: int = 20000) -> bool:
    """等后台识别任务收尾；返回是否真的等到了。

    ``qWait`` 之后必须让出一下 GIL：紧循环里 ``qWait`` 会长时间占着 GIL，把识别线程
    饿死（真模型加载一下就看得出来），加个 ``sleep`` 才是"等它跑完"而不是"跟它抢"。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while window._ocr_job is not None and time.monotonic() < deadline:
        QTest.qWait(10)
        time.sleep(0.01)
    return window._ocr_job is None


def test_ocr_workflow(window: MainWindow, root: Path, engine: FakeOcrEngine) -> None:
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.resize(900, 700)
    window.show()
    QTest.qWaitForWindowExposed(window)

    window.view.set_rois([Roi(10, 10, 40, 20), Roi(60, 30, 30, 30), Roi(120, 40, 50, 25)])
    check("OCR 准备：三个标注", window.view.roi_count() == 3, str(window.view.roi_count()))
    check("结果面板行数跟随标注", window._panel.rowCount() == 3, str(window._panel.rowCount()))
    check("有标注时「识别全部」可用", window.act_ocr_all.isEnabled())
    check("没选中任何标注时「识别选中」不可用", not window.act_ocr_selected.isEnabled())

    # ---- 只识别选中的那一个
    window.view.roi_items()[1].setSelected(True)
    check("选中标注后「识别选中」可用", window.act_ocr_selected.isEnabled())
    before = len(engine.sizes)
    window.act_ocr_selected.trigger()
    check("识别期间 OCR 动作禁用",
          not window.act_ocr_selected.isEnabled() and not window.act_ocr_all.isEnabled())
    check("识别任务能跑完", wait_for_ocr(window))
    check("只把选中的那个送去识别", engine.sizes[before:] == [(30, 30)],
          str(engine.sizes[before:]))

    texts = [roi.text for roi in window.view.rois()]
    check("结果只挂到被识别的 ROI 上", [bool(t) for t in texts] == [False, True, False],
          str(texts))
    check("状态栏报告识别结果", "已识别 1 个标注" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())

    window.act_save_rois.trigger()
    raw = json.loads(sidecar_path(paths[0]).read_text(encoding="utf-8"))
    check("识别文本写进边车文件", raw["annotations"][1].get("text") == texts[1],
          json.dumps(raw, ensure_ascii=False))
    check("没识别的标注仍不写 text 字段", "text" not in raw["annotations"][0],
          json.dumps(raw["annotations"][0], ensure_ascii=False))
    check("边车文件版本为 3", raw["version"] == 3, str(raw["version"]))

    # ---- 识别全部
    window.act_ocr_all.trigger()
    check("识别全部也能跑完", wait_for_ocr(window))
    all_texts = [roi.text for roi in window.view.rois()]
    check("全部识别：三个都有文本", all(all_texts), str(all_texts))
    check("重识别会覆盖旧结果", all_texts[1] != texts[1], f"{texts[1]} -> {all_texts[1]}")
    check("结果面板同步显示文本", window._panel.item(0, 2).text() == all_texts[0],
          window._panel.item(0, 2).text())

    # 切图会清空场景（连选中的图元一起删掉），结果面板此时不能去碰已经作废的引用
    window.view.roi_items()[0].setSelected(True)
    window.show_next()
    window.show_prev()
    check("切走再切回，识别结果跟着标注回来",
          [roi.text for roi in window.view.rois()] == all_texts,
          str([roi.text for roi in window.view.rois()]))

    item = window.view.roi_items()[0]
    item.setPos(item.pos() + QPointF(5, 5))
    check("移动标注不丢识别结果", window.view.rois()[0].text == all_texts[0],
          window.view.rois()[0].text)

    window._panel.clearSelection()
    window._panel.selectRow(2)
    QApplication.processEvents()
    check("点结果面板的行会选中对应的框", window.view.roi_items()[2].isSelected())

    window.view.roi_items()[2].setSelected(True)
    window.act_delete_roi.trigger()
    window.show_next()
    check("删除标注后文件里也不再有它的文本", len(RoiStore().get(paths[0])) == 2,
          str(RoiStore().get(paths[0])))


def test_ocr_cancel(window: MainWindow, root: Path, engine: FakeOcrEngine) -> None:
    """识别到一半切图：要能停下来，而且已经识别出来的部分不能丢。"""
    paths = [p for p in collect_images(root) if p.name != "broken.png"]
    window.open_paths(paths)
    window.view.set_rois([Roi(10, 10, 40, 20), Roi(60, 30, 30, 30), Roi(120, 40, 50, 25)])
    window.act_save_rois.trigger()

    engine.delay = 0.05  # 让每个图块慢一点，好在识别中途切图
    window.act_ocr_all.trigger()
    check("识别任务已经起来", window._ocr_job is not None)
    window.show_next()
    engine.delay = 0.0

    check("切图会取消识别并收尾", window._ocr_job is None)
    check("切图后停在新的一张", window._index == 1, str(window._index))
    stored = RoiStore().get(paths[0])
    check("取消后标注一个不少", len(stored) == 3, str(stored))
    check("取消最多只保留已经识别完的那一个", sum(1 for r in stored if r.text) <= 1,
          str([r.text for r in stored]))


def test_ocr_merge(window: MainWindow, root: Path) -> None:
    """兜底路径：结果迟到、图已切走时按几何并回文件。"""
    path = root / "sub" / "deep.png"
    window._roi_store.set(path, [Roi(10, 20, 30, 40)])
    merged = window._merge_ocr_text(path, [(Roi(10, 20, 30, 40), "兜底文本")])
    check("迟到结果按位置并回文件", merged == 1, str(merged))
    check("兜底文本真的落了盘", RoiStore().get(path)[0].text == "兜底文本",
          str(RoiStore().get(path)))

    merged = window._merge_ocr_text(path, [(Roi(99, 99, 9, 9), "对不上的结果")])
    check("位置对不上的结果不写", merged == 0, str(merged))
    check("原有文本没被改掉", RoiStore().get(path)[0].text == "兜底文本")


def _shortcut_texts(action) -> list[str]:
    return [seq.toString() for seq in action.shortcuts()]


def fits_in_viewport(window: MainWindow, tolerance: int = 3) -> bool:
    """图像当前映射到视口里的矩形是否完整落在视口内。

    只量图像本身，不用 ``itemsBoundingRect()``：ROI 的缩放手柄是挂在矩形外的子图元，
    会把包围盒撑大，量出来就不是"整张图可见"了。
    """
    view = window.view
    image_rect = view.image_rect()
    if image_rect.isEmpty():
        return False
    shown = view.mapFromScene(image_rect).boundingRect()
    viewport = view.viewport().rect().adjusted(-tolerance, -tolerance, tolerance, tolerance)
    return viewport.contains(shown)


def _send_wheel(window: MainWindow, delta: int, pos: QPoint | None = None) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent

    if pos is None:
        pos = QPoint(60, 40)
    event = QWheelEvent(
        QPointF(pos),
        QPointF(window.view.viewport().mapToGlobal(pos)),
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
        engine = FakeOcrEngine()

        test_collect(root)
        test_roi_store(root)
        test_ocr_engine()

        window = MainWindow(ocr_factory=lambda name: engine)
        test_navigation(window, root)
        test_zoom(window)
        test_broken_image(window, root)
        test_roi_interaction(window, root)
        test_hand_mode(window, root)
        test_text_edit(window, root)
        test_text_box(window, root)
        test_text_box_font(window, root)
        test_roi_order(window, root)
        test_ocr_workflow(window, root, engine)
        test_ocr_cancel(window, root, engine)
        test_ocr_merge(window, root)
        window.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：" + "; ".join(FAILURES))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
