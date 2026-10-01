"""转绿瓶颈分解（纯计算，描述性诊断）。不改变任何规则。

口径（负责人 2026-10-01 确认）：
- 各条件的“首次成立日”在 [Tr, 同资产下一事件 T0 的前一日] 内搜索（无下一事件则到窗口末日），
  Tr 当天已成立记 0，范围内从未成立记空。
- 瓶颈只对“绿灯信号日不早于 Tr”的类别②事件判定：对每个候选条件，找出包含绿灯信号日的那一段连续成立期，
  取其起点；起点最晚的条件为瓶颈，起点同日时各计一次。
- 条件 (7)“系统已由红转黄”以“系统不处于红灯”这一状态计，只在事件内出现过红灯时适用。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import distribution
from market_risk.wavewarn.condition_trace import NOT_RED, ConditionDay
from market_risk.wavewarn.exit_costs import ExitCostEvent

CLASS_1 = "类别①低点前已绿"
CLASS_3 = "类别③下一事件前未转绿"
CLASS_2_EARLY = "类别②，信号日为 Tr−1"
CLASS_2_JUDGED = "类别②，判瓶颈"
CATEGORIES = (CLASS_1, CLASS_3, CLASS_2_EARLY, CLASS_2_JUDGED)
SIGNAL_DAY = "(8) 首个绿灯信号日"
EXECUTION_DAY = "(8) 首个绿灯执行日"
Row = tuple[object, ...]


@dataclass(frozen=True)
class EventBottleneck:
    symbol: str
    peak_date: dt.date
    trough_date: dt.date
    search_end: dt.date
    category: str
    first_days: Mapping[str, int | None]     # 条件 → 自 Tr 起首次成立的交易日数；未成立或不适用为 None
    signal_days: int | None                  # 绿灯信号日距 Tr 的交易日数（可为 −1）
    execution_days: int | None
    bottlenecks: tuple[str, ...]             # 起点最晚的条件；并列时多个
    bottleneck_days: int | None              # 瓶颈那段连续成立期的起点距 Tr 的交易日数（可为负）


def _holds(day: ConditionDay, condition: str) -> bool:
    if condition == NOT_RED:
        return day.light != "红"
    return day.flags[condition] is True


def first_hold(trace: Sequence[ConditionDay], condition: str, start: int, end: int) -> int | None:
    """[start, end] 内条件首次成立的位置相对 start 的天数。"""
    return next((index - start for index in range(start, end + 1) if _holds(trace[index], condition)), None)


def run_start(trace: Sequence[ConditionDay], condition: str, signal: int) -> int:
    """包含绿灯信号日的那一段连续成立期的起点位置；信号日当天条件必须成立。"""
    if not _holds(trace[signal], condition):
        raise ValueError(f"{trace[signal].date} 绿灯信号日 {condition} 不成立")
    index = signal
    while index > 0 and _holds(trace[index - 1], condition):
        index -= 1
    return index


def red_applicable(trace: Sequence[ConditionDay], first: int, last: int) -> bool:
    """事件内是否出现过红灯：[first, last] 内有红灯日。"""
    return any(trace[index].light == "红" for index in range(max(first, 0), last + 1))


def judge_bottleneck(trace: Sequence[ConditionDay], candidates: Sequence[str], peak: int,
                     signal: int) -> tuple[tuple[str, ...], int]:
    """起点最晚的候选条件及该起点位置；(7) 只在本段非红期之前的红灯日不早于高点 P 时参与。"""
    starts = {name: run_start(trace, name, signal) for name in candidates}
    red_start = run_start(trace, NOT_RED, signal)
    if red_start > 0 and red_start - 1 >= peak and trace[red_start - 1].light == "红":
        starts[NOT_RED] = red_start
    latest = max(starts.values())
    return tuple(name for name, value in starts.items() if value == latest), latest


def event_bottleneck(trace: Sequence[ConditionDay], cost: ExitCostEvent, next_t0: dt.date | None,
                     listed: Sequence[str], candidates: Sequence[str]) -> EventBottleneck:
    """一个纳入事件的各条件首次成立天数、类别与瓶颈。trace 须按日期连续且覆盖 [P, 搜索上限]。"""
    position = {day.date: index for index, day in enumerate(trace)}
    peak, trough = position[cost.peak_date], position[cost.trough_date]
    end = position[next_t0] - 1 if next_t0 is not None and next_t0 in position else len(trace) - 1
    first_days = {name: first_hold(trace, name, trough, end) for name in listed}
    first_days[NOT_RED] = (first_hold(trace, NOT_RED, trough, end) if red_applicable(trace, peak, end) else None)
    base = (cost.symbol, cost.peak_date, cost.trough_date, trace[end].date)
    if cost.rebound_class != "②低点或之后转绿":
        category = CLASS_1 if cost.rebound_class == "①低点前已绿" else CLASS_3
        return EventBottleneck(*base, category, first_days, None, None, (), None)
    if cost.first_green_from_trough is None:
        raise ValueError("类别②事件缺少首个绿灯执行日")
    execution = position[cost.first_green_from_trough]
    signal = execution - 1
    if signal < trough:
        return EventBottleneck(*base, CLASS_2_EARLY, first_days, signal - trough, execution - trough, (), None)
    names, start = judge_bottleneck(trace, candidates, peak, signal)
    return EventBottleneck(*base, CLASS_2_JUDGED, first_days, signal - trough, execution - trough, names,
                           start - trough)


def setting_bottlenecks(trace: Sequence[ConditionDay], costs: Sequence[ExitCostEvent],
                        next_t0: Mapping[tuple[str, dt.date], dt.date | None], listed: Sequence[str],
                        candidates: Sequence[str]) -> tuple[EventBottleneck, ...]:
    """只处理纳入事件（P ≥ τ，已确认）。"""
    return tuple(event_bottleneck(trace, cost, next_t0[(cost.symbol, cost.peak_date)], listed, candidates)
                 for cost in costs if cost.inclusion == "纳入")


CATEGORY_HEADER = ("symbol", "included", *CATEGORIES, "tied_events")
CONDITION_HEADER = ("symbol", "condition", "bottleneck_candidate", "found", "not_found_or_not_applicable",
                    "first_days_median", "first_days_p75", "first_days_max", "bottleneck_events")


def category_row(symbol: str, events: Sequence[EventBottleneck]) -> Row:
    own = [event for event in events if event.symbol == symbol]
    counts = Counter(event.category for event in own)
    return (symbol, len(own), *(counts[name] for name in CATEGORIES),
            sum(len(event.bottlenecks) > 1 for event in own))


def _days_cells(values: Sequence[int | None]) -> Row:
    found = [Decimal(value) for value in values if value is not None]
    spread = distribution(found)
    return (len(found), len(values) - len(found), *("" if item is None else item
                                                    for item in (spread.median, spread.p75, spread.maximum)))


def condition_rows(symbol: str, events: Sequence[EventBottleneck], listed: Sequence[str],
                   candidates: Sequence[str]) -> tuple[Row, ...]:
    """每个条件：首次成立天数的分布（全部纳入事件）与成为瓶颈的事件数（只在判瓶颈的事件中）。"""
    own = [event for event in events if event.symbol == symbol]
    hits = Counter(name for event in own for name in event.bottlenecks)
    rows: list[Row] = [(symbol, name, "是" if name in candidates or name == NOT_RED else "否（只列天数）",
                        *_days_cells([event.first_days[name] for event in own]), hits[name])
                       for name in (*listed, NOT_RED)]
    for name, values in ((SIGNAL_DAY, [event.signal_days for event in own]),
                         (EXECUTION_DAY, [event.execution_days for event in own])):
        rows.append((symbol, name, "—", *_days_cells(values), ""))
    return tuple(rows)


EVENT_HEADER = ("symbol", "peak_date", "trough_date", "search_end", "category", "signal_days", "execution_days",
                "bottlenecks", "bottleneck_start_days")


def event_row(event: EventBottleneck, listed: Sequence[str]) -> Row:
    """逐事件一行；各条件的首次成立天数接在固定列之后，顺序同 listed 加 (7)。"""
    blank = lambda value: "" if value is None else value  # noqa: E731
    return (event.symbol, event.peak_date, event.trough_date, event.search_end, event.category,
            blank(event.signal_days), blank(event.execution_days), "；".join(event.bottlenecks),
            blank(event.bottleneck_days), *(blank(event.first_days[name]) for name in (*listed, NOT_RED)))
