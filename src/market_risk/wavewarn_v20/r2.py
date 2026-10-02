"""R2 的逐事件判定、比例与提示段账（产品规格第八节第 4、5 条；实施口径补充第 10、11、14、15 条）。纯计算。

事件判定严格按“左截断 → 输入不足 → 五类”的顺序。“之前”一律为严格之前（d < T3）。
提示 ⇔ 系统状态为一级或二级；“无法确定”指按缺值政策处理之后仍无法确定系统状态的信号日。
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise

from market_risk.wavewarn_v20.labels_r2 import R2Event


class R2Error(ValueError):
    """输入不合法，或“无法确定”的状态影响了分类（停下报告，不归类）。"""


class Prompt(Enum):
    YES = "提示"
    NO = "非提示"
    UNKNOWN = "无法确定"


class EventClass(Enum):
    LEFT_TRUNCATED = "左截断"
    INSUFFICIENT = "输入不足"
    COVERED = "持续覆盖达标"
    NEW = "新提示达标"          # 曾在 T3 之前及时产生新提示；不表示持续覆盖，也不表示已完成保护
    INTERRUPTED = "提示中断"
    LATE = "迟到"
    MISSED = "漏报"


class SegmentClass(Enum):
    PRE_WINDOW = "窗口前已启动"
    IN_EVENT = "事件内提示"
    AFTER_TROUGH = "低点后提示"
    EARLY = "提前提示"
    FALSE_ALARM = "误报"
    INCOMPLETE = "观察不完整"


ACHIEVED = (EventClass.COVERED, EventClass.NEW)


@dataclass(frozen=True)
class R2Rule:
    """R2 的达标下限（登记：3/5，即 60%）与提示段的观察期（登记：20 个交易日）。"""

    numerator: int
    denominator: int
    observation: int


@dataclass(frozen=True)
class Window:
    """评价窗口：信号日为 [first, last]（即 [j₀ − 1, E]），提示状态覆盖 [first 的前一个交易日, last]。"""

    days: tuple[dt.date, ...]                 # 交易日序列，至少覆盖 first 的前一个交易日至 last
    first: dt.date                            # f：窗口第一个信号日
    last: dt.date                             # E：窗口最后一个信号日
    status: Mapping[dt.date, Prompt]

    def __post_init__(self) -> None:
        if any(later <= earlier for earlier, later in pairwise(self.days)):
            raise R2Error("交易日序列必须严格升序")
        if self.first not in self.days or self.last not in self.days or self.first > self.last:
            raise R2Error("窗口的首末信号日必须在交易日序列里，且首日不晚于末日")
        start = self.index(self.first)
        if start == 0:
            raise R2Error("交易日序列须包含窗口第一个信号日的前一个交易日")
        expected = set(self.days[start - 1:self.index(self.last) + 1])
        if set(self.status) != expected or any(not isinstance(value, Prompt) for value in self.status.values()):
            raise R2Error("提示状态须恰好覆盖窗口第一个信号日的前一日至最后一个信号日")

    def index(self, day: dt.date) -> int:
        position = bisect_left(self.days, day)
        if position >= len(self.days) or self.days[position] != day:
            raise R2Error(f"{day} 不在交易日序列里")
        return position

    def before(self, day: dt.date) -> dt.date:
        return self.days[self.index(day) - 1]

    def span(self, start: dt.date, stop: dt.date, inclusive: bool) -> tuple[dt.date, ...]:
        """[start, stop) 或 [start, stop] 内的信号日；须全部落在已给出提示状态的范围内。"""
        days = self.days[self.index(start):self.index(stop) + (1 if inclusive else 0)]
        if any(day not in self.status for day in days):
            raise R2Error(f"[{start}, {stop}] 超出了已给出提示状态的范围")
        return days


@dataclass(frozen=True)
class EventJudgement:
    event: R2Event
    category: EventClass
    # 新提示达标：首次新提示日 d。peak_new_uncertain 为真时，它只是“首次可确认的新提示日”，
    # 即 [P, T3) 内最早的确定的“非提示 → 提示”转换日，不是真实的首次新提示日。
    first_new_day: dt.date | None
    executable_day: dt.date | None      # 可执行日 d+1；超出交易日序列时为 None。peak_new_uncertain 为真时按可确认日计算
    executable_offset: int | None       # d+1 相对 T3 的交易日偏移（0 即 T3 当日，负数为更早）；同上
    # P 当日是否为新提示无法确定（实施口径补充第 20 条）：只在 S_P 为提示、S_{P−1} 无法确定、
    # 而 [P, T3) 内另有确定的“非提示 → 提示”转换时为真；其他情形为假。
    peak_new_uncertain: bool


@dataclass(frozen=True)
class R2Result:
    """某资产的 R2。computable 为假即“R2 无法计算”（非左截断事件为 0），此时不给比例，也不判断是否达标。"""

    judgements: tuple[EventJudgement, ...]
    counts: Mapping[EventClass, int]
    computable: bool
    denominator: int                               # 全部非左截断事件
    achieved: int                                  # 持续覆盖达标 + 新提示达标
    new_only: int                                  # 只计新提示达标
    meets: bool | None                             # 是否不低于下限（整数判断）；无法计算时为 None
    # 剔除输入不足后的（达标，分母），只作描述；分母为 0 时“无定义”（None）
    excluding_insufficient: tuple[int, int] | None


def judge_event(event: R2Event, window: Window) -> EventJudgement:
    """单个事件的唯一分类。"""
    if event.peak <= window.first:
        return EventJudgement(event, EventClass.LEFT_TRUNCATED, None, None, None, False)
    early = window.span(event.peak, event.t3, inclusive=False)                 # [P, T3)
    states = [window.status[day] for day in early]
    if Prompt.UNKNOWN in states:
        return EventJudgement(event, EventClass.INSUFFICIENT, None, None, None, False)
    if all(state is Prompt.YES for state in states):
        return EventJudgement(event, EventClass.COVERED, None, None, None, False)
    restarts = [day for day, state, previous in zip(early[1:], states[1:], states, strict=False)
                if state is Prompt.YES and previous is Prompt.NO]
    uncertain = False
    if states[0] is Prompt.YES:
        previous = window.status.get(window.before(event.peak))
        if previous is None:
            raise R2Error(f"{event.asset} 高点 {event.peak} 的前一日不在已给出提示状态的范围内")
        if previous is Prompt.NO:
            restarts = [event.peak, *restarts]
        elif previous is Prompt.UNKNOWN:
            if not restarts:
                raise R2Error(f"{event.asset} 高点 {event.peak} 的前一日状态无法确定，"
                              "影响“新提示达标”与“提示中断”的区分")
            # 第 20 条：另有确定的转换，归“新提示达标”；但 P 当日是否为真实的新提示无法确定。
            uncertain = True
    if restarts:
        first = restarts[0]
        position = window.index(first) + 1
        executable = window.days[position] if position < len(window.days) else None
        offset = None if executable is None else position - window.index(event.t3)
        return EventJudgement(event, EventClass.NEW, first, executable, offset, uncertain)
    if states[0] is Prompt.YES:
        return EventJudgement(event, EventClass.INTERRUPTED, None, None, None, False)
    late = [window.status[day] for day in window.span(event.t3, event.trough, inclusive=True)]   # [T3, Tr]
    if Prompt.YES in late:
        return EventJudgement(event, EventClass.LATE, None, None, None, False)
    if Prompt.UNKNOWN in late:
        raise R2Error(f"{event.asset} 高点 {event.peak} 的事件：[T3, Tr] 内有无法确定的状态，影响“迟到”与“漏报”的区分")
    return EventJudgement(event, EventClass.MISSED, None, None, None, False)


def r2_result(events: Sequence[R2Event], window: Window, rule: R2Rule) -> R2Result:
    """R2 比例 = (持续覆盖达标 + 新提示达标) ÷ 全部非左截断事件；是否达标用整数判断。"""
    judgements = tuple(judge_event(event, window) for event in events)
    counts = {category: sum(1 for item in judgements if item.category is category) for category in EventClass}
    denominator = len(judgements) - counts[EventClass.LEFT_TRUNCATED]
    achieved = sum(counts[category] for category in ACHIEVED)
    if denominator == 0:
        return R2Result(judgements, counts, False, 0, 0, 0, None, None)
    evaluable = denominator - counts[EventClass.INSUFFICIENT]
    return R2Result(judgements, counts, True, denominator, achieved, counts[EventClass.NEW],
                    rule.denominator * achieved >= rule.numerator * denominator,
                    (achieved, evaluable) if evaluable > 0 else None)


@dataclass(frozen=True)
class Segment:
    """一个提示段：[start, end] 内连续处于提示状态；pre_window 表示它在窗口之前已经启动。"""

    start: dt.date
    end: dt.date
    pre_window: bool


@dataclass(frozen=True)
class SegmentLedger:
    """某资产的提示段账。false_alarm_ratio 为（误报，误报 + 提前提示）；分母为 0 时“无定义”（None）。"""

    classes: tuple[tuple[Segment, SegmentClass], ...]
    counts: Mapping[SegmentClass, int]
    false_alarm_ratio: tuple[int, int] | None


def prompt_segments(window: Window) -> tuple[Segment, ...]:
    """[f, E] 内连续处于提示状态的最长串。段首的前一状态无法确定时抛异常（补充第 11 条 c、第 14 条）。"""
    days = window.days[window.index(window.first):window.index(window.last) + 1]
    segments: list[Segment] = []
    start: dt.date | None = None
    pre_window = False
    for day in days:
        prompting = window.status[day] is Prompt.YES
        if prompting and start is None:
            previous = window.status[window.before(day)]
            if previous is Prompt.UNKNOWN:
                raise R2Error(f"提示段在 {day} 开始，但前一日的状态无法确定，起始日无法判断")
            start, pre_window = day, day == window.first and previous is Prompt.YES
        elif not prompting and start is not None:
            segments.append(Segment(start, window.before(day), pre_window))
            start = None
    if start is not None:
        segments.append(Segment(start, window.last, pre_window))
    return tuple(segments)


def classify_segment(segment: Segment, events: Sequence[R2Event], window: Window, rule: R2Rule) -> SegmentClass:
    """提示段对某资产的唯一分类；使用事件表中的全部事件，含左截断事件。"""
    if segment.pre_window:
        return SegmentClass.PRE_WINDOW
    start = segment.start
    if any(event.peak <= start < event.trough for event in events):
        return SegmentClass.IN_EVENT
    if any((event.trough <= start <= window.last) if event.end is None else (event.trough <= start < event.end)
           for event in events):
        return SegmentClass.AFTER_TROUGH
    position = window.index(start) + rule.observation                  # s + 20，按交易日序号
    limit = window.days[position] if position < len(window.days) else None
    if any(start < event.t5 and (limit is None or event.t5 <= limit) for event in events):
        return SegmentClass.EARLY
    if position <= window.index(window.last):
        return SegmentClass.FALSE_ALARM
    return SegmentClass.INCOMPLETE


def segment_ledger(events: Sequence[R2Event], window: Window, rule: R2Rule) -> SegmentLedger:
    """误报比例 = 误报 ÷ (误报 + 提前提示)；其余类别只列件数。"""
    classes = tuple((segment, classify_segment(segment, events, window, rule)) for segment in prompt_segments(window))
    counts = {category: sum(1 for _, value in classes if value is category) for category in SegmentClass}
    total = counts[SegmentClass.FALSE_ALARM] + counts[SegmentClass.EARLY]
    return SegmentLedger(classes, counts, (counts[SegmentClass.FALSE_ALARM], total) if total > 0 else None)
