"""v1.3 描述性诊断的编排（纯计算）：转绿瓶颈、亮灯原因、200日均线参照的事件表现、逐年对照。

不改变任何登记：不选参、不锁定、不运行验证期。需要“代表设定”的地方一律用网格中位设定
K=5、θ_P=2%、q=10%，并另附全部设定的汇总。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.bottleneck import EventBottleneck, setting_bottlenecks
from market_risk.wavewarn.condition_trace import (
    ABOVE_MA50,
    ALL_QUIET,
    NONPRICE_QUIET,
    PRICE_CLEAR,
    Q_OK,
    REPAIRED_QQQ,
    REPAIRED_SPX,
    ConditionDay,
    condition_trace,
    required_conditions,
)
from market_risk.wavewarn.config import ChannelSelection
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    FeatureSet,
    PreparedEvaluation,
    candidate_channel_setup,
    evaluate_candidate,
)
from market_risk.wavewarn.evaluation_tables import n_exit_costs
from market_risk.wavewarn.evaluation_v13 import display_model, display_version, setting_key
from market_risk.wavewarn.extended_history import window_exit_costs
from market_risk.wavewarn.feasibility import N_PRIME_MODEL, AssetRebound, asset_rebound
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.lighting_reasons import ReasonCounts, non_green_reasons
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.period_stats import Row, period_row, yearly_rows
from market_risk.wavewarn.state_machine import is_price_channel
from market_risk.wavewarn.timing import ReferenceRow, ma200_states, reference_rows

MEDIAN_K, MEDIAN_THETA, MEDIAN_Q = 5, Decimal("0.02"), Decimal("0.10")
BOTTLENECK_GROUPS = (("P1", "X2"), (N_PRIME_MODEL, "X2"), ("N", "X2"), ("P1", "E2"))
REASON_GROUPS = (("P1", "E2"), ("P1", "X2"), ("N", "E2"), ("N", "X2"), (N_PRIME_MODEL, "X2"))
YEARLY_GROUPS = (("P0", "P0护栏"), ("P1", "E2"), ("P1", "X2"), (N_PRIME_MODEL, "X2"), ("N", "X2"))
BEAR_MARKETS = (("2000-03-24 至 2002-10-09", dt.date(2000, 3, 24), dt.date(2002, 10, 9)),
                ("2007-10-09 至 2009-03-09", dt.date(2007, 10, 9), dt.date(2009, 3, 9)))


def is_median(candidate: Candidate) -> bool:
    """网格中位设定：K=5、θ_P=2%，N 另取 q=10%。"""
    return (candidate.k == MEDIAN_K and candidate.theta_p == MEDIAN_THETA
            and candidate.q in (None, MEDIAN_Q))


def group_of(candidate: Candidate) -> tuple[str, str]:
    return candidate.model, display_version(candidate)


def group_label(candidate: Candidate) -> str:
    return f"{display_model(candidate.model)}·{display_version(candidate)}"


def listed_conditions(version: str, has_nonprice: bool) -> tuple[str, ...]:
    """报告首次成立天数的条件：X2 列 (1)–(6)；E2 用“全部通道连续5天”代替 (3)，(2) 照列作参考。"""
    if version == "E2":
        return Q_OK, PRICE_CLEAR, ALL_QUIET, REPAIRED_SPX, REPAIRED_QQQ, ABOVE_MA50
    return (Q_OK, PRICE_CLEAR, *((NONPRICE_QUIET,) if has_nonprice else ()), REPAIRED_SPX, REPAIRED_QQQ,
            ABOVE_MA50)


@dataclass(frozen=True)
class SettingDiagnostics:
    candidate: Candidate
    evaluated: CandidateEvaluation
    listed: tuple[str, ...]
    candidates: tuple[str, ...]
    bottlenecks: tuple[EventBottleneck, ...]     # 只对瓶颈分解的四组计算，其余为空
    reasons: ReasonCounts | None                 # 只对亮灯分解的五组计算


@dataclass(frozen=True)
class TraceContext:
    """重放条件所需的共同输入。"""

    prepared: PreparedEvaluation
    features: FeatureSet
    ratios: tuple[Decimal | None, ...]
    selection: ChannelSelection
    events: Mapping[str, Sequence[ZZEvent]]
    unknown: UnknownLabels


def setting_trace(context: TraceContext, states: CandidateStates) -> tuple[tuple[ConditionDay, ...], bool]:
    """重放一个设定的逐日条件，并核对灯色与正式状态序列逐日相同。"""
    prepared, candidate = context.prepared, states.candidate
    fixed = prepared.config.fixed_parameters()
    setup = candidate_channel_setup(candidate, prepared.config, prepared.inputs.days, context.features,
                                    context.ratios, context.selection)
    trace = condition_trace(prepared.inputs.days, setup.channels, setup.ready, prepared.t0,  # type: ignore[arg-type]
                            candidate.k, setup.version, fixed.quiet_red_days,  # type: ignore[arg-type]
                            fixed.quiet_all_days)
    if tuple(day.light for day in trace) != tuple(row.light for row in states.rows[1:]):
        raise ValueError(f"条件追踪的灯色与状态序列不一致：{candidate.key}")
    return trace, any(not is_price_channel(name) for name in setup.channels)


def setting_diagnostics(context: TraceContext, states: CandidateStates) -> SettingDiagnostics:
    """一个设定的评价结果、瓶颈（若属于四组）与亮灯原因（若属于五组）。"""
    prepared, candidate = context.prepared, states.candidate
    version = display_version(candidate) if candidate.model != "P0" else "P0"
    trace, has_nonprice = setting_trace(context, states)
    evaluated = evaluate_candidate(prepared, states, context.events, context.unknown)
    listed = listed_conditions(version, has_nonprice)
    required = required_conditions(version, has_nonprice)
    bottlenecks: tuple[EventBottleneck, ...] = ()
    if group_of(candidate) in BOTTLENECK_GROUPS:
        next_t0 = {(symbol, event.peak_date): events[index + 1].t0_date if index + 1 < len(events) else None
                   for symbol in SYMBOLS for events in (context.events[symbol],)
                   for index, event in enumerate(events)}
        costs = [cost for symbol in SYMBOLS
                 for cost in n_exit_costs(prepared, states, symbol, context.events[symbol], prepared.tau)]
        bottlenecks = setting_bottlenecks(trace, costs, next_t0, listed, required)
    reasons = (non_green_reasons(trace, evaluated.system_executed[:-1], evaluated.days[:-1], required)
               if group_of(candidate) in REASON_GROUPS else None)
    return SettingDiagnostics(candidate, evaluated, listed, required, bottlenecks, reasons)


def diagnosed_states(prepared: PreparedEvaluation) -> tuple[CandidateStates, ...]:
    """需要诊断的设定：瓶颈四组、亮灯五组的全部设定，加上逐年对照用的 P0。"""
    groups = set(BOTTLENECK_GROUPS) | set(REASON_GROUPS) | set(YEARLY_GROUPS)
    return tuple(states for states in prepared.states if group_of(states.candidate) in groups)


def ma200_rebounds(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]], window: int,
                   bounded: bool) -> dict[str, AssetRebound]:
    """200日均线参照的逐事件退出代价，口径与模型相同；bounded 表示补充历史窗口（Tr 不晚于窗口末日）。"""
    states = ma200_states(prepared, window)
    costs = {symbol: (window_exit_costs(prepared, states, symbol, events[symbol]) if bounded
                      else n_exit_costs(prepared, states, symbol, events[symbol], prepared.tau))
             for symbol in SYMBOLS}
    return {symbol: asset_rebound(symbol, costs[symbol]) for symbol in SYMBOLS}


def yearly_table(references: Sequence[ReferenceRow], models: Sequence[tuple[str, CandidateEvaluation]],
                 eta: Decimal) -> tuple[Row, ...]:
    """始终绿、200日均线与各模型逐年一行；T 用当年的 ē、L_G、L_R。"""
    green, red = references[0].evaluated, references[2].evaluated
    named = [(references[0].name, green), (references[3].name, references[3].evaluated), *models]
    return tuple(row for name, evaluated in named for row in yearly_rows(name, evaluated, green, red, eta))


def bear_market_table(references: Sequence[ReferenceRow], p0: CandidateEvaluation,
                      eta: Decimal) -> tuple[Row, ...]:
    """两次熊市期间：始终绿、200日均线、P0 中位设定的平均执行暴露、主损失与按暴露累计收益的最大回撤。"""
    named = ((references[0].name, references[0].evaluated), (references[3].name, references[3].evaluated),
             ("P0 中位设定", p0))
    return tuple(period_row(label, name, evaluated, eta, start, end)
                 for label, start, end in BEAR_MARKETS for name, evaluated in named)


@dataclass(frozen=True)
class DevelopmentDiagnostics:
    settings: tuple[SettingDiagnostics, ...]
    references: tuple[ReferenceRow, ...]
    ma200: Mapping[str, AssetRebound]
    median_rebounds: Mapping[str, Mapping[str, AssetRebound]]    # 各组中位设定的退出代价，作对照
    yearly: tuple[Row, ...]
    yearly_all: tuple[Row, ...]


def development_diagnostics(context: TraceContext, ma200_window: int) -> DevelopmentDiagnostics:
    """开发期四项诊断的全部结果。"""
    prepared = context.prepared
    eta = configured_loss_settings(prepared.config).parameters.eta
    references = reference_rows(prepared, context.events, context.unknown, ma200_window)
    settings = tuple(setting_diagnostics(context, states) for states in diagnosed_states(prepared))
    in_yearly = [item for item in settings if group_of(item.candidate) in YEARLY_GROUPS]
    by_group = {group: [item for item in in_yearly if group_of(item.candidate) == group]
                for group in YEARLY_GROUPS}
    medians = [(f"{group_label(item.candidate)} 中位设定", item.evaluated)
               for group in YEARLY_GROUPS for item in by_group[group] if is_median(item.candidate)]
    everything = [(setting_key(item.candidate), item.evaluated)
                  for group in YEARLY_GROUPS for item in by_group[group]]
    rebounds = {group_label(states.candidate): {
        symbol: asset_rebound(symbol, n_exit_costs(prepared, states, symbol, context.events[symbol], prepared.tau))
        for symbol in SYMBOLS}
        for states in diagnosed_states(prepared)
        if is_median(states.candidate) and group_of(states.candidate) in YEARLY_GROUPS}
    return DevelopmentDiagnostics(settings, references,
                                  ma200_rebounds(prepared, context.events, ma200_window, False), rebounds,
                                  yearly_table(references, medians, eta),
                                  yearly_table(references, everything, eta))


@dataclass(frozen=True)
class ExtendedDiagnostics:
    references: tuple[ReferenceRow, ...]
    ma200: Mapping[str, AssetRebound]
    p0_median: Mapping[str, AssetRebound]
    bear_markets: tuple[Row, ...]
    yearly: tuple[Row, ...]
    yearly_all: tuple[Row, ...]


def extended_diagnostics(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                         unknown: UnknownLabels, ma200_window: int) -> ExtendedDiagnostics:
    """补充历史（1999—2009）：只有不依赖广度的 P0 与 200日均线。"""
    eta = configured_loss_settings(prepared.config).parameters.eta
    references = reference_rows(prepared, events, unknown, ma200_window)
    evaluated = [(states, evaluate_candidate(prepared, states, events, unknown)) for states in prepared.states]
    median_states, median = next(item for item in evaluated if is_median(item[0].candidate))
    p0_rebounds = {symbol: asset_rebound(symbol, window_exit_costs(prepared, median_states, symbol,
                                                                   events[symbol])) for symbol in SYMBOLS}
    return ExtendedDiagnostics(
        references, ma200_rebounds(prepared, events, ma200_window, True), p0_rebounds,
        bear_market_table(references, median, eta),
        yearly_table(references, [("P0 中位设定", median)], eta),
        yearly_table(references, [(setting_key(states.candidate), item) for states, item in evaluated], eta))
