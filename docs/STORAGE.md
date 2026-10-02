# market-risk 数据与结果存储设计（SPEC 补充）

> 放在 `docs/STORAGE.md`。TradingView 导出数据的目录见 `docs/TRADINGVIEW.md` 第2节（已同步到下方第2节）。本文件补充并替代 `docs/SPEC.md` 第3节中 `data/`、`output/` 的目录设计，以及第7节的输出位置。其他内容不变。

---

## 1. 设计原则

1. **数据按性质分三处存放**（2026-09-27，`docs/decisions/0001-数据存储架构.md`）：原始资料（原始文件，提交 git）；市场数据（`data/market/`，整理后的 CSV，每个序列一个文件，提交 git，**评分与逐日回测一律从这里读取**）；分析结果（SQLite，不提交，可重建）。可重新下载的接口缓存（`data/cache/`）只作为生成市场数据的输入，不提交。
2. **分析结果按"对象 → 框架 → 年份 → 日期"分类**。对象是 `MARKET`（大盘）或股票代码（如 `NVDA`）；框架是 `risk_scoring`（确认型风险评分），以后加入 `overheat`（过热预警）、`bottom`（底部信号）或单只股票的分析框架。
3. **每次运行都保留，不覆盖**。同一个基准日可以运行多次（例如修正数据后重算），每次一个独立目录，并用指针文件标明哪一次是正式记录。
4. **保存当次实际使用的数据**。FRED 等数据源会修订历史数值，缓存也可能被刷新，所以每次运行都要把参与计算的数据另存一份，保证日后能复现当时的结果。
5. **文件与数据库并存**：文件（CSV / JSON / Markdown）方便人工查看和 git 管理；SQLite 数据库方便统计查询。数据库中的内容都能从文件重建。
6. **结果标签与评分严格隔离**：风险事件标签（基准日之后20个交易日的涨跌结果）单独存放；生成 prompt 和评分的代码不得读取标签。

---

## 2. 目录结构

