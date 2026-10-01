"""v1.2.1 评价编排（纯计算）；主损失只用 E2。不读写文件，不导入读写模块。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.channels import price_predicates
from market_risk.wavewarn.config import ChannelSelection, WavewarnConfig
from market_risk.wavewarn.convergence import loss_start, system_convergence
from market_risk.wavewarn.execution import ExecutionDay, execute_asset, exposure
from market_risk.wavewarn.features import AssetFeatures, asset_features, vix_term_ratio
from market_risk.wavewarn.input_model import DevelopmentInputs
from market_risk.wavewarn.labels_zz import (
    MergedZZEvent,
    UnknownLabels,
    ZZEvent,
    build_unknown_labels,
    find_zz_events,
)
from market_risk.wavewarn.loss import (
    AssetLossDay,
    LossSettings,
    MainLossDay,
    configured_loss_settings,
    daily_main_loss,
    evaluated_asset_price_loss,
    full_exposure_events,
)
from market_risk.wavewarn.state_machine import Light
from market_risk.wavewarn.state_sequences import (
    DiagnosticRow,
    diagnostic_sequence,
    first_complete_day,
    n_channel_inputs,
    n_diagnostic_sequence,
    ready_inputs,
)

Model = Literal["P0", "P1", "N", "N去B/DV"]
RANKED_MODELS: tuple[Model, ...] = ("P0", "P1", "N")
SYMBOLS = ("SPX", "QQQ")
VALIDATION_START = dt.date(2017, 1, 3)        # 规格第八节：验证期第一个区间起点
LEDGER_LOOKBACK = 20                          # 事件账需要高点前 20 个交易日的灯色
# 对数按 Decimal 28 位有效数字计算，相加次序不同会在末位产生舍入差；核对只容许这一量级。
RECONCILE_TOLERANCE = Decimal("1e-20")


@dataclass(frozen=True)
class Candidate:
    model: Model
    k: int
    theta_p: Decimal
    q: Decimal | None
    order: int
    exit_version: Literal["E2", "X1", "X2"] = "E2"      # P0 不使用；v1.2.1 只有 E2，v1.3 另有 X1、X2

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
    # 系统执行灯色 S_{j−1}（与单个资产是否缺价无关），与 days 对齐
    system_executed: tuple[str, ...] = ()
    # 漏报罚项（危险区间全程满暴露）：资产 → {区间 Tr−1 的起点日期: μ × 触发事件数}
    miss_penalty: Mapping[str, Mapping[dt.date, Decimal]] = field(default_factory=dict)

    @property
    def total_loss(self) -> Decimal:
        return sum((row.total for row in self.daily_losses), Decimal(0))

    @property
    def executed_non_green_days(self) -> int:
        """主损失计入的区间 j（j₀ 至最后一个计入区间）中执行灯色 S_{j−1} 非绿的天数，系统级计一次。"""
        return sum(light != "绿" for light in self.system_executed[:-1])

    @property
    def billed_switches(self) -> int:
        """j₀ 至窗口倒数第二日已执行且计费的系统切换次数；窗口末日切换不计。"""
        return sum(row.switched for row in self.executions["SPX"][:-1])


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
    miss_penalty: Decimal
    excluded_reason: str
    terminal_switch_unbilled: bool

    @property
    def total(self) -> Decimal:
        """行合计 = 加权价格损失 + 切换罚分分摊额 + 漏报罚项。"""
        return self.weighted_price_loss + self.switch_share + self.miss_penalty


def _asset_detail(evaluated: CandidateEvaluation, index: int, symbol: str, weight: Decimal,
                  system: MainLossDay, switched: bool) -> DailyAssetDetail:
    """一个资产、一个区间的明细行；窗口末日没有后续区间，价格项为 0。"""
    day = evaluated.days[index]
    execution = evaluated.executions[symbol][index]
    loss = evaluated.asset_losses[symbol][index] if index < len(evaluated.days) - 1 else None
    danger = loss.danger_loss if loss else Decimal(0)
    drawdown = loss.drawdown_loss if loss else Decimal(0)
    opportunity = loss.opportunity_loss if loss else Decimal(0)
    return DailyAssetDetail(
        symbol, day, evaluated.days[index + 1] if loss else None,
        execution.signal, execution.executed, execution.exposure, weight,
        loss.log_return if loss else None, loss.dangerous if loss else None,
        loss.drawdown_increment if loss else Decimal(0), danger, drawdown, opportunity,
        weight * (danger + drawdown + opportunity), int(switched), system.switch_cost / 2,
        evaluated.miss_penalty.get(symbol, {}).get(day, Decimal(0)),
        loss.excluded_reason if loss else "评价窗口末日，无后续区间", system.terminal_switch_unbilled)


def allocated_asset_days(evaluated: CandidateEvaluation,
                         weights: Mapping[str, Decimal]) -> tuple[DailyAssetDetail, ...]:
    """系统切换费按两资产行均分（与价格权重无关）；漏报罚项整笔记在所属资产的 Tr−1 行，不乘权重。"""
    if set(weights) != set(SYMBOLS) or set(evaluated.asset_losses) != set(weights):
        raise ValueError("逐资产明细要求 SPX 与 QQQ 两行")
    result: list[DailyAssetDetail] = []
    for index, day in enumerate(evaluated.days):
        system = evaluated.daily_losses[index]
        switches = {evaluated.executions[symbol][index].switched for symbol in weights}
        if len(switches) != 1:
            raise ValueError("两个资产的系统切换标记不一致")
        switched = switches.pop()
        if system.switch_cost and not switched:
            raise ValueError("未切换日出现系统切换罚分")
        result.extend(_asset_detail(evaluated, index, symbol, weights[symbol], system, switched)
                      for symbol in SYMBOLS)
        if abs(sum((row.total for row in result[-2:]), Decimal(0)) - system.total) > RECONCILE_TOLERANCE:
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


@dataclass(frozen=True)
class ScopeRule:
    """一个评价期的事件纳入规则；开发期与验证期用同一套判断、不同的边界。"""

    first_interval: dt.date          # 第一个计入损失的区间起点：开发期为 j₀，验证期为 2017-01-03
    ledger_peak_floor: dt.date       # 事件账要求高点 P 不早于此日：开发期为 τ，验证期为 2017-01-03
    ledger_lookback_floor: dt.date   # 事件账要求 P 前 20 个交易日不早于此日：两期均为 τ（此前灯色不可用）


@dataclass(frozen=True)
class EventScope:
    crosses_start: bool              # P < 起点 ≤ Tr：只计起点之后的逐日危险项
    complete_for_penalty: bool       # P ≥ 起点且非右截尾：纳入“危险区间全程满暴露”罚项
    in_ledger: bool                  # 纳入事件账五类
    category: str                    # 五种互斥情形之一，见 SCOPE_CATEGORIES


SCOPE_CATEGORIES = ("右截尾", "起点之前结束", "跨起点", "完整纳入并进事件账", "完整纳入但高点前20天不足")


def development_scope_rule(tau: dt.date, first_loss_day: dt.date) -> ScopeRule:
    """开发期：逐日项自 j₀ 起；事件账要求 P 前 20 个交易日不早于 τ。"""
    return ScopeRule(first_loss_day, tau, tau)


def validation_scope_rule(tau: dt.date) -> ScopeRule:
    """验证期（本轮只实现、不运行）：模型自评价起点连续运行，事件按 P ≥ 2017-01-03 归属；
    事件账所需高点前 20 天的灯色可以落在 2016 年，只要不早于 τ。"""
    if tau > VALIDATION_START:
        raise ValueError("τ 晚于验证期起点，验证期事件账缺少可用灯色")
    return ScopeRule(VALIDATION_START, VALIDATION_START, tau)


def ledger_included(days: Sequence[dt.date], peak_date: dt.date, right_censored: bool,
                    rule: ScopeRule) -> bool:
    """事件账五类的纳入：非右截尾、P 不早于期内下限、P 前 20 个交易日不早于灯色可用下限。"""
    if peak_date not in days:
        raise ValueError("事件高点不在交易日轴")
    peak = days.index(peak_date)
    return (not right_censored and peak_date >= rule.ledger_peak_floor and peak >= LEDGER_LOOKBACK
            and days[peak - LEDGER_LOOKBACK] >= rule.ledger_lookback_floor)


TAIL_PEAK_REASON = "尾段（寻峰）"


def touches_tail_unknown(peak_date: dt.date, trough_date: dt.date, unknown: UnknownLabels) -> bool:
    """合并事件的闭区间 [P, Tr] 是否与任一资产的尾段（寻峰）未定区间相交（端点相接也算）。

    尾段未定区间从该资产的候选高点延伸到标签截止日；相交时那个资产可能正在形成一件会并进来的事件，
    合并事件的成员尚未确定，不纳入事件账（负责人 2026-10-01 确认）。
    """
    for reasons in unknown.reasons_by_asset.values():
        tail = [day for day, reason in reasons.items() if reason == TAIL_PEAK_REASON]
        if tail and peak_date <= unknown.label_end and trough_date >= min(tail):
            return True
    return False


def merged_ledger_included(days: Sequence[dt.date], event: MergedZZEvent, rule: ScopeRule,
                           unknown: UnknownLabels) -> bool:
    """合并事件进事件账：最早高点前 20 个交易日不早于 τ，无右截尾成员，且不与尾段未定区间相交。"""
    censored = any(member.right_censored for member in event.members)
    return (ledger_included(days, event.peak_date, censored, rule)
            and not touches_tail_unknown(event.peak_date, event.trough_date, unknown))


def event_scope(days: Sequence[dt.date], event: ZZEvent, rule: ScopeRule) -> EventScope:
    """跨起点、满暴露罚项、事件账三个纳入标志各自独立；另给互斥的情形名称用于计数。"""
    if rule.first_interval not in days:
        raise ValueError("评价起点不在交易日轴")
    crosses = event.peak_date < rule.first_interval <= event.trough_date
    complete = not event.right_censored and event.peak_date >= rule.first_interval
    ledger = ledger_included(days, event.peak_date, event.right_censored, rule)
    if event.right_censored:
        category = "右截尾"
    elif event.trough_date < rule.first_interval:
        category = "起点之前结束"
    elif crosses:
        category = "跨起点"
    else:
        category = "完整纳入并进事件账" if ledger else "完整纳入但高点前20天不足"
    return EventScope(crosses, complete, ledger, category)


def candidate_grid(config: WavewarnConfig) -> tuple[Candidate, ...]:
    """候选与解释性分解按登记顺序生成；分解去掉 B 后 q 不再起作用。"""
    sets = config.candidate_sets()
    result: list[Candidate] = []
    for model in ("P0", "P1", "N", "N去B/DV"):
        for k in sets.k:
            for theta in sets.theta_p:
                for q in (sets.q if model == "N" else (None,)):
                    result.append(Candidate(model, k, theta, q, len(result)))  # type: ignore[arg-type]
    return tuple(result)


def _price_channels(spx: Sequence[AssetFeatures], qqq: Sequence[AssetFeatures],
                    theta: Decimal, k: int) -> dict[str, tuple[str, tuple]]:
    channels = {}
    for symbol, features in (("SPX", spx), ("QQQ", qqq)):
        channels[f"P_{symbol}"] = ("黄", price_predicates(features, theta, k))
        channels[f"PR_{symbol}"] = ("红", price_predicates(features, theta, k, red=True))
    return channels


def require_e2(config: WavewarnConfig) -> None:
    """P1 与 N 的主损失只接受 E2（负责人 2026-09-30 确认的唯一退出版本）。"""
    if config.require_business_parameters().e_version != "E2":
        raise ValueError("P1/N 主损失只能使用 E2")


def _validate_development_config(config: WavewarnConfig) -> ChannelSelection:
    """主损失口径必须与负责人登记的 E2 及四侧通道一致。"""
    require_e2(config)
    selection = config.require_channel_selection()
    if selection != ChannelSelection(False, True, True, True):
        raise ValueError("N 的 B/DV 四侧开关与负责人确认的通道组成不一致")
    return selection


def candidate_states(candidate: Candidate, config: WavewarnConfig, inputs: DevelopmentInputs,
                      features: Mapping[Decimal, tuple[Sequence[AssetFeatures], Sequence[AssetFeatures]]],
                      ratios: Sequence[Decimal | None], t0: dt.date,
                      selection: ChannelSelection) -> CandidateStates:
    """单个候选的状态与系统收敛日，纯计算。"""
    fixed = config.fixed_parameters()
    sets = config.candidate_sets()
    spx, qqq = features[candidate.q if candidate.q is not None else sets.q[0]]
    version = "P0" if candidate.model == "P0" else candidate.exit_version
    if candidate.model in ("P0", "P1"):
        scenario = "P0" if candidate.model == "P0" else f"P1-{version}"
        channels = _price_channels(spx, qqq, candidate.theta_p, candidate.k)
        state_rows = diagnostic_sequence(inputs.days, spx, qqq, t0, scenario,  # type: ignore[arg-type]
                                         candidate.theta_p, candidate.k, fixed)
    else:
        scenario = f"N-{version}"
        selected = selection if candidate.model == "N" else ChannelSelection(False, False, False, False)
        channels = n_channel_inputs(spx, qqq, ratios, candidate.theta_p, candidate.k, selected, fixed)
        state_rows = n_diagnostic_sequence(inputs.days, spx, qqq, ratios, t0, candidate.theta_p,
                                           candidate.k, selected, version, fixed)  # type: ignore[arg-type]
    ready = ready_inputs(inputs.days, spx, qqq, scenario, fixed)  # type: ignore[arg-type]
    converged = system_convergence(inputs.days, channels, ready, t0, candidate.k,  # type: ignore[arg-type]
                                   version, fixed)  # type: ignore[arg-type]
    if converged.system_date is None:
        raise ValueError(f"候选未在开发期内收敛：{candidate.key}")
    return CandidateStates(candidate, converged.system_date, state_rows)


def prepare_from_inputs(config: WavewarnConfig, inputs: DevelopmentInputs) -> PreparedEvaluation:
    """v1.2.1：τ 与 j₀ 取全部 45 组（含去 B/DV 分解）的系统收敛日最大值；退出版本只有 E2。"""
    return prepare_grid(config, inputs, candidate_grid(config))


def prepare_grid(config: WavewarnConfig, inputs: DevelopmentInputs,
                 grid: Sequence[Candidate]) -> PreparedEvaluation:
    """开发期序列已在边界截断；τ 与 j₀ 取 grid 中全部设定的系统收敛日最大值。"""
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
    rows = tuple(candidate_states(candidate, config, inputs, features, ratios, t0, selection)
                 for candidate in grid)
    tau = loss_start(inputs.days, t0, [item.convergence_date for item in rows], fixed)
    first_loss_day = first_loss_interval(inputs.days, tau, [item.convergence_date for item in rows])
    return PreparedEvaluation(config, inputs, t0, tau, first_loss_day, rows)


def development_labels(prepared: PreparedEvaluation) -> tuple[dict[str, tuple[ZZEvent, ...]], UnknownLabels]:
    """只从已截断开发期价格生成本次评价的资产标签与尾段未定集合。"""
    thresholds = prepared.config.zz_thresholds()
    events = {symbol: find_zz_events(symbol, prepared.inputs.days, prepared.inputs.series[symbol],
                                     prepared.config.development_end(), thresholds)
              for symbol in SYMBOLS}
    unknown = build_unknown_labels(events, prepared.inputs.days,
                                   {symbol: prepared.inputs.series[symbol] for symbol in events},
                                   prepared.config.development_end())
    return events, unknown


@dataclass(frozen=True)
class AxisLoss:
    """同一日期轴上、给定执行序列的逐资产与系统损失。"""

    asset_losses: Mapping[str, tuple[AssetLossDay, ...]]
    daily_losses: tuple[MainLossDay, ...]
    miss_penalty: Mapping[str, Mapping[dt.date, Decimal]]


def axis_loss(prepared: PreparedEvaluation, executions: Mapping[str, Sequence[ExecutionDay]],
              events: Mapping[str, Sequence[ZZEvent]], unknown: UnknownLabels,
              settings: LossSettings) -> AxisLoss:
    """候选模型与参照行共用的损失计算：j₀ 起重置回撤高点；满暴露罚项只纳入 P ≥ j₀ 的已确认事件。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    asset_losses: dict[str, tuple[AssetLossDay, ...]] = {}
    miss: dict[str, dict[dt.date, Decimal]] = {}
    triggered: list[ZZEvent] = []
    for symbol in SYMBOLS:
        closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
        asset_losses[symbol] = evaluated_asset_price_loss(
            axis, closes, executions[symbol], events[symbol], settings.parameters, symbol, unknown)
        # 低点在日期轴之后的事件（只出现在补充历史的窗口右端）危险区间未走完，与右截尾一样不判定。
        complete = [event for event in events[symbol]
                    if event.peak_date >= prepared.first_loss_day and not event.right_censored
                    and event.trough_date <= axis[-1]]
        own = full_exposure_events(complete, axis, executions[symbol])
        triggered.extend(own)
        miss[symbol] = {}
        for event in own:
            day = axis[axis.index(event.trough_date) - 1]
            miss[symbol][day] = miss[symbol].get(day, Decimal(0)) + settings.mu
    daily = daily_main_loss(axis, asset_losses, executions["SPX"], settings.weights,
                            settings.parameters, triggered, settings.mu)
    return AxisLoss(asset_losses, daily, miss)


