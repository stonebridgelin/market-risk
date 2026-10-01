"""v1.2.1 开发期评价编排；只用 E2 计算候选模型主损失。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.channels import price_predicates
from market_risk.wavewarn.config import ChannelSelection, WavewarnConfig
from market_risk.wavewarn.convergence import loss_start, system_convergence
from market_risk.wavewarn.diagnostics import (
    DiagnosticRow,
    _ready_inputs,
    diagnostic_sequence,
    first_complete_day,
    n_channel_inputs,
    n_diagnostic_sequence,
)
from market_risk.wavewarn.execution import ExecutionDay, execute_asset
from market_risk.wavewarn.features import AssetFeatures, asset_features, vix_term_ratio
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import UnknownLabels, ZZEvent, build_unknown_labels, find_zz_events
from market_risk.wavewarn.loss import (
    AssetLossDay,
    MainLossDay,
    configured_loss_settings,
    daily_main_loss,
    evaluated_asset_price_loss,
    full_exposure_events,
)

Model = Literal["P0", "P1", "N", "N去B/DV"]


@dataclass(frozen=True)
class Candidate:
    model: Model
    k: int
    theta_p: Decimal
    q: Decimal | None
    order: int

    @property
    def key(self) -> str:
        return f"{self.model}|K={self.k}|θ={self.theta_p}|q={self.q if self.q is not None else '-'}"


@dataclass(frozen=True)
class CandidateStates:
    candidate: Candidate
    convergence_date: dt.date
    rows: tuple[DiagnosticRow, ...]


@dataclass(frozen=True)
class PreparedEvaluation:
    config: WavewarnConfig
    inputs: DevelopmentInputs
    t0: dt.date
    tau: dt.date
    first_loss_day: dt.date
    states: tuple[CandidateStates, ...]


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: Candidate
    days: tuple[dt.date, ...]
    signals: tuple[str, ...]
    status: tuple[str, ...]
    active_channels: tuple[tuple[str, ...], ...]
    reasons: tuple[str, ...]
    executions: Mapping[str, tuple[ExecutionDay, ...]]
    asset_losses: Mapping[str, tuple[AssetLossDay, ...]]
    daily_losses: tuple[MainLossDay, ...]

    @property
    def total_loss(self) -> Decimal:
        return sum((row.total for row in self.daily_losses), Decimal(0))


@dataclass(frozen=True)
class DailyAssetDetail:
    symbol: str
    date: dt.date
    next_date: dt.date | None
    signal: str
    executed: str
    exposure: Decimal
    weight: Decimal
    log_return: Decimal | None
    dangerous: bool | None
    drawdown_increment: Decimal
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    weighted_price_loss: Decimal
    system_switches: int
    switch_share: Decimal
    full_exposure_share: Decimal
    excluded_reason: str
    terminal_switch_unbilled: bool

    @property
    def total(self) -> Decimal:
        return self.weighted_price_loss + self.switch_share + self.full_exposure_share


def allocated_asset_days(evaluated: CandidateEvaluation,
                         weights: Mapping[str, Decimal]) -> tuple[DailyAssetDetail, ...]:
    """系统切换费按两资产行均分，与价格权重无关；末日保留不计费标记。"""
    if set(weights) != {"SPX", "QQQ"} or set(evaluated.asset_losses) != set(weights):
        raise ValueError("逐资产分摊要求 SPX 与 QQQ 两行")
    result = []
    for index, day in enumerate(evaluated.days):
        system = evaluated.daily_losses[index]
        switches = {evaluated.executions[symbol][index].switched for symbol in weights}
        if len(switches) != 1:
            raise ValueError("两个资产的系统切换标记不一致")
        switched = switches.pop()
        if system.switch_cost and not switched:
            raise ValueError("未切换日出现系统切换罚分")
        for symbol in ("SPX", "QQQ"):
            execution = evaluated.executions[symbol][index]
            loss = evaluated.asset_losses[symbol][index] if index < len(evaluated.days) - 1 else None
            danger = loss.danger_loss if loss else Decimal(0)
            drawdown = loss.drawdown_loss if loss else Decimal(0)
            opportunity = loss.opportunity_loss if loss else Decimal(0)
            result.append(DailyAssetDetail(
                symbol, day, evaluated.days[index + 1] if loss else None,
                execution.signal, execution.executed, execution.exposure, weights[symbol],
                loss.log_return if loss else None, loss.dangerous if loss else None,
                loss.drawdown_increment if loss else Decimal(0), danger, drawdown, opportunity,
                weights[symbol] * (danger + drawdown + opportunity), int(switched),
                system.switch_cost / 2, system.full_exposure_cost / 2,
                loss.excluded_reason if loss else "评价窗口末日，无后续区间",
                system.terminal_switch_unbilled))
        if sum((row.total for row in result[-2:]), Decimal(0)) != system.total:
            raise ValueError(f"{day} 两资产逐日明细未与系统主损失对齐")
    return tuple(result)


def first_loss_interval(days: Sequence[dt.date], tau: dt.date,
                        convergence_dates: Sequence[dt.date]) -> dt.date:
    """j₀=max(τ,全部系统最晚收敛日之后第2个交易日)。"""
    if not convergence_dates or tau not in days or any(day not in days for day in convergence_dates):
        raise ValueError("τ 或系统收敛日不在评价时间轴")
    latest = max(convergence_dates)
    position = days.index(latest) + 2
    if position >= len(days):
        raise ValueError("全部模型收敛后不足两个交易日，无法确定首个损失区间")
    return max(tau, days[position])


def event_scope(days: Sequence[dt.date], event: ZZEvent, tau: dt.date,
                first_loss_day: dt.date) -> tuple[bool, bool, bool]:
    """返回跨 j₀、完整危险罚项、五类事件账纳入标志，三者口径各自独立。"""
    if tau not in days or first_loss_day not in days or event.peak_date not in days:
        raise ValueError("事件与评价边界不在同一交易日轴")
    cross_start = event.peak_date < first_loss_day <= event.trough_date
    complete_penalty = not event.right_censored and event.peak_date >= first_loss_day
    peak = days.index(event.peak_date)
    ledger = (not event.right_censored and peak >= 20
              and days[peak - 20] >= tau)
    return cross_start, complete_penalty, ledger


def candidate_grid(config: WavewarnConfig) -> tuple[Candidate, ...]:
    """候选与解释性分解按登记顺序生成；分解去掉 B 后 q 不再起作用。"""
    sets = config.candidate_sets()
    result = []
    for model in ("P0", "P1", "N", "N去B/DV"):
        for k in sets.k:
            for theta in sets.theta_p:
                for q in (sets.q if model == "N" else (None,)):
                    result.append(Candidate(model, k, theta, q, len(result)))
    return tuple(result)


def _price_channels(spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                    theta: Decimal, k: int) -> dict[str, tuple[str, tuple]]:
    channels = {}
    for symbol, features in (("SPX", spx), ("QQQ", qqq)):
        channels[f"P_{symbol}"] = ("黄", price_predicates(features, theta, k))
        channels[f"PR_{symbol}"] = ("红", price_predicates(features, theta, k, red=True))
    return channels


def _validate_development_config(config: WavewarnConfig) -> ChannelSelection:
    """主损失口径必须与负责人登记的 E2 及四侧通道一致。"""
    business = config.require_business_parameters()
    if business.e_version != "E2":
        raise ValueError("开发期主损失评价仅允许配置中的 E2")
    selection = config.require_channel_selection()
    if selection != ChannelSelection(False, True, True, True):
        raise ValueError("N 的 B/DV 四侧开关与负责人确认的通道组成不一致")
    return selection


def _candidate_states(candidate: Candidate, config: WavewarnConfig, inputs: DevelopmentInputs,
                      features: Mapping[Decimal, tuple[Sequence[AssetFeatures], Sequence[AssetFeatures]]],
                      ratios: Sequence[Decimal | None], t0: dt.date,
                      selection: ChannelSelection) -> CandidateStates:
    """单个候选的状态与系统收敛日，纯计算。"""
    fixed = config.fixed_parameters()
    sets = config.candidate_sets()
    spx, qqq = features[candidate.q if candidate.q is not None else sets.q[0]]
    if candidate.model in ("P0", "P1"):
        scenario = "P0" if candidate.model == "P0" else "P1-E2"
        channels = _price_channels(spx, qqq, candidate.theta_p, candidate.k)
        state_rows = diagnostic_sequence(inputs.days, spx, qqq, t0, scenario,
                                         candidate.theta_p, candidate.k, fixed)
        version = "P0" if candidate.model == "P0" else "E2"
    else:
        selected = selection if candidate.model == "N" else ChannelSelection(False, False, False, False)
        channels = n_channel_inputs(spx, qqq, ratios, candidate.theta_p, candidate.k, selected, fixed)
        state_rows = n_diagnostic_sequence(inputs.days, spx, qqq, ratios, t0,
                                           candidate.theta_p, candidate.k, selected, "E2", fixed)
        version = "E2"
    ready = _ready_inputs(inputs.days, spx, qqq, scenario if candidate.model in ("P0", "P1")
                          else "N-E2", fixed)
    converged = system_convergence(inputs.days, channels, ready, t0, candidate.k, version, fixed)
    if converged.system_date is None:
        raise ValueError(f"候选未在开发期内收敛：{candidate.key}")
    return CandidateStates(candidate, converged.system_date, state_rows)


def prepare_from_inputs(config: WavewarnConfig, inputs: DevelopmentInputs) -> PreparedEvaluation:
    """开发期序列已在边界截断；以纯计算求所有候选与共同损失起点。"""
    selection = _validate_development_config(config)
    fixed = config.fixed_parameters()
    sets = config.candidate_sets()
    features = {q: (asset_features(inputs.days, inputs.series["SPX"], inputs.series["S5TW"], q, fixed),
                    asset_features(inputs.days, inputs.series["QQQ"], inputs.series["NDTW"], q, fixed))
                for q in sets.q}
    base_spx, base_qqq = features[sets.q[0]]
    t0 = first_complete_day(inputs.days, base_spx, base_qqq,
                            inputs.series["VIX"], inputs.series["VIX3M"], fixed)
    ratios = tuple(vix_term_ratio(inputs.series["VIX"].get(day), inputs.series["VIX3M"].get(day))
                   for day in inputs.days)
    rows = tuple(_candidate_states(candidate, config, inputs, features, ratios, t0, selection)
                 for candidate in candidate_grid(config))
    tau = loss_start(inputs.days, t0, [item.convergence_date for item in rows], fixed)
    first_loss_day = first_loss_interval(inputs.days, tau, [item.convergence_date for item in rows])
    return PreparedEvaluation(config, inputs, t0, tau, first_loss_day, rows)


def development_labels(prepared: PreparedEvaluation) -> tuple[dict[str, tuple[ZZEvent, ...]], UnknownLabels]:
    """只从已截断开发期价格生成本次评价的资产标签与尾段未定集合。"""
    thresholds = prepared.config.zz_thresholds()
    events = {symbol: find_zz_events(symbol, prepared.inputs.days, prepared.inputs.series[symbol],
                                     prepared.config.development_end(), thresholds)
              for symbol in ("SPX", "QQQ")}
    unknown = build_unknown_labels(events, prepared.inputs.days,
                                   {symbol: prepared.inputs.series[symbol] for symbol in events},
                                   prepared.config.development_end())
    return events, unknown


def evaluate_candidate(prepared: PreparedEvaluation, states: CandidateStates,
                       events: Mapping[str, Sequence[ZZEvent]],
                       unknown: UnknownLabels) -> CandidateEvaluation:
    """从完整 t0 状态连续执行，j₀ 起重置回撤高点并计算同轴逐日损失。"""
    settings = configured_loss_settings(prepared.config)
    if prepared.config.require_business_parameters().e_version != "E2":
        raise ValueError("P1/N 主损失只能使用 E2")
    days = prepared.inputs.days
    start = days.index(prepared.first_loss_day)
    t0_index = days.index(prepared.t0)
    axis = days[start:]
    state_days = days[t0_index:]
    if tuple(row.date for row in states.rows) != state_days:
        raise ValueError("候选状态与 t0 后交易日轴不一致")
    signals = tuple(row.light for row in states.rows)
    all_executions = {
        symbol: execute_asset(state_days, signals,
                              tuple(prepared.inputs.series[symbol].get(day) for day in state_days),
                              settings.parameters.eta)
        for symbol in ("SPX", "QQQ")
    }
    eval_executions = {symbol: rows[start - t0_index:] for symbol, rows in all_executions.items()}
    asset_losses = {}
    full_events = []
    for symbol in ("SPX", "QQQ"):
        closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
        asset_losses[symbol] = evaluated_asset_price_loss(
            axis, closes, eval_executions[symbol], events[symbol], settings.parameters, symbol, unknown)
        complete = [event for event in events[symbol]
                    if event.peak_date >= prepared.first_loss_day and not event.right_censored]
        full_events.extend(full_exposure_events(complete, axis, eval_executions[symbol]))
    daily = daily_main_loss(axis, asset_losses, eval_executions["SPX"], settings.weights,
                            settings.parameters, full_events, settings.mu)
    offset = start - t0_index
    return CandidateEvaluation(states.candidate, axis, signals[offset:],
                               tuple(row.data_status for row in states.rows[offset:]),
                               tuple(row.active_channels for row in states.rows[offset:]),
                               tuple(row.reason for row in states.rows[offset:]),
                               eval_executions, asset_losses, daily)