```
market-risk/
├── data/
│   ├── cache/                          # 可重新下载的接口缓存，不提交 git；只作为 data build 的输入
│   │   ├── yahoo/
│   │   ├── fred/
│   │   ├── treasury/
│   │   ├── cboe/
│   │   └── tiingo/                     # 只用于 tv crosscheck 的第三方核对
│   ├── market/                         # 市场数据集（评分输入），由 data build 生成，提交 git
│   │   ├── daily/<序列>.csv            # SPY/QQQ/RSP/HYG/LQD、VIXCLS、VIX_CBOE、BAMLH0A0HYM2、UST10Y、S5FI、S5TW、SPX、NDX
│   │   ├── weekly/<序列>.csv           # STLFSI4、NFCI（周频参考序列，revisable：整体替换为最新版本）
│   │   ├── vintage/<序列>_<基准日>.csv # ALFRED 基准日版本（只为正式样本生成）
│   │   ├── manifest.json               # 来源、下载时间、行数、起止日期、sha256
│   │   └── README.md
│   ├── legacy/                         # 截图时代的历史记录，提交 git
│   │   └── backtest_record_legacy.xlsx # 样本1至4（import-legacy 的输入）
│   ├── manual/                         # 人工录入的数据，提交 git
│   │   ├── breadth.csv                 # date,s5fi,s5tw,note（每个交易日一行）
│   │   ├── outcomes.csv                # 风险事件标签（见第5节）
│   │   ├── provenance/                 # 数据留痕（docs/research/数据留痕设计说明.md）：只追加，不接入评分与研究计算
│   │   │   ├── records.csv             # 录入留痕记录（每条一行）
│   │   │   ├── confirmations.csv       # 确认记录
│   │   │   ├── record_id.counter       # 已发出的最大记录顺序号（编号不因删除而复用）
│   │   │   └── snapshots/<指标>/<交易日>/  # 原始快照文件
│   │   └── tradingview/                # TradingView 导出数据（docs/TRADINGVIEW.md）
│   │       ├── raw/<导出日期>/         # 原始导出文件，只读，保留默认文件名
│   │       └── manifest.csv            # 每个原始文件一行（含 sha256、校验结果）
│   ├── processed/                      # 由原始数据重建的清洗结果，不提交 git
│   │   └── tradingview/<标的>.csv      # date,open,high,low,close,volume,extra_*,source_file
│   └── materials/                      # 待分析资料，按对象和日期分类，提交 git（大文件除外）
│       ├── MARKET/
│       │   └── 2025/
│       │       └── 2025-11-28/
│       │           ├── tiger_ai_background.md   # Tiger AI 背景问答（不计分）
│       │           ├── chatgpt_response.md      # ChatGPT 的评分回复
│       │           ├── claude_review.md         # Claude 的核查意见
│       │           ├── notes.md                 # 你自己的笔记
│       │           └── screenshots/             # 截图（可选，不提交 git）
│       └── stocks/
│           └── NVDA/
│               └── 2026/
│                   └── 2026-09-25/
├── results/                            # 程序分析结果，提交 git
│   ├── MARKET/
│   │   └── risk_scoring/
│   │       └── 2025/
│   │           └── 2025-11-28/
│   │               ├── official.json            # 指向正式记录的那次运行（含 reviewed 标记）
│   │               ├── run_20260927T021530Z_a1b2c3d/
│   │               │   ├── meta.json            # 运行信息（见第3节）
│   │               │   ├── inputs/              # 当次实际使用的数据
│   │               │   │   ├── daily_data.csv
│   │               │   │   ├── fred_observations.csv
│   │               │   │   ├── treasury_yields.csv
│   │               │   │   └── breadth.csv
│   │               │   ├── dates.json
│   │               │   ├── snapshot.json
│   │               │   ├── scores.json
│   │               │   ├── three_segment.csv    # 三环节完整遍历记录
│   │               │   ├── prompt.md
│   │               │   └── summary.md
│   │               └── run_20260928T011200Z_e4f5a6b/
│   └── stocks/
│       └── NVDA/
│           └── <框架名>/
│               └── 2026/2026-09-25/run_.../
├── db/
│   ├── market_risk.sqlite              # 统计用数据库，不提交 git（可由文件重建）
│   └── sql/                            # 由 db export-sql 导出的 SQL（表结构、参考表），提交 git，不得手工修改
├── reports/                            # 统计汇总与导出，提交 git
│   ├── backtest_history.xlsx           # 样本汇总表（格式同现有 Excel）
│   ├── backtest_stats.md               # 第9.4节指标统计
│   ├── tradingview_crosscheck.md       # TradingView 与接口数据的交叉校验
│   └── daily/                          # 前瞻逐日汇总（按月一个文件）
│       └── 2026-10.csv
├── config/
│   ├── symbols.yaml                    # 标的登记表（docs/TRADINGVIEW.md 第4节）
│   ├── holidays.yaml                   # 休市日历
│   └── data_decisions.yaml             # 已裁定日期表
└── docs/decisions/                     # 架构决策记录（如 0001-数据存储架构.md）
```

命名规则：
- 日期一律用 `YYYY-MM-DD`；按年份建一层目录，避免单个目录下文件过多。
- 股票代码一律大写；大盘统一用 `MARKET`。
- 运行目录名：`run_<UTC 运行时间 YYYYMMDDTHHMMSSZ>_<git commit 前7位>`，例如 `run_20260927T021530Z_a1b2c3d`；同一秒内重复运行时依次加后缀 `_2`、`_3`……（2026-09-26 确认）。
- 基准日仍按美东时间理解；运行时间只用于区分运行。
- Windows 下不使用符号链接，用 `official.json` 指针文件代替。
- SPEC 第7节的 `output/history.csv` 已取消，其功能由数据库 `runs`、`totals` 表和 `reports/` 覆盖。

### 2.3 逐日历史回测的运行目录（阶段6，2026-09-27）

