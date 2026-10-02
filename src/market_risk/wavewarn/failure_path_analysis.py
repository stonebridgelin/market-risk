"""失败路径分析的编排（纯计算，描述性，不参与任何判定）：一个评价窗口内的全部对象。

输入是已经算好的信号序列、通道状态、收盘价与事件标签；这里不重算任何模型规则，
只把它们放到同一本账上：逐区间 d_j、非绿执行段、分类、背景标签与三项对账。
不读写文件，不导入读写模块。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market_risk.wavewarn.failure_path import (
    GREEN,
    PathSettings,
    Reconciliation,
    SegmentRow,
    WindowPath,
    executed_lights,
    full_returns,
    interval_returns,
    max_drawdown,
    rebuilt_wealth,
    reconcile,
    segment_rows,
    wealth_from,
    window_path,
)
from market_risk.wavewarn.failure_path_context import (
    ClassTotal,
    EnvironmentSettings,
    MergedEvent,
    Position,
    channel_classes,
    class_totals,
    environment_of,
    segment_position,
)


@dataclass(frozen=True)
class ObjectInput:
    """一个对象自 t0 起逐日的信号与信号日激活的通道；对照没有通道，active 为空。"""

    name: str
    signals: tuple[str, ...]
    active: tuple[tuple[str, ...], ...] | None


@dataclass(frozen=True)
class WindowInput:
    """一个评价窗口的公共输入。axis 为 t0 至窗口末日的交易日，offset 为 j₀ 在其上的行号。"""

    label: str
    axis: tuple[dt.date, ...]
    offset: int
    closes: Mapping[str, tuple[float, ...]]        # 各资产在窗口内 N+1 个交易日的收盘价
    weights: Mapping[str, float]
    levels: Mapping[str, float]                    # 灯色 → 暴露
    events: tuple[MergedEvent, ...]
    label_axis: tuple[dt.date, ...]                # 标签所用的完整交易日轴（数“高点前第 20 个交易日”）
    tail_unknown: Mapping[str, frozenset[dt.date]]  # 各资产的尾段未定（寻峰）日期
    annual: Mapping[int, float | None]             # 各日历年的 SPX 年度收益；不可得为空
    lookback: int                                  # 高点前的交易日数
    case_periods: tuple[tuple[dt.date, dt.date], ...]

    @property
    def days(self) -> tuple[dt.date, ...]:
        return self.axis[self.offset:]


@dataclass(frozen=True)
class CaseRow:
    """案例段（只作个案描述）：区间起点日 d 满足 start ≤ d < end 的全部区间。"""

    start: dt.date
    end: dt.date
    intervals: int
    non_green: int
    gap: float
    strategy_log_return: float
    full_log_return: float
    strategy_drawdown: float
    full_drawdown: float
    mean_exposure: float
    segments: tuple[int, ...]                      # 与之相交的非绿段编号


@dataclass(frozen=True)
class ObjectAnalysis:
    path: WindowPath
    segments: tuple[SegmentRow, ...]
    positions: tuple[Position, ...]                # 与 segments 一一对应
    classes: tuple[str | None, ...] | None         # 各区间的通道同时出现类别；对照为空（不适用）
    totals: tuple[ClassTotal, ...] | None
    environments: tuple[str, ...]                  # 各区间起点日的市场环境
    reconciliation: Reconciliation
    cases: tuple[CaseRow, ...]


def case_row(start: dt.date, end: dt.date, path: WindowPath, segments: Sequence[SegmentRow]) -> CaseRow:
    cells = [index for index, day in enumerate(path.days[:-1]) if start <= day < end]
    if not cells or cells != list(range(cells[0], cells[-1] + 1)):
        raise ValueError("案例段不在评价窗口内")
    span = slice(cells[0], cells[-1] + 1)
    touching = tuple(row.number for row in segments if row.span.start <= cells[-1] and row.span.end > cells[0])
    return CaseRow(start, end, len(cells), sum(light != GREEN for light in path.lights[span]),
                   math.fsum(path.gaps[span]), math.fsum(math.log(1 + value) for value in path.strategy[span]),
                   math.fsum(math.log(1 + value) for value in path.full[span]),
                   max_drawdown(wealth_from(path.strategy[span])), max_drawdown(wealth_from(path.full[span])),
                   math.fsum(path.exposures[span]) / len(cells), touching)


def analyse_object(item: ObjectInput, window: WindowInput, settings: PathSettings,
                   environment: EnvironmentSettings) -> ObjectAnalysis:
    """一个对象的完整账本；三项对账不满足即报错停下。"""
    days, count = window.days, len(window.days) - 1
    if len(item.signals) != len(window.axis) or (item.active is not None and len(item.active) != len(window.axis)):
        raise ValueError(f"{item.name} 的信号或通道状态与日期轴不等长")
    lights = executed_lights(item.signals, window.offset, count)
    returns = {symbol: interval_returns(values) for symbol, values in window.closes.items()}
    signal_days = tuple(window.axis[window.offset + index - 1] if window.offset + index > 0 else None
                        for index in range(count))
    path = window_path(item.name, days, signal_days, lights, window.levels, full_returns(returns, window.weights),
                       window.axis[:window.offset], executed_lights(item.signals, 0, window.offset))
    segments = segment_rows(path, settings)
    positions = tuple(segment_position(row.start_date, window.events, window.label_axis, window.tail_unknown,
                                       window.lookback, days[-1]) for row in segments)
    classes, totals = None, None
    if item.active is not None:
        # 区间 j 用信号日 j−1 的通道状态；t0 当日没有前一日，初始快照里没有通道激活。
        on_signal_day = tuple(item.active[window.offset + index - 1] if window.offset + index > 0 else ()
                              for index in range(count))
        classes = channel_classes(lights, on_signal_day)
        totals = class_totals(classes, path.gaps, lights)
    strategy_wealth = rebuilt_wealth(window.closes, window.weights, path.exposures)
    full_wealth = rebuilt_wealth(window.closes, window.weights, (1.0,) * count)
    reconciliation = reconcile(path, segments, None if totals is None else [row.gap for row in totals],
                               strategy_wealth, full_wealth, settings)
    return ObjectAnalysis(path, segments, positions, classes, totals,
                          tuple(environment_of(day, window.annual, environment) for day in days[:-1]),
                          reconciliation,
                          tuple(case_row(start, end, path, segments) for start, end in window.case_periods))


def analyse_window(objects: Sequence[ObjectInput], window: WindowInput, settings: PathSettings,
                   environment: EnvironmentSettings) -> tuple[ObjectAnalysis, ...]:
    """全部对象按同一本账输出；对象之间不作比较选优。"""
    return tuple(analyse_object(item, window, settings, environment) for item in objects)
