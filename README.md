# market-risk：美股大盘风险评分

用程序替代截图，完成美股大盘风险评分（v2-M 与 v3-R1 并行）的数据准备、机械计算和 prompt 生成。
评分规则的权威来源是 [`docs/SOP.md`](docs/SOP.md) 第3、6、7节，开发规格见 [`docs/SPEC.md`](docs/SPEC.md)。

> 本文件为初稿（阶段1），阶段5完善。

## 安装

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

## 配置

1. 复制 `.env.example` 为 `.env`，填入 `FRED_API_KEY`（在 https://fred.stlouisfed.org/docs/api/api_key.html 免费申请）。
2. `config/settings.yaml`：标的、均线周期、口径开关、贴近门槛阈值、缓存设置。
3. `config/holidays.yaml`：股市/债市休市日与提前收盘日（人工维护，仅作核对）。

## 用法

```bash
uv run market-risk dates --date 2025-11-28
```

```bash
uv run market-risk fetch --date 2025-11-28            # 下载并显示核对摘要（缓存于 data/cache/）
uv run market-risk fetch --date 2025-11-28 --refresh  # 忽略缓存重新下载
```

其余命令（`score`、`validate`、`samples` 等）在后续阶段实现。存储目录见 [`docs/STORAGE.md`](docs/STORAGE.md)。

## 测试

```bash
uv run pytest
```

默认跳过联网测试；运行联网测试：`uv run pytest -m network`。
