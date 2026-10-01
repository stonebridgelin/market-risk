"""亮灯时间分解（纯计算，描述性诊断）。不改变任何规则。

口径（负责人 2026-10-01 确认）：每个执行非绿区间 j 的原因取信号日 j−1 当天的状态。
互斥的主分类三类合计 100%：
- A 有通道激活；
- B 无通道激活，但转绿（或红转黄）条件未满足；
- C 降级限速：当日刚由红降黄，且黄转绿条件当日已全部满足（每天最多降一级）。
细分原因（具体通道、具体条件）多标签计数，各项之和可超过 100%。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.condition_trace import Q_OK, RED_QUIET, ConditionDay

MAIN_ACTIVE = "A 有通道激活"
MAIN_UNMET = "B 无通道激活但条件未满足"
MAIN_RATE_LIMIT = "C 降级限速（刚由红降黄，黄转绿条件已全部满足）"
MAIN_CLASSES = (MAIN_ACTIVE, MAIN_UNMET, MAIN_RATE_LIMIT)
Row = tuple[object, ...]


@dataclass(frozen=True)
class DayReason:
    main: str
    labels: tuple[str, ...]


def day_reason(day: ConditionDay, required: Sequence[str]) -> DayReason:
    """信号日为非绿时的原因；required 为该版本的黄转绿条件。"""
    if day.light == "绿":
        raise ValueError(f"{day.date} 信号为绿，不属于非绿原因")
    if day.active:
        return DayReason(MAIN_ACTIVE, tuple(f"通道激活：{name}" for name in day.active))
    if day.light == "红":
        unmet = [name for name, ok in ((RED_QUIET, day.red_quiet), (Q_OK, day.flags[Q_OK] is True)) if not ok]
        if not unmet:
            raise ValueError(f"{day.date} 红灯、无通道激活且红转黄条件已满足，与状态机不符")
        return DayReason(MAIN_UNMET, tuple(f"红转黄未满足：{name}" for name in unmet))
    unmet = [name for name in required if day.flags[name] is not True]
    if not unmet:
        if day.previous_light != "红":
            raise ValueError(f"{day.date} 黄灯、无通道激活且黄转绿条件已满足，与状态机不符")
        return DayReason(MAIN_RATE_LIMIT, ("降级限速：每天最多降一级",))
    return DayReason(MAIN_UNMET, tuple(f"黄转绿未满足：{name}" for name in unmet))


@dataclass(frozen=True)
class ReasonCounts:
    non_green: int
    main: Counter[str]
    labels: Counter[str]


def count_reasons(reasons: Sequence[DayReason]) -> ReasonCounts:
    return ReasonCounts(len(reasons), Counter(reason.main for reason in reasons),
                        Counter(label for reason in reasons for label in reason.labels))


def non_green_reasons(trace: Sequence[ConditionDay], executed: Sequence[str], interval_dates: Sequence,
                      required: Sequence[str]) -> ReasonCounts:
    """executed[i] 是区间 interval_dates[i] 的执行灯色 S_{j−1}；其信号日是 trace 中该日期的前一行。"""
    position = {day.date: index for index, day in enumerate(trace)}
    reasons = []
    for date, light in zip(interval_dates, executed, strict=True):
        if light == "绿":
            continue
        if position[date] < 1 or trace[position[date] - 1].light != light:
            raise ValueError(f"{date} 执行灯色与前一日信号不一致")
        signal = trace[position[date] - 1]
        reasons.append(day_reason(signal, required))
    return count_reasons(reasons)


REASON_HEADER = ("level", "reason", "days", "share_of_non_green")


def reason_rows(counts: ReasonCounts) -> tuple[Row, ...]:
    """主分类三行（合计 100%）在前；细分原因按天数从多到少，占比之和可超过 100%。"""
    share = lambda value: Decimal(value) / counts.non_green if counts.non_green else ""  # noqa: E731
    rows: list[Row] = [("主分类", name, counts.main[name], share(counts.main[name])) for name in MAIN_CLASSES]
    rows.extend(("细分原因", name, value, share(value))
                for name, value in sorted(counts.labels.items(), key=lambda item: (-item[1], item[0])))
    return tuple(rows)
