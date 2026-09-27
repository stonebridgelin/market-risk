# market-risk：美股大盘风险评分

用程序替代截图，完成美股大盘确认型风险评分（**v2-M 与 v3-R1 并行**）的数据准备、机械计算和 prompt 生成，并保存每次运行的结果、做回测统计。

- 评分规则的权威来源：[`docs/SOP.md`](docs/SOP.md) 第3、6、7节（程序逐条对应，规则版本冻结）。
- 开发规格：[`docs/SPEC.md`](docs/SPEC.md)（第5.6节为 SOP 未写清之处的既定口径）。
- 存储设计：[`docs/STORAGE.md`](docs/STORAGE.md)；TradingView 导入：[`docs/TRADINGVIEW.md`](docs/TRADINGVIEW.md)。
- 开发进度：[`docs/progress/`](docs/progress/)。

> 分数是规则标签，不是经过校准的概率；本项目不提供仓位或投资建议。

---

## 1. 安装

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

命令行入口为 `uv run market-risk ...`，`uv run market-risk --help` 查看全部命令。

## 2. 配置

### 2.1 FRED API 密钥

1. 在 https://fred.stlouisfed.org/docs/api/api_key.html 免费申请。
2. 复制 `.env.example` 为 `.env`，填入 `FRED_API_KEY=你的密钥`。
3. `.env` 已写入 `.gitignore`，**不要把真实密钥写进 `.env.example` 或任何会提交的文件**。

### 2.1.1 可选：Tiingo 与 MySQL 验证库

- `TIINGO_API_KEY`：只用于 `tv crosscheck` 核对 ETF 不复权收盘价（第三方核对），未设置时跳过。
- `MYSQL_VERIFY_URL`：`db verify-mysql` 使用的专用、可清空的空库（先 `uv sync --extra mysql` 安装驱动）。未设置时跳过。
- 两者都只写在 `.env`，不得输出到任何地方。

### 2.2 配置文件

| 文件 | 内容 |
|---|---|
| `config/settings.yaml` | 标的、均线周期、三环节 d1 口径开关、OAS 历史修订比对与长历史开关、网络重试、贴近门槛阈值、存储根目录 |
| `config/holidays.yaml` | 股市/债市休市日与提前收盘日（人工维护，只用于核对） |
| `config/symbols.yaml` | TradingView 标的登记表（用途、单位、时区、已知读数、交叉校验接口） |

评分门槛写在 `src/market_risk/scoring/v2m.py`、`v3r1.py` 中，按 SOP 冻结，不在配置文件里。

## 3. 数据来源

| 数据 | 来源 | 说明 |
|---|---|---|
| SPY、QQQ、RSP、HYG、LQD 日线 | Yahoo Finance（yfinance） | **不复权 Close**（`auto_adjust=False`），不使用 Adj Close |
| 均线 MA5/10/20/30/50/200 | 程序计算 | 简单移动平均，截至基准日 |
| VIX | FRED `VIXCLS` | 缺失时用 Cboe 官方 `VIX_History.csv`（SPEC 5.6 第7条） |
| 10年期收益率 | 财政部 Daily Par Yield Curve 的 `10 Yr` | 主源失败时用 FRED `DGS10` |
| OAS | FRED `BAMLH0A0HYM2` | O1 为基准日之前的观测；FRED 只提供最近三年 |
| S5FI、S5TW | TradingView 导出数据，其次手工录入 | 没有免费接口 |

所有数据由 `data build` 整理为 `data/market/` 下的市场数据集（每个序列一个 CSV，提交 git），**评分只读这里**；接口缓存 `data/cache/` 只作为其输入。所有数据都**截断到基准日（含）**，快照构建后会断言没有晚于基准日的数据。存储架构见 `docs/decisions/0001-数据存储架构.md`。

## 4. 常用命令

### 4.1 日期与样本

```bash
uv run market-risk dates --date 2025-11-28
```

```bash
uv run market-risk samples --year 2026
```

`dates` 列出 T−5、T−20、20日窗口、O1/O6、T−45、结果窗口、休市日；`samples` 按 SOP 9.2 列出每月样本日期。

### 4.2 生成市场数据集、下载并核对数据

```bash
uv run market-risk data build
```

按需下载全部历史（缓存在 `data/cache/`，不提交 git），生成 `data/market/` 与 `manifest.json`。`--offline` 只用现有缓存；历史数值被修订时列出清单（`reports/market_data_revisions.md`）、不自动覆盖，确认后加 `--accept-revisions`。

```bash
uv run market-risk fetch --date 2025-11-28
```

数据集未覆盖基准日时先运行 data build；并为该基准日取得 OAS 的 ALFRED 版本（正式样本用），然后显示核对摘要。加 `--refresh` 忽略缓存重新下载。

### 4.3 评分（回测）

三环节 d1 候选为 T−20 至 T−2（SOP 7.2）；另一口径（T−19 至 T−2）只作参考，结果不同时标注。

```bash
uv run market-risk score --date 2025-11-28 --s5fi 58.44 --s5tw 76.73
```

