# 架构说明

写给要改这个项目的人：模块怎么分层、一次"打开图像"经过哪些环节、哪些约定不能破坏。
安装、运行与操作方式见 [README](../README.md)。

## 设计边界

当前版本做"浏览 + 框选 + 每图标注存取"：打开图像、缩放平移、上下张切换、左键拖出矩形并调整、
切图时自动保存与恢复标注。OCR 识别与文字校对本身不在范围内。

由此定下一条贯穿全项目的原则：**视图层不保存任何跨图状态**。`ImageView` 永远只持有"当前这一张图"，
切图后要恢复什么、什么时候落盘，都由 `MainWindow` 决定（见 [标注数据的生命周期](#标注数据的生命周期)）。

## 主流程

```
命令行参数 / 拖放 / 菜单 / 文件夹对话框
        │
        ▼
MainWindow.open_paths(paths)     先保存当前图的标注，建立图像列表并把索引置为 -1
        │
        ▼
MainWindow._show(index)          按索引读取；失败时仅提示，不改动当前索引
        │
        ├─ RoiStore.set(上一张, ImageView.rois())     送走上一张的标注
        ▼
ImageView.set_image(image)       清空场景 → 放入 pixmap → 适应窗口
        │
        ▼
ImageView.set_rois(RoiStore.get(path))            装回这张图的标注
```

`MainWindow._index` 是唯一的"当前位置"，`-1` 表示没有打开任何图像；状态栏文本与各动作的
启用状态都由它推导，不存在第二份真相。读取失败时不推进索引，因此坏图不会打断连续浏览。

**当前这一张图的标注以场景为准**：`ImageView.rois()` 直接从场景里的图元读几何，`MainWindow`
只在切图、`Ctrl+S`、关窗这几个时刻把它写回 `RoiStore`。所以移动、缩放不需要任何信号或脏标记，
也不会出现"内存里的数据和画面上看到的不一致"。

## 标注数据的生命周期

```
切图 / 关窗 / Ctrl+S        场景 ──ImageView.rois()──▶ RoiStore.set(图像路径, 标注)
                                                              │ 内容有变化才写盘
                                                              ▼
                                                 <图像文件名>.roi.json（原子替换）
```

- **落盘时机**：切上一张 / 下一张、重新打开一组图像、关窗，以及 `Ctrl+S`。
  空标注不落盘，已存在的边车文件会被删除，所以从没标过的图不会多出文件。
- **`RoiStore` 是唯一读写标注文件的地方**：内存缓存 + 惰性读盘，内容没变就不碰磁盘，
  因此反复翻页不会反复写文件，也不会把 mtime 刷得满天飞。
- **存储位置**：图像旁边、与图像同名加后缀（`page_02.png` → `page_02.png.roi.json`）。
  边车文件不是图像扩展名，`collect_images()` 不会把它扫进浏览列表。
- **文件格式**：`version` 用于以后升级，读取时忽略未知字段；坏记录逐条跳过，
  不让一条烂数据作废整份文件。坐标写盘前四舍五入到 2 位小数（误差远小于 1 像素）。

```json
{
  "version": 1,
  "image": "page_02.png",
  "annotations": [{ "x": 150.0, "y": 630.0, "w": 940.0, "h": 70.0 }]
}
```

## 分层与模块职责

```
.
├── pyproject.toml              # 项目、依赖、`ocr-proofread` 命令入口
├── uv.lock                     # 锁定的依赖版本
├── .python-version             # 固定 Python 版本
├── docs/
│   ├── architecture.md         # 本文件
│   └── preview.png             # 界面预览图
├── src/ocr_proofread/
│   ├── __init__.py             # main() 入口
│   ├── __main__.py             # python -m ocr_proofread
│   ├── app.py                  # 主窗口：菜单、快捷键、打开/切换图像、标注存取、状态持久化
│   ├── image_view.py           # QGraphicsView 子类：显示、缩放、平移、左键框选
│   ├── roi_item.py             # 场景里的矩形图元：选中、移动、八向手柄缩放
│   ├── annotations.py          # Roi 数据结构 + RoiStore（内存缓存＋JSON 边车文件）
│   └── images.py               # 图像扫描、自然排序、读取（无界面依赖）
└── tests/smoke_test.py         # 无界面冒烟测试
```

- **`app.py` / `MainWindow`**：唯一持有"图像列表 + 当前索引 + 标注存储"的地方。菜单、快捷键、
  拖放、命令行参数最终都汇入 `open_paths()`。窗口大小与上次目录通过 `QSettings` 持久化。
- **`image_view.py` / `ImageView`**：只负责当前这张图的变换（缩放、平移）、场景管理，
  以及"左键在图像空白处拉矩形"的交互；对外暴露 `set_image()`、`set_rois()` / `rois()`、
  `zoomChanged`、`roisChanged` 信号。
- **`roi_item.py` / `RoiItem`**：单个矩形的外观与交互（`ItemIsMovable` + 八个手柄），
  用 `mapRectToScene()` 与图像坐标互转。手柄是它的子图元，对 `ImageView` 透明。
- **`annotations.py`**：纯数据层，不含任何控件。`Roi` 是四个浮点数，`RoiStore` 管内存缓存与
  边车文件读写。可在没有界面的场景下单独使用或测试。
- **`images.py`**：纯函数式的扫描、自然排序与解码，返回 `Path` 列表和 `QImage`，
  不引用任何 Qt 控件类型。可在没有界面的场景下单独使用或测试。
- **`__init__.py` / `__main__.py`**：命令入口，只做转发。

依赖方向单向：`app.py` → `image_view.py` → `roi_item.py` → `annotations.py`，
另有 `app.py` → `images.py`。反向引用一律禁止，`annotations.py` 与 `images.py` 不依赖任何视图模块。

## 环境与依赖

- **Python 3.14**，由 `.python-version` 固定；依赖由 `uv` 与 `uv.lock` 管理。
- **只装 `pyside6-essentials`，不装 `pyside6` 元包**：元包会一并拉入 Addons、WebEngine 等
  数百 MB 而本工具用不到的模块。需要 `QtMultimedia`、`QtCharts` 时再 `uv add pyside6-addons`。
- **uv 缓存放在项目内的 `.uv-cache/`**：当前沙箱下 `$HOME` 中的 uv 缓存目录只读。
  具体命令见 README 的"环境准备"。

## 扩展点与约定

ROI 图元直接挂到 `ImageView` 的场景上，以下约定必须遵守：

- `ImageView.scene()` 是图元的挂载点；坐标原点即图像左上角，1 场景单位 = 1 图像像素。
- `ImageView.set_image()` 会**清空整个场景**。它只管底图、不碰标注数据；切图后必须由上层
  紧接着调用 `set_rois()`，否则这张图的标注看起来就"丢了"。
- `RoiItem` 只用平移（`pos` + 矩形），不旋转不缩放，所以 `to_roi()` 里一次 `mapRectToScene()`
  就能得到图像坐标。以后若要支持旋转，出口仍只有 `to_roi()` 一处，但读回时要注意 `pos` 与
  `rect` 的拆分。
- 左键在图像空白处是"拉新矩形"，落在已有图元上则交回场景做选中 / 移动 / 缩放，判断在
  `ImageView._begin_roi()`。不可见的图元（未选中图元的手柄）不会参与命中，这是 Qt 的行为，
  依赖它比依赖 `isVisible()` 自己过滤更可靠。
- 手柄用 `ItemIgnoresTransformations` 绘制，屏幕尺寸恒为 9px；改动这块要复查极端缩放
  （2% 和 4000%）下的手感。
- 移动与缩放都被夹在图像矩形内（`RoiItem._bounds`），拖不出画面，也拖不成一条线。
- **线程边界**：`images.load_qimage()` 只产生 `QImage`，可以在 GUI 线程之外调用；`QPixmap`
  的转换集中在 `ImageView.set_image()`，始终在 GUI 线程。新增解码/预加载逻辑时请保持这条界线。

## 测试

`tests/smoke_test.py` 是脚本式冒烟测试，不引入 pytest：

```bash
uv run python tests/smoke_test.py
```

用 `QtTest` 发送真实的按键与滚轮事件，覆盖文件夹扫描与自然排序、打开/切换图像、缩放上下限、
快捷键绑定，以及坏图不让程序崩溃。无显示器时自动使用 offscreen 平台插件；失败时打印 `[FAIL]`
行并以非零码退出。

ROI 相关的部分自己造 `QMouseEvent` 并带上按键状态——`QTest.mouseMove()` 发的事件不带按键，
场景不会认为在拖图元，测不出移动与缩放。覆盖：左键拖出矩形、误点击不留碎图元、
切图保存与恢复、拖动本体移动、拖手柄缩放、拖不出图像范围、`Delete` 删除，
以及 `RoiStore` 的写盘、去重、损坏文件容错与空标注删文件。
