"""转绿双层报告（纯计算，描述性，不参与任何判定）：补充登记 D。

事件的纳入与三类分类沿用 exit_costs 的逐事件结果（类别①低点前已绿、②低点或之后转绿、③下一事件前未转绿）。
本模块只在其上补充：类别③的原因、低点后是否有完整的观察窗口、可观察的实际首次转绿执行日与延迟，
以及“低点后若干交易日内转绿”的两种分母。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import INFINITY, linear_percentile
from market_risk.wavewarn.exit_costs import ExitCostEvent

CLASS_1, CLASS_2, CLASS_3 = "①低点前已绿", "②低点或之后转绿", "③下一事件前未转绿"
REASON_NEXT_EVENT = "(a) 下一事件 T0 先到"
REASON_WINDOW_END = "(b) 评价窗口结束，尚未观察到转绿"


@dataclass(frozen=True)
class GreenEvent:
    """一个纳入事件的转绿情况。"""

    symbol: str
    peak_date: dt.date
    trough_date: dt.date
    rebound_class: str
    class_3_reason: str                 # 只对类别③；其余为空
    complete_window: bool               # Tr 之后第 window 个交易日不晚于窗口最后一日
    first_green: dt.date | None         # 低点当日或之后实际首次转绿的执行日（类别③也给出）；类别①为空
    delay: int | None                   # first_green 与 Tr 之间的交易日数
    unobserved_days: int | None         # 窗口内始终未转绿时：截至窗口末日已过的交易日数
    half_way_green_count: int           # 低点前（[P, Tr) 内）转绿次数
    deepest_decline: Decimal | None     # 低点前转绿之后到最终低点的最深跌幅
    within_window: bool                 # 类别②且延迟 ≤ window


def green_event(axis: Sequence[dt.date], row: ExitCostEvent, next_t0: dt.date | None, window: int) -> GreenEvent:
    """row 须是纳入事件；next_t0 为同资产下一事件的 T0（没有下一事件时为空）。"""
    if row.inclusion != "纳入" or row.rebound_class is None:
        raise ValueError("转绿报告只接受纳入事件")
    position = {day: index for index, day in enumerate(axis)}
    trough, last = position[row.trough_date], len(axis) - 1
    first = row.first_green_from_trough if row.rebound_class != CLASS_1 else None
    delay = position[first] - trough if first is not None else None
    reason = ""
    if row.rebound_class == CLASS_3:
        reason = REASON_NEXT_EVENT if next_t0 is not None else REASON_WINDOW_END
    unobserved = last - trough if row.rebound_class != CLASS_1 and first is None else None
    return GreenEvent(row.symbol, row.peak_date, row.trough_date, row.rebound_class, reason,
                      trough + window <= last, first, delay, unobserved, row.half_way_green_count,
                      row.deepest_decline,
                      row.rebound_class == CLASS_2 and delay is not None and delay <= window)


def green_events(axis: Sequence[dt.date], costs: Sequence[ExitCostEvent], next_t0: Sequence[dt.date | None],
                 window: int) -> tuple[GreenEvent, ...]:
    """costs 与 next_t0 按事件顺序对齐；只保留纳入事件。"""
    if len(costs) != len(next_t0):
        raise ValueError("事件与下一事件 T0 未对齐")
    return tuple(green_event(axis, row, following, window)
                 for row, following in zip(costs, next_t0, strict=True) if row.inclusion == "纳入")


@dataclass(frozen=True)
class GreenSummary:
    symbol: str
    included: int
    class_1: int
    class_2: int
    class_3: int
    class_3_next_event: int             # 类别③原因 (a)
    class_3_window_end: int             # 类别③原因 (b)
    complete_window: int                # 有完整观察窗口的事件数
    within_window: int                  # 类别②且延迟 ≤ window
    within_window_complete: int         # 上一项中有完整观察窗口的
    median_class_2: Decimal | None      # 类别②延迟的中位数（有条件的统计）
    median_conservative: Decimal | None # 类别②与③合并、③按无穷计的中位数（保守综合口径）

    def share(self, count: int) -> Decimal | None:
        """占全部纳入事件的比例。"""
        return Decimal(count) / self.included if self.included else None

    @property
    def within_share_all(self) -> Decimal | None:
        return self.share(self.within_window)

    @property
    def within_share_complete(self) -> Decimal | None:
        """分母只含有完整观察窗口的事件；分子同样只取其中的事件。"""
        return Decimal(self.within_window_complete) / self.complete_window if self.complete_window else None


def green_summary(symbol: str, events: Sequence[GreenEvent]) -> GreenSummary:
    """类别①单列，不进入延迟分布；保守口径只把类别③按无穷计入，不代表真实等待时间无穷。"""
    count = lambda name: sum(item.rebound_class == name for item in events)  # noqa: E731
    delays = [Decimal(item.delay) for item in events if item.rebound_class == CLASS_2 and item.delay is not None]
    if len(delays) != count(CLASS_2):
        raise ValueError("类别②事件缺少首个绿灯执行日")
    conservative = [*delays, *(INFINITY for item in events if item.rebound_class == CLASS_3)]
    return GreenSummary(
        symbol, len(events), count(CLASS_1), count(CLASS_2), count(CLASS_3),
        sum(item.class_3_reason == REASON_NEXT_EVENT for item in events),
        sum(item.class_3_reason == REASON_WINDOW_END for item in events),
        sum(item.complete_window for item in events), sum(item.within_window for item in events),
        sum(item.within_window and item.complete_window for item in events),
        linear_percentile(delays, Decimal("0.5")), linear_percentile(conservative, Decimal("0.5")))
