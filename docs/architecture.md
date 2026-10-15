# 架构说明

写给要改这个项目的人：模块怎么分层、一次"打开图像"经过哪些环节、哪些约定不能破坏。
安装、运行与操作方式见 [README](../README.md)。

## 设计边界

当前版本做"浏览 + 框选 + 每图标注存取 + OCR 识别 + 文本编辑"：打开图像、缩放平移、上下张
切换、左键拖出矩形并调整、切图时自动保存与恢复标注，对选中的或全部的 ROI 跑识别、
把文本挂在对应的 ROI 上一起落盘，选中标注时在它旁边显示这段文本（横排 / 竖排可切），
并且可以双击就地改掉它。批量替换、逐字校对与导出等更完整的校对工作流不在范围内。

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
切图 / 关窗 / Ctrl+S / 识别完成 / 文本编辑被采纳    场景 ──ImageView.rois()──▶ RoiStore.set(图像路径, 标注)
                                                              │ 内容有变化才写盘
                                                              ▼
                                                 <图像文件名>.roi.json（原子替换）
```

- **落盘时机**：切上一张 / 下一张、重新打开一组图像、关窗、`Ctrl+S`、一批识别结束
  （`_finalize_ocr()` 会立刻 `_flush_rois()`），以及一次文本编辑被采纳（`roiTextChanged`
  → `_flush_rois()`），所以识别结果和手改的字都不用等切图就已经在文件里了。
  空标注不落盘，已存在的边车文件会被删除，所以从没标过的图不会多出文件。
- **`RoiStore` 是唯一读写标注文件的地方**：内存缓存 + 惰性读盘，内容没变就不碰磁盘，
  因此反复翻页不会反复写文件，也不会把 mtime 刷得满天飞。
- **存储位置**：图像旁边、与图像同名加后缀（`page_02.png` → `page_02.png.roi.json`）。
  边车文件不是图像扩展名，`collect_images()` 不会把它扫进浏览列表。
- **文件格式**：`version` 用于以后升级，读取时忽略未知字段；坏记录逐条跳过，
  不让一条烂数据作废整份文件。坐标写盘前四舍五入到 2 位小数（误差远小于 1 像素）。
  v2 起每条记录可以带可选的 `text`（该 ROI 的识别结果），没识别过就不写这个字段，
  v1 老文件照常读入（`text` 视为空串）。

```json
{
  "version": 2,
  "image": "page_02.png",
  "annotations": [
    { "x": 150.0, "y": 630.0, "w": 940.0, "h": 70.0, "text": "認識されたテキスト" },
    { "x": 150.0, "y": 720.0, "w": 940.0, "h": 70.0 }
  ]
}
```

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
│   ├── app.py                  # 主窗口：菜单、快捷键、打开/切换图像、标注存取、OCR 调度
│   ├── image_view.py           # QGraphicsView 子类：显示、缩放、平移（中键 / 空格抓手）、左键框选
│   ├── roi_item.py             # 场景里的矩形图元：选中、移动、八向手柄缩放、带识别文本
│   ├── text_box.py             # 标注旁的文本小框：排版方向（横排 / 竖排）、尺寸与贴边
│   ├── annotations.py          # Roi 数据结构 + RoiStore（内存缓存＋JSON 边车文件）
│   ├── images.py               # 图像扫描、自然排序、读取（无界面依赖）
│   └── ocr.py                  # OCR 引擎注册表、ROI 裁剪、后台识别任务（无控件依赖）
└── tests/smoke_test.py         # 无界面冒烟测试
```

- **`app.py` / `MainWindow`**：唯一持有"图像列表 + 当前索引 + 标注存储 + OCR 任务"的地方。
  菜单、快捷键、拖放、命令行参数最终都汇入 `open_paths()`。窗口大小、上次目录、上次用的
  识别模型、文本框排版方向与字号通过 `QSettings` 持久化；结果面板里双击改文本也在这里
  收口（`_on_panel_item_changed`）。