- `score` 只读 `data/market/`；数据集未覆盖基准日时报错，先运行 `fetch --date`。
- 传入的广度读数会写入 `data/manual/breadth.csv`，并离线更新数据集的 S5FI、S5TW；已有 TradingView 数据时可不传。
- 规则需要 5 日前读数时（例如 L<40%），加 `--s5fi-t5 X --s5tw-t5 Y`；缺少时程序按待补处理，并提示"需要补录 YYYY-MM-DD 的 S5FI、S5TW 读数"。

### 4.4 评分（每日前瞻）

```bash
uv run market-risk score --mode daily --s5fi 55.1 --s5tw 60.2
```

基准日默认为今天（美东）。财政部当日数值未发布时利率维度记待补；输出中注明"本日是否为本周最后一个交易日"。

### 4.5 回归核对

```bash
uv run market-risk validate
```

用 `tests/fixtures/` 中的离线数据，把样本1至4的程序值与截图读数逐项比对（价格与均线容差 0.02，其余完全一致）；已有 `data/market/` 时，另用数据集对同一样本重新计分并逐项比对。

### 4.6 TradingView 导出数据

```bash
uv run market-risk tv import --dir data/manual/tradingview/raw/2026-09-26
```

```bash
uv run market-risk tv list
```

```bash
uv run market-risk tv crosscheck
```

```bash
uv run market-risk tv compare --symbol BAMLH0A0HYM2
```

- 把导出的 CSV 原样放进 `data/manual/tradingview/raw/<导出日期>/`，**不要改文件名**（程序靠 `INDEX_S5FI, 1D.csv` 这样的默认文件名识别标的）。
- `tv import` 打印汇总表（标的、起止日期、行数、校验结果、问题说明），据此判断是否需要重新导出。
- `tv validate [--symbol S5FI]` 按 manifest 重新校验；原始文件被改动时会报错（sha256）。
- OAS 长历史（`oas.long_history_source: tradingview`）已于 2026-09-27 开启（`tv compare` 787 天重叠、0 处不一致）：只有 FRED API 取不到的日期（早于三年）才使用 TradingView 数据，并在数据说明中注明。

### 4.6.1 数据审计

```bash
uv run market-risk tv quality
```

```bash
uv run market-risk audit calendar --write
```

- `tv quality`：广度指标早期数据质量检查（单值K线阶段、跳变、节假日数据、重复值与偶然期望对照），以及 MOVE 两个版本比对、HIGN/LOWN 缺口清单，写 `reports/tradingview_data_quality.md`。
- `audit calendar`：补齐 `config/holidays.yaml`（加 `--write` 才写入），与 SIFMA 常见规则对照，并列出债市休市日 OAS 数值不同的日期，写 `reports/holiday_calendar_audit.md`。裁定结果登记到 `config/data_decisions.yaml`。

### 4.7 正式记录、资料、标签

```bash
uv run market-risk official set --date 2025-11-28 --run run_20260927T004923Z_629aaf8
```

```bash
uv run market-risk official confirm --date 2025-11-28
```

```bash
uv run market-risk material add --date 2025-11-28 --type chatgpt_response --file 路径/回复.md
```

```bash
uv run market-risk breadth add --date 2025-11-28 --s5fi 58.44 --s5tw 76.73
```

```bash
uv run market-risk outcome compute --date 2025-10-31
```

```bash
uv run market-risk outcome add --date 2025-10-31 --spx -4.41 --qqq -6.90 --qqq-peak-to-trough -7.34
```

- 某基准日第一次运行、状态为 complete 或 pending、代码与配置没有未提交修改时，自动设为正式记录（reviewed=false）；复核后用 `official confirm`。
- 结果标签由程序计算（SOP 9.5），只能在结果窗口（基准日后第20个交易日）结束后计算；用户抽查核对，手工录入与程序计算不一致时报告差异，统计以手工为准。
- 标签字段：`drawdown_from_base`（基准日口径，风险事件按此判断）、`peak_to_trough_drawdown`（峰谷回撤，峰值起点包含基准日，仅参考）、"接近事件"（按基准日口径标普500 ≥4% 或 QQQ ≥6%，仅参考）。

### 4.8 导入旧记录、数据库、统计

```bash
uv run market-risk import-legacy
```

```bash
uv run market-risk rebuild-db
```

```bash
uv run market-risk db export-sql
```

```bash
uv run --extra mysql market-risk db verify-mysql
```

```bash
uv run market-risk stats --from 2025-08-01 --to 2026-09-30
```

- `import-legacy` 读取 `data/legacy/backtest_record_legacy.xlsx`，截图记录设为正式记录（reviewed=true），与程序记录对照；分数不同时以退出码 3 结束并列出差异，交给人工判断。
- `stats` 写 `reports/backtest_stats.md` 与 `reports/backtest_history.xlsx`；样本量不足时结论为"不确定"。

## 5. 输出说明

每次 `score` 运行在 `results/MARKET/risk_scoring/<年>/<日期>/run_<UTC时间>_<commit>/` 下生成（不覆盖旧运行）：

