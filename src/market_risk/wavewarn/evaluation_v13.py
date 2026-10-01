"""v1.3 开发期评价的编排（纯计算）：设定网格、逐设定结果、选择程序的输入。

设定为 P0（9 组，原护栏）与 P1、N、N′ 各乘 E2、X1、X2 三个退出版本，共 9 + 27 + 54 + 27 = 117 组；
全部参与统一 τ 与 j₀ 的计算。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.config import WavewarnConfig
from market_risk.wavewarn.config_v13 import FeasibilityLimits, V13Config
from market_risk.wavewarn.evaluation import (
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    development_labels,
    evaluate_candidate,
    prepare_grid,
)
from market_risk.wavewarn.evaluation_tables import (
    ModelSummary,
    Row,
    missing_rows,
    model_summary,
    n_exit_costs,
)
from market_risk.wavewarn.evaluation_tables import setting_prefix as table_prefix
from market_risk.wavewarn.feasibility import (
    N_PRIME_MODEL,
    AssetRebound,
    Feasibility,
    SelectionItem,
    SelectionTrace,
    asset_rebound,
    best_feasible,
    feasibility,
    select,
)
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent
from market_risk.wavewarn.loss import configured_loss_settings
from market_risk.wavewarn.timing import ReferenceRow, TimingResult, reference_rows, timing_result

VERSIONED_MODELS = ("P1", "N", N_PRIME_MODEL)
DISPLAY_MODEL = {N_PRIME_MODEL: "N′"}


def display_model(model: str) -> str:
    return DISPLAY_MODEL.get(model, model)


def display_version(candidate: Candidate) -> str:
    """P0 保持原护栏，没有退出版本。"""
    return "P0护栏" if candidate.model == "P0" else candidate.exit_version


def setting_key(candidate: Candidate) -> str:
    return (f"{display_model(candidate.model)}|{display_version(candidate)}|K={candidate.k}|θ={candidate.theta_p}"
            f"|q={candidate.q if candidate.q is not None else '-'}")


def v13_grid(config: WavewarnConfig, versions: Sequence[str]) -> tuple[Candidate, ...]:
    """P0 九组在前；其后每个退出版本依次列 P1、N、N′，组内按 K、θ_P、q 的登记顺序。"""
    sets = config.candidate_sets()
    result: list[Candidate] = [Candidate("P0", k, theta, None, index)
                               for index, (k, theta) in enumerate((k, theta) for k in sets.k
                                                                  for theta in sets.theta_p)]
    for version in versions:
        for model in VERSIONED_MODELS:
            for k in sets.k:
                for theta in sets.theta_p:
                    for q in (sets.q if model == "N" else (None,)):
                        result.append(Candidate(model, k, theta, q, len(result), version))  # type: ignore[arg-type]
    return tuple(result)


def prepare_v13(config: V13Config, inputs: DevelopmentInputs) -> PreparedEvaluation:
    """统一 τ 与 j₀ 按原规则取全部 117 组的系统收敛日。"""
    return prepare_grid(config.base, inputs, v13_grid(config.base, config.exit_versions))


@dataclass(frozen=True)
class SettingResult:
    """一个设定的主损失、择时得分、退出代价分布与可行条件。"""

    candidate: Candidate
    summary: ModelSummary
    timing: TimingResult
    rebounds: Mapping[str, AssetRebound]
    feasibility: Feasibility

    @property
    def key(self) -> str:
        return setting_key(self.candidate)


def setting_rebounds(prepared: PreparedEvaluation, states: CandidateStates,
                     events: Mapping[str, Sequence[ZZEvent]]) -> dict[str, AssetRebound]:
    """R 的事件纳入门槛沿用 exit_costs：高点 P 不早于统一 τ 的已确认事件。"""
    return {symbol: asset_rebound(symbol, n_exit_costs(prepared, states, symbol, events[symbol], prepared.tau))
            for symbol in SYMBOLS}


def setting_result(prepared: PreparedEvaluation, states: CandidateStates, evaluated: CandidateEvaluation,
                   events: Mapping[str, Sequence[ZZEvent]], eta: Decimal, green_loss: Decimal,
                   red_loss: Decimal, limits: FeasibilityLimits) -> SettingResult:
    """汇总一个设定；可行条件只用非绿占比与主口径的 R。"""
    timing = timing_result(evaluated, eta, green_loss, red_loss)
    rebounds = setting_rebounds(prepared, states, events)
    return SettingResult(states.candidate, model_summary(states, evaluated), timing, rebounds,
                         feasibility(timing.non_green_share, rebounds, limits))


def selection_item(result: SettingResult) -> SelectionItem:
    candidate = result.candidate
    return SelectionItem(result.key, candidate.model, display_version(candidate), result.timing.score,
                         result.summary.executed_non_green_days, result.summary.billed_switches,
                         candidate.order, result.feasibility.feasible)


def best_feasible_p0(results: Sequence[SettingResult]) -> SelectionItem | None:
    """P0 只作描述：标出可行设定中 T 最小的一组，不进入选择程序。"""
    return best_feasible([selection_item(row) for row in results if row.candidate.model == "P0"])


@dataclass(frozen=True)
class V13Evaluation:
    """开发期评价的全部计算结果；写出由读写边界完成。"""

    events: Mapping[str, tuple[ZZEvent, ...]]
    unknown: UnknownLabels
    references: tuple[ReferenceRow, ...]
    results: tuple[SettingResult, ...]
    missing: tuple[Row, ...]
    trace: SelectionTrace
    p0_best: SelectionItem | None
    selected: tuple[tuple[CandidateStates, CandidateEvaluation], ...]    # 选定的 P1 与 N（或 N′）


def evaluate_settings(prepared: PreparedEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                      unknown: UnknownLabels, references: Sequence[ReferenceRow],
                      limits: FeasibilityLimits) -> tuple[tuple[SettingResult, ...], tuple[Row, ...]]:
    """逐个设定评价；同时收集缺值审计行（设定列用 v1.2.1 的四列，供报告复用）。"""
    eta = configured_loss_settings(prepared.config).parameters.eta
    green, red = references[0].evaluated.total_loss, references[2].evaluated.total_loss
    results: list[SettingResult] = []
    missing: list[Row] = []
    for states in prepared.states:
        evaluated = evaluate_candidate(prepared, states, events, unknown)
        results.append(setting_result(prepared, states, evaluated, events, eta, green, red, limits))
        label = f"{display_model(states.candidate.model)}·{display_version(states.candidate)}"
        missing.extend(missing_rows((label, *table_prefix(states.candidate)[1:]), evaluated))
    return tuple(results), tuple(missing)


def evaluate_v13(prepared: PreparedEvaluation, config: V13Config) -> V13Evaluation:
    """参照行 → 全部设定 → 选择程序 → 选定设定的逐日结果。程序停止时 selected 只含已选出的设定。"""
    events, unknown = development_labels(prepared)
    references = reference_rows(prepared, events, unknown, config.ma200_window)
    results, missing = evaluate_settings(prepared, events, unknown, references, config.limits)
    trace = select([selection_item(row) for row in results if row.candidate.model != "P0"],
                   config.exit_versions)
    chosen = {item.key for item in (trace.p1, trace.final_n) if item is not None}
    selected = tuple((states, evaluate_candidate(prepared, states, events, unknown))
                     for states in prepared.states if setting_key(states.candidate) in chosen)
    return V13Evaluation(events, unknown, references, results, missing, trace, best_feasible_p0(results),
                         selected)