```
results/MARKET/risk_scoring/backtests/
├── official.json                  # 正式回测指针（run_id、set_at_utc、set_by）
├── .gitignore                     # 由 backtest official 生成并提交：只放行正式回测的运行目录
└── run_<UTC>_<commit7>/           # 一次回测一个目录（不为每个交易日建目录，不覆盖）
    ├── meta.json                  # 代码 commit、数据集 manifest sha256、回测配置 sha256、规则版本、区间划分、耗时、保留期解锁时间
    ├── README.md                  # 文件说明（含 daily_metrics 的精度与舍入方式）
    ├── daily_scores.csv           # 每个基准日 × 版本：五维分数、总分或范围、阶段、待补维度、明确恶化证据链、预警、flags（提交 git）
    ├── daily_metrics.csv          # 每个基准日：评分实际使用的原始数值与派生值（6 位小数，ROUND_HALF_UP）
    ├── outcomes.csv               # 结果标签（隔离）
    ├── pullback_episodes.csv      # 回调事件（隔离，多层级）
    └── episode_windows.csv        # 回调窗口：高点前20至低点后20个交易日的分数与价格进度（隔离）
```

- 只提交正式回测的运行目录；其他运行目录保留在本地、不提交。meta.json 的代码 commit 与数据集 sha256 足以复现任何一次运行。
- 标签文件（outcomes、pullback_episodes、episode_windows）使用基准日之后的数据，评分代码不得读取（有传递依赖测试）。
- 保留期屏蔽原则：任何字段，只要其取值需要用到保留期的数据，未解锁时一律屏蔽（SPEC 阶段6）。
- 数据库（迁移 0006、0007）：`backtest_runs`、`backtest_daily_scores`、`backtest_outcomes`（隔离，保留单日 `outcomes` 表）、`pullback_episodes`，rebuild-db 由回测运行目录重建。0007 允许 `backtest_outcomes` 的事件布尔值为空并增加 `data_note`，用于记录窗口缺价、标签留空的基准日。
- `daily_metrics.csv` 中 QQQ/RSP 的 MA200 仅用于展示；T−199 至 T 缺价时该列留空，不影响按 T−49 至 T 评分。`outcomes.csv` 在窗口日期已结束但基准日或窗口缺价时保留一行，标签字段留空、`data_note` 说明缺价；回测基础报告单独列出开发期和验证期的这类行。
- `episode_windows.csv` 的 `drawdown_from_peak` =（当日收盘价÷高点收盘价−1）×100，`rebound_from_trough` =（当日收盘价÷低点收盘价−1）×100，单位均为百分数；`decline_progress` =（高点收盘价−当日收盘价）÷（高点收盘价−低点收盘价），保存原始计算值，不截断。高点当天为 0，低点当天为 1；高点至低点期间在 0 至 1 之间。窗口其他日期可能超范围：低点后随反弹减小，超过高点时小于 0；高点前若收盘价低于本段低点，会大于 1。低点未确认或被屏蔽时本列留空。回调窗口只写有分数的日期；起点以前开始的回调从回测起点开始（L-26）。
- **H-07**：ZigZag 从价格序列首日开始，以首日为候选高点。低点确认门槛与下跌门槛同一层级，按低点计算：收盘价 ≥ 低点×（1＋层级）；确认低点的当天成为新的候选高点。“交易日数”为高低点在序列中的行号差。收复日期取低点之后第一个收盘价 ≥ 高点收盘价的日期（含等号）。
- **L-27**：回调跌幅为百分数，保留 4 位小数并以 `ROUND_HALF_UP` 舍入；层级在 CSV 中写为 5、10、20。

### 2.1 正式记录 `official.json`（2026-09-26 确认）

