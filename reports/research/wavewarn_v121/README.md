# v1.2.1 开发期工程诊断

本目录只含截至 2016-12-30 的研究标签和工程诊断，不含 2017—2022 验证期或 2023 年起保留期数据，也不含历史预警效果。主损失与开发期名次只在子目录 `evaluation_development/`（2026-10-01 新增，开发期工程运行，未锁定），其余文件不含主损失、排名或选参。

- `evaluation_development/`：45 组设定（P0、P1、N 及“N 去 B、DV”分解）的开发期评价，唯一退出版本 E2；文件与列说明见该目录的 `README.md`，由 `uv run market-risk wavewarn evaluate-development` 生成，目录已存在时拒绝覆盖。
- `E退出代价_校准.md` 与 `E退出校准_*.csv`：固定延迟参照的描述性校准，由 `uv run market-risk wavewarn calibrate-exit-development` 生成。

- `zz_events_development.csv`：SPX 4%/5%、QQQ 5%/6.5% 独立 ZZ 事件；`right_censored=是` 表示开发期末尚未确认结束。
- `zz_merged_development.csv`：按闭区间 `[高点,低点]` 传递合并的研究事件，仅供事件账计数。
- `zz_unknown_development.csv`：按资产逐区间起点列出右截尾（寻底）与尾段（寻峰）的未定归属；标签截止日为 2016-12-30，两资产可以处于不同状态。
- `anchor_126_audit.csv`：P 与 PR 分列的126日锚点审计，只记录63日未触发而126日会触发的日期；不改变通道。
- `development_state_diagnostics.csv`：全部预登记 K、θ 候选的 P0 与 P1-E1/E2/E3 逐日灯色。E 版本并列输出用于实现核查，不代表选择；N 四侧 B/DV 开关仍为空，故无 N 灯色。
- `independent_channels.csv`：P、PR、B、DV、BW、V 独立状态及原因。B 的 q=0.10/0.20 分开保存；没有组合 N。
- `convergence_diagnostics.csv`：上述 P0/P1 各候选通道与系统的三态收敛日。统一损失起点 τ 还需要 N 的显式 B/DV 去留与负责人业务参数，当前不计算。
- `input_coverage.csv`：每条序列的起止、观测数、未开始交易日、开始后交易日缺口、非 NYSE 额外行。额外行不填补交易日缺口。
- `development_missing_audit.csv`：t0 后独立通道无效及系统“沿用”的计数；目前仅表头表示这批诊断没有此类日子，不代表历史原始数据完全无缺口。
- `exit_cost_events_development.csv`：E1/E2/E3 × 九组 P1 × 两资产的逐事件描述；包括统一 τ_E、四类分母归属、①a/①b/②/③、g=Tr 与 R。未选择参数或比较模型主损失。
- `green_executions_development.csv`：每次绿灯执行切换的信号日、执行日、两日价格、相对最终低点的事后值、半山腰跌幅和在本事件中的角色；跨事件统计须按场景、资产、执行日去重。
- `exit_cost_summary_development.csv`、`E1_E2_E3退出代价开发期描述.md`：54 组场景/资产的类别、发生率、跌幅和 R 分布；主 R 含 g=Tr 的零值，另列剔除 g=Tr 的对照。类别③只列保守超限数，不作尚未选定参数的接受范围判定。
- `exit_cost_class1a_ledger_cross.csv`：①a 与五类事件账的交叉明细；P−20 早于 τ_E 时标状态窗口不足。
- `superseded/development_state_diagnostics_t0_entry_only_preliminary.csv`：在 t0 回看范围明确前形成的初步诊断，t0=2009-09-18。负责人随后确认 t0 必须包含 V 通道10日退出回看和广度3日修复回看，正式文件因此从 2009-10-01 开始。旧文件移入 `superseded/` 留存审计轨迹，不用于后续研究。

使用仓库根目录运行 `uv run python -c "from pathlib import Path; from market_risk.wavewarn.development import run_development_diagnostics; print(run_development_diagnostics(Path('.')))"` 可重算正式开发期文件。程序从研究专用的 Cboe VIX3M 开发期副本读取期限结构，不与原口径前瞻报告的 TradingView VIX3M 混用。

随后运行 `uv run python -c "from pathlib import Path; from market_risk.wavewarn.exit_costs import run_development_exit_costs; print(run_development_exit_costs(Path('.')))"` 可重算退出代价明细与描述报告。它只使用上述开发期标签、价格、状态与收敛诊断；不会计算主损失或 N 的灯色。
