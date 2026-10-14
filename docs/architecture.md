# 架构说明

写给要改这个项目的人：模块怎么分层、一次"打开图像"经过哪些环节、哪些约定不能破坏。
安装、运行与操作方式见 [README](../README.md)。

## 设计边界

当前版本只做图像浏览——打开、缩放平移、上下张切换。ROI 绘制与标注数据的存取不在范围内。

由此定下一条贯穿全项目的原则：**视图层不保存任何跨图状态**。`ImageView` 永远只持有"当前这一张图"，
标注要画什么、切图后要恢复什么，都由上层决定（见 [扩展点与约定](#扩展点与约定)）。

## 主流程

```
命令行参数 / 拖放 / 菜单 / 文件夹对话框
        │
        ▼
MainWindow.open_paths(paths)     记住目录，建立图像列表并把索引置为 -1
        │
        ▼
MainWindow._show(index)          按索引读取；失败时仅提示，不改动当前索引
        │
        ▼
ImageView.set_image(image)       清空场景 → 放入 pixmap → 适应窗口
```

`MainWindow._index` 是唯一的"当前位置"，`-1` 表示没有打开任何图像；状态栏文本与各动作的
启用状态都由它推导，不存在第二份真相。读取失败时不推进索引，因此坏图不会打断连续浏览。

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
│   ├── app.py                  # 主窗口：菜单、快捷键、打开/切换图像、状态持久化
│   ├── image_view.py           # QGraphicsView 子类：显示、缩放、平移
│   └── images.py               # 图像扫描、自然排序、读取（无界面依赖）
└── tests/smoke_test.py         # 无界面冒烟测试
```

- **`app.py` / `MainWindow`**：唯一持有"图像列表 + 当前索引"的地方。菜单、快捷键、拖放、
  命令行参数最终都汇入 `open_paths()`。窗口大小与上次目录通过 `QSettings` 持久化。
- **`image_view.py` / `ImageView`**：只负责当前这张图的变换（缩放、平移）与场景管理，
  对外暴露 `set_image()`、`zoomChanged` 信号和场景本身。
- **`images.py`**：纯函数式的扫描、自然排序与解码，返回 `Path` 列表和 `QImage`，
  不引用任何 Qt 控件类型。可在没有界面的场景下单独使用或测试。
- **`__init__.py` / `__main__.py`**：命令入口，只做转发。

依赖方向单向：`app.py` → `image_view.py`、`images.py`；后两者互不依赖，也不反向引用 `app.py`。

## 环境与依赖

- **Python 3.14**，由 `.python-version` 固定；依赖由 `uv` 与 `uv.lock` 管理。
- **只装 `pyside6-essentials`，不装 `pyside6` 元包**：元包会一并拉入 Addons、WebEngine 等
  数百 MB 而本工具用不到的模块。需要 `QtMultimedia`、`QtCharts` 时再 `uv add pyside6-addons`。
- **uv 缓存放在项目内的 `.uv-cache/`**：当前沙箱下 `$HOME` 中的 uv 缓存目录只读。
  具体命令见 README 的"环境准备"。

## 扩展点与约定

ROI 图元直接挂到 `ImageView` 的场景上，以下约定必须遵守：

- `ImageView.scene()` 是图元的挂载点；坐标原点即图像左上角，1 场景单位 = 1 图像像素。
- `ImageView.set_image()` 会**清空整个场景**，切图后需要由上层重新绘制该图的标注。
- 左键未绑定任何行为，可安全用于框选；中键已用于平移，与之不冲突。
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