- 内容：`run_id`、`set_at_utc`、`set_by`（`auto` / `manual` / `import-legacy`）、`reviewed`（true/false）、`reviewed_at_utc`。
- **自动设定**：某个基准日第一次运行时，同时满足以下条件才自动设为正式记录，且 `reviewed=false`：
  1. 该对象、框架、基准日尚无正式记录；
  2. 运行状态为 `complete` 或 `pending`（`failed` 不设）；
  3. git 工作区干净（无未提交的修改）。口径（2026-09-27 确认）：排除 `results/`、`reports/`、`data/`、`db/`（数据与输出，否则每次运行后工作区都会变"脏"），**但 `data/market/` 例外、纳入检查**（它是计分输入；2026-09-27 第二次确认）；其余全部纳入检查，**特别是 `docs/SOP.md` 和 `templates/`**——prompt 的规则全文与格式来自这两处，它们有未提交的修改时不自动设定。代码、`config/`、`migrations/` 同样纳入检查。
  4. 无法确定 git 状态时按工作区不干净处理，不自动设正式记录（L-22）。
  否则不自动设定，并在命令行提示。
- 之后再运行只新增运行目录，不改变正式记录；更换正式记录必须用 `official set` 手动指定（手动指定后 `reviewed=false`，需要重新确认）。
- `official confirm` 把当前正式记录设为 `reviewed=true`。
- `stats` 分别统计已复核与未复核的样本数。

---

## 3. 运行信息 `meta.json`

每次运行必须记录：

- `run_id`、`created_at_utc`、`created_at_local`（带时区偏移）
- `subject`（`MARKET` 或股票代码）、`framework`、`base_date`、`mode`（`backtest` / `daily`）
- `rule_versions`（如 `["v2-M", "v3-R1"]`）
- `git_commit`，以及工作区是否有未提交的修改（有则警告）
- 配置开关与口径的取值（如 `oas.revision_check`、三环节 d1 范围）
- 每个数据源的来源 URL、下载时间、数据截止日期（从数据集读取时为 `data/market/` 中的文件）
- `market_manifest_sha256`：`data/market/manifest.json` 的 sha256，作为数据集版本
- JSON 文件（如 scores.json、metrics.json）的 Decimal 以浮点数写出；metrics.json 先保留 6 位小数。需要精确值时以 CSV 中的字符串为准（L-10）。
- `data_source_type`：`api`（程序获取）或 `screenshot`（历史上的截图方式，用于导入样本1至4）
- 运行状态：`complete` / `pending`（有待补维度）/ `failed`

---

## 4. SQLite 数据库

用于统计与查询，表结构至少包括：

| 表 | 内容 |
|---|---|
| `runs` | 每次运行一行：`meta.json` 的主要字段、结果目录路径、是否为正式记录（`is_official`）、是否已复核（`reviewed`） |
| `dimension_scores` | 每次运行 × 每个规则版本 × 每个维度一行：分数、可能取值、触发条件、待补原因 |
| `totals` | 每次运行 × 每个规则版本一行：总分或总分范围、阶段、是否明确恶化、是否预警 |
| `metrics` | 长表格式（`run_id, key, value`）：收盘价、均线、差值、L、g、Δy、ΔOAS 等全部数值 |
| `near_threshold` | 贴近门槛的读数：项目、数值、门槛、差距 |
| `outcomes` | 风险事件标签（见第5节） |
| `materials` | 资料索引：对象、日期、类型、文件路径、来源、说明 |
| `reviews` | ChatGPT / Claude / 本人的复核结论，以及与程序结果的差异 |
| `symbols` | 参考表：标的登记（由 `config/symbols.yaml` 生成） |
| `market_holidays` | 参考表：股市、债市的休市日与提前收盘日，含注释（由 `config/holidays.yaml` 生成） |
| `data_decisions` | 参考表：已裁定日期表（由 `config/data_decisions.yaml` 生成） |
| `provenance_records` | 数据留痕：录入留痕记录（由 `data/manual/provenance/records.csv` 重建；迁移 0008，2026-10-01） |
| `provenance_confirmations` | 数据留痕：确认记录（由 `confirmations.csv` 重建） |
| `signal_input_links` | 数据留痕的预留关联表：信号实际使用的输入记录编号（本批为空，不接入信号计算） |