- **`image_view.py` / `ImageView`**：只负责当前这张图的变换（缩放、平移）、场景管理，
  "左键在图像空白处拉矩形"的交互，双击图元后的文本就地编辑（编辑器是视口的子控件，
  见 [扩展点与约定](#扩展点与约定)），以及文本框的排版方向、字号与每次重算；对外暴露
  `set_image()`、`image()`、`set_rois()` / `rois()`、`select_roi_item()`、
  `set_hand_mode()`、`set_text_direction()`、`set_text_font_size()`、
  `begin_text_edit()` / `commit_text_edit()` / `cancel_text_edit()`，以及
  `zoomChanged`、`roisChanged`、`roiTextChanged`、`textFontChanged`、
  `textFontLimitReached`、`selectionChanged` 信号。
- **`roi_item.py` / `RoiItem`**：单个矩形的外观与交互（`ItemIsMovable` + 八个手柄），
  用 `mapRectToScene()` 与图像坐标互转；另外挂着这个框的 OCR 文本（`text()` / `set_text()`），
  文本由上层编辑好后写回。手柄与文本框都是它的子图元，对 `ImageView` 透明；
  `setRect()` 被重写，改几何时顺带把两类子图元同步过去。
- **`text_box.py`**：标注旁那个小框。`TextDirection` 是排版方向（横排 / 竖排）这个设定
  本身，`RoiTextBox` 负责量尺寸、贴边与绘制，只用 QtCore / QtGui 的字体度量与画笔，
  不含控件；它也持有字号，但"到底能调多大"要问视图喂进来的现场（见下）。
- **`annotations.py`**：纯数据层，不含任何控件。`Roi` 是四个浮点数加一段可选的识别文本，
  `RoiStore` 管内存缓存与边车文件读写。可在没有界面的场景下单独使用或测试。
- **`images.py`**：纯函数式的扫描、自然排序与解码，返回 `Path` 列表和 `QImage`，
  不引用任何 Qt 控件类型。可在没有界面的场景下单独使用或测试。
- **`ocr.py`**：模型注册表（`OCR_MODELS` / `create_engine()`）、`crop_roi()` 裁剪、
  `QImage` → PIL 转换，以及跑在子线程里的 `OcrTask`。只用 QtCore / QtGui，不含控件，
  界面通过 `MainWindow(ocr_factory=...)` 注入引擎，测试里可以换成假引擎。
- **`__init__.py` / `__main__.py`**：命令入口，只做转发。

依赖方向单向：`app.py` → `image_view.py` → `roi_item.py` → `text_box.py` →
`annotations.py`，另有 `app.py` → `images.py`、`app.py` → `ocr.py`、
`app.py` → `text_box.py`（只为了那个方向设定）。反向引用一律禁止，`annotations.py`、
`images.py`、`ocr.py`、`text_box.py` 不依赖任何视图或窗口模块。

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
  就能得到图像坐标，识别文本顺带带出去。以后若要支持旋转，出口仍只有 `to_roi()` 一处，
  但读回时要注意 `pos` 与 `rect` 的拆分。
- 左键在图像空白处是"拉新矩形"，落在已有图元上则交回场景做选中 / 移动 / 缩放，判断在
  `ImageView._begin_roi()`。不可见的图元（未选中图元的手柄）不会参与命中，这是 Qt 的行为，
  依赖它比依赖 `isVisible()` 自己过滤更可靠。双击另走 `ImageView.mouseDoubleClickEvent()`
  （命中手柄或文本框时上溯到父图元），不经过场景。
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
  上层在 `_flush_rois()`、`_save_rois()`、`_start_ocr()` 开头都会 `commit_text_edit()`，
  所以切图、`Ctrl+S`、识别之前正在敲的字不会丢；`_finalize_ocr()` 则先 `cancel_text_edit()`，
  让识别结果说了算，避免编辑器里的旧文本在收尾时盖回去。
- **`RoiItem.setRect()` 是子图元的同步点**：`QGraphicsRectItem.setRect()` 不会通知子图元，
  画新框（`ImageView._resize_drawing()` 直接调 `setRect()`）时手柄和文本框会留在旧位置，
  所以 `RoiItem` 重写了它，一次把手柄与文本框都摆好。以后再加"挂在矩形上的东西"，
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
  （2% 和 4000%）下的手感。
- 移动与缩放都被夹在图像矩形内（`RoiItem._bounds`），拖不出画面，也拖不成一条线。
- **加新 OCR 模型**：在 `ocr.py` 的 `OCR_MODELS` 里加一条 `OcrModel(name, label, factory)`，
  实现 `recognize(QImage) -> str`（失败抛 `OcrError`）即可，菜单与模型选择会自动出现这一项。
  引擎必须能"第一次调用时才加载"，且不要自己开线程——`OcrTask` 已经在子线程里调它。
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
以及 `RoiStore` 的写盘、去重、损坏文件容错、空标注删文件与 `text` 字段的读写。
空格抓手另有覆盖：按住空格拖动只平移画布（滚动条动了）而不留新标注，从已有图元上
按下也不改动它，松开空格后左键恢复成拉矩形。

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
