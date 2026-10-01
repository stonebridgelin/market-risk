# v1.4 开发期评价输出说明（开发期工程运行，未锁定）

登记文件：`docs/research/波段预警研究规格_v1.4_修订登记.md`。只使用开发期数据，不是历史预警效果。t0=2009-10-01，τ=2009-12-31，j₀=2009-12-31。入口命令：`uv run market-risk wavewarn evaluate-v14-development`。

## 文件

- `settings_summary.csv`：全部 21 组设定（v1.4 九组、去掉 MR 九组、三个中位对照）的主损失与分项、ē、同暴露基准、T、非绿占比、SPX 与 QQQ 的转绿延迟（中位数、P75、最大值、类别①②③件数）、R（主口径与保守口径）、半山腰转绿、三项条件是否满足；`role` 区分候选与描述性对照。
- `selection_trace.csv`：三级选择程序的逐步追踪。`reference_rows.csv`：四条参照行。
- `convergence.csv`：各组系统收敛日与共同的 t0、τ、j₀。`event_scope_counts.csv`：事件纳入件数。
- `daily_selected.csv`（入库为 `.csv.gz`）：选定设定与四条参照行的逐日明细；列同 v1.3 的 `daily_selected.csv`（见 `reports/research/wavewarn_v13/evaluation_development/README.md`）。
- `event_ledger_selected.csv`、`event_class_summary_selected.csv`、`alert_ledger_selected.csv`、`alert_summary_selected.csv`、`yearly_alert_selected.csv`：选定设定的事件账与警报账。
- `missing_audit.csv`：全部设定的沿用日与被排除区间。`开发期评价报告.md`：报告（含披露声明）。
- `extended_history/`：补充历史（v1.4 纯价格版），由 `uv run market-risk wavewarn v14-extended-history` 写入。

## 逐日明细的入库形式

- 程序写出未压缩的 `daily_selected.csv`，SHA-256：`BA82944E649BA4B5E6938280B13D1DDD317BF68B5004F11A9F9516B43A213902`。
- 入库的是用 Git 自带 gzip 执行 `gzip -n daily_selected.csv` 得到的 `daily_selected.csv.gz`（不含文件名与时间戳）。压缩后的 SHA-256 在压缩后追加于本节末尾，不由程序生成。
- 解压：`gzip -dk daily_selected.csv.gz`，解压后的 SHA-256 应与上面一致。
- 重算：把本目录改名或移走后运行 `uv run market-risk wavewarn evaluate-v14-development`，再比较新写出的 `daily_selected.csv` 的 SHA-256。
- 压缩后 `daily_selected.csv.gz` 的 SHA-256：`7488317FE7F476AD1209A373FC72BE038F30EE1A87658662C0BD7F986CFB8CDF`（gzip 1.13，820535 字节；本行在压缩后追加）。
- 本目录是两处修正（f_BW、f_label）之后机械重跑的输出，只作纠错证据，不自动恢复候选资格；来源、哈希与原输出的去向见 `纠错重跑说明.md`（本行在压缩后追加）。