留痕表保存的是录入事件及其读数，属于审计记录，不是数据集副本；评分与研究计算不得从该表读取市场时间序列（目前也没有接入）。评分与研究计算仍只读 `data/market/` 与各自既有的输入。字段与约束见 `docs/research/数据留痕设计说明.md`；迁移 0009 增加了快照缺失原因与来源文件两组字段。

要求：
- 数据库**只存分析结果与索引**：运行记录、分数、指标、结果标签、资料索引、复核记录，以及以后逐日回测的结果；另有三张参考表（YAML 仍为源头，供以后前端与统计查询使用）。**市场时间序列不存入数据库**（在 `data/market/`）。
- 数据库可以随时由 `results/`、`data/manual/`、`data/materials/`、`config/` 重建（提供命令 `rebuild-db`；参考表在重建时由 YAML 写入）。
- 同一对象、框架、基准日只能有一个正式记录（`is_official`）。实现：`officials` 表以（对象, 框架, 基准日）为复合主键。

### 4.1 数据库约定（2026-09-27 确认，为以后接 Vue 前端和更换数据库做准备）

1. 数据库访问统一使用 **SQLAlchemy**（Core）。连接地址从 `.env` 的 `DATABASE_URL` 读取，其次 `config/settings.yaml` 的 `database.url`，默认 `sqlite:///db/market_risk.sqlite`（SQLite 相对路径以存储根目录为基准）。
2. **不使用任何数据库的专有语法**（例如不用 SQLite 的部分索引；唯一性用主键或唯一约束表达）。小数一律用 `Numeric`（与程序的 Decimal 精确计算一致），不用 `Float`；日期用 `Date`，是否用 `Boolean`。
3. 表结构用 **Alembic** 管理：迁移脚本在 `migrations/versions/`，表结构的最新描述在 `src/market_risk/storage/schema.py`。修改表结构必须新增迁移，不得直接改已有迁移。`rebuild-db` 会先清空数据库并执行 `upgrade head`，再由文件写入内容。
4. **所有数据库读写集中在 `src/market_risk/storage/`**（`db.py`、`schema.py`）；其他模块只调用其中的函数（如 `official_runs`、`totals_by_version`），不写 SQL、不直接连接数据库。
5. SQLite 开启 **WAL 模式**（并开启外键约束），只在连接 SQLite 时设置。
6. 说明：SQLite 没有原生 DECIMAL，`Numeric(20, 8)` 在 SQLite 中按数值存储、读出时还原为 Decimal；换用 PostgreSQL 等数据库后为精确小数。`alembic.ini` 只含 ASCII 字符（Windows 上 Alembic 按系统编码读取该文件）；程序内部运行迁移时不读取该文件。
7. 改造验证（2026-09-27）：改为 SQLAlchemy 后运行 `rebuild-db`，与改造前的 8 张表逐行比对，内容全部一致（仅"全部样本"类复核记录的日期由空字符串改为 NULL）；新增 `officials` 表 4 行。
8. 选型（2026-09-27，`docs/decisions/0001-数据存储架构.md`）：继续使用 SQLite；MySQL 为已验证的备选，部署到服务器或多客户端访问时只需修改 `DATABASE_URL`。

### 4.2 `db/sql/`（由程序导出，提交 git）

- `01_schema_sqlite.sql`、`01_schema_mysql.sql`：由当前 Alembic 最新版本的表结构导出（导出前核对 `schema.py` 与迁移结果一致），含 `alembic_version`；
- `02_base_data.sql`：参考表的 INSERT 语句，由 YAML 生成，两种数据库都可执行；
- `README.md`：具体数据不导出 SQL（分别在 `data/market/`、`data/manual/`、`data/materials/`、`results/`），以及如何用 `rebuild-db` 重建。
- 命令 `market-risk db export-sql`；测试保证表结构或 YAML 变化后已重新导出。SQL 文件中不得包含任何账户、密码或本机路径；不含生成时间。

### 4.3 MySQL 兼容性验证