def evaluate_candidate(prepared: PreparedEvaluation, states: CandidateStates,
                       events: Mapping[str, Sequence[ZZEvent]],
                       unknown: UnknownLabels) -> CandidateEvaluation:
    """从完整 t0 状态连续执行，j₀ 起重置回撤高点并计算同轴逐日损失。"""
    require_e2(prepared.config)
    settings = configured_loss_settings(prepared.config)
    days = prepared.inputs.days
    offset = days.index(prepared.first_loss_day) - days.index(prepared.t0)
    state_days = days[days.index(prepared.t0):]
    if tuple(row.date for row in states.rows) != state_days:
        raise ValueError("候选状态与 t0 后交易日轴不一致")
    signals = tuple(row.light for row in states.rows)
    # 第 j 天收盘执行前一日信号 S_{j−1}；t0 当天沿用初始快照的绿灯（与 execute_asset 一致）。
    system_executed = ("绿", *signals[:-1])[offset:]
    executions = {
        symbol: execute_asset(state_days, signals,  # type: ignore[arg-type]
                              tuple(prepared.inputs.series[symbol].get(day) for day in state_days),
                              settings.parameters.eta)[offset:]
        for symbol in SYMBOLS
    }
    losses = axis_loss(prepared, executions, events, unknown, settings)
    return CandidateEvaluation(states.candidate, state_days[offset:], signals[offset:],
                               tuple(row.data_status for row in states.rows[offset:]),
                               tuple(row.active_channels for row in states.rows[offset:]),
                               tuple(row.reason for row in states.rows[offset:]),
                               executions, losses.asset_losses, losses.daily_losses,
                               system_executed, losses.miss_penalty)


