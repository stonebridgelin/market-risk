"""v1.4 开发期评价的编排（纯计算）：逐设定结果、可行条件、三级选择程序。

参与统一 τ 与 j₀ 的 21 组：v1.4 九组（候选）、“v1.4 去掉 MR”九组与 P1·E2、N′·X2、N·E2 的中位设定（描述性对照）。
只有 v1.4 九组参与选择。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config_v14 import V14Config, V14Limits
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    development_labels,
    evaluate_candidate,
)
from market_risk.wavewarn.evaluation_tables import ModelSummary, Row, missing_rows, model_summary, n_exit_costs
from market_risk.wavewarn.feasibility import AssetRebound, asset_rebound
from market_risk.wavewarn.feasibility_v14 import (
    GreenDelay,
    TierItem,
    TierTrace,
    condition_flags,
    green_delay,
    select_by_tiers,
)
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result
from market_risk.wavewarn.v14_model import DISPLAY, FULL, NO_TREND, PRICE_ONLY

V14_MODELS = (FULL, NO_TREND, PRICE_ONLY)


def setting_label(candidate: Candidate) -> str:
    """v1.4 的变体直接用名称；中位对照写成“模型·退出版本”。"""
    name = DISPLAY.get(candidate.model, candidate.model)
    return name if candidate.model in V14_MODELS else f"{name}·{candidate.exit_version}"


def setting_key(candidate: Candidate) -> str:
    return (f"{setting_label(candidate)}|K={candidate.k}|θ={candidate.theta_p}"
            f"|q={candidate.q if candidate.q is not None else '-'}")


@dataclass(frozen=True)
class V14Result:
    """一个设定的主损失、择时得分、转绿延迟、退出代价与三项条件。"""

    candidate: Candidate
    summary: ModelSummary
    timing: TimingResult
    rebounds: Mapping[str, AssetRebound]
    delays: Mapping[str, GreenDelay]
    flags: Mapping[str, bool]

    @property
    def key(self) -> str:
        return setting_key(self.candidate)


def exit_metrics(prepared: PreparedEvaluation, states: CandidateStates,
                 events: Mapping[str, Sequence[ZZEvent]]) -> tuple[dict[str, AssetRebound], dict[str, GreenDelay]]:
    """纳入门槛同 v1.3：高点 P 不早于统一 τ 的已确认事件。R 与转绿延迟来自同一批逐事件结果。"""
    rebounds: dict[str, AssetRebound] = {}
    delays: dict[str, GreenDelay] = {}
    for symbol in SYMBOLS:
        costs = n_exit_costs(prepared, states, symbol, events[symbol], prepared.tau)
        rebounds[symbol] = asset_rebound(symbol, costs)
        delays[symbol] = green_delay(symbol, prepared.inputs.days, costs)
    return rebounds, delays


def v14_result(prepared: PreparedEvaluation, states: CandidateStates, evaluated: CandidateEvaluation,
               events: Mapping[str, Sequence[ZZEvent]], eta: Decimal, green_loss: Decimal, red_loss: Decimal,
               limits: V14Limits) -> V14Result:
    timing = timing_result(evaluated, eta, green_loss, red_loss)
    rebounds, delays = exit_metrics(prepared, states, events)
    return V14Result(states.candidate, model_summary(states, evaluated), timing, rebounds, delays,
                     condition_flags(timing.non_green_share, delays, limits))


def tier_item(result: V14Result) -> TierItem:
    return TierItem(result.key, result.timing.score, result.summary.executed_non_green_days,
                    result.summary.billed_switches, result.candidate.order, result.flags)


@dataclass(frozen=True)
class V14Evaluation:
    """开发期评价的全部计算结果；写出由读写边界完成。"""

    events: Mapping[str, tuple[ZZEvent, ...]]
    unknown: UnknownLabels
    references: tuple[ReferenceRow, ...]
    results: tuple[V14Result, ...]
    missing: tuple[Row, ...]
    trace: TierTrace
    selected_states: CandidateStates
    selected: CandidateEvaluation


def evaluate_settings(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                      unknown: UnknownLabels, references: Sequence[ReferenceRow],
                      limits: V14Limits) -> tuple[tuple[V14Result, ...], tuple[Row, ...]]:
    """逐个设定评价，并收集缺值审计行。"""
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    results: list[V14Result] = []
    missing: list[Row] = []
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        results.append(v14_result(prepared, states, evaluated, events, eta, green, red, limits))
        candidate = states.candidate
        missing.extend(missing_rows((setting_label(candidate), candidate.k, candidate.theta_p,
                                     candidate.q if candidate.q is not None else ""), evaluated))
    return tuple(results), tuple(missing)


def evaluate_v14(prepared: PreparedEvaluation, config: V14Config) -> V14Evaluation:
    """参照行 → 全部 21 组 → 在 v1.4 九组中按三级程序选择 → 选定设定的逐日结果。"""
    events, unknown = development_labels(prepared)
    references = reference_rows(prepared, events, unknown, config.mr_window)
    results, missing = evaluate_settings(prepared, events, unknown, references, config.limits)
    trace = select_by_tiers([tier_item(row) for row in results if row.candidate.model == FULL], config.tiers)
    chosen = next(states for states in prepared.states if setting_key(states.candidate) == trace.selected.key)
    return V14Evaluation(events, unknown, references, results, missing, trace, chosen,
                         evaluate_candidate(prepared, chosen, events, unknown))
