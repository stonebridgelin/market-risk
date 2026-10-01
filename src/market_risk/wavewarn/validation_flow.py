"""v1.4 验证期评价流程（纯计算）：评价窗口、全部运行对象、主检验与大跌事件表。

同一套流程用于两种窗口：
- 验证期：模型自 t0 连续运行至 2022-12-30；计入区间起点自 2017-01-03 起；回撤参考高点在 2017-01-03 重置；
  ē 按验证期计入区间计算；事件按 P ≥ 2017-01-03 归属，事件账所需高点前 20 天的灯色可落在 2016 年。
  只在锁定之后由正式命令运行一次。
- 开发期演练：窗口为开发期（起点 j₀，事件门槛 τ），只用来发现流程问题，不构成任何证据。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import PairedParameters
from market_risk.wavewarn.config_v14 import ValidationConfig
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    ScopeRule,
    development_scope_rule,
    evaluate_candidate,
    period_labels,
    validation_scope_rule,
)
from market_risk.wavewarn.evaluation_tables import ModelSummary, Row, missing_rows, model_summary
from market_risk.wavewarn.evaluation_v14 import exit_metrics, setting_label
from market_risk.wavewarn.feasibility import AssetRebound
from market_risk.wavewarn.feasibility_v14 import GreenDelay
from market_risk.wavewarn.labels_zz import MergedZZEvent, UnknownLabels, ZZEvent, merge_zz_events
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.main_test import DailyDifference, MainTestResult, daily_differences, run_main_test
from market_risk.wavewarn.period_stats import period_row
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result
from market_risk.wavewarn.v14_model import FULL

REHEARSAL_TITLE = "开发期演练，不是验证期结果，不构成任何证据"
VALIDATION_TITLE = "验证期评价（锁定后只运行一次）"


@dataclass(frozen=True)
class WindowSpec:
    """评价窗口：计入区间起点、事件归属门槛、事件账规则与前后两半的分界。"""

    title: str
    rehearsal: bool
    first_interval: dt.date
    event_floor: dt.date          # 退出代价、转绿延迟与大跌事件表纳入 P ≥ 此日的事件
    rule: ScopeRule               # 事件账与“危险区间全程满暴露”的纳入规则
    split: dt.date                # 区间起点早于此日的归前半
    split_note: str


def validation_window(prepared: PreparedEvaluation, config: ValidationConfig,
                      params: PairedParameters) -> WindowSpec:
    """正式验证期窗口；前后两半以登记的分界日划分。"""
    if prepared.inputs.days[-1] != config.end or config.first_interval not in prepared.inputs.days:
        raise ValueError("验证期窗口要求输入恰好截至验证期末，且起点是交易日")
    return WindowSpec(VALIDATION_TITLE, False, config.first_interval, config.first_interval,
                      validation_scope_rule(prepared.tau), params.half_split,
                      f"前后两半以 {params.half_split} 分界（登记）")


def rehearsal_window(prepared: PreparedEvaluation) -> WindowSpec:
    """开发期演练窗口：起点 j₀、事件门槛 τ；按计入区间数对半分（演练专用分界）。"""
    days = prepared.inputs.days
    start = days.index(prepared.first_loss_day)
    intervals = len(days) - 1 - start
    split = days[start + intervals // 2]
    return WindowSpec(REHEARSAL_TITLE, True, prepared.first_loss_day, prepared.tau,
                      development_scope_rule(prepared.tau, prepared.first_loss_day), split,
                      f"演练专用分界：按计入区间数对半分，前 {intervals // 2} 个、后 {intervals - intervals // 2} 个，"
                      f"分界日 {split}；正式验证期以登记的日期分界")


@dataclass(frozen=True)
class WindowRow:
    """一个设定在评价窗口内的结果（描述性）。"""

    candidate: Candidate
    summary: ModelSummary
    timing: TimingResult
    rebounds: Mapping[str, AssetRebound]
    delays: Mapping[str, GreenDelay]


@dataclass(frozen=True)
class BigDrop:
    """SPX 的一次大跌事件及各行在其 [P, Tr) 期间的执行暴露与回撤。"""

    event: ZZEvent
    decline: Decimal
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class WindowEvaluation:
    prepared: PreparedEvaluation              # first_loss_day 为窗口的第一个计入区间起点
    window: WindowSpec
    events: Mapping[str, tuple[ZZEvent, ...]]
    merged: tuple[MergedZZEvent, ...]
    unknown: UnknownLabels
    references: tuple[ReferenceRow, ...]
    rows: tuple[WindowRow, ...]
    locked_states: CandidateStates
    locked: CandidateEvaluation
    differences: tuple[DailyDifference, ...]
    main_test: MainTestResult
    big_drops: tuple[BigDrop, ...]
    missing: tuple[Row, ...]


def window_prepared(prepared: PreparedEvaluation, window: WindowSpec) -> PreparedEvaluation:
    """模型自 t0 连续运行；只把第一个计入区间（回撤参考高点在此重置）换成窗口起点。"""
    if window.first_interval < prepared.first_loss_day:
        raise ValueError("评价窗口起点不得早于 j₀")
    return PreparedEvaluation(prepared.config, prepared.inputs, prepared.t0, prepared.tau,
                              window.first_interval, prepared.states)


def locked_states(prepared: PreparedEvaluation, config: ValidationConfig) -> CandidateStates:
    """锁定的 v1.4 设定；只运行这一组，不重新选择。"""
    found = [states for states in prepared.states
             if states.candidate.model == FULL and states.candidate.k == config.locked_k
             and states.candidate.theta_p == config.locked_theta]
    if len(found) != 1:
        raise ValueError("找不到唯一的锁定设定")
    return found[0]


def window_rows(prepared: PreparedEvaluation, window: WindowSpec, events: Mapping[str, Sequence[ZZEvent]],
                unknown: UnknownLabels, references: Sequence[ReferenceRow]
                ) -> tuple[tuple[WindowRow, ...], tuple[Row, ...]]:
    """全部运行对象（锁定设定与登记第六节的描述性对照）在窗口内的结果与缺值审计行。"""
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    rows: list[WindowRow] = []
    missing: list[Row] = []
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        rebounds, delays = exit_metrics(prepared, states, events, window.event_floor)
        candidate = states.candidate
        rows.append(WindowRow(candidate, model_summary(states, evaluated),
                              timing_result(evaluated, eta, green, red), rebounds, delays))
        missing.extend(missing_rows((setting_label(candidate), candidate.k, candidate.theta_p,
                                     candidate.q if candidate.q is not None else ""), evaluated))
    return tuple(rows), tuple(missing)


def big_drop_events(events: Sequence[ZZEvent], floor: dt.date, threshold: Decimal) -> tuple[ZZEvent, ...]:
    """高点 P 不早于 floor、按收盘价 1 − 低点 ÷ 高点 ≥ threshold 的事件；期末右截尾事件也列出（暂定低点）。"""
    return tuple(event for event in events
                 if event.peak_date >= floor and 1 - event.trough_close / event.peak_close >= threshold)


def big_drops(events: Sequence[ZZEvent], named: Sequence[tuple[str, CandidateEvaluation]], eta: Decimal,
              floor: dt.date, threshold: Decimal) -> tuple[BigDrop, ...]:
    """每个大跌事件取区间起点在 [P, Tr) 内的区间，两个资产共用，列各行的平均执行暴露与按 Σe·r 计算的最大回撤。"""
    result = []
    for event in big_drop_events(events, floor, threshold):
        label = f"{event.peak_date} 至 {event.trough_date}" + ("（暂定低点）" if event.right_censored else "")
        rows = tuple(period_row(label, name, evaluated, eta, event.peak_date, event.trough_date)
                     for name, evaluated in named)
        result.append(BigDrop(event, 1 - event.trough_close / event.peak_close, rows))
    return tuple(result)


def evaluate_window(base: PreparedEvaluation, window: WindowSpec, config: ValidationConfig,
                    params: PairedParameters) -> WindowEvaluation:
    """标签 → 参照行 → 全部运行对象 → 主检验（锁定设定对 200 日均线）→ 大跌事件表。"""
    prepared = window_prepared(base, window)
    events, unknown = period_labels(prepared.config, prepared.inputs, prepared.inputs.days[-1])
    merged = merge_zz_events(tuple(event for symbol in SYMBOLS for event in events[symbol]))
    references = reference_rows(prepared, events, unknown, config.model.mr_window)
    rows, missing = window_rows(prepared, window, events, unknown, references)
    chosen = locked_states(prepared, config)
    locked = evaluate_candidate(prepared, chosen, events, unknown)
    timing = next(row.timing for row in rows if row.candidate == chosen.candidate)
    green, average, red = references[0], references[3], references[2]
    differences = daily_differences(locked, average.evaluated, green.evaluated, red.evaluated,
                                    timing.mean_exposure, average.timing.mean_exposure)
    test = run_main_test(differences, window.split, merged, params, timing.mean_exposure,
                         average.timing.mean_exposure, green.evaluated.total_loss, red.evaluated.total_loss)
    eta = configured_loss_settings(prepared.config).parameters.eta
    named = (("选定的 v1.4 设定", locked), (average.name, average.evaluated), (green.name, green.evaluated))
    drops = big_drops(events["SPX"], named, eta, window.event_floor, config.big_drop_threshold)
    return WindowEvaluation(prepared, window, events, merged, unknown, references, rows, chosen, locked,
                            differences, test, drops, missing)
