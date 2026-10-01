"""v1.2.1 评价的表格行与汇总（纯计算）：逐日明细、事件账、警报账、缺值、排名、N 的 E 代价。

写文件在 evaluation_run.py；本模块不读写文件，也不导入读写模块。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import distribution
from market_risk.wavewarn.convergence import loss_start
from market_risk.wavewarn.evaluation import (
    RANKED_MODELS,
    RECONCILE_TOLERANCE,
    SCOPE_CATEGORIES,
    SYMBOLS,
    Candidate,
    CandidateEvaluation,
    CandidateStates,
    EventScope,
    PreparedEvaluation,
    ReferenceResult,
    ScopeRule,
    allocated_asset_days,
    event_scope,
    ledger_included,
)
from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.exit_costs import ExitCostEvent, exit_cost_for_event
from market_risk.wavewarn.labels_zz import MergedZZEvent, ZZEvent
from market_risk.wavewarn.ledgers import (
    EVENT_CLASSES,
    AlertLedgerRow,
    EventLedgerRow,
    EventTimingDetails,
    build_alert_ledger,
    classify_asset_event,
    classify_merged_event,
    event_timing_details,
    yearly_alert_summary,
)

Row = tuple[object, ...]
STATUS_NOTE = "开发期工程运行，未锁定"
SETTING_HEADER = ("model", "k", "theta_p", "q")
WINDOWS = (5, 10, 20)


def setting_prefix(candidate: Candidate) -> Row:
    return candidate.model, candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else ""


def _flag(value: bool | None) -> str:
    return "" if value is None else "是" if value else "否"


def _blank(value: object | None) -> object:
    return "" if value is None else value


# ---------------------------------------------------------------------------
# 逐日明细
# ---------------------------------------------------------------------------

MISS_PENALTY_COLUMN = "漏报罚项（危险区间全程满暴露）"
DAILY_HEADER = (*SETTING_HEADER, "symbol", "date", "next_date", "close", "next_close", "signal_light",
                "executed_light", "system_executed_light", "exposure", "log_return", "dangerous",
                "drawdown_increment", "danger_loss_raw", "drawdown_loss_raw", "opportunity_loss_raw",
                "weight", "weighted_price_loss", "system_switch_count", "switch_cost_share",
                MISS_PENALTY_COLUMN, "row_total", "excluded_reason", "terminal_switch_unbilled",
                "data_status", "active_channels", "state_reason")


def daily_rows(evaluated: CandidateEvaluation, weights: Mapping[str, Decimal],
               prices: Mapping[str, Mapping[dt.date, Decimal]]) -> tuple[Row, ...]:
    """每个设定、资产、区间一行；窗口末日另有两行只用于标记不计费的末日切换。"""
    prefix = setting_prefix(evaluated.candidate)
    position = {day: index for index, day in enumerate(evaluated.days)}
    rows: list[Row] = []
    for row in allocated_asset_days(evaluated, weights):
        index = position[row.date]
        closes = prices[row.symbol]
        rows.append((
            *prefix, row.symbol, row.date, _blank(row.next_date), _blank(closes.get(row.date)),
            _blank(closes.get(row.next_date)) if row.next_date else "", row.signal, row.executed,
            evaluated.system_executed[index], row.exposure, _blank(row.log_return),
            "未定" if row.dangerous is None else _flag(row.dangerous), row.drawdown_increment,
            row.danger_loss, row.drawdown_loss, row.opportunity_loss, row.weight,
            row.weighted_price_loss, row.system_switches, row.switch_share, row.miss_penalty, row.total,
            row.excluded_reason, _flag(row.terminal_switch_unbilled), evaluated.status[index],
            ";".join(evaluated.active_channels[index]), evaluated.reasons[index]))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 事件纳入范围（只取决于标签、τ 与 j₀，与模型设定无关）
# ---------------------------------------------------------------------------

SCOPE_HEADER = ("symbol", "peak_date", "t0_date", "trough_date", "end_date", "right_censored",
                "scope_category", "crosses_first_interval", "complete_for_penalty", "in_event_ledger")
SCOPE_COUNT_HEADER = ("symbol", "events", *SCOPE_CATEGORIES, "complete_for_penalty", "in_event_ledger")


def event_scope_rows(days: Sequence[dt.date], events: Mapping[str, Sequence[ZZEvent]],
                     rule: ScopeRule) -> tuple[Row, ...]:
    """逐个资产事件列出三种纳入标志与互斥情形。"""
    rows: list[Row] = []
    for symbol in SYMBOLS:
        for event in events[symbol]:
            scope = event_scope(days, event, rule)
            rows.append((symbol, event.peak_date, event.t0_date, event.trough_date, _blank(event.end_date),
                         _flag(event.right_censored), scope.category, _flag(scope.crosses_start),
                         _flag(scope.complete_for_penalty), _flag(scope.in_ledger)))
    return tuple(rows)


def event_scope_counts(days: Sequence[dt.date], events: Mapping[str, Sequence[ZZEvent]],
                       rule: ScopeRule) -> tuple[Row, ...]:
    """每个资产一行：五种互斥情形的件数，另列罚项口径与事件账口径的纳入件数。"""
    rows: list[Row] = []
    for symbol in SYMBOLS:
        scopes = [event_scope(days, event, rule) for event in events[symbol]]
        counts = Counter(scope.category for scope in scopes)
        if sum(counts.values()) != len(scopes) or set(counts) - set(SCOPE_CATEGORIES):
            raise ValueError("事件纳入情形未穷尽")
        rows.append((symbol, len(scopes), *(counts[name] for name in SCOPE_CATEGORIES),
                     sum(scope.complete_for_penalty for scope in scopes),
                     sum(scope.in_ledger for scope in scopes)))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 事件账
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerEntry:
    """事件账的一行：资产事件或合并事件；未纳入的事件只保留日期与纳入标志。"""

    scope: str                       # SPX、QQQ 或 合并
    source: str                      # 合并事件的来源：SPX、QQQ、SPX+QQQ
    peak_date: dt.date
    t0_date: dt.date
    trough_date: dt.date
    right_censored: bool
    asset_scope: EventScope | None   # 合并事件不参与损失，故无此项
    in_ledger: bool
    classified: EventLedgerRow | None
    timing: EventTimingDetails | None


def asset_ledger_entries(prepared: PreparedEvaluation, states: CandidateStates,
                         events: Mapping[str, Sequence[ZZEvent]], rule: ScopeRule) -> tuple[LedgerEntry, ...]:
    """资产事件：只在纳入事件账时给五类判定与转绿时点明细。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    lights = tuple(row.light for row in states.rows)
    active = tuple(row.active_channels for row in states.rows)
    reasons = tuple(row.reason for row in states.rows)
    entries: list[LedgerEntry] = []
    for symbol in SYMBOLS:
        closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
        for event in events[symbol]:
            scope = event_scope(prepared.inputs.days, event, rule)
            classified = classify_asset_event(axis, lights, event) if scope.in_ledger else None  # type: ignore[arg-type]
            timing = (event_timing_details(axis, lights, closes, event, active, reasons)  # type: ignore[arg-type]
                      if scope.in_ledger else None)
            entries.append(LedgerEntry(symbol, symbol, event.peak_date, event.t0_date, event.trough_date,
                                       event.right_censored, scope, scope.in_ledger, classified, timing))
    return tuple(entries)