任务1迁移0005在 `data_decisions` 增加 `corrected_value Numeric(20, 8)` 与 `evidence_source Text`。
两字段随 YAML 重建及 SQL 导出；已有裁定保持 NULL。阶段6回测结构迁移顺延为0006（负责人已确认）。
价格数据集保留修正前整行与裁定的方式见 `data/market/README.md`，数据库仍不保存市场价格序列。

- 命令 `market-risk db verify-mysql`（需 `uv sync --extra mysql` 安装 pymysql）：连接地址从 `.env` 的 `MYSQL_VERIFY_URL` 读取，未设置时跳过、不报错；**地址、账户、密码不得输出到任何文件、日志或汇报**，错误信息脱敏。
- 验证库必须专用：库中只允许本项目的表，否则拒绝清空。
- 流程：在验证库上执行 Alembic 迁移与 `rebuild-db`，与临时 SQLite 的 `rebuild-db` 逐表逐行比对（小数按数值、布尔、日期规范化后比较）；再用 `db/sql/` 的 SQL 建库核对参考表；报告字符集（utf8mb4）、排序规则与大小写敏感性。
- 集成测试 `tests/test_dbsql.py::test_mysql_compatibility`，默认跳过（`uv run --extra mysql pytest -m mysql`）。

---

## 5. 风险事件标签的隔离

- 标签单独存放在 `data/manual/outcomes.csv` 和数据库 `outcomes` 表。字段：`subject`、`base_date`、`window_start`、`window_end`、`spx_drawdown_from_base`、`qqq_drawdown_from_base`（基准日口径，风险事件按此判断）、`is_event`、`event_date`、`spx_peak_to_trough_drawdown`、`qqq_peak_to_trough_drawdown`（峰谷回撤，参考）、`near_event`（接近事件，参考）、`source`（`manual` / `computed`）、`entered_at`。字段口径见 SOP 9.3。
- 字段名于 2026-09-27 由 `*_min_close_drawdown` / `*_max_drawdown` 改为上述名称（Alembic 迁移 0003）；读取旧 CSV 时兼容旧列名。
- 可以由程序在结果窗口结束后自动计算（只用收盘价，按 SOP 9.3），也可以人工录入；两者不一致时报告差异。
- **隔离要求**：`scoring/`、`report.py` 中生成 prompt 的部分、`data/snapshot.py` 都不得读取 `outcomes`。写一个测试：扫描这些模块的导入和文件读取，确认不引用标签文件和标签表。
- 结果窗口尚未结束时，不得计算标签。
- 价格精度（审查 M-11）：评分、结果标签及回调事件都从市场数据集的十进制文本读取不复权 Close，按 `ROUND_HALF_UP` 舍入到两位小数后使用 Decimal 计算；写出 JSON/CSV 时才按各字段的展示精度序列化。

---

## 6. 统计功能

命令：`uv run market-risk stats [--framework risk_scoring] [--from 2025-08-01 --to 2026-09-30]`

按 SOP 第9.4节，分别统计 v2-M 与 v3-R1：
- 命中率、误报率、预警准确率、正确静默率、报警比例、数据完整率、命中样本的提前量；
- 待补样本：总分下限 ≥3 视为预警，上限 <3 视为未预警，其余单独列为"状态未知"；
- 两个版本的差异，以及差异对应 SOP 7.4 的哪一处修改；
- 只统计正式记录；分别列出已复核（`reviewed=true`）与未复核的样本数；
- 样本量不足时明确写"不确定"，不给结论。

输出到 `reports/backtest_stats.md`，并更新 `reports/backtest_history.xlsx`。

---

## 7. 资料管理命令