@dataclass(frozen=True)
class ReferenceResult:
    """始终绿、黄、红的参照行：恒定暴露、零切换，只给主损失及其分项。"""

    name: str
    exposure: Decimal
    total_loss: Decimal
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    switch_cost: Decimal
    miss_penalty: Decimal
    executed_non_green_days: int


REFERENCE_LIGHTS: tuple[tuple[str, Light], ...] = (("始终绿", "绿"), ("始终黄", "黄"), ("始终红", "红"))


def reference_evaluation(prepared: PreparedEvaluation, name: str, light: Light,
                         events: Mapping[str, Sequence[ZZEvent]],
                         unknown: UnknownLabels) -> ReferenceResult:
    """从 τ 起即为恒定暴露、零切换，按 j₀ 与同一损失函数计算；不参与选参与检验。"""
    settings = configured_loss_settings(prepared.config)
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.first_loss_day):]
    level = exposure(light, settings.parameters.eta)
    executions = {
        symbol: tuple(ExecutionDay(day, light, light, level,
                                   prepared.inputs.series[symbol].get(day) is not None, False)
                      for day in axis)
        for symbol in SYMBOLS
    }
    daily = axis_loss(prepared, executions, events, unknown, settings).daily_losses
    return ReferenceResult(
        name, level, sum((row.total for row in daily), Decimal(0)),
        sum((row.danger_loss for row in daily), Decimal(0)),
        sum((row.drawdown_loss for row in daily), Decimal(0)),
        sum((row.opportunity_loss for row in daily), Decimal(0)),
        sum((row.switch_cost for row in daily), Decimal(0)),
        sum((row.full_exposure_cost for row in daily), Decimal(0)),
        0 if light == "绿" else len(axis) - 1)
