# TradingView 导出数据的导入与使用（SPEC 补充）

> 放在 `docs/TRADINGVIEW.md`。本文件补充 `docs/SPEC.md` 第6节（数据获取）和 `docs/STORAGE.md` 第2节（目录结构）。与这两份文件冲突时，以本文件为准，并同步修改那两份文件。

---

## 1. 背景与原则

- 数据来源：用户通过一天的 TradingView Premium 会员，用"导出图表数据"功能手工导出的日线 CSV。**会员到期后无法再导出**，所以原始文件必须完整保存，不得修改。
- 用途分三类（写在 `config/symbols.yaml` 中）：
  - `scoring`：参与评分（目前只有 S5FI、S5TW）；
  - `crosscheck`：与现有接口数据互相校验（SPY、QQQ、RSP、HYG、LQD、VIX、10年期收益率、OAS 的重叠部分）；
  - `reference`：以后的框架可能用到，现在只保存、不参与计算（其他广度指标、市场内部指标、MOVE 等）。
- 原始数据只读；所有清洗结果另存，可随时由原始数据重新生成。
- 数据仅供个人研究使用，不对外分发。仓库保持私有。

---

## 2. 目录结构（补充 STORAGE 第2节）

```
data/
├── manual/
│   └── tradingview/
│       ├── raw/                              # 原始导出文件，只读，提交 git
│       │   └── 2026-09-26/                   # 按导出日期分目录
│       │       ├── INDEX_S5FI, 1D.csv        # 保留 TradingView 默认文件名
│       │       ├── INDEX_S5TW, 1D.csv
│       │       └── ...
│       └── manifest.csv                      # 每个原始文件一行（见第3节）
└── processed/
    └── tradingview/                          # 清洗后的数据，不提交 git，可重建
        ├── S5FI.csv                          # date,open,high,low,close,volume,source_file
        └── ...
config/
└── symbols.yaml                              # 标的登记表（见第4节）
```

---

## 3. `manifest.csv`

由导入命令自动生成和更新，字段：

`file_path, export_date, tv_symbol, symbol, timeframe, first_date, last_date, rows, time_format, sha256, imported_at, validation_status, notes`

- `tv_symbol`：从文件名解析，如 `INDEX:S5FI`（文件名中的 `INDEX_S5FI` 还原为 `INDEX:S5FI`）。解析失败时报错，要求在 `symbols.yaml` 中补充文件名映射。
- `sha256`：原始文件的哈希值。以后发现文件被改动时报错。
- `validation_status`：`passed` / `warning` / `failed`。

---

## 4. `config/symbols.yaml`（标的登记表）

每个标的一条，至少包括：

```yaml
- symbol: S5FI
  tv_symbol: INDEX:S5FI
  name: 标普500成分股站上50日均线比例
  category: breadth
  usage: scoring
  unit: percent            # percent / index / price / ratio / count
  known_values:            # 已知读数，用于校验
    2025-10-24: 52.88
    2025-10-31: 40.15
    2025-11-28: 58.44
- symbol: S5TW
  tv_symbol: INDEX:S5TW
  name: 标普500成分股站上20日均线比例
  category: breadth
  usage: scoring
  unit: percent
  known_values:
    2025-10-24: 57.65
    2025-10-31: 38.56
    2025-11-28: 76.73
```

其他标的（ETF、VIX、OAS、其他广度指标等）按实际导出情况补充，`usage` 分别为 `crosscheck` 或 `reference`。ETF 的已知收盘价可取自 SPEC 第9节的回归样本。

- （2026-09-26 确认）`known_values` 只登记截图读数。SOP 10.1 样本2备注中"前一日S5TW为37.97%"（2025-09-25）**不登记**为 known_values。
- （实现补充）`timezone`（默认 `America/New_York`）、`calendar`（`nyse` / `bond` / `none`）、`inception`、`filename_aliases`、`api_source`（交叉校验用的接口数据，如 `yahoo:SPY`、`fred:BAMLH0A0HYM2`）、`tolerance`（交叉校验容差）为可选字段。
- 导出文件的交易所前缀与登记不同时（例如登记 `AMEX:SPY`、导出为 `BATS_SPY`），若代码部分只对应一个已登记标的，按该标的处理并在说明中注明。

---

## 5. 导入与清洗（`src/market_risk/data/tradingview.py`）

### 5.1 读取
- 支持 TradingView 两种时间格式：UNIX 时间戳，以及带时区偏移的 ISO 时间（如 `2025-11-28T09:30:00-05:00`）。
- **日期换算是最容易出错的地方**：日线K线的时间戳可能对应交易所当地的开盘时刻或午夜。不同标的的时间戳可能不同，不能统一转成 UTC 再取日期，否则会错一天。处理方法：
  - ISO 格式：直接取时区偏移下的本地日期部分；
  - UNIX 时间戳：先按该标的的交易所时区换算（美股、美国指数用 `America/New_York`；FRED 数据按美国东部时间处理），再取日期；
  - 用 `known_values` 校验日期是否对齐：若数值与已知读数正好错开一天，报错并说明"疑似日期偏移"。
- 保留全部列。除 OHLC 和成交量外，如果文件中有其他指标列，原样保存到清洗结果中，列名加前缀 `extra_`。

### 5.2 校验（每个文件都做）
1. 日期无重复，按时间顺序排列。
2. `known_values` 中的每个读数都能对上（容差 0.005）。
3. **不完整K线**：若最后一根K线的日期等于导出日期，且导出时间在美东16:00之前，则标记为不完整并排除。
4. 与 NYSE 交易日历对比：列出缺失的交易日，以及在非交易日出现的数据（FRED 类数据在债市营业日出现属正常，另行说明）。
5. 百分比类指标的数值在 0–100 之间；价格类指标为正数。
6. 报告最早日期、最晚日期、行数，并与该标的的上市或起始日期对比，提示"历史是否已完整加载"。

