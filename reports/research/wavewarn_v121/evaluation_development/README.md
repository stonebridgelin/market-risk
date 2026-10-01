# 开发期 v1.2.1 评价输出说明（开发期工程运行，未锁定）

只使用截至 2016-12-30 的开发期输入；不是历史预警效果。t0=2009-10-01，τ=2009-12-31，j₀=2009-12-31。

## `daily_asset_intervals.csv`（入库为 `.csv.gz`）：每个模型设定 × 资产 × 区间一行

- `date`→`next_date`：价格区间的起点与终点（交易日）。`close`、`next_close`：两端的不复权收盘价（指数点或美元）。
- `signal_light`：当日收盘产生的信号灯色 S_j。`system_executed_light`：当日收盘执行的系统灯色 S_{j−1}，决定本区间暴露。`executed_light`：该资产当日收盘后的实际灯色（缺价日保持前值）。
- `exposure`：本区间暴露 e，绿=1、黄=η=0.5、红=0（无量纲）。
- `log_return` = ln(next_close ÷ close)（无量纲，对数收益）。
- `dangerous`：是 = 区间属于该资产危险区间 [P, Tr)；否；未定 = 右截尾或尾段，不计价格损失。
- `drawdown_increment`：ΔX，超过 2% 噪声下限的回撤纪录增量（对数，无量纲）。
- `danger_loss_raw` = κ_D × e × max(−r, 0)（仅危险区间）；`drawdown_loss_raw` = κ_0 × e × ΔX（仅区间外）；`opportunity_loss_raw` = β × (1−e) × r（仅区间外，可为负）。三者是该资产**未乘权重**的原始值。
- `weight`：资产权重 w_a。`weighted_price_loss` = w_a × (危险项 + 回撤项 + 机会项)。
- `system_switch_count`：当日系统执行切换次数（0 或 1），两条资产行相同。
- `switch_cost_share` = γ × 计费切换次数 ÷ 2，γ=0.0050：按两条资产行均分，与 w_a 无关。窗口末日的切换两行都显示切换标记，但分摊额为 0（`terminal_switch_unbilled`=是）。
- `漏报罚项（危险区间全程满暴露）` = μ × 该资产在本行触发的事件数。罚项属于某一资产的某一事件，整笔记在该资产区间 Tr−1 的那一行，另一资产同日记 0，不乘资产权重。**当前 μ=0，该列恒为 0。**
- `row_total` = `weighted_price_loss` + `switch_cost_share` + `漏报罚项（危险区间全程满暴露）`。同一日期两行合计等于系统日度主损失；全部行合计等于该设定的主损失（相加次序不同带来的 Decimal 末位舍入差不超过 1E-20）。配对检验使用两行合计的系统级损失差。
- `excluded_reason`：跨缺价区间、右截尾（寻底）、尾段（寻峰）或窗口末日。被排除区间的价格项为 0，切换分摊额照常计入。
- `data_status`、`active_channels`、`state_reason`：当日系统数据状态、激活的通道、状态机给出的原因。

## 其他文件

- `model_summary.csv`：每个设定的主损失与分项、执行非绿天数、计费切换次数、被排除区间数、沿用日数。
- `ranking.csv`：每类模型内的名次（开发期工程运行，未锁定）；`reference_rows.csv`：始终绿、黄、红三条参照行。
- `convergence.csv`：各设定系统收敛日与共同的 t0、τ、j₀。
- `event_scope.csv`、`event_scope_counts.csv`：每个资产事件的纳入情形与件数。
- `event_ledger.csv`、`event_class_summary.csv`：事件账逐件（资产事件与合并事件）与五类件数。
- `alert_ledger.csv`、`alert_summary.csv`、`yearly_alert.csv`：警报账逐段、汇总与年度统计（年度非绿天数按信号灯色，年度切换含窗口末日那一次）。
- `missing_audit.csv`：沿用日与被排除区间的数量及首末日期。
- `n_exit_costs.csv`、`n_exit_cost_summary.csv`：N 各设定的 E2 退出代价描述，不作范围判定。
- `开发期评价报告.md`：主损失、事件账、警报账、缺值四部分。

## 逐日明细的入库形式

- 程序写出未压缩的 `daily_asset_intervals.csv`，SHA-256：`7587646EDC86D7901236681AF16B6431A587281FAF8AD76055AF42256CE157A9`。
- 入库的是用 Git 自带 gzip 执行 `gzip -n daily_asset_intervals.csv` 得到的 `daily_asset_intervals.csv.gz`（不含文件名与时间戳）。压缩后的 SHA-256 在压缩后追加于本节末尾，不由程序生成。
- 解压：`gzip -dk daily_asset_intervals.csv.gz`，解压后的 SHA-256 应与上面一致。
- 重算：把本目录改名或移走后运行 `uv run market-risk wavewarn evaluate-development`，再比较新写出的 `daily_asset_intervals.csv` 的 SHA-256。
- 压缩后 `daily_asset_intervals.csv.gz` 的 SHA-256：`DF62698BA7E4F82C24674476CF64BEBC82ACA047BF254341ADAB1668B301157C`（gzip 1.13，8,084,401 字节；本行在压缩后追加）。
