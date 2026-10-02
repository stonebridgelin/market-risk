"""失败路径分析的三类背景标签（纯计算，描述性，不进入任何信号与选择）。

- 通道与系统规则同时出现统计：非绿区间按信号日的通道状态归入唯一一类；
- 段相对合并事件的位置：按每段起始执行日归入唯一一类，保留全部匹配事件的编号；
- 市场环境：熊市优先，其余按完整日历年的 SPX 价格指数年度收益。
分类只作描述，不是因果证明；“系统保持”不表示这种保持合理。不读写文件，不导入读写模块。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market_risk.wavewarn.failure_path import GREEN, PathError

HOLD, PULLBACK, TREND, STRESS, MULTIPLE = "系统保持", "只有回调层", "只有趋势层", "只有压力层", "多层同时"
CHANNEL_CLASSES = (HOLD, PULLBACK, TREND, STRESS, MULTIPLE)
LAYER_CLASS = {"回调层": PULLBACK, "趋势层": TREND, "压力层": STRESS}

BEFORE_T0, AFTER_T0, AFTER_TROUGH, AFTER_TROUGH_OPEN, PRE_PEAK, OUTSIDE, OUTSIDE_TAIL = (
    "确认前", "确认后", "低点后", "低点后（事件未结束）", "高点前20日", "事件外", "事件外（尾段未定）")
POSITIONS = (BEFORE_T0, AFTER_T0, AFTER_TROUGH, AFTER_TROUGH_OPEN, PRE_PEAK, OUTSIDE, OUTSIDE_TAIL)

BEAR, UP_YEAR, DOWN_YEAR, FLAT_YEAR, YEAR_UNAVAILABLE = "熊市", "上涨年", "下跌年", "平淡年", "完整年度分类不可得"
ENVIRONMENTS = (BEAR, UP_YEAR, DOWN_YEAR, FLAT_YEAR, YEAR_UNAVAILABLE)


def channel_layer(name: str) -> str:
    """通道名 → 所属的层。回调层 P、PR；趋势层 MR；压力层 BW、V。同层通道对两个资产取并集。"""
    prefix = name.split("_")[0]
    if prefix in ("P", "PR"):
        return "回调层"
    if prefix == "MR":
        return "趋势层"
    if prefix in ("BW", "V"):
        return "压力层"
    raise PathError(f"未登记的通道：{name}")


def channel_class(active: Sequence[str]) -> str:
    """一个非绿区间按信号日激活的通道归入唯一一类。"""
    layers = {channel_layer(name) for name in active}
    if not layers:
        return HOLD
    return LAYER_CLASS[next(iter(layers))] if len(layers) == 1 else MULTIPLE


def channel_classes(lights: Sequence[str], active: Sequence[Sequence[str]]) -> tuple[str | None, ...]:
    """各区间的类别：非绿区间恰好一类，绿灯区间为空。active 为各区间信号日（j−1）激活的通道。"""
    if len(lights) != len(active):
        raise PathError("执行灯色与通道状态的区间数不一致")
    return tuple(None if light == GREEN else channel_class(names) for light, names in zip(lights, active, strict=True))


@dataclass(frozen=True)
class ClassTotal:
    name: str
    days: int
    gap: float


def class_totals(classes: Sequence[str | None], gaps: Sequence[float], lights: Sequence[str]) -> tuple[ClassTotal, ...]:
    """五类各自的天数与 Σ d_j；检查每个非绿区间恰好归入一类。"""
    if any((light == GREEN) != (name is None) for light, name in zip(lights, classes, strict=True)):
        raise PathError("存在未归类的非绿区间，或被归类的绿灯区间")
    return tuple(ClassTotal(name, sum(item == name for item in classes),
                            math.fsum(gap for item, gap in zip(classes, gaps, strict=True) if item == name))
                 for name in CHANNEL_CLASSES)


@dataclass(frozen=True)
class MergedEvent:
    """合并事件：P 取成员最早的高点，T0 取最早的确认日，Tr 取最晚的低点，End 取最晚的结束日。

    任一成员右截尾，合并事件即右截尾，End 为空（Tr 是暂定低点）。
    """

    number: int
    peak: dt.date
    t0: dt.date
    trough: dt.date
    end: dt.date | None
    right_censored: bool
    members: tuple[str, ...]               # 资产:高点:低点


@dataclass(frozen=True)
class Position:
    category: str
    event: int | None                      # 决定类别的事件编号；事件外为空
    matches: tuple[int, ...]               # 全部匹配事件的编号（正在发生的与高点前 20 日的），作审计


def ongoing_category(event: MergedEvent, start: dt.date, window_end: dt.date) -> str | None:
    """半开区间：确认前 [P, T0)，确认后 [T0, Tr)，低点后 [Tr, End)；End 当日不属于该事件。

    右截尾的事件没有结束日：低点后为 [暂定低点, 窗口末)，标“低点后（事件未结束）”。
    """
    if event.peak <= start < event.t0:
        return BEFORE_T0
    if event.t0 <= start < event.trough:
        return AFTER_T0
    if event.right_censored:
        return AFTER_TROUGH_OPEN if event.trough <= start < window_end else None
    if event.end is None:
        raise PathError("已确认的合并事件缺少结束日")
    return AFTER_TROUGH if event.trough <= start < event.end else None


def segment_position(start: dt.date, events: Sequence[MergedEvent], axis: Sequence[dt.date],
                     tail_unknown: Mapping[str, frozenset[dt.date]], lookback: int,
                     window_end: dt.date) -> Position:
    """段的起始执行日 start 相对合并事件的位置。

    先匹配正在发生的事件，多个时取高点最晚的；没有时再匹配“高点前 lookback 个交易日”，多个时取高点最早的；
    都不匹配归事件外。尾段未定按资产分别给出：起始日在两个资产里一个属于尾段未定、另一个不属于时，
    规格没有写明怎样归类，报错停下。
    """
    index = {day: number for number, day in enumerate(axis)}
    current = [(event, category) for event in events
               if (category := ongoing_category(event, start, window_end)) is not None]
    ahead = [event for event in events
             if start < event.peak and axis[max(index[event.peak] - lookback, 0)] <= start]
    matches = tuple(sorted({event.number for event, _ in current} | {event.number for event in ahead}))
    if current:
        event, category = max(current, key=lambda item: item[0].peak)
        return Position(category, event.number, matches)
    if ahead:
        return Position(PRE_PEAK, min(ahead, key=lambda item: item.peak).number, matches)
    flags = {symbol: start in days for symbol, days in tail_unknown.items()}
    if len(set(flags.values())) > 1:
        raise PathError(f"{start} 只在部分资产里属于尾段未定：规格未写明合并口径，待负责人裁决")
    return Position(OUTSIDE_TAIL if any(flags.values()) else OUTSIDE, None, matches)


@dataclass(frozen=True)
class EnvironmentSettings:
    """市场环境标签的参数；全部来自配置。"""

    bear_markets: tuple[tuple[dt.date, dt.date], ...]      # SPX 收盘价的高点 P 与低点 Tr
    up_threshold: float                                    # 年度收益不低于它为上涨年
    down_threshold: float                                  # 年度收益不高于它为下跌年


def annual_returns(closes: Mapping[dt.date, float], year_ends: Mapping[int, dt.date],
                   cutoff: dt.date) -> dict[int, float | None]:
    """各日历年的 SPX 价格指数年度收益：当年最后一个收盘价 ÷ 上一年最后一个收盘价 − 1。

    year_ends 为各年最后一个交易日（由交易日历给出）。只有当年的最后一个交易日不晚于 cutoff 时才计算；
    否则为空（完整年度分类不可得），不用部分年度收益代替，也不读取 cutoff 之后的价格。
    """
    result: dict[int, float | None] = {}
    for year, last in year_ends.items():
        previous = year_ends.get(year - 1)
        if last > cutoff or previous is None or last not in closes or previous not in closes:
            result[year] = None
        else:
            result[year] = closes[last] / closes[previous] - 1
    return result


def environment_of(day: dt.date, returns: Mapping[int, float | None], settings: EnvironmentSettings) -> str:
    """区间起点日 day 的市场环境：熊市优先（P ≤ day < Tr），其余按所在日历年的年度收益。"""
    if any(start <= day < end for start, end in settings.bear_markets):
        return BEAR
    value = returns.get(day.year)
    if value is None:
        return YEAR_UNAVAILABLE
    if value >= settings.up_threshold:
        return UP_YEAR
    return DOWN_YEAR if value <= settings.down_threshold else FLAT_YEAR
