"""开发期 v1.2.1 实现诊断统一入口；无标签评分回流或损失选参。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from market_risk.wavewarn.config import load_wavewarn_config
from market_risk.wavewarn.diagnostics import (
    write_convergence_diagnostics,
    write_development_diagnostics,
    write_development_missing_audit,
    write_independent_channel_diagnostics,
    write_input_coverage,
)
from market_risk.wavewarn.export import write_development_labels
from market_risk.wavewarn.features import asset_features
from market_risk.wavewarn.inputs import load_development_inputs


@dataclass(frozen=True)
class DevelopmentSummary:
    t0: dt.date
    state_rows: int
    independent_channel_rows: int
    convergence_rows: int
    asset_events: int
    merged_events: int
    missing_status_rows: int


def run_development_diagnostics(root: Path) -> DevelopmentSummary:
    """输出预登记候选的状态、收敛、标签与缺值审计；绝不计算主损失或排名。"""
    config = load_wavewarn_config(root / "config/wavewarn_v121.yaml")
    fixed = config.fixed_parameters()
    candidates = config.candidate_sets()
    config.paired_parameters()
    inputs = load_development_inputs(root, config.development_end())
    source = inputs.series
    spx_q10 = asset_features(inputs.days, source["SPX"], source["S5TW"], candidates.q[0], fixed)
    qqq_q10 = asset_features(inputs.days, source["QQQ"], source["NDTW"], candidates.q[0], fixed)
    spx_q20 = asset_features(inputs.days, source["SPX"], source["S5TW"], candidates.q[1], fixed)
    qqq_q20 = asset_features(inputs.days, source["QQQ"], source["NDTW"], candidates.q[1], fixed)
    output = root / "reports/research/wavewarn_v121"
    output.mkdir(parents=True, exist_ok=True)
    t0, state_count = write_development_diagnostics(inputs, spx_q10, qqq_q10,
                                                    output / "development_state_diagnostics.csv", fixed, candidates)
    _, channel_count = write_independent_channel_diagnostics(inputs, spx_q10, qqq_q10, spx_q20, qqq_q20,
                                                             output / "independent_channels.csv", fixed, candidates)
    _, convergence_count = write_convergence_diagnostics(inputs, spx_q10, qqq_q10,
                                                         output / "convergence_diagnostics.csv", fixed, candidates)
    asset_events, merged_events = write_development_labels(inputs, output, config)
    missing_count = write_development_missing_audit(output / "development_state_diagnostics.csv",
                                                    output / "independent_channels.csv",
                                                    output / "development_missing_audit.csv")
    write_input_coverage(inputs, output / "input_coverage.csv")
    return DevelopmentSummary(t0, state_count, channel_count, convergence_count,
                              asset_events, merged_events, missing_count)
