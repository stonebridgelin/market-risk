# market-risk 数据与结果存储设计（SPEC 补充）

> 放在 `docs/STORAGE.md`。TradingView 导出数据的目录见 `docs/TRADINGVIEW.md` 第2节（已同步到下方第2节）。本文件补充并替代 `docs/SPEC.md` 第3节中 `data/`、`output/` 的目录设计，以及第7节的输出位置。其他内容不变。

---

## 1. 设计原则

1. **四类数据分开存放**：可重新下载的缓存、人工录入与收集的资料、程序分析结果、统计汇总。
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
│   ├── cache/                          # 可重新下载的接口缓存，不提交 git
│   │   ├── yahoo/
│   │   ├── fred/
│   │   ├── treasury/
│   │   └── cboe/
│   ├── legacy/                         # 截图时代的历史记录，提交 git
│   │   └── backtest_record_legacy.xlsx # 样本1至4（import-legacy 的输入）
│   ├── manual/                         # 人工录入的数据，提交 git
│   │   ├── breadth.csv                 # date,s5fi,s5tw,note（每个交易日一行）
│   │   ├── outcomes.csv                # 风险事件标签（见第5节）
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
│   └── market_risk.sqlite              # 统计用数据库，不提交 git（可由 results/ 重建）
├── reports/                            # 统计汇总与导出，提交 git
│   ├── backtest_history.xlsx           # 样本汇总表（格式同现有 Excel）
│   ├── backtest_stats.md               # 第9.4节指标统计
│   ├── tradingview_crosscheck.md       # TradingView 与接口数据的交叉校验
│   └── daily/                          # 前瞻逐日汇总（按月一个文件）
│       └── 2026-10.csv
└── config/
    └── symbols.yaml                    # 标的登记表（docs/TRADINGVIEW.md 第4节）
```

命名规则：
- 日期一律用 `YYYY-MM-DD`；按年份建一层目录，避免单个目录下文件过多。
- 股票代码一律大写；大盘统一用 `MARKET`。
- 运行目录名：`run_<UTC 运行时间 YYYYMMDDTHHMMSSZ>_<git commit 前7位>`，例如 `run_20260927T021530Z_a1b2c3d`；同一秒内重复运行时依次加后缀 `_2`、`_3`……（2026-09-26 确认）。
- 基准日仍按美东时间理解；运行时间只用于区分运行。
- Windows 下不使用符号链接，用 `official.json` 指针文件代替。
- SPEC 第7节的 `output/history.csv` 已取消，其功能由数据库 `runs`、`totals` 表和 `reports/` 覆盖。

### 2.1 正式记录 `official.json`（2026-09-26 确认）

- 内容：`run_id`、`set_at_utc`、`set_by`（`auto` / `manual` / `import-legacy`）、`reviewed`（true/false）、`reviewed_at_utc`。
- **自动设定**：某个基准日第一次运行时，同时满足以下条件才自动设为正式记录，且 `reviewed=false`：
  1. 该对象、框架、基准日尚无正式记录；
  2. 运行状态为 `complete` 或 `pending`（`failed` 不设）；
  3. git 工作区干净（无未提交的修改）。实现口径（待用户确认）：只检查代码与配置，`results/`、`reports/`、`data/`、`db/` 下的变动不算"未提交的修改"——否则结果目录本身要提交 git，每次运行后工作区都会变"脏"，只有提交后的第一次运行能自动设定。
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
- 配置开关的取值（如 `three_segment.d1_includes_t_minus_20`）
- 每个数据源的来源 URL、下载时间、数据截止日期
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

要求：
- 数据库可以随时由 `results/`、`data/manual/`、`data/materials/` 重建（提供命令 `rebuild-db`）。
- 同一对象、框架、基准日只能有一个正式记录（`is_official`）。

---

## 5. 风险事件标签的隔离

- 标签单独存放在 `data/manual/outcomes.csv` 和数据库 `outcomes` 表。字段：`subject`、`base_date`、`window_start`、`window_end`、`spx_min_close_drawdown`、`qqq_min_close_drawdown`、`is_event`、`source`（`manual` / `computed`）、`entered_at`。
- 可以由程序在结果窗口结束后自动计算（只用收盘价，按 SOP 9.3），也可以人工录入；两者不一致时报告差异。
- **隔离要求**：`scoring/`、`report.py` 中生成 prompt 的部分、`data/snapshot.py` 都不得读取 `outcomes`。写一个测试：扫描这些模块的导入和文件读取，确认不引用标签文件和标签表。
- 结果窗口尚未结束时，不得计算标签。

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
- `outcomes.csv` 在第5节字段之外增加 `event_date`（首次达到门槛的日期），用于计算"命中样本的提前量"。同一样本同时有手工与程序标签时，统计以手工标签为准。
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
