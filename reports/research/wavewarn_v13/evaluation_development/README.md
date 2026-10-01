# v1.3 开发期评价输出说明（开发期工程运行，未锁定）

登记文件：`docs/research/波段预警研究规格_v1.3_修订登记.md`。只使用开发期数据，不是历史预警效果。t0=2009-10-01，τ=2009-12-31，j₀=2009-12-31。

## 文件

- `settings_summary.csv`：全部 117 组设定（P0、P1×3、N×3、N′×3）的主损失与分项、ē（`mean_exposure`）、同暴露基准（`benchmark_loss`）、T（`timing_score`）、非绿占比、SPX 与 QQQ 的 R 中位数与 P75（主口径 `r_median`/`r_p75`，保守口径 `r_conservative_*`，“超限”表示涉及类别③）、各可行条件是否满足。
- `selection_trace.csv`：选择程序的逐步追踪。`reference_rows.csv`：始终绿、黄、红与 200 日均线四条参照行。
- `convergence.csv`：各组系统收敛日与共同的 t0、τ、j₀。`event_scope_counts.csv`：事件纳入件数。
- `daily_selected.csv`（入库为 `.csv.gz`）：只含选定的 P1、N（或 N′）与四条参照行的逐日明细。
- `event_ledger_selected.csv`、`event_class_summary_selected.csv`、`alert_ledger_selected.csv`、`alert_summary_selected.csv`、`yearly_alert_selected.csv`：选定模型的事件账与警报账。
- `missing_audit.csv`：全部设定的沿用日与被排除区间。`开发期评价报告.md`：报告（含披露声明）。

## `daily_selected.csv` 的列

设定列为 `model`、`exit_version`、`k`、`theta_p`、`q`（参照行的 `exit_version` 为“参照行”，其余三列为空）；其后各列的含义、单位与公式同 `reports/research/wavewarn_v121/evaluation_development/README.md`：
- `exposure` 由 `system_executed_light`（S_{j−1}）决定：绿 1、黄 0.5、红 0；`row_total` = `weighted_price_loss` + `switch_cost_share` + `漏报罚项（危险区间全程满暴露）`；同日两行合计为系统日度主损失。
- 由本文件可重算：主损失 L = 全部行 `row_total` 之和；ē = 计入区间（`next_date` 非空）上 `exposure` 的平均（取任一资产的行）；非绿占比 = 计入区间中 `system_executed_light` 非绿的比例；T = L − [ē·L_G + (1−ē)·L_R]，L_G、L_R 为始终绿、始终红两条参照行的 L。

## 逐日明细的入库形式

- 程序写出未压缩的 `daily_selected.csv`，SHA-256：`10A31AC43FF71F158C7786CBB43B5685DF86BC68B46B0641DEA259A09472DE40`。
- 入库的是用 Git 自带 gzip 执行 `gzip -n daily_selected.csv` 得到的 `daily_selected.csv.gz`（不含文件名与时间戳）。压缩后的 SHA-256 在压缩后追加于本节末尾，不由程序生成。
- 解压：`gzip -dk daily_selected.csv.gz`，解压后的 SHA-256 应与上面一致。
- 重算：把本目录改名或移走后运行 `uv run market-risk wavewarn evaluate-v13-development`，再比较新写出的 `daily_selected.csv` 的 SHA-256。
- 压缩后 `daily_selected.csv.gz` 的 SHA-256：`033D509497B42D7E1DEE7CFC6E4FDBC5DB11CEE2E5419195F1C2D25AB51706CD`（gzip 1.13，665774 字节；本行在压缩后追加）。
