"""无界面冒烟测试：不需要 pytest，直接 ``uv run python tests/smoke_test.py``。

覆盖：文件夹扫描与自然排序、打开/切换图像、缩放范围、快捷键绑定、ROI 框选与
标注存取、OCR（裁剪 / 引擎接线 / 后台任务 / 结果落盘）以及坏图不让程序崩溃。

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
from PySide6.QtWidgets import QApplication  # noqa: E402

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
    """两组标注是否一致（几何允许存盘四舍五入的误差，识别文本要求完全相同）。"""
    if len(left) != len(right):
        return False
    return all(
        abs(a.x - b.x) <= tolerance
        and abs(a.y - b.y) <= tolerance
        and abs(a.width - b.width) <= tolerance
        and abs(a.height - b.height) <= tolerance
        and a.text == b.text
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

    recognized = [Roi(1, 2, 30, 40, "認識された"), Roi(50, 60, 70, 80)]
    store = RoiStore()
    store.set(path, recognized)
    check("识别文本随标注一起读回", same_rois(RoiStore().get(path), recognized),
          str(RoiStore().get(path)))
    raw = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    check("识别过的标注写了 text 字段", raw["annotations"][0].get("text") == "認識された",
          json.dumps(raw, ensure_ascii=False))
    check("没识别过的标注不写 text 字段", "text" not in raw["annotations"][1],
          json.dumps(raw["annotations"][1], ensure_ascii=False))
    check("边车文件版本升到 2", raw["version"] == 2, str(raw["version"]))

    legacy = sidecar_path(path)
    legacy.write_text('{"version": 1, "annotations": [{"x": 1, "y": 2, "w": 3, "h": 4}]}',
                      encoding="utf-8")
    check("v1 老文件照常读入（text 为空串）", RoiStore().get(path) == [Roi(1, 2, 3, 4, "")],
          str(RoiStore().get(path)))


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
    check("边车文件版本为 2", raw["version"] == 2, str(raw["version"]))

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
        engine = FakeOcrEngine()

        test_collect(root)
        test_roi_store(root)
        test_ocr_engine()

        window = MainWindow(ocr_factory=lambda name: engine)
        test_navigation(window, root)
        test_zoom(window)
        test_broken_image(window, root)
        test_roi_interaction(window, root)
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
