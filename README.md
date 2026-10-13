# ocr-proofread

OCR 校对工具。

## 环境准备

本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境，解释器为 Python 3.14。

```bash
uv sync              # 创建 .venv 并安装依赖
uv run python -V     # 在虚拟环境中执行命令
```

> **注意**：当前沙箱环境下 `$HOME` 中的 uv 缓存目录是只读的，因此本项目的 uv 缓存被放在项目内的 `.uv-cache/`（已加入 `.gitignore`）。
> 执行 `uv` 命令前请先设置，或直接使用一次性的持久化配置：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
```

```bash
uv config --global set cache-dir "$PWD/.uv-cache"   # 一次性配置，之后无需再设置
```

## 目录结构

```
.
├── pyproject.toml        # 项目与依赖定义
├── uv.lock               # 锁定的依赖版本
├── .python-version       # 固定 Python 版本
└── src/ocr_proofread/    # 包源码
```

## 开发

```bash
uv run ocr-proofread    # 运行入口脚本
```
