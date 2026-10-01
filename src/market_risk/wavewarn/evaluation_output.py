"""开发期 v1.2.1 评价明细导出；全部数值可按资产、日期复算。"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.calibration import distribution
from market_risk.wavewarn.convergence import loss_start
from market_risk.wavewarn.evaluation import (
    CandidateEvaluation,
    CandidateStates,
    PreparedEvaluation,
    allocated_asset_days,
    event_scope,
)
from market_risk.wavewarn.execution import execute_asset
from market_risk.wavewarn.exit_costs import exit_cost_for_event
from market_risk.wavewarn.labels_zz import ZZEvent
from market_risk.wavewarn.ledgers import (
    build_alert_ledger,
    classify_asset_event,
    event_timing_details,
    yearly_alert_summary,
)


def _candidate_prefix(evaluated: CandidateEvaluation) -> tuple[object, ...]:
    candidate = evaluated.candidate
    return candidate.model, candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else ""


def write_daily_details(writer: csv.writer, evaluated: CandidateEvaluation,
                        weights: Mapping[str, Decimal], prices: Mapping[str, Mapping[dt.date, Decimal]]) -> None:
    """每个设定、资产、区间一行；末日另留一行标记不计费切换。"""
    prefix = _candidate_prefix(evaluated)
    for row in allocated_asset_days(evaluated, weights):
        writer.writerow((*prefix, row.symbol, row.date, row.next_date or "", prices[row.symbol].get(row.date, ""),
                         prices[row.symbol].get(row.next_date, "") if row.next_date else "",
                         row.signal, row.executed, row.exposure, row.weight,
                         row.log_return if row.log_return is not None else "",
                         "未知" if row.dangerous is None else "是" if row.dangerous else "否",
                         row.drawdown_increment, row.danger_loss, row.drawdown_loss,
                         row.opportunity_loss, row.weighted_price_loss, row.system_switches,
                         row.switch_share, row.full_exposure_share, row.total,
                         row.excluded_reason, "是" if row.terminal_switch_unbilled else "否"))


DAILY_HEADER = ("model", "k", "theta_p", "q", "symbol", "date", "next_date", "close", "next_close",
                "signal_light", "executed_light", "exposure", "weight", "log_return", "dangerous",
                "drawdown_increment", "danger_loss_raw", "drawdown_loss_raw", "opportunity_loss_raw",
                "weighted_price_loss", "system_switch_count", "switch_cost_share", "full_exposure_cost_share",
                "row_total", "excluded_reason", "terminal_switch_unbilled")


def write_event_rows(writer: csv.writer, prepared: PreparedEvaluation, states: CandidateStates,
                     events: Mapping[str, Sequence[ZZEvent]]) -> Counter[str]:
    """只在 P−20≥τ 时给五类归因；其他事件仍保留逐件审计行。"""
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    lights = tuple(row.light for row in states.rows)
    active = tuple(row.active_channels for row in states.rows)
    reasons = tuple(row.reason for row in states.rows)
    candidate = states.candidate
    prefix = (candidate.model, candidate.k, candidate.theta_p, candidate.q if candidate.q is not None else "")
    counts: Counter[str] = Counter()
    for symbol in ("SPX", "QQQ"):
        closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
        for event in events[symbol]:
            crossing, full, ledger = event_scope(prepared.inputs.days, event,
                                                  prepared.tau, prepared.first_loss_day)
            inclusion = "右截尾" if event.right_censored else "纳入" if ledger else "状态窗口不足"
            counts[f"{symbol}:{inclusion}"] += 1
            if crossing:
                counts[f"{symbol}:跨损失起点"] += 1
            if full:
                counts[f"{symbol}:完整危险事件"] += 1
            if full and not ledger:
                counts[f"{symbol}:仅P−20不足"] += 1
            classified = classify_asset_event(axis, lights, event) if ledger else None
            timing = (event_timing_details(axis, lights, closes, event, active, reasons)
                      if ledger else None)
            if classified is not None and classified.classification is not None:
                counts[f"{symbol}:{classified.classification}"] += 1
            writer.writerow((*prefix, symbol, event.peak_date, event.t0_date, event.trough_date,
                             event.end_date or "", inclusion, "是" if crossing else "否",
                             "是" if full else "否", classified.classification if classified else "",
                             classified.signal_before_t0 if classified else "",
                             classified.reduction_executed_before_t0 if classified else "",
                             classified.alert_round_trips if classified else "",
                             classified.light_switches if classified else "",
                             timing.first_alert_date if timing else "",
                             timing.first_green_date if timing else "",
                             timing.release_system_reason if timing else "",
                             ";".join(timing.release_exited_channels) if timing else "",
                             *(timing.decline_after_green_percent[window]
                               if timing and timing.decline_after_green_percent[window] is not None else ""
                               for window in (5, 10, 20))))
    return counts


EVENT_HEADER = ("model", "k", "theta_p", "q", "symbol", "peak_date", "t0_date", "trough_date", "end_date",
                "inclusion", "cross_first_loss_day", "complete_for_penalty", "event_class",
                "signal_before_t0", "reduction_executed_before_t0", "alert_round_trips", "light_switches",
                "first_alert_date", "first_green_date", "release_system_reason", "release_exited_channels",
                "decline_after_green_5_pct", "decline_after_green_10_pct", "decline_after_green_20_pct")


def write_alert_rows(writer: csv.writer, yearly_writer: csv.writer, evaluated: CandidateEvaluation,
                     events: Mapping[str, Sequence[ZZEvent]], weights: Mapping[str, Decimal]) -> Counter[str]:
    """输出警报段、年度占用及无事件/尾段未定的互斥审计计数。"""
    all_events = (*events["SPX"], *events["QQQ"])
    prefix = _candidate_prefix(evaluated)
    ledgers = build_alert_ledger(evaluated.days, evaluated.signals, evaluated.active_channels,
                                 all_events, dict(evaluated.asset_losses), dict(weights))
    counts: Counter[str] = Counter()
    for row in ledgers:
        category = "尾段待定" if row.tail_pending else "无事件警报段" if row.no_event_alert else "关联事件"
        counts[category] += 1
        counts["区间外亮灯天数"] += row.outside_danger_days
        counts["被排除区间天数"] += row.excluded_interval_days
        writer.writerow((*prefix, row.start, row.end, row.trading_days, category,
                         ";".join(row.active_channels), ";".join(row.covered_events),
                         row.protected_decline, row.outside_danger_days, row.net_opportunity_cost,
                         row.tail_pending_days, row.excluded_interval_days))
    yearly = yearly_alert_summary(evaluated.days, evaluated.signals, evaluated.executions["SPX"],
                                  evaluated.active_channels)
    for row in yearly:
        yearly_writer.writerow((*prefix, row.year, row.trading_days, row.non_green_days,
                                row.non_green_ratio, row.longest_alert_days, row.executed_switches,
                                ";".join(f"{key}:{value}" for key, value in sorted(row.active_channel_days.items()))))
    return counts


ALERT_HEADER = ("model", "k", "theta_p", "q", "start", "end", "trading_days", "category",
                "active_channels", "covered_events", "protected_decline", "outside_danger_days",
                "net_opportunity_cost", "tail_pending_days", "excluded_interval_days")
YEARLY_HEADER = ("model", "k", "theta_p", "q", "year", "trading_days", "non_green_days", "non_green_ratio",
                 "longest_alert_days", "executed_switches", "active_channel_days")


def missing_counts(evaluated: CandidateEvaluation) -> Counter[str]:
    """系统沿用、跨缺价与逐资产未定原因按模型设定分开计数。"""
    counts: Counter[str] = Counter()
    counts["系统沿用日"] = sum(status == "沿用" for status in evaluated.status)
    for symbol in ("SPX", "QQQ"):
        for row in evaluated.asset_losses[symbol]:
            if row.excluded_reason:
                counts[f"{symbol}:{row.excluded_reason}"] += 1
    return counts


def write_column_readme(path: Path) -> None:
    path.write_text(
        "# 开发期 v1.2.1 逐日明细列说明\n\n"
        "每个模型设定、资产、起点日期固定两条资产行；期末另保留两行标记末日切换，"
        "无后续价格区间，价格项与切换分摊均为0。只使用截至2016-12-30的开发期输入。\n\n"
        "`date→next_date` 是价格区间，`close/next_close` 是不复权收盘价；"
        "`signal_light` 是当日收盘信号，`executed_light` 是当日收盘后已执行灯色，"
        "`exposure` 为绿1、黄η=0.5、红0。`log_return=ln(next_close/close)`，无单位。"
        "`dangerous` 为当前资产的区间标签；未定记“未知”。\n\n"
        "危险项、回撤项、机会项均为该资产**未乘权重**的原始无量纲损失，逐项公式遵循"
        "规格第五节与 `loss.py`；`drawdown_increment` 为本日超过2%噪声下限的回撤纪录增量。"
        "`weight` 为资产价格权重 w_a；`weighted_price_loss=w_a×(三项原始损失之和)`。"
        "`system_switch_count` 为系统执行切换标记，两资产行相同，末日若有切换仍标1。"
        "`switch_cost_share=γ×当日计费切换次数÷2`：**按两条资产行均分，与 w_a 无关**；"
        "末日分摊0。`full_exposure_cost_share` 同样均分，当前 μ=0。"
        "`row_total=weighted_price_loss+switch_cost_share+full_exposure_cost_share`，"
        "同日两资产合计等于系统日度主损失，全部日期合计等于该设定总损失。\n\n"
        "`excluded_reason` 表明跨缺价、右截尾或寻峰尾段等原因；被排除资产区间的价格项为0，"
        "系统切换罚分仍照常计费。`terminal_switch_unbilled` 标记窗口末日切换。"
        "事件账、警报账与年度警报另见相应 CSV。\n",
        encoding="utf-8",
    )


def write_n_exit_costs(path: Path, prepared: PreparedEvaluation,
                       events: Mapping[str, Sequence[ZZEvent]]) -> tuple[dt.date, tuple[tuple[object, ...], ...]]:
    """N 的 18 组 E2 只给描述性退出代价，不据此判范围或选择参数。"""
    n_states = [states for states in prepared.states if states.candidate.model == "N"]
    if len(n_states) != 18:
        raise ValueError("N 应有 18 组登记候选")
    tau_n = loss_start(prepared.inputs.days, prepared.t0,
                       [states.convergence_date for states in n_states], prepared.config.fixed_parameters())
    axis = prepared.inputs.days[prepared.inputs.days.index(prepared.t0):]
    eta = prepared.config.require_business_parameters().eta
    summary = []
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("model", "k", "theta_p", "q", "symbol", "peak_date", "trough_date", "inclusion",
                         "rebound_class", "half_way_green_count", "deepest_decline", "rebound_recovery",
                         "first_green_from_trough", "tau_n"))
        for states in n_states:
            lights = tuple(row.light for row in states.rows)
            for symbol in ("SPX", "QQQ"):
                closes = tuple(prepared.inputs.series[symbol].get(day) for day in axis)
                executions = execute_asset(axis, lights, closes, eta)
                results = []
                for index, event in enumerate(events[symbol]):
                    next_t0 = events[symbol][index + 1].t0_date if index + 1 < len(events[symbol]) else None
                    row = exit_cost_for_event(axis, executions, closes, event, next_t0, prepared.t0, tau_n)
                    results.append(row)
                    writer.writerow((states.candidate.model, states.candidate.k, states.candidate.theta_p,
                                     states.candidate.q, symbol, event.peak_date, event.trough_date,
                                     row.inclusion, row.rebound_class or "", row.half_way_green_count,
                                     row.deepest_decline if row.deepest_decline is not None else "",
                                     row.rebound_recovery if row.rebound_recovery is not None else "",
                                     row.first_green_from_trough or "", tau_n))
                included = [row for row in results if row.inclusion == "纳入"]
                rebound = distribution(tuple(row.rebound_recovery for row in included
                                             if row.rebound_recovery is not None))
                halfway = distribution(tuple(row.deepest_decline for row in included
                                             if row.deepest_decline is not None))
                summary.append((states.candidate.k, states.candidate.theta_p, states.candidate.q, symbol,
                                len(included), sum(row.inclusion.startswith("状态窗口不足") for row in results),
                                sum(row.inclusion == "右截尾" for row in results),
                                halfway.n, halfway.median, halfway.p75, halfway.maximum,
                                rebound.n, rebound.median, rebound.p75, rebound.maximum))
    return tau_n, tuple(summary)
