# ocr-proofread

OCR 校对 / 图像 ROI 标注工具（PySide6 + QGraphicsView）。

**用途与边界**：逐张查看扫描件或拍摄的文档图像，为 OCR 结果校对提供底图浏览界面。
当前版本只覆盖"浏览"这一环——打开图像、缩放平移、上下张切换；ROI 绘制与标注的存取尚未实现。

![界面预览](docs/preview.png)

## 文档

- [docs/architecture.md](docs/architecture.md) —— 模块职责、主流程、依赖取舍、扩展约定。
  改动源码前先读这份。

## 环境准备

本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境，解释器为 Python 3.14。

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"   # 见下方沙箱说明
uv sync                                 # 创建 .venv 并安装依赖
```

> **沙箱说明**：当前环境下 `$HOME` 中的 uv 缓存目录只读，因此本项目的 uv 缓存被放在项目内的
> `.uv-cache/`（已加入 `.gitignore`）。执行任何 `uv` 命令前都需要先设置上面的 `UV_CACHE_DIR`。

只安装 `pyside6-essentials`，不含 Addons、WebEngine 等模块；原因与取舍见
[docs/architecture.md](docs/architecture.md#环境与依赖)。

## 运行

```bash
uv run ocr-proofread                       # 空窗口启动
uv run ocr-proofread ~/data/pages          # 直接打开文件夹
uv run ocr-proofread a.png b.jpg           # 直接打开若干图像
uv run python -m ocr_proofread <路径...>    # 等价写法
```

也可以用鼠标把文件夹或图像文件拖进窗口。命令行传入的文件夹按自然顺序展开，文件按给定顺序打开。

## 功能

- **打开文件夹**：扫描文件夹内的图像（png/jpg/bmp/gif/webp/tif/…），按**自然顺序**排序
  （`img2.png` 排在 `img10.png` 前面），忽略隐藏文件与非图像文件；仅扫描顶层，不递归。
- **导入图像**：多选文件，按选择顺序浏览。
- **浏览**：上一张 / 下一张、第一张 / 最后一张，到边界时对应动作自动禁用。
- **缩放**：以鼠标为锚点的滚轮缩放，快捷键放大/缩小，适应窗口、实际大小（100%）；
  缩放范围限制在 2%–4000%。窗口尺寸变化时，若仍处于"适应窗口"状态会自动重新适配。
  适应窗口沿用 Qt `fitInView` 语义：小图会被放大填满窗口，`Ctrl+1` 可回到 100%。
- **平移**：中键拖动画布；Shift+滚轮水平滚动、Alt+滚轮垂直滚动。
- **状态栏**：`[当前/总数] 文件名 宽×高 所在目录` + 当前缩放百分比。
- **持久化**：记住窗口大小与上次打开的目录。
- **容错**：单张图读取失败时只在状态栏提示并停留在原图，不中断浏览；EXIF 方向会自动校正。

## 快捷键

| 按键 | 功能 |
| --- | --- |
| `Ctrl+Shift+O` | 打开文件夹 |
| `Ctrl+O` | 导入图像文件 |
| `←` / `A` / `PageUp` / `Backspace` | 上一张 |
| `→` / `D` / `PageDown` / `Space` | 下一张 |
| `Home` / `End` | 第一张 / 最后一张 |
| 滚轮 或 `Ctrl+滚轮` | 以鼠标为中心缩放 |
| `Ctrl+=` / `Ctrl+-` | 放大 / 缩小 |
| `Ctrl+0` 或 `F` | 适应窗口 |
| `Ctrl+1` | 实际大小（100%） |
| `Ctrl+Q` | 退出 |
| 中键拖动 | 平移画布 |
| `Shift+滚轮` / `Alt+滚轮` | 水平 / 垂直滚动 |

程序内「帮助 → 快捷键」会弹出同一份列表。

## 开发

```bash
uv run ocr-proofread                  # 运行程序
uv run python tests/smoke_test.py     # 冒烟测试，无显示器时自动用 offscreen
```

测试的组织方式与覆盖范围见 [docs/architecture.md](docs/architecture.md#测试)。

### 提交约定

提交信息统一用**中文**书写，格式遵循 Conventional Commits：

```
<type>(<scope>): <主题>

正文说明「为什么改」，一行一条，每行约 72 字符。
```

常用的 type：`feat` / `fix` / `docs` / `refactor` / `test` / `chore`。主题用祈使句、不加句号；
一个提交只做一件事。仓库早期历史是英文的，从本约定起一律用中文。
