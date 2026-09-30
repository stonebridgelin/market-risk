# v1.2.1 开发期工程诊断

本目录只含截至 2016-12-30 的研究标签和工程诊断，不含 2017—2022 验证期或 2023 年起保留期数据，也不含主损失、模型排名、选参或历史预警效果。

- `zz_events_development.csv`：SPX 4%/5%、QQQ 5%/6.5% 独立 ZZ 事件；`right_censored=是` 表示开发期末尚未确认结束。
- `zz_merged_development.csv`：按闭区间 `[高点,低点]` 传递合并的研究事件，仅供事件账计数。
- `development_state_diagnostics.csv`：全部预登记 K、θ 候选的 P0 与 P1-E1/E2/E3 逐日灯色。E 版本并列输出用于实现核查，不代表选择；N 四侧 B/DV 开关仍为空，故无 N 灯色。
- `independent_channels.csv`：P、PR、B、DV、BW、V 独立状态及原因。B 的 q=0.10/0.20 分开保存；没有组合 N。
- `convergence_diagnostics.csv`：上述 P0/P1 各候选通道与系统的三态收敛日。统一损失起点 τ 还需要 N 的显式 B/DV 去留与负责人业务参数，当前不计算。
- `input_coverage.csv`：每条序列的起止、观测数、未开始交易日、开始后交易日缺口、非 NYSE 额外行。额外行不填补交易日缺口。
- `development_missing_audit.csv`：t0 后独立通道无效及系统“沿用”的计数；目前仅表头表示这批诊断没有此类日子，不代表历史原始数据完全无缺口。
- `development_state_diagnostics_t0_entry_only_preliminary.csv`：在 t0 回看范围明确前形成的初步诊断，t0=2009-09-18。负责人随后确认 t0 必须包含 V 通道10日退出回看和广度3日修复回看，正式文件因此从 2009-10-01 开始。此文件只保留口径变更审计轨迹，不用于后续研究。

使用仓库根目录运行 `uv run python -c "from pathlib import Path; from market_risk.wavewarn.development import run_development_diagnostics; print(run_development_diagnostics(Path('.')))"` 可重算正式开发期文件。程序从研究专用的 Cboe VIX3M 开发期副本读取期限结构，不与原口径前瞻报告的 TradingView VIX3M 混用。