def merged_ledger_entries(prepared: PreparedEvaluation, states: CandidateStates,
                          merged: Sequence[MergedZZEvent], rule: ScopeRule) -> tuple[LedgerEntry, ...]:
    """合并事件只做五类判定（用最早的 P、T0 与最晚的 Tr）；不合并价格，故无转绿价格明细。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    lights = tuple(row.light for row in states.rows)
    entries: list[LedgerEntry] = []
    for event in merged:
        censored = any(member.right_censored for member in event.members)
        included = ledger_included(prepared.inputs.days, event.peak_date, censored, rule)
        classified = classify_merged_event(axis, lights, event) if included else None  # type: ignore[arg-type]
        entries.append(LedgerEntry("合并", event.source, event.peak_date, event.t0_date, event.trough_date,
                                   censored, None, included, classified, None))
    return tuple(entries)


EVENT_HEADER = (*SETTING_HEADER, "scope", "source", "peak_date", "t0_date", "trough_date", "right_censored",
                "scope_category", "crosses_first_interval", "complete_for_penalty", "in_event_ledger",
                "event_class", "signal_before_t0", "reduction_executed_before_t0", "prior_alert", "lead_days",
                "alert_start", "alert_round_trips", "light_switches", "first_alert_date",
                "first_alert_channels", "first_green_date", "green_before_trough", "rebound_from_trough_pct",
                *(f"decline_after_green_{window}_pct" for window in WINDOWS),
                *(f"reupgraded_within_{window}" for window in WINDOWS),
                "release_system_reason", "release_exited_channels")


def _timing_cells(timing: EventTimingDetails | None) -> Row:
    if timing is None:
        return ("",) * (7 + 2 * len(WINDOWS))
    return (_blank(timing.first_alert_date), ";".join(timing.first_alert_channels),
            _blank(timing.first_green_date), _flag(timing.green_before_trough),
            _blank(timing.rebound_from_trough_percent),
            *(_blank(timing.decline_after_green_percent[window]) for window in WINDOWS),
            *(_flag(timing.reupgraded_after_green[window]) for window in WINDOWS),
            timing.release_system_reason, ";".join(timing.release_exited_channels))


def ledger_row(prefix: Row, entry: LedgerEntry) -> Row:
    scope, row = entry.asset_scope, entry.classified
    scope_cells: Row = ((scope.category, _flag(scope.crosses_start), _flag(scope.complete_for_penalty))
                        if scope else ("", "", ""))
    class_cells: Row = ((_blank(row.classification), _flag(row.signal_before_t0),
                         _flag(row.reduction_executed_before_t0), _flag(row.prior_alert),
                         _blank(row.lead_days), _blank(row.alert_start), row.alert_round_trips,
                         row.light_switches) if row else ("",) * 8)
    return (*prefix, entry.scope, entry.source, entry.peak_date, entry.t0_date, entry.trough_date,
            _flag(entry.right_censored), *scope_cells, _flag(entry.in_ledger), *class_cells,
            *_timing_cells(entry.timing))


EVENT_CLASS_HEADER = (*SETTING_HEADER, "scope", "events", "right_censored", "in_event_ledger", *EVENT_CLASSES,
                      "signal_before_t0", "reduction_executed_before_t0", "green_before_trough")


def ledger_class_rows(prefix: Row, entries: Sequence[LedgerEntry]) -> tuple[Row, ...]:
    """按 SPX、QQQ、合并 分别计数；五类只以纳入事件账的已确认事件为分母，且必须穷尽。"""
    rows: list[Row] = []
    for scope in (*SYMBOLS, "合并"):
        own = [entry for entry in entries if entry.scope == scope]
        included = [entry.classified for entry in own if entry.classified is not None]
        counts = Counter(row.classification for row in included)
        if sum(counts[name] for name in EVENT_CLASSES) != len(included):
            raise ValueError("事件账五类未穷尽纳入事件")
        green_early = sum(bool(entry.timing and entry.timing.green_before_trough) for entry in own)
        rows.append((*prefix, scope, len(own), sum(entry.right_censored for entry in own), len(included),
                     *(counts[name] for name in EVENT_CLASSES),
                     sum(row.signal_before_t0 for row in included),
                     sum(row.reduction_executed_before_t0 for row in included),
                     "" if scope == "合并" else green_early))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 警报账
# ---------------------------------------------------------------------------

ALERT_HEADER = (*SETTING_HEADER, "start", "end", "trading_days", "category", "active_channels",
                "covered_events", "protected_decline", "outside_danger_days", "net_opportunity_cost",
                "tail_pending_days", "excluded_interval_days")
ALERT_SUMMARY_HEADER = (*SETTING_HEADER, "segments", "with_event", "no_event", "tail_pending",
                        "longest_segment_days", "signal_non_green_days", "protected_decline",
                        "outside_danger_days", "net_opportunity_cost")
YEARLY_HEADER = (*SETTING_HEADER, "year", "trading_days", "signal_non_green_days", "signal_non_green_ratio",
                 "longest_alert_days", "executed_switches", "active_channel_days")


def alert_category(row: AlertLedgerRow) -> str:
    return "尾段待定" if row.tail_pending else "无事件警报段" if row.no_event_alert else "关联事件"


def alert_ledger(evaluated: CandidateEvaluation, events: Mapping[str, Sequence[ZZEvent]],
                 weights: Mapping[str, Decimal]) -> tuple[AlertLedgerRow, ...]:
    """警报段按信号灯色划分；保护效果与占用成本沿用 ledgers.build_alert_ledger 的口径。"""
    all_events = tuple(event for symbol in SYMBOLS for event in events[symbol])
    return build_alert_ledger(evaluated.days, evaluated.signals, evaluated.active_channels,  # type: ignore[arg-type]
                              all_events, dict(evaluated.asset_losses), dict(weights))


def alert_rows(prefix: Row, ledger: Sequence[AlertLedgerRow]) -> tuple[Row, ...]:
    return tuple((*prefix, row.start, row.end, row.trading_days, alert_category(row),
                  ";".join(row.active_channels), ";".join(row.covered_events), row.protected_decline,
                  row.outside_danger_days, row.net_opportunity_cost, row.tail_pending_days,
                  row.excluded_interval_days) for row in ledger)


def alert_summary_row(prefix: Row, ledger: Sequence[AlertLedgerRow]) -> Row:
    categories = Counter(alert_category(row) for row in ledger)
    return (*prefix, len(ledger), categories["关联事件"], categories["无事件警报段"], categories["尾段待定"],
            max((row.trading_days for row in ledger), default=0),
            sum(row.trading_days for row in ledger),
            sum((row.protected_decline for row in ledger), Decimal(0)),
            sum(row.outside_danger_days for row in ledger),
            sum((row.net_opportunity_cost for row in ledger), Decimal(0)))


def yearly_rows(prefix: Row, evaluated: CandidateEvaluation) -> tuple[Row, ...]:
    """每年的信号非绿天数比例、最长警报段、执行切换次数与各通道激活天数。"""
    yearly = yearly_alert_summary(evaluated.days, evaluated.signals,  # type: ignore[arg-type]
                                  evaluated.executions["SPX"], evaluated.active_channels)
    return tuple((*prefix, row.year, row.trading_days, row.non_green_days, row.non_green_ratio,
                  row.longest_alert_days, row.executed_switches,
                  ";".join(f"{name}:{count}" for name, count in sorted(row.active_channel_days.items())))
                 for row in yearly)


# ---------------------------------------------------------------------------
# 缺值
# ---------------------------------------------------------------------------

MISSING_HEADER = (*SETTING_HEADER, "scope", "reason", "count", "first_date", "last_date")


def missing_rows(prefix: Row, evaluated: CandidateEvaluation) -> tuple[Row, ...]:
    """系统“沿用”日（任一通道或降级输入无效）与逐资产被排除区间，按原因计数并给首末日期。"""
    carried = [day for day, status in zip(evaluated.days, evaluated.status, strict=True) if status != "完整"]
    rows: list[Row] = [(*prefix, "系统", "沿用日（通道或降级输入无效）", len(carried),
                        _blank(carried[0] if carried else None), _blank(carried[-1] if carried else None))]
    for symbol in SYMBOLS:
        by_reason: dict[str, list[dt.date]] = {}
        for row in evaluated.asset_losses[symbol]:
            if row.excluded_reason:
                by_reason.setdefault(row.excluded_reason, []).append(row.start)
        rows.extend((*prefix, symbol, reason, len(dates), dates[0], dates[-1])
                    for reason, dates in sorted(by_reason.items()))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 汇总、排名与参照行
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSummary:
    candidate: Candidate
    convergence_date: dt.date
    intervals: int
    total_loss: Decimal
    danger_loss: Decimal
    drawdown_loss: Decimal
    opportunity_loss: Decimal
    switch_cost: Decimal
    miss_penalty: Decimal
    parts_gap: Decimal               # |分项之和 − 逐日合计|，只来自 Decimal 末位舍入
    executed_non_green_days: int
    billed_switches: int
    terminal_switch_unbilled: bool
    excluded_asset_intervals: int
    carried_days: int


def model_summary(states: CandidateStates, evaluated: CandidateEvaluation) -> ModelSummary:
    """一个设定的主损失分解与选参并列时用到的两个计数。"""
    daily = evaluated.daily_losses
    parts = [sum((getattr(row, name) for row in daily), Decimal(0))
             for name in ("danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost",
                          "full_exposure_cost")]
    gap = abs(sum(parts, Decimal(0)) - evaluated.total_loss)
    if gap > RECONCILE_TOLERANCE:
        raise ValueError("主损失分项之和与总损失不一致")
    return ModelSummary(
        evaluated.candidate, states.convergence_date, len(evaluated.days) - 1, evaluated.total_loss, *parts, gap,
        evaluated.executed_non_green_days, evaluated.billed_switches, daily[-1].terminal_switch_unbilled,
        sum(bool(row.excluded_reason) for rows in evaluated.asset_losses.values() for row in rows),
        sum(status != "完整" for status in evaluated.status))


SUMMARY_HEADER = (*SETTING_HEADER, "registration_order", "convergence_date", "t0", "tau", "first_loss_day",
                  "intervals", "total_loss", "danger_loss", "drawdown_loss", "opportunity_loss", "switch_cost",
                  "miss_penalty", "parts_minus_total_abs", "executed_non_green_days", "billed_switches",
                  "terminal_switch_unbilled", "excluded_asset_intervals", "carried_days")


def summary_row(prepared: PreparedEvaluation, row: ModelSummary) -> Row:
    return (*setting_prefix(row.candidate), row.candidate.order, row.convergence_date, prepared.t0,
            prepared.tau, prepared.first_loss_day, row.intervals, row.total_loss, row.danger_loss,
            row.drawdown_loss, row.opportunity_loss, row.switch_cost, row.miss_penalty, row.parts_gap,
            row.executed_non_green_days, row.billed_switches, _flag(row.terminal_switch_unbilled),
            row.excluded_asset_intervals, row.carried_days)


TIE_LEVELS = ("主损失", "执行非绿天数", "计费切换次数", "登记顺序")


def ranking_key(row: ModelSummary) -> tuple[Decimal, int, int, int]:
    """主损失最小；并列依次比较执行非绿天数、计费切换次数、登记顺序。"""
    return row.total_loss, row.executed_non_green_days, row.billed_switches, row.candidate.order


def decided_by(better: ModelSummary, worse: ModelSummary) -> str:
    """相邻两名由哪一级决出。"""
    for level, first, second in zip(TIE_LEVELS, ranking_key(better), ranking_key(worse), strict=True):
        if first != second:
            return level
    raise ValueError("两个设定的登记顺序相同")


def ranked(summaries: Sequence[ModelSummary], model: str) -> tuple[ModelSummary, ...]:
    """只在同一类模型内排名；解释性分解不参与。"""
    if model not in RANKED_MODELS:
        raise ValueError(f"{model} 不参与选参")
    return tuple(sorted((row for row in summaries if row.candidate.model == model), key=ranking_key))


RANKING_HEADER = ("status", "model", "rank", "k", "theta_p", "q", "registration_order", "total_loss",
                  "executed_non_green_days", "billed_switches", "decided_against_next_by")


def ranking_rows(summaries: Sequence[ModelSummary]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for model in RANKED_MODELS:
        ordered = ranked(summaries, model)
        for index, row in enumerate(ordered):
            following = decided_by(row, ordered[index + 1]) if index + 1 < len(ordered) else ""
            rows.append((STATUS_NOTE, model, index + 1, row.candidate.k, row.candidate.theta_p,
                         _blank(row.candidate.q), row.candidate.order, row.total_loss,
                         row.executed_non_green_days, row.billed_switches, following))
    return tuple(rows)


REFERENCE_HEADER = ("reference", "exposure", "total_loss", "danger_loss", "drawdown_loss", "opportunity_loss",
                    "switch_cost", "executed_non_green_days")


def reference_rows(references: Sequence[ReferenceResult]) -> tuple[Row, ...]:
    """参照行只给主损失、四个分项与执行非绿天数；不进排名、事件账与警报账。"""
    return tuple((row.name, row.exposure, row.total_loss, row.danger_loss, row.drawdown_loss,
                  row.opportunity_loss, row.switch_cost, row.executed_non_green_days) for row in references)


CONVERGENCE_HEADER = (*SETTING_HEADER, "convergence_date", "t0", "tau", "first_loss_day")


def convergence_rows(prepared: PreparedEvaluation) -> tuple[Row, ...]:
    return tuple((*setting_prefix(states.candidate), states.convergence_date, prepared.t0, prepared.tau,
                  prepared.first_loss_day) for states in prepared.states)


@dataclass(frozen=True)
class CandidateTables:
    """一个设定的全部表格行；由边界层逐个写出。"""

    summary: ModelSummary
    daily: tuple[Row, ...]
    events: tuple[Row, ...]
    event_classes: tuple[Row, ...]
    alerts: tuple[Row, ...]
    alert_summary: Row
    yearly: tuple[Row, ...]
    missing: tuple[Row, ...]


def candidate_tables(prepared: PreparedEvaluation, states: CandidateStates, evaluated: CandidateEvaluation,
                     events: Mapping[str, Sequence[ZZEvent]], merged: Sequence[MergedZZEvent],
                     rule: ScopeRule, weights: Mapping[str, Decimal]) -> CandidateTables:
    """把一个设定的评价结果整理成各张表的行；不做任何读写。"""
    prefix = setting_prefix(states.candidate)
    entries = (*asset_ledger_entries(prepared, states, events, rule),
               *merged_ledger_entries(prepared, states, merged, rule))
    ledger = alert_ledger(evaluated, events, weights)
    return CandidateTables(
        model_summary(states, evaluated), daily_rows(evaluated, weights, prepared.inputs.series),
        tuple(ledger_row(prefix, entry) for entry in entries), ledger_class_rows(prefix, entries),
        alert_rows(prefix, ledger), alert_summary_row(prefix, ledger), yearly_rows(prefix, evaluated),
        missing_rows(prefix, evaluated))


# ---------------------------------------------------------------------------
# N 各设定的 E 代价描述（不作范围判定，不参与选参）
# ---------------------------------------------------------------------------

N_EXIT_HEADER = ("model", "k", "theta_p", "q", "symbol", "peak_date", "trough_date", "inclusion",
                 "rebound_class", "green_at_trough_subclass", "half_way_green_count", "deepest_decline",
                 "rebound_recovery", "first_green_from_trough", "tau_n")
N_EXIT_SUMMARY_HEADER = ("model", "k", "theta_p", "q", "symbol", "tau_n", "included", "state_insufficient",
                         "right_censored", "class_1", "class_2", "class_3", "half_way_n", "half_way_median",
                         "half_way_p75", "half_way_max", "r_n", "r_median", "r_p75", "r_max")


def n_exit_tau(prepared: PreparedEvaluation) -> dt.date:
    """N 用自己 18 组设定的系统收敛日确定纳入门槛 τ_N。"""
    dates = [states.convergence_date for states in prepared.states if states.candidate.model == "N"]
    if len(dates) != 18:
        raise ValueError("N 应有 18 组登记候选")
    return loss_start(prepared.inputs.days, prepared.t0, dates, prepared.config.fixed_parameters())


def n_exit_costs(prepared: PreparedEvaluation, states: CandidateStates, symbol: str,
                 events: Sequence[ZZEvent], tau_n: dt.date) -> tuple[ExitCostEvent, ...]:
    """沿用 exit_costs 的逐事件口径；执行序列自 t0 连续生成。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    lights = tuple(row.light for row in states.rows)
    closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
    executions = execute_asset(axis, lights, closes,  # type: ignore[arg-type]
                               prepared.config.require_business_parameters().eta)
    return tuple(exit_cost_for_event(axis, executions, closes, event,
                                     events[index + 1].t0_date if index + 1 < len(events) else None,
                                     prepared.t0, tau_n)
                 for index, event in enumerate(events))


