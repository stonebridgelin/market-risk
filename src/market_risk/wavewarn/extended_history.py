"""v1.3 补充历史（纯计算）：2009-09-30 及以前的固定延迟校准与 P0 稳健性。只作描述，不参与选参与检验。

窗口边界按登记“实施细则”第 2 条：
- 固定延迟校准：纳入高点 P 不早于窗口起点、低点 Tr 不晚于窗口末日的已确认事件；窗口只限定纳入哪些事件，
  执行日 Tr+m 的价格可以取自窗口之后（截至开发期末）。
- P0 稳健性：状态与执行只算到窗口末日；危险标签取自完整标签；退出代价只纳入 P ≥ τ′ 且 Tr ≤ 窗口末日的事件。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market_risk.wavewarn.calibration import EventWithNext
from market_risk.wavewarn.config import ChannelSelection, WavewarnConfig
from market_risk.wavewarn.convergence import loss_start
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateStates,
    PreparedEvaluation,
    candidate_states,
    evaluate_candidate,
    first_loss_interval,
)
from market_risk.wavewarn.evaluation_tables import ModelSummary, Row, missing_rows, model_summary
from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.exit_costs import ExitCostEvent, exit_cost_for_event
from market_risk.wavewarn.feasibility import AssetRebound, asset_rebound
from market_risk.wavewarn.features import AssetFeatures, asset_features
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result

P0_REQUIRED = ("close", "high63", "drawdown63", "new_low20", "ma50")     # P0 所需输入
CROSSING_END = "跨窗口末端，不纳入"


def window_events(events: Sequence[ZZEvent], start: dt.date, end: dt.date) -> tuple[EventWithNext, ...]:
    """固定延迟校准纳入的事件：已确认、P ≥ start、Tr ≤ end；下一事件 T0 取自完整标签序列。"""
    result: list[EventWithNext] = []
    for index, event in enumerate(events):
        if event.right_censored or event.peak_date < start or event.trough_date > end:
            continue
        result.append((event, events[index + 1].t0_date if index + 1 < len(events) else None))
    return tuple(result)


def truncate_inputs(inputs: DevelopmentInputs, end: dt.date) -> DevelopmentInputs:
    """状态与执行只算到窗口末日：交易日轴与各序列都截断到 end（含）。"""
    if end not in inputs.days:
        raise ValueError("窗口末日不是交易日")
    return DevelopmentInputs(tuple(day for day in inputs.days if day <= end),
                             {name: {day: value for day, value in series.items() if day <= end}
                              for name, series in inputs.series.items()})


def p0_first_complete_day(days: Sequence[dt.date], spx: Sequence[AssetFeatures],
                          qqq: Sequence[AssetFeatures], start: dt.date) -> dt.date:
    """t0′：窗口内两资产的收盘价、63 日高点与回撤、20 日新低窗口、MA50 首次全部具备的交易日。"""
    for day, a, b in zip(days, spx, qqq, strict=True):
        if day >= start and all(getattr(item, name) is not None for item in (a, b) for name in P0_REQUIRED):
            return day
    raise ValueError("窗口内 P0 的输入未同时具备")


def prepare_p0_window(config: WavewarnConfig, inputs: DevelopmentInputs, start: dt.date) -> PreparedEvaluation:
    """九组 P0 的状态、收敛日与 τ′、j₀′（按原规则）；inputs 须已截断到窗口末日。

    SPX 的滚动窗口可以用到窗口起点之前的 SPX 价格（都早于当日）；QQQ 自 1999-03-10 才有价格。
    """
    fixed, sets = config.fixed_parameters(), config.candidate_sets()
    features = tuple(asset_features(inputs.days, inputs.series[symbol], {}, sets.q[0], fixed)
                     for symbol in SYMBOLS)
    t0 = p0_first_complete_day(inputs.days, features[0], features[1], start)
    grid = tuple(Candidate("P0", k, theta, None, index)
                 for index, (k, theta) in enumerate((k, theta) for k in sets.k for theta in sets.theta_p))
    unused = ChannelSelection(False, False, False, False)                 # P0 只有价格通道
    states = tuple(candidate_states(candidate, config, inputs, {sets.q[0]: (features[0], features[1])},
                                    (None,) * len(inputs.days), t0, unused) for candidate in grid)
    dates = [item.convergence_date for item in states]
    tau = loss_start(inputs.days, t0, dates, fixed)
    return PreparedEvaluation(config, inputs, t0, tau, first_loss_interval(inputs.days, tau, dates), states)


def window_exit_costs(prepared: PreparedEvaluation, states: CandidateStates, symbol: str,
                      events: Sequence[ZZEvent]) -> tuple[ExitCostEvent, ...]:
    """Tr 不晚于窗口末日的事件逐件计算；下一事件 T0 取自完整标签序列，窗口末日仍未转绿归类别③。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
    eta = prepared.config.require_business_parameters().eta
    executions = execute_asset(axis, tuple(row.light for row in states.rows), closes, eta)  # type: ignore[arg-type]
    return tuple(exit_cost_for_event(axis, executions, closes, event,
                                     events[index + 1].t0_date if index + 1 < len(events) else None,
                                     prepared.t0, prepared.tau)
                 for index, event in enumerate(events) if event.trough_date <= axis[-1])


def crossing_events(prepared: PreparedEvaluation, events: Sequence[ZZEvent]) -> tuple[ZZEvent, ...]:
    """高点在窗口内（且不早于 τ′）、低点在窗口之后的事件：单列，不纳入退出代价。"""
    end = prepared.inputs.days[-1]
    return tuple(event for event in events if prepared.tau <= event.peak_date <= end < event.trough_date)


@dataclass(frozen=True)
class P0WindowRow:
    """一组 P0 在补充历史窗口内的描述性结果。"""

    candidate: Candidate
    summary: ModelSummary
    timing: TimingResult
    rebounds: Mapping[str, AssetRebound]


@dataclass(frozen=True)
class P0WindowResult:
    references: tuple[ReferenceRow, ...]
    rows: tuple[P0WindowRow, ...]
    exit_costs: tuple[tuple[Candidate, ExitCostEvent], ...]
    crossing: Mapping[str, tuple[ZZEvent, ...]]
    missing: tuple[Row, ...]


def evaluate_p0_window(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                       unknown: UnknownLabels, ma200_window: int) -> P0WindowResult:
    """九组 P0 与四条参照行在窗口内的主损失、ē、T、非绿占比、半山腰转绿与 R。"""
    references = reference_rows(prepared, events, unknown, ma200_window)
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    rows: list[P0WindowRow] = []
    costs: list[tuple[Candidate, ExitCostEvent]] = []
    missing: list[Row] = []
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        own = {symbol: window_exit_costs(prepared, states, symbol, events[symbol]) for symbol in SYMBOLS}
        costs.extend((states.candidate, cost) for symbol in SYMBOLS for cost in own[symbol])
        rows.append(P0WindowRow(states.candidate, model_summary(states, evaluated),
                                timing_result(evaluated, eta, green, red),
                                {symbol: asset_rebound(symbol, own[symbol]) for symbol in SYMBOLS}))
        candidate = states.candidate
        missing.extend(missing_rows(("P0", candidate.k, candidate.theta_p, ""), evaluated))
    return P0WindowResult(references, tuple(rows), tuple(costs),
                          {symbol: crossing_events(prepared, events[symbol]) for symbol in SYMBOLS},
                          tuple(missing))

