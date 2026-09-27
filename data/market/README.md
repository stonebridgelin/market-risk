# data/market/：市场数据集（评分输入）

由 `uv run market-risk data build` 生成，提交 git。评分与以后的逐日回测一律从这里读取；接口缓存（`data/cache/`，不提交）只作为 `data build` 的输入。架构说明见 `docs/decisions/0001-数据存储架构.md`，存储约定见 `docs/STORAGE.md` 第2节。

## 目录

| 路径 | 内容 |
|---|---|
| `daily/<序列>.csv` | 逐日序列，每个序列一个文件 |
| `vintage/<序列>_<基准日>.csv` | OAS 的 ALFRED 基准日版本，只为正式样本（月末样本、每日前瞻运行）生成，用于历史修订比对；逐日历史回测不生成 |
| `manifest.json` | 每个序列的文件、来源（各来源行数）、下载时间、输入缓存、行数、起止日期、sha256；另记 `vintage` 与广度来源冲突 |

## 序列

| 文件 | 来源 | 说明 |
|---|---|---|
| `SPY.csv`、`QQQ.csv`、`RSP.csv`、`HYG.csv`、`LQD.csv` | Yahoo（yfinance，`auto_adjust=False`） | 列 `date,value,open,high,low,close,volume,source`；`value` = 不复权 `Close`；价格四舍五入到4位小数（计分时按两位小数读取，SPEC 5.3） |
| `VIXCLS.csv` | FRED VIXCLS | FRED 缺失（`"."` 或无该日）而 Cboe 有值时按 SPEC 5.6 第7条用 Cboe 补充，`source=cboe`；空值为 FRED 的 `"."` |
| `VIX_CBOE.csv` | Cboe 官方 `VIX_History.csv` 的 `CLOSE` | 备用源全序列，用于报告 FRED 与 Cboe 的差异 |
| `BAMLH0A0HYM2.csv` | FRED（最近三年）+ TradingView（更早日期） | 早于 FRED 返回的第一个观测的日期由 TradingView 导出数据补充（`source=tradingview`，TRADINGVIEW 6.2） |
| `UST10Y.csv` | 财政部 Daily Treasury Par Yield Curve 的 `10 Yr` | 主源失败时为 FRED DGS10（`source=fred:DGS10`）；债市日历由此推出 |
| `S5FI.csv`、`S5TW.csv` | TradingView 清洗结果 + 手工录入（`data/manual/breadth.csv`） | TradingView 优先（SPEC 6.5）；只收两项都有数值的日期 |

参考序列（不参与评分）不在此重复存放，直接使用 `data/processed/tradingview/<标的>.csv`（由 `data/manual/tradingview/raw/` 重建，登记见 `config/symbols.yaml`）。

## 规则

- 只写入已完整收盘的交易日（美东收盘后 15 分钟；提前收盘日 13:00）。
- 与上一版相比，已有日期的数值被修订时**不自动覆盖**：保留旧值，清单写入 `reports/market_data_revisions.md`；确认后运行 `market-risk data build --accept-revisions`。修订判定：Yahoo 价格按4位小数、成交量按整数；其他来源按两位小数。新下载中没有的旧日期保留。
- 本目录有未提交的修改时，运行结果不能自动设为正式记录（git 干净检查包含 `data/market/`）；运行目录的 `meta.json` 记录 `market_manifest_sha256`。
- 不得手工修改本目录的文件。

## 人工价格修正（任务1）

`config/data_decisions.yaml` 的 `decision: correct` 只在负责人批准后添加（2026-09-27 批准 3 条：SPY 2009-07-16、QQQ 2008-09-08、RSP 2016-06-24）。
字段为标的、日期、`corrected_value`（正数，最多4位小数）、`evidence_source`、理由及裁定日期。
当前用于价格序列（kind=etf）：同步修改 `value` 与 `close`，开高低量保持来源原值，
所以修正后收盘不一定落在未修正的日内高低区间内；OHLC 不被推测重写。
manifest 的 `outside_source_high_low` 标注修正后收盘价是否超出来源当日最高价与最低价的区间（3 条均在区间内）。

- 历史修订比较发生在应用裁定之前：上一版人工修正行先从 manifest 恢复来源原值，再与新接口数值比较。
- manifest 各序列的 `corrections` 记录日期、`original_value`、`corrected_value`、修正前完整行
  `original_row`（含来源）、证据、理由及裁定日期。CSV 来源为 `correct:<原来源>`。
- 未接受的接口修订保持原来源旧值；显式接受后更新来源原值，再叠加裁定值。不会把人工修正当成接口修订。
- 重复构建结果稳定；来源某日不再返回时保留该日；未取得新缓存时仍可在现有数据集上应用裁定。
- 更改裁定会重新应用；负责人移除裁定后，下一次构建恢复已接受的来源原值。
- 重复裁定、未知标的、不存在的日期、无效值在写入任何序列前报错。

双侧窗口残差审计与评分隔离。审计中使用的未来日数据不会成为基准日评分输入。
