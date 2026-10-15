# 架构说明

写给要改这个项目的人：模块怎么分层、一次"打开图像"经过哪些环节、哪些约定不能破坏。
安装、运行与操作方式见 [README](../README.md)。

## 设计边界

当前版本做"浏览 + 框选 + 每图标注存取 + ROI 排序 + OCR 识别 + 文本编辑 + 导出文本"：
打开图像、缩放平移、上下张切换、左键拖出矩形并调整、切图时自动保存与恢复标注，对选中的
或全部的 ROI 跑识别、把文本挂在对应的 ROI 上一起落盘，选中标注时在它旁边显示这段文本
（横排 / 竖排可切），并且可以双击就地改掉它。每个 ROI 还有一个"本张图上的顺序号"，默认是
创建顺序，也可以在排序模式里按点击顺序重排；最后可以把已打开图像的全部 ROI 文本导成一个
纯文本文件。批量替换、逐字校对等更完整的校对工作流不在范围内。

由此定下两条贯穿全项目的原则：

- **视图层不保存任何跨图状态**。`ImageView` 永远只持有"当前这一张图"，切图后要恢复什么、
  什么时候落盘，都由 `MainWindow` 决定（见 [标注数据的生命周期](#标注数据的生命周期)）。
- **重活不进界面线程**。模型加载与推理都在子线程里跑，界面线程只做裁剪、收结果和落盘
  （见 [OCR 的线程与生命周期](#ocr-的线程与生命周期)）。

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

**当前这一张图的标注以场景为准**：`ImageView.rois()` 直接从场景里的图元读几何与识别文本，
`MainWindow` 只在切图、`Ctrl+S`、识别完成、关窗这几个时刻把它写回 `RoiStore`。所以移动、
缩放不需要任何信号或脏标记，也不会出现"内存里的数据和画面上看到的不一致"。

## OCR 的线程与生命周期

```
Ctrl+R / Ctrl+Shift+R
        │  界面线程：挑图元 → 按 ROI 裁出 QImage 图块（crop_roi）
        ▼
MainWindow._start_ocr()         造一个 OcrTask，moveToThread(QThread)，start()
        │
        ▼  子线程：逐个识别
OcrTask.run()                   结果攒在 task.results / task.errors，发 progressed 信号
        │
        ▼  界面线程：信号排队回来
MainWindow._finalize_ocr()      写回 RoiItem → _flush_rois() 落盘 → 刷新面板与状态栏
```

- **引擎按名缓存**：`MainWindow._ocr_engines` 里一个模型一个引擎，模型只在第一次识别时加载
  （manga-ocr 约 400 MB，首次还要从 HuggingFace 下载），不会每识别一次重新加载。
- **一个任务一个线程**：`OcrTask` 是 `QObject`，被 `moveToThread` 到一次性 `QThread` 上；
  结果先写进任务对象的字典、再发信号，所以"信号还没送达"和"结果已经算好"不会打架。
- **取消是同步的**：切图 / 关窗 / 重新打开都会先 `_cancel_ocr()`：给任务打取消标记，等
  当前这一张图块的推理跑完（不可中断，通常不到一秒），把已有结果写回并落盘，然后才换图。
  宁可多等这一下，也不让识别结果悄悄丢掉。
- **迟到的结果也有归宿**：正常流程下取消先于切图，结果一定落在当前图上；万一顺序反了，
  `_merge_ocr_text()` 会按矩形位置把文本并回磁盘（几何对不上就丢弃，不乱写）。
- **线程边界**：裁剪出的 `QImage` 图块是值语义副本，可以安全地交给子线程；`QPixmap` 仍然
  只在界面线程里转换（见 `ImageView.set_image()`）。

## 标注数据的生命周期

```
切图 / 关窗 / Ctrl+S / 识别完成 / 文本编辑被采纳 / 排序模式里每点中一个框
        │
        ▼
ImageView.rois() ──▶ RoiStore.set(图像路径, 标注) ──▶ <图像文件名>.roi.json（原子替换）
                                  （内容有变化才写盘）
```

- **落盘时机**：切上一张 / 下一张、重新打开一组图像、关窗、`Ctrl+S`、一批识别结束
  （`_finalize_ocr()` 会立刻 `_flush_rois()`）、一次文本编辑被采纳（`roiTextChanged`
  → `_flush_rois()`），以及排序模式里每点中一个框（`roiOrderChanged` → `_flush_rois()`，
  排序是"点一下算一次"的操作，丢了不好重来）。所以识别结果、手改的字和新排的顺序都不用
  等切图就已经在文件里了。空标注不落盘，已存在的边车文件会被删除，所以从没标过的图不会
  多出文件。
- **`RoiStore` 是唯一读写标注文件的地方**：内存缓存 + 惰性读盘，内容没变就不碰磁盘，
  因此反复翻页不会反复写文件，也不会把 mtime 刷得满天飞。
- **存储位置**：图像旁边、与图像同名加后缀（`page_02.png` → `page_02.png.roi.json`）。
  边车文件不是图像扩展名，`collect_images()` 不会把它扫进浏览列表。
- **文件格式**：`version` 用于以后升级，读取时忽略未知字段；坏记录逐条跳过，
  不让一条烂数据作废整份文件。坐标写盘前四舍五入到 2 位小数（误差远小于 1 像素）。
  v2 起每条记录可以带可选的 `text`（该 ROI 的识别结果），v3 起再带可选的 `index`
  （顺序号，见 [ROI 的顺序与排序模式](#roi-的顺序与排序模式)）；两个字段都是"没有就不写"，
  v1 / v2 老文件照常读入（`text` 视为空串、`index` 视为没排过号）。

```json
{
  "version": 3,
  "image": "page_02.png",
  "annotations": [
    { "x": 150.0, "y": 720.0, "w": 940.0, "h": 70.0, "index": 1 },
    { "x": 150.0, "y": 630.0, "w": 940.0, "h": 70.0, "text": "認識されたテキスト", "index": 2 }
  ]
}
```

## ROI 的顺序与排序模式

每个 ROI 在**本张图上**有一个从 1 开始的顺序号（`Roi.index` / `RoiItem.order()`），
它决定结果面板的行序、`rois()` 的返回顺序（也就是存盘顺序）、`Ctrl+Shift+R` 识别全部时
的处理顺序：

```
框选新建 / 读盘            set_rois 按 index 排序后重编号 1..N（没有 index 的老文件按文件先后）
        │
        ▼
RoiItem.order()  ◀── 排序模式：点一个就把已点的排到前面，其余保持原相对次序接在后面
        │
        ▼
to_roi() ──▶ Roi.index ──▶ <图像文件名>.roi.json 的 "index" 字段
```

- **初始顺序 = 创建顺序**，不是坐标顺序。新建的框取"现有最大序号 + 1"排在最后；
  读盘时按 `index` 排（`_load_order_key()`；老文件没有 `index`，稳定排序保持文件里的
  先后），然后统一重编号，所以序号永远是连续的 1..N。删掉几个之后同样重编号，
  不留空档（`_renumber()`）。
- **顺序号存在图元上**（`RoiItem.set_order()`），和 `text` 一样由 `to_roi()` 带出去，
  所以"内存里的顺序"和"文件里的顺序"不会各说各话；`RoiStore.set()` 比较的是含 `index`
  的完整内容，重排顺序同样会触发写盘。
- **排序模式只作用于单张图**：`ImageView.set_sort_mode()` 是这张图上的临时状态，
  `set_image()` 会先退出它。进入时所有框都算"没点到"（角标 `?`），每点一个就把它排到
  已点队列末尾并整体重编号——已点的拿 1..k，没点的按原顺序接在后面，因此点一半退出也
  不会丢框。已经点过的框再点一次不做改动，只由上层在状态栏说明。
- **模式下的鼠标语义由视图收口**：`ImageView.mousePressEvent()` 在左键分支之前拦下排序
  模式，只做 `_pick_for_sort()`，不调用 `scene` 的默认处理——所以既不新建矩形，也不选中
  或拖动图元；中键 / 空格平移与滚轮缩放的判断都在它之前，照常可用。`Esc` 在
  `keyPressEvent()` 里退出模式。
- **反馈靠角标**：`OrderBadge` 是 `RoiItem` 的子图元（和文本框一样带
  `ItemIgnoresTransformations`，位置锚在矩形左上角、自身按屏幕像素绘制），只在排序模式下
  显示，所以"哪个已经排到第几号"不用看右侧面板。它不接受鼠标按键，但 `_roi_at()` 仍然把
  它上溯到父图元，点角标等于点这个框。

## 导出文本

导出把**已打开**的这批图像的全部 ROI 文本拼成一个 `.txt`。分工是"选哪些内容"归
`MainWindow`、"怎么拼、怎么写"归 `export.py`：

```
Ctrl+E / Ctrl+Shift+E
        │  界面线程：确定导出路径
        ▼
MainWindow._export_to()     _flush_rois()（先把当前图的改动写回）
        │
        ├─ _export_pages()  按 self._paths 的顺序取 RoiStore 里的标注，没有 ROI 的图跳过
        ▼
export.build_text() 拼字符串 ──▶ export.write_export() 原子替换写盘
```

- **导出位置**：`Ctrl+E` 用 `default_export_path(第一个图像的文件夹)`，也就是
  `<文件夹>/<文件夹名>.txt`——打开文件夹时就是这个文件夹，导入若干图像时是第一个图像
  所在的那个。文件夹不存在（或压根没打开图像）时 `_export_folder()` 返回 `None`，
  由 `_warn_export_unavailable()` 弹错误；`Ctrl+Shift+E` 走 `QFileDialog.getSaveFileName`
  让用户自己选，用户取消（返回空串）就直接 `return`，不提示也不落盘。
- **取哪些图**：只遍历 `self._paths`（工具里已打开的这批），不是重新扫描文件夹；
  图像顺序就是浏览顺序。`_export_pages()` 用 `RoiStore.get()` 逐图取标注——没有边车文件
  的图读出来就是空列表，和"有文件但没记录"一样被整张跳过，所以"连配置文件都没有"不需要
  单独判断。
- **顺序与内容**：`roi_items()` 已经按排序号排好，`RoiStore` 里的顺序就是它，因此 ROI 顺序
  天然跟着排序模式的结果走；`build_text()` 只取 `roi.text`，空串照样输出成空行，不写
  占位符、序号或坐标。
- **写盘**：`write_export()` 与 `save_rois()` 一样"先写 `.tmp` 再 `os.replace`"，
  半途失败不会留下半份导出结果，也不会把原文件写坏；IO 失败抛 `OSError`，由
  `MainWindow` 转成错误弹窗。
- **不含界面**：`export.py` 只用 `pathlib` 与 `os`，不 import 任何 Qt 模块，
  可以在没有界面的场景下单独测试（`build_text()` 就是纯字符串函数）。

## 分层与模块职责

```
.
├── pyproject.toml              # 项目、依赖、`ocr-proofread` 命令入口
├── uv.lock                     # 锁定的依赖版本
├── .python-version             # 固定 Python 版本
├── docs/
│   └── architecture.md         # 本文件
├── src/ocr_proofread/
│   ├── __init__.py             # main() 入口
│   ├── __main__.py             # python -m ocr_proofread
│   ├── app.py                  # 主窗口：菜单、快捷键、打开/切换图像、标注存取、OCR 调度、导出
│   ├── image_view.py           # QGraphicsView 子类：显示、缩放、平移（中键 / 空格抓手）、左键框选
│   ├── roi_item.py             # 场景里的矩形图元：选中、移动、八向手柄缩放、带识别文本
│   ├── text_box.py             # 标注旁的文本小框：排版方向（横排 / 竖排）、尺寸与贴边
│   ├── annotations.py          # Roi 数据结构 + RoiStore（内存缓存＋JSON 边车文件）
│   ├── export.py               # 导出文本：拼格式、默认路径、原子写盘（无 Qt 依赖）
│   ├── images.py               # 图像扫描、自然排序、读取（无界面依赖）
│   └── ocr.py                  # OCR 引擎注册表、ROI 裁剪、后台识别任务（无控件依赖）
└── tests/smoke_test.py         # 无界面冒烟测试
```

- **`app.py` / `MainWindow`**：唯一持有"图像列表 + 当前索引 + 标注存储 + OCR 任务"的地方。
  菜单、快捷键、拖放、命令行参数最终都汇入 `open_paths()`。窗口大小、上次目录、上次用的
  识别模型、文本框排版方向与字号通过 `QSettings` 持久化；结果面板里双击改文本也在这里
  收口（`_on_panel_item_changed`），ROI 排序模式的菜单勾选、状态栏提示与"每点一次就落盘"
  同样在这里接线（`_toggle_sort_rois()` / `_on_roi_order_changed()`）。导出的"用哪些图、
  放哪里"也在这里决定（`_export_folder()` / `_export_pages()` / `_export_to()`），
  拼字符串与写盘则交给 `export.py`。
- **`image_view.py` / `ImageView`**：只负责当前这张图的变换（缩放、平移）、场景管理，
  "左键在图像空白处拉矩形"的交互，双击图元后的文本就地编辑（编辑器是视口的子控件，
  见 [扩展点与约定](#扩展点与约定)），文本框的排版方向、字号与每次重算，以及这张图上的
  ROI 顺序（读盘重编号、新建排最后、删除后补号）与 ROI 排序模式；对外暴露
  `set_image()`、`image()`、`set_rois()` / `rois()`、`roi_items()`、`select_roi_item()`、
  `set_hand_mode()`、`set_sort_mode()`、`set_text_direction()`、`set_text_font_size()`、
  `begin_text_edit()` / `commit_text_edit()` / `cancel_text_edit()`，以及
  `zoomChanged`、`roisChanged`、`roiOrderChanged`、`sortModeChanged`、`sortPicked`、
  `roiTextChanged`、`textFontChanged`、`textFontLimitReached`、`selectionChanged` 信号。
- **`roi_item.py` / `RoiItem`**：单个矩形的外观与交互（`ItemIsMovable` + 八个手柄），
  用 `mapRectToScene()` 与图像坐标互转；另外挂着这个框的 OCR 文本（`text()` / `set_text()`）
  与顺序号（`order()` / `set_order()`），都由上层维护好后经 `to_roi()` 一起带走。
  手柄、文本框与排序角标（`OrderBadge`）都是它的子图元，对 `ImageView` 透明；
  `setRect()` 被重写，改几何时顺带把这几类子图元同步过去。
- **`text_box.py`**：标注旁那个小框。`TextDirection` 是排版方向（横排 / 竖排）这个设定
  本身，`RoiTextBox` 负责量尺寸、贴边与绘制，只用 QtCore / QtGui 的字体度量与画笔，
  不含控件；它也持有字号，但"到底能调多大"要问视图喂进来的现场（见下）。
- **`annotations.py`**：纯数据层，不含任何控件。`Roi` 是四个浮点数加一段可选的识别文本
  与一个顺序号，`RoiStore` 管内存缓存与边车文件读写。可在没有界面的场景下单独使用或测试。
- **`images.py`**：纯函数式的扫描、自然排序与解码，返回 `Path` 列表和 `QImage`，
  不引用任何 Qt 控件类型。可在没有界面的场景下单独使用或测试。
- **`ocr.py`**：模型注册表（`OCR_MODELS` / `create_engine()`）、`crop_roi()` 裁剪、
  `QImage` → PIL 转换，以及跑在子线程里的 `OcrTask`。只用 QtCore / QtGui，不含控件，
  界面通过 `MainWindow(ocr_factory=...)` 注入引擎，测试里可以换成假引擎。
- **`export.py`**：纯数据层，不含任何 Qt 依赖。`build_text()` 把"文件名 + 每个 ROI 文本"
  拼成导出文本，`default_export_path()` 给出默认导出位置，`write_export()` 原子写盘。
  可以在没有界面的场景下单独使用或测试。
- **`__init__.py` / `__main__.py`**：命令入口，只做转发。

依赖方向单向：`app.py` → `image_view.py` → `roi_item.py` → `text_box.py` →
`annotations.py`，另有 `app.py` → `images.py`、`app.py` → `ocr.py`、
`app.py` → `export.py`、`app.py` → `text_box.py`（只为了那个方向设定）。反向引用一律禁止，
`annotations.py`、`export.py`、`images.py`、`ocr.py`、`text_box.py` 不依赖任何视图或
窗口模块。

## 环境与依赖

- **Python 3.14**，由 `.python-version` 固定；依赖由 `uv` 与 `uv.lock` 管理。
- **只装 `pyside6-essentials`，不装 `pyside6` 元包**：元包会一并拉入 Addons、WebEngine 等
  数百 MB 而本工具用不到的模块。需要 `QtMultimedia`、`QtCharts` 时再 `uv add pyside6-addons`。
- **OCR 依赖 `manga-ocr`**：它拉入 PyTorch，而 PyPI 上 Linux 版 torch 自带一整套 CUDA
  运行库（nvidia-* 包），装完几个 GB——这是"照上游文档 `uv add manga-ocr`"的代价。
  只想跑 CPU 的话，在 `pyproject.toml` 里把 torch 指到官方 CPU 索引（见下），依赖体积
  能降到几百 MB；识别本来就在后台线程里跑，CPU 完全够用。
- **模型在第一次识别时才加载**：权重约 400 MB，首次从 HuggingFace 下载并缓存。
  `MangaOcrEngine` 把"没装依赖"和"加载失败"都转成带可读消息的 `OcrError`，
  所以缺依赖只会让识别失败，不会让程序起不来。
- **uv 缓存放在项目内的 `.uv-cache/`**：当前沙箱下 `$HOME` 中的 uv 缓存目录只读。
  具体命令见 README 的"环境准备"。

CPU 版 PyTorch 的接法（可选，不写就是默认的 CUDA 版）：

```toml
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }
```

## 扩展点与约定

ROI 图元直接挂到 `ImageView` 的场景上，以下约定必须遵守：

- `ImageView.scene()` 是图元的挂载点；坐标原点即图像左上角，1 场景单位 = 1 图像像素。
- `ImageView.set_image()` 会**清空整个场景**。它只管底图、不碰标注数据；切图后必须由上层
  紧接着调用 `set_rois()`，否则这张图的标注看起来就"丢了"。
- `RoiItem` 只用平移（`pos` + 矩形），不旋转不缩放，所以 `to_roi()` 里一次 `mapRectToScene()`
  就能得到图像坐标，识别文本与顺序号顺带带出去。以后若要支持旋转，出口仍只有 `to_roi()`
  一处，但读回时要注意 `pos` 与 `rect` 的拆分。
- **ROI 顺序只有一个出口**：顺序号存在 `RoiItem.order()` 上，`ImageView.roi_items()` 按它
  排序，`rois()`（存盘）、结果面板、批量识别都从这个排序结果出发。改顺序只走
  `set_order()` / `_renumber()` / `_pick_for_sort()` 三处，别再让别的代码按坐标排——
  "按坐标排"正是这一版要改掉的老行为。
- 左键在图像空白处是"拉新矩形"，落在已有图元上则交回场景做选中 / 移动 / 缩放，判断在
  `ImageView._begin_roi()`。不可见的图元（未选中图元的手柄）不会参与命中，这是 Qt 的行为，
  依赖它比依赖 `isVisible()` 自己过滤更可靠。双击另走 `ImageView.mouseDoubleClickEvent()`
  （命中手柄、文本框或排序角标时上溯到父图元），不经过场景。排序模式下这两个入口都先被
  `mousePressEvent()` 的左键分支拦掉，见 [ROI 的顺序与排序模式](#roi-的顺序与排序模式)。
- **标注旁的文本框**：`RoiTextBox` 是 `RoiItem` 的子图元（不是在场景里另挂一批），
  这样"跟着标注移动 / 缩放 / 删除"和"只有选中才显示"都由 Qt 的父子关系与
  `ItemSelectedHasChanged` 白送，视图不必维护任何映射表。它带
  `ItemIgnoresTransformations`：位置锚在标注的角上（父坐标，图像像素），自身尺寸却是
  屏幕像素，所以缩放画布时框的字号不变、也不会跑偏。内容往哪个方向铺由
  `set_after()` 决定（横排 = 下方、竖排 = 右侧；反方向的内容用负坐标），"离标注一个固定
  屏幕空隙"因此不需要知道画布缩放。
- **文本框的尺寸与贴边：视图说、图元摆、框来量**。视图是唯一知道画布缩放与视口大小
  的地方，所以每次重算（选中、缩放、改变窗口大小、滚动、拖动中与拖动结束）都由
  `ImageView._sync_text_boxes()` 把"标注在屏幕上的矩形 + 视口 + 字号"喂给
  `RoiItem.update_text_box()`；图元据此算好横排/竖排的对齐尺寸与两侧余量，交给
  `RoiTextBox` 量文字、决定尺寸，再按"能整框放下的一侧优先、否则余量大的一侧"摆好锚点。
  - **对齐规则**：横排的框宽 = 标注的屏幕宽、竖排的框高 = 标注的屏幕高，前提是标注
    自己要塞得下一个字、且这个尺寸不超出视口；否则退回按文字自然大小排（横排上限
    `MAX_LINE_WIDTH`、竖排上限 `MAX_COLUMN_HEIGHT`）。提示文字（`EMPTY_HINT`）不参与
    对齐，免得空框也被撑成一条。
  - **字号上限**：`RoiTextBox.fits_font_size()` 用候选字号重排一次，检查整框是否还在
    余量与视口内；装不下就不允许再调大（`ImageView._adjust_text_font()`）。余量是
    "排版轴两侧的较大者"，所以判据与框宽无关，滚动、缩放时重算很便宜。
- **文本就地编辑**：编辑器是 `ImageView.viewport()` 的子控件，用屏幕坐标贴在 ROI 上，
  所以画布缩放到 2% 或 4000% 输入手感都一样（`ItemIgnoresTransformations` 的那套只解决
  绘制，管不了输入控件）。采纳时先 `RoiItem.set_text()` 再发 `roiTextChanged`；这个信号与
  `roisChanged` 分开——前者是"文本内容变了、该落盘"，后者是"图元增删了、面板要重填"。
  上层在 `_flush_rois()`、`_save_rois()`、`_start_ocr()`、`ImageView.set_sort_mode()` 开头都会
  `commit_text_edit()`，所以切图、`Ctrl+S`、识别、进排序模式之前正在敲的字不会丢；
  `_finalize_ocr()` 则先 `cancel_text_edit()`，让识别结果说了算，避免编辑器里的旧文本在
  收尾时盖回去。
- **`RoiItem.setRect()` 是子图元的同步点**：`QGraphicsRectItem.setRect()` 不会通知子图元，
  画新框（`ImageView._resize_drawing()` 直接调 `setRect()`）时手柄、文本框与排序角标会留在
  旧位置，所以 `RoiItem` 重写了它，一次把这几个子图元都摆好。以后再加"挂在矩形上的东西"，
  同步逻辑加在这一处，别散到调用方。
- **排版的边界**：竖排是按字符逐格摆放的（原文里的换行就是换列），不做日语竖排里"ー、（）"这类
  需要旋转的处理，拉丁字母也不转 90°。Qt 的 `QTextLayout` 没有竖排模式，要支持上面这些
  细节得另找排版引擎或自己做字符旋转，目前按"识别结果以中日文为主"取舍；横排交给
  `QFontMetrics.boundingRect()` / `QPainter.drawText()` 的 `TextWordWrap`，
  尺寸和绘制用的是同一套规则。
- **空格抓手模式**：`ImageView` 只提供 `hand_mode` / `set_hand_mode()` 开关，按键本身由
  `MainWindow.eventFilter()` 在**应用级**过滤后转发——空格要区分按下与松开两个时刻，
  且焦点可能不在视图上（刚点过右侧结果面板），只写 `ImageView.keyPressEvent` 会漏掉
  这些情况。抓手模式下左键按下在 `_begin_roi()` 之前就被拦成平移，所以不会新建矩形、
  也不会拖动已有图元；过滤器只读不消费（返回 `False`），其他控件照常使用空格。
- 手柄用 `ItemIgnoresTransformations` 绘制，屏幕尺寸恒为 9px；改动这块要复查极端缩放
  （2% 和 4000%）下的手感。排序角标同理，屏幕尺寸恒为 20px。
- 移动与缩放都被夹在图像矩形内（`RoiItem._bounds`），拖不出画面，也拖不成一条线。
- **加新 OCR 模型**：在 `ocr.py` 的 `OCR_MODELS` 里加一条 `OcrModel(name, label, factory)`，
  实现 `recognize(QImage) -> str`（失败抛 `OcrError`）即可，菜单与模型选择会自动出现这一项。
  引擎必须能"第一次调用时才加载"，且不要自己开线程——`OcrTask` 已经在子线程里调它。
- **导出只读 `RoiStore`、不读场景**：`MainWindow._export_pages()` 遍历 `self._paths` 从存储层
  取标注，当前这张图的内容先由 `_export_to()` 开头的 `_flush_rois()` 写回，所以导出的一定是
  "已落盘 + 刚写回"的最新内容。要加别的导出格式（CSV、每图一个文件…）时沿用这个入口，
  别去问 `ImageView` 要数据；拼格式与写盘放进 `export.py`，那里不允许 import Qt。
- **线程边界**：`images.load_qimage()` 只产生 `QImage`，可以在 GUI 线程之外调用；`QPixmap`
  的转换集中在 `ImageView.set_image()`，始终在 GUI 线程。`OcrTask` 只碰 `QImage` 与纯数据，
  新增识别逻辑时请保持这条界线。

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
以及 `RoiStore` 的写盘、去重、损坏文件容错、空标注删文件与 `text` / `index` 字段的读写
（含 v1 / v2 老文件的兼容、认不出来的 `index` 容错）。
空格抓手另有覆盖：按住空格拖动只平移画布（滚动条动了）而不留新标注，从已有图元上
按下也不改动它，松开空格后左键恢复成拉矩形。

ROI 排序的覆盖：初始顺序就是创建顺序（故意让坐标顺序与创建顺序相反）、新画的框排最后、
删除后序号补成连续的 1..N、面板第一列与行序跟着排序号；排序模式下进入后角标全为 `?`、
按点击顺序排成 1、2、3、重复点已编号的框不改动、点空白不新建、拖动不动图元、双击不进
文本编辑、每点一次就落盘、`Esc` 退出后角标收起、没点到的框按原顺序接在后面、重新进入
从头排、切图自动退出且切回来顺序还在。

文本编辑的覆盖：双击图元打开就地编辑器并带出原文本、`Ctrl+Enter` 采纳后立刻落盘、
`Esc` 放弃不留痕、没识别过的标注也能编辑、编辑到一半切图或 `Ctrl+S` 会先收下正在敲的
字、双击空白处不打开编辑器，以及结果面板文本列可编辑（`editItem` 走真实的代理编辑器）、
改完写回图元并落盘、编辑中遇到面板重填不崩。

文本框的覆盖：没选中不显示、选中后框里有识别文本、框不算标注、横排贴上下且框宽 = 标注的
屏幕宽（拖手柄改标注宽度后立刻跟着对齐）、竖排贴左右且框高 = 标注的屏幕高、标注太小或
排不下时退回文字自然大小、脱离视图时有兜底贴边、方向写进 `QSettings` 且菜单勾选同步、
空文本显示提示、超长文本截断加省略号，以及点框不会在下面拉出新标注、双击框能编辑对应
的标注。字号另有覆盖：框上滚轮只改字号不缩放、字号变大后框跟着变大且仍然与标注对齐、
一直调大到"再大一档就放不下"被拒绝并提示、调不回比默认更小、设置里的字号会被夹回范围。

OCR 部分**不加载真模型**：`MainWindow(ocr_factory=...)` 注入 `FakeOcrEngine`，
覆盖裁剪尺寸与越界处理、模型注册表、`QImage` → PIL 接线（把假的 `manga_ocr` 模块塞进
`sys.modules`，因此不需要 torch）、后台任务的完成与取消（识别中途切图）、识别结果写回
图元与边车文件、面板与选中的双向同步，以及"迟到结果按几何并回磁盘"的兜底路径。

导出的覆盖：`build_text()` 的格式（文件名与每个 ROI 各占一段、空文本与多行文本原样输出）、
`default_export_path()`、写盘失败抛 `OSError` 且不留 `.tmp`；窗口侧覆盖按图像顺序与 ROI
顺序导出、跳过没有 ROI 的图、导出前收下当前图还没落盘的改动、`Ctrl+Shift+E` 的保存对话框
（用假对话框替换 `QFileDialog.getSaveFileName`，含"只填文件名时补 `.txt`"与"取消就退回、
不作提示"），以及路径不可用（没有打开图像 / 文件夹被删掉）时的错误弹窗（同样用假的
`QMessageBox.warning` 接住，不真弹窗）。