def n_exit_rows(prefix: Row, costs: Sequence[ExitCostEvent], tau_n: dt.date) -> tuple[Row, ...]:
    return tuple((*prefix, row.symbol, row.peak_date, row.trough_date, row.inclusion,
                  _blank(row.rebound_class), _blank(row.green_at_trough_subclass), row.half_way_green_count,
                  _blank(row.deepest_decline), _blank(row.rebound_recovery),
                  _blank(row.first_green_from_trough), tau_n) for row in costs)


def n_exit_summary_row(prefix: Row, symbol: str, costs: Sequence[ExitCostEvent], tau_n: dt.date) -> Row:
    """纳入、状态窗口不足、右截尾三类件数，三种退出类别件数，半山腰跌幅与 R 的分布。"""
    included = [row for row in costs if row.inclusion == "纳入"]
    classes = Counter(row.rebound_class for row in included)
    halfway = distribution(tuple(row.deepest_decline for row in included if row.deepest_decline is not None))
    rebound = distribution(tuple(row.rebound_recovery for row in included
                                 if row.rebound_recovery is not None))
    return (*prefix, symbol, tau_n, len(included),
            sum(row.inclusion.startswith("状态窗口不足") for row in costs),
            sum(row.inclusion == "右截尾" for row in costs),
            classes["①低点前已绿"], classes["②低点或之后转绿"], classes["③下一事件前未转绿"],
            halfway.n, _blank(halfway.median), _blank(halfway.p75), _blank(halfway.maximum),
            rebound.n, _blank(rebound.median), _blank(rebound.p75), _blank(rebound.maximum))