```
uv run market-risk material add --subject MARKET --date 2025-11-28 --type chatgpt_response --file 路径
uv run market-risk material list --subject MARKET --from 2025-11-01
uv run market-risk breadth add --date 2025-11-28 --s5fi 58.44 --s5tw 76.73
uv run market-risk outcome compute --date 2025-11-28      # 结果窗口结束后才允许
uv run market-risk official set --subject MARKET --framework risk_scoring --date 2025-11-28 --run <run_id>
uv run market-risk official confirm --subject MARKET --framework risk_scoring --date 2025-11-28
uv run market-risk import-legacy --excel data/legacy/backtest_record_legacy.xlsx   # 导入截图时代的样本1至4
uv run market-risk rebuild-db
```

- `material add` 把文件复制到 `data/materials/<对象>/<年>/<日期>/`，并写入数据库索引。
- `breadth add` 写入 `data/manual/breadth.csv`，同一日期重复录入时提示确认。
- `import-legacy`（2026-09-26 确认）：
  - 把样本1至4以 `data_source_type=screenshot` 导入，保留原有备注；导入的截图记录设为正式记录，`set_by=import-legacy`，`reviewed=true`。
  - 只读取录入值（分数与原始读数）；公式列由程序重新计算，并与 Excel 中的缓存值比对作为校验，不一致时报告。
  - "变更记录"工作表导入为复核记录（`reviews` 表）或资料（`materials`）。
  - 阶段4的程序运行记录作为对照保留（不是正式记录），与截图记录的差异写入 `reviews` 表；若差异导致任何维度分数不同，不自动处理，列出交给用户判断。

---

## 8. 为单只股票分析预留的接口

- 所有数据获取函数以 `symbol` 为参数，不写死 SPY、QQQ、RSP。
- 路径生成集中在一个模块（如 `storage/paths.py`），由 `subject`、`framework`、`base_date` 决定，不在各处拼接路径字符串。
- 新的分析框架以独立模块加入（如 `frameworks/stock_trend/`），复用 `calendar`、`data`、`indicators`、`storage`，结果写入 `results/stocks/<代码>/<框架名>/`。
- 数据库的 `runs.subject` 与 `runs.framework` 两个字段即可区分大盘与个股、不同框架，不需要为个股另建数据库。

---

## 8.1 实现补充（阶段4.5）

- 数据库的每张表都由文件重建：`reviews` 来自 `data/manual/reviews.csv`，`materials` 来自 `data/materials/index.csv`，`metrics` 来自运行目录的 `metrics.json`（没有时由 `snapshot.json` 计算）。
- 运行目录另有 `metrics.json`（扁平数值指标）；import-legacy 的运行目录另有 `legacy.json`（Excel 中该样本的原始单元格），`data_source_type=screenshot`，`meta.json` 的 `legacy.sha256` 用于防止重复导入。
- `outcomes.csv` 的 `event_date` 为首次达到门槛的日期，用于计算"命中样本的提前量"。同一样本同时有手工与程序标签时，统计以手工标签为准。
- 命令补充：`outcome add --date --spx --qqq [--event-date]`（人工录入标签）。
- 统计中的 95% 区间按 Newcombe 方法（独立样本近似）计算，仅供参考；样本量不足时结论固定为"不确定"。
- 标签隔离：评分所用的指标提取放在不读取标签的 `metrics.py`；`scoring/`、`report.py`、`data/snapshot.py`、prompt 模板由测试扫描，确认不引用标签。

## 9. 开发安排

这部分作为 SPEC 第9节的**阶段4.5**，在阶段4（回归测试与输出）之后、阶段5（收尾）之前完成：
1. 实现 `storage/paths.py`、运行目录与 `meta.json`、`official.json`。
2. 实现 SQLite 建表、写入与 `rebuild-db`。
3. 实现资料管理、广度录入、标签计算与隔离测试。
4. 实现 `import-legacy`，导入样本1至4。
5. 实现 `stats`，用样本1至4生成第一版统计（样本不足，结论应为"不确定"）。

验收：
- 删除 `db/` 后运行 `rebuild-db`，数据库内容与删除前一致；
- 同一基准日运行两次，两个运行目录都保留，`official.json` 只指向一个；
- 标签隔离测试通过；
- `stats` 输出中样本1至4的分数与现有 Excel 一致。