| 文件 | 内容 |
|---|---|
| `meta.json` | 运行信息：时间（UTC 与本地）、git commit 与是否有未提交修改、配置开关、各数据源 URL/下载时间/截止日期、状态、待补维度、需人工判断事项 |
| `dates.json` | 全部日期参照 |
| `snapshot.json` | 评分输入（不含逐日序列） |
| `scores.json` | 两个版本的五维分数、计算过程、总分与阶段、证据链、贴近门槛、三环节遍历 |
| `metrics.json` | 扁平数值指标（数据库用） |
| `three_segment.csv` | 三环节完整遍历（两种 d1 口径） |
| `prompt.md` | 交给 ChatGPT 的 prompt（结构同 SOP 11.1；要求不联网、只用提供的数据复核；附三环节遍历表与逐日收盘价；规则全文从 SOP 原样截取） |
| `summary.md` | 中文摘要：分项分数、总分、阶段、证据链、计算过程、贴近门槛、数据问题 |
| `inputs/` | 当次实际使用的数据（逐日数据、FRED 观测、财政部数值、广度读数、全部原始序列），可复现 |

同一基准日的 `official.json` 指向正式记录。

数据库默认为 `db/market_risk.sqlite`（SQLite，WAL 模式），可随时由文件重建（`rebuild-db`）。连接地址可用 `.env` 的 `DATABASE_URL` 或 `settings.yaml` 的 `database.url` 更换；访问统一经 SQLAlchemy，表结构由 Alembic 迁移管理（`migrations/`），详见 STORAGE.md 4.1。数据库只存分析结果与索引（另有由 YAML 生成的参考表：标的登记、休市日历、裁定日期表），市场时间序列在 `data/market/`。`db/sql/` 下的 SQL 文件由 `db export-sql` 导出，不得手工修改（STORAGE 4.2）；MySQL 为已验证的备选（`db verify-mysql`，STORAGE 4.3）。

## 6. 与截图方式的差异

| 项目 | 截图方式 | 程序 |
|---|---|---|
| 价格与均线 | Tiger 图表十字光标读数 | Yahoo 不复权收盘价，均线由程序计算（样本1至4与截图差值 ≤0.005） |
| VIX、OAS | FRED 网页 Observations 文字值 | FRED API；VIX 缺失时用 Cboe 官方数据 |
| 10年期收益率 | Claude 从财政部网站补充 | 财政部年度 CSV（与 SOP 附录A逐日一致） |
| 广度 | TradingView 截图读数 | TradingView 导出数据或手工录入 |
| 三环节 | ChatGPT 查询收盘价后判断 | 程序完整遍历全部候选 d1（两种口径），附在 prompt 中供复核 |
| 日期与休市 | 人工计算 | NYSE 日历 + 财政部实际数据；债市休市日的 OAS 沿用值按 SPEC 5.6 第10条处理 |
| 未来信息 | 靠人工裁剪截图 | 程序截断到基准日并断言；prompt 不附图表 |
| 记录 | Excel | 每次运行一个目录 + SQLite 数据库 + 统计报表 |

## 7. 常见问题

- **提示"未配置 FRED_API_KEY"**：见 2.1 节。
- **财政部或 FRED 暂时访问失败**：程序会重试 3 次；财政部失败时自动改用 DGS10 并在数据说明中注明。
- **提示"需要补录 YYYY-MM-DD 的 S5FI、S5TW 读数"**：规则需要 5 日前读数，用 `breadth add` 或 `score --s5fi-t5 --s5tw-t5` 补录后重新运行。
- **tv import 报"疑似日期偏移"**：多半是 UNIX 时间戳为 UTC 午夜；把 `symbols.yaml` 中该标的的 `timezone` 改为 `UTC` 后运行 `tv validate`。
- **tv import 报"历史可能未完整加载"**：在 TradingView 图表上向左拖到最早，再重新导出。
- **结果为"待补"**：必要数据缺失且缺失值可能改变结果时，程序不给0分，而是列出可能的分数与总分范围。
- **为什么 ALFRED 不能核验发布时点**：该系列的版本日期等于观测日期，不代表真实发布时间；程序按"次日发布"处理，只用 ALFRED 做历史修订比对（SPEC 5.6 第3条）。
- **本机 git 不在 PATH 中**：`storage/runs.py` 会在常见路径（含 `C:\Execute\Git\bin`）查找；找不到时 commit 记为 `0000000` 且不自动设定正式记录。

## 8. 代码结构

- `src/market_risk/services.py`：统一的业务入口，每个函数返回数据类（可用 `storage.runs.to_jsonable` 转为 JSON）。命令行、以后的 FastAPI 接口、Vue 前端和 LangChain Agent 都调用这些函数。
- `cli.py` 只负责解析参数和格式化输出。
- 数据库读写集中在 `src/market_risk/storage/`（`db.py`、`schema.py`）。

## 9. 开发

```bash
uv run pytest
```

```bash
uv run ruff check
```

联网测试默认跳过，运行 `uv run pytest -m network`。长期约定见 [`CLAUDE.md`](CLAUDE.md)。