### 5.3 合并多次导出
- 以后若再次导出同一标的，重叠日期的数值必须一致（容差 0.005）；不一致时报错并列出差异，不自动覆盖。
- 清洗结果包含 `source_file` 列，标明每一行来自哪个原始文件。

---

## 6. 在评分中的使用

### 6.1 广度（修改 SPEC 6.5）
读取顺序：
1. TradingView 导出数据（`data/processed/tradingview/S5FI.csv`、`S5TW.csv`）；
2. 手工录入的 `data/manual/breadth.csv`（导出日之后的新日期）。

两个来源在同一日期都有数值但不一致时，报告差异，以 TradingView 导出数据为准。这样所有历史样本的 F、W、F5、W5 都能自动取得。

### 6.2 OAS 长历史（修改 SPEC 6.3，默认关闭）
- 如果 TradingView 导出了 FRED 的 `BAMLH0A0HYM2` 且历史早于 FRED API 能提供的范围，保存为 `crosscheck` 数据。
- 在 `settings.yaml` 中增加开关 `oas.long_history_source`，取值 `none`（默认）或 `tradingview`。
- 开启前必须先运行重叠比对（第7节命令 `tv compare`）：在两者都有数据的日期上，数值须完全一致（两位小数）。比对结果写入 `reports/`，由用户决定是否开启。
- 开启后，只有 FRED API 取不到的日期才使用 TradingView 数据，`data_notes` 中注明来源。

### 6.3 交叉校验
- ETF、VIX、10年期收益率：TradingView 数据只用于与接口数据比对，不参与评分。比对结果写入 `reports/tradingview_crosscheck.md`，列出差异超过容差的日期。

### 6.4 结果标签
- 若导出了标普500指数和纳斯达克100指数（或 QQQ），可作为 SOP 第9.3节风险事件标签的数据来源之一，与 Yahoo 数据互相校验。标签的隔离要求不变（STORAGE 第5节）。

---

## 7. 命令

```
uv run market-risk tv import --dir data/manual/tradingview/raw/2026-09-26   # 导入并校验一个目录下的全部文件
uv run market-risk tv validate [--symbol S5FI]                             # 重新校验
uv run market-risk tv list                                                 # 列出已导入标的、起止日期、校验状态
uv run market-risk tv compare --symbol BAMLH0A0HYM2                        # 与 FRED API 数据做重叠比对
uv run market-risk tv crosscheck                                           # 全部 crosscheck 标的，写 reports/tradingview_crosscheck.md
```

- `tv compare` 的结果写入 `reports/tradingview_compare_<标的>.md`。比对的接口来源由 `symbols.yaml` 的 `api_source` 指定，容差由 `tolerance` 指定（OAS、DGS10 为 0，即两位小数完全一致）。

`tv import` 结束时打印一张汇总表：每个文件的标的、起止日期、行数、校验结果、问题说明。**用户在会员有效期内要根据这张表判断是否需要重新导出**，所以输出必须清楚易读。

---

## 7.1 2026-09-26 导出的数据（导入与核对记录）

| 标的 | 范围 | 行数 | 校验 | 说明 |
|---|---|---|---|---|
| BAMLH0A0HYM2 | 1996-12-31 至 2026-09-24 | 7765 | 通过 | FRED 序列起点，已含全部历史；`tv compare` 与 FRED API 在 2023-09-26 至 2026-09-24 的787天重叠区间0处不一致，已开启 OAS 长历史 |
| S5FI | 2008-08-04 至 2026-09-25 | 4575 | 警告 | 10 个非 NYSE 交易日有数据（见下） |
| S5TW | 2008-07-24 至 2026-09-25 | 4582 | 警告 | 同上 |

- **非 NYSE 交易日的数据**（2008-09-01、2008-11-27、2009-01-19、2009-02-16、2009-04-10、2009-05-25、2009-07-03、2009-09-07、2009-11-26、2018-12-05）：S5FI、S5TW 共 20 个数值**全部等于前一个交易日的数值**，为沿用值；这些日期不是股票交易日，不会成为基准日或 T−5，不影响计分。另注意：S5FI 在 2009-07-06、2018-12-06 的数值也与前一交易日相同（S5TW 不同），可能是陈旧值，使用这两天的读数时留意。
- **OAS 缺失的股市交易日**：2013-08-30、2015-01-16（长周末前债市提前收盘，ICE 无观测）。其余债市休市日（哥伦布日、退伍军人节）有数值，为沿用值，按 SPEC 5.6 第10条处理。若缺失日成为某基准日的 O1，v3-R1 信用记"待补：数据滞后"；成为 O6 时，O1≥4.00% 则无需 O6（SOP 7.3），否则记待补（有测试）。

## 8. 测试

- 用 `tests/fixtures/tradingview/` 下的小样本文件（手工构造，包含 ISO 与 UNIX 两种时间格式）测试读取与日期换算。
- 构造"日期整体错开一天"的文件，确认能报出"疑似日期偏移"。
- 构造最后一根为不完整K线的文件，确认被排除。
- 构造两次导出在重叠日期数值不同的情况，确认报错。
- 真实导出文件导入后，用 `known_values` 做回归测试。

---

## 9. 开发安排

作为 **阶段2.5** 插入，**优先于阶段3的剩余工作**，原因是会员只有一天，必须在有效期内完成导入和校验，发现问题才能重新导出。
1. 实现第5节的读取与校验，以及 `tv import`、`tv list`。
2. 用户导出第一批文件后立即运行 `tv import`，根据汇总表决定是否重新导出。
3. 第6节的使用、`tv compare`、交叉校验报告，可以在阶段3完成后再做。
