"""失败路径分析的核心（纯计算，float64，描述性，不参与任何判定）。

对象在评价窗口内相对满仓的逐区间对数收益差 d_j、非绿执行段及其分类、三项对账。
不改变模型规则、参数、选定设定与 γ；不读写文件，不导入读写模块。

口径（规格 T2 第二至四节）：
- 区间 j 为第 j 日收盘到第 j+1 日收盘。R_{a,j} = C_{a,j+1} ÷ C_{a,j} − 1；R_j^满仓 = Σ_a w_a·R_{a,j}（登记权重各半）；
  R_j^策略 = e_j·R_j^满仓，e_j = x(S_{j−1}) 由前一日的信号决定；d_j = ln(1 + R_j^策略) − ln(1 + R_j^满仓)。
- 价格收益模拟口径：每日收盘按总暴露、两资产按权重再平衡，现金收益为 0，不含分红与费用。
- 任何参与计算的 1 + R 不大于 0 时立即报错，不跳过、不截断。
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

GREEN, YELLOW, RED = "绿", "黄", "红"
IMPROVED, DRAGGED, NEUTRAL = "相对满仓收益改善", "相对满仓收益拖累", "接近零"
START_YELLOW, START_YELLOW_RED, START_RED, START_BEFORE = (
    "(a) 绿→黄启动，段内未出现红灯", "(b) 绿→黄启动，段内出现过红灯", "(c) 绿→红直接启动",
    "(d) 窗口开始前已启动，启动方式未纳入本窗口")


class PathError(ValueError):
    """失败路径分析的停止条件：输入不合规、对账不满足，或遇到规格没有写明的情形。"""


@dataclass(frozen=True)
class PathSettings:
    """分类与验收参数；全部来自配置，不在函数里写默认值。"""

    neutral_band: float            # 结果分类的报告中性带（|D| 不超过它即“接近零”）
    short_segment: int             # 段长不超过它（收益区间数）即标“短段”
    tolerance: float               # 三项对账的绝对误差上限


def interval_returns(closes: Sequence[float]) -> tuple[float, ...]:
    """各区间的简单收益 C_{j+1} ÷ C_j − 1；收盘价须全部为正。"""
    if any(not value > 0 for value in closes):
        raise PathError("收盘价缺失或不为正，不能计算区间收益")
    return tuple(closes[index + 1] / closes[index] - 1 for index in range(len(closes) - 1))


def full_returns(returns: Mapping[str, Sequence[float]], weights: Mapping[str, float]) -> tuple[float, ...]:
    """满仓：R_j = Σ_a w_a·R_{a,j}；每日收盘按权重再平衡。"""
    if set(returns) != set(weights) or abs(sum(weights.values()) - 1) > 1e-12:
        raise PathError("资产与权重不一致，或权重之和不为 1")
    counts = {len(values) for values in returns.values()}
    if len(counts) != 1:
        raise PathError("各资产的区间数不一致")
    return tuple(sum(weights[symbol] * returns[symbol][index] for symbol in weights) for index in range(counts.pop()))


def executed_lights(signals: Sequence[str], offset: int, intervals: int) -> tuple[str, ...]:
    """评价窗口内各区间的执行灯色：区间 j 在第 j 日收盘执行第 j−1 日的信号 S_{j−1}。

    signals 为自 t0 起逐日的信号（t0 当日为初始快照）；offset 为窗口第一个区间的起点日在该轴上的行号。
    t0 当日没有前一日信号，沿用初始的绿灯。不使用第 j 日收盘后才产生的信号 S_j。
    """
    if offset < 0 or offset + intervals > len(signals):
        raise PathError("评价窗口超出信号序列")
    shifted = (GREEN, *signals)
    return tuple(shifted[offset + index] for index in range(intervals))


def exposures_of(lights: Sequence[str], levels: Mapping[str, float]) -> tuple[float, ...]:
    """执行灯色 → 暴露（绿 1、黄 η、红 0，取自配置）。"""
    unknown = sorted(set(lights) - set(levels))
    if unknown:
        raise PathError(f"未知的灯色：{'、'.join(unknown)}")
    return tuple(levels[light] for light in lights)


def log_gaps(exposures: Sequence[float], full: Sequence[float]) -> tuple[float, ...]:
    """d_j = ln(1 + e_j·R_j^满仓) − ln(1 + R_j^满仓)；任何 1 + R 不大于 0 即报错。"""
    if len(exposures) != len(full):
        raise PathError("暴露与收益的区间数不一致")
    result = []
    for index, (level, value) in enumerate(zip(exposures, full, strict=True)):
        if not 1 + value > 0 or not 1 + level * value > 0:
            raise PathError(f"第 {index} 个区间的 1 + R 不大于 0，停止计算")
        result.append(math.log(1 + level * value) - math.log(1 + value))
    return tuple(result)


def rebuilt_wealth(closes: Mapping[str, Sequence[float]], weights: Mapping[str, float],
                   exposures: Sequence[float]) -> tuple[float, ...]:
    """独立的逐日净值重建：直接由收盘价逐日相乘，W_0 = 1，W_{j+1} = W_j·(1 + e_j·Σ_a w_a·(C_{a,j+1} ÷ C_{a,j} − 1))。

    不经过 interval_returns、full_returns 与 log_gaps，用于第 (3) 项对账。
    """
    wealth = [1.0]
    for index, level in enumerate(exposures):
        change = 0.0
        for symbol, weight in weights.items():
            change += weight * (closes[symbol][index + 1] / closes[symbol][index] - 1)
        wealth.append(wealth[-1] * (1 + level * change))
    return tuple(wealth)


@dataclass(frozen=True)
class Span:
    """一个非绿执行段 [start, end)：区间行号 start 至 end − 1 的执行灯色都不是绿。"""

    start: int                     # u：第一个执行灯色非绿的区间
    end: int                       # v：之后第一个执行灯色恢复为绿的区间；未闭合时为区间总数
    at_window_start: bool          # 窗口第一个计入区间即为非绿（是否左截断还要看窗口前的执行灯色）
    unclosed: bool                 # 到窗口末仍非绿


def non_green_spans(lights: Sequence[str]) -> tuple[Span, ...]:
    """执行灯色连续为非绿的最长区间串，按执行日记录。"""
    spans, start = [], None
    for index, light in enumerate(lights):
        if light != GREEN and start is None:
            start = index
        elif light == GREEN and start is not None:
            spans.append(Span(start, index, start == 0, False))
            start = None
    if start is not None:
        spans.append(Span(start, len(lights), start == 0, True))
    return tuple(spans)


def max_drawdown(wealth: Sequence[float]) -> float:
    """路径上的最大回撤 max_t (1 − W_t ÷ max_{s≤t} W_s)；路径含起点净值与最后一个区间的终点净值。"""
    peak, worst = wealth[0], 0.0
    for value in wealth:
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    return worst


def wealth_from(returns: Sequence[float]) -> tuple[float, ...]:
    """起点记为 1 的净值路径（长度比区间数多 1）。"""
    path = [1.0]
    for value in returns:
        path.append(path[-1] * (1 + value))
    return tuple(path)


def outcome_of(gap: float, band: float) -> str:
    """按结果分类：只影响类别，不改原始的 D。中性带内不表示结果严格相同，也不表示差异只来自舍入。"""
    if gap > band:
        return IMPROVED
    return DRAGGED if gap < -band else NEUTRAL


def started_before_window(span: Span, prior_lights: Sequence[str]) -> bool:
    """段是否在窗口开始之前已经启动（左截断）。

    窗口第一个计入区间即为非绿，且窗口前最后一个执行灯色不是绿灯（或没有窗口前的记录）时为真。
    窗口前最后一个执行灯色是绿灯时，该段在窗口第一个区间启动（“窗口首日启动”），不属于窗口开始前已启动。
    """
    return span.at_window_start and not (bool(prior_lights) and prior_lights[-1] == GREEN)


def start_type_of(span: Span, lights: Sequence[str], left_truncated: bool) -> str:
    """按启动方式分类；左截断的段归 (d)，不把窗口第一天的颜色当成启动转换。

    窗口首日启动的段（窗口前最后一个执行灯色为绿）按窗口第一个区间的执行灯色正常归入 (a)(b)(c)。
    """
    if left_truncated:
        return START_BEFORE
    if lights[span.start] == RED:
        return START_RED
    return START_YELLOW_RED if RED in lights[span.start:span.end] else START_YELLOW


@dataclass(frozen=True)
class PriorStart:
    """左截断的段在窗口之前的实际启动转换。"""

    date: dt.date                  # 实际启动的执行日（第一个非绿执行区间的起点日）
    transition: str                # “绿→黄”或“绿→红”


def prior_start(prior_days: Sequence[dt.date], prior_lights: Sequence[str]) -> PriorStart | None:
    """依据窗口前已有的执行灯色序列（自 t0 起连续）找出左截断段的实际启动转换。

    没有窗口前的记录、或记录里找不到此前的绿灯时返回空（没有合法记录）。只对左截断的段调用。
    """
    if len(prior_days) != len(prior_lights):
        raise PathError("窗口前的日期与执行灯色不等长")
    if not prior_lights:
        return None
    if prior_lights[-1] == GREEN:
        raise PathError("窗口前最后一个执行灯色为绿的段不是左截断，不应查找窗口前的启动转换")
    index = len(prior_lights) - 1
    while index >= 0 and prior_lights[index] != GREEN:
        index -= 1
    if index < 0:
        return None
    return PriorStart(prior_days[index + 1], f"{GREEN}→{prior_lights[index + 1]}")


@dataclass(frozen=True)
class WindowPath:
    """一个对象在一个评价窗口内的逐区间路径。days 为 N+1 个日期，其余各列为 N 个区间。"""

    name: str
    days: tuple[dt.date, ...]
    signal_days: tuple[dt.date | None, ...]     # 各区间的信号日（起点日的前一交易日）；没有前一日时为空
    lights: tuple[str, ...]                     # 执行灯色 S_{j−1}
    exposures: tuple[float, ...]
    full: tuple[float, ...]                     # R_j^满仓
    strategy: tuple[float, ...]                 # R_j^策略 = e_j·R_j^满仓
    gaps: tuple[float, ...]                     # d_j
    prior_days: tuple[dt.date, ...]             # 窗口之前（自 t0 起）各区间的起点日
    prior_lights: tuple[str, ...]               # 窗口之前各区间的执行灯色


def window_path(name: str, days: Sequence[dt.date], signal_days: Sequence[dt.date | None], lights: Sequence[str],
                levels: Mapping[str, float], full: Sequence[float], prior_days: Sequence[dt.date],
                prior_lights: Sequence[str]) -> WindowPath:
    if not (len(days) - 1 == len(lights) == len(full) == len(signal_days)):
        raise PathError("日期、执行灯色与收益的长度不一致")
    exposures = exposures_of(lights, levels)
    gaps = log_gaps(exposures, full)
    strategy = tuple(level * value for level, value in zip(exposures, full, strict=True))
    return WindowPath(name, tuple(days), tuple(signal_days), tuple(lights), exposures, tuple(full), strategy, gaps,
                      tuple(prior_days), tuple(prior_lights))


@dataclass(frozen=True)
class SegmentRow:
    """段账本的一行。D 可以相加；exp(D) − 1 不可相加。"""

    number: int
    span: Span
    start_date: dt.date                    # 起始执行日 s（第一个非绿区间的起点日）
    end_date: dt.date                      # 结束执行日（恢复绿灯的区间的起点日；未闭合时为最后一个区间的终点日）
    start_signal_date: dt.date | None      # 触发启动的信号日
    end_signal_date: dt.date | None        # 触发恢复绿灯的信号日；未闭合时为空
    length: int                            # 收益区间数
    gap: float                             # D = Σ d_j
    gap_simple: float                      # exp(D) − 1
    mean_exposure: float
    full_log_return: float                 # Σ ln(1 + R^满仓)
    strategy_log_return: float             # Σ ln(1 + R^策略)
    full_drawdown: float
    strategy_drawdown: float
    short: bool
    yellow_to_red: int
    red_to_yellow: int
    round_trips: int                       # 两者较小值
    outcome: str
    start_type: str
    prior: PriorStart | None               # 只对 (d) 的段填写
    left_truncated: bool                   # 确实在窗口开始之前已经启动
    first_day_start: bool                  # 窗口首日启动：窗口第一个区间即非绿，而窗口前最后一个执行灯色为绿


def segment_row(number: int, span: Span, path: WindowPath, settings: PathSettings) -> SegmentRow:
    cells = slice(span.start, span.end)
    lights, gaps = path.lights[cells], path.gaps[cells]
    total = math.fsum(gaps)
    pairs = list(itertools.pairwise(lights))
    up, down = pairs.count((YELLOW, RED)), pairs.count((RED, YELLOW))
    left_truncated = started_before_window(span, path.prior_lights)
    start_type = start_type_of(span, path.lights, left_truncated)
    return SegmentRow(
        number, span, path.days[span.start], path.days[span.end], path.signal_days[span.start],
        None if span.unclosed else path.signal_days[span.end], span.end - span.start, total, math.expm1(total),
        math.fsum(path.exposures[cells]) / (span.end - span.start),
        math.fsum(math.log(1 + value) for value in path.full[cells]),
        math.fsum(math.log(1 + value) for value in path.strategy[cells]),
        max_drawdown(wealth_from(path.full[cells])), max_drawdown(wealth_from(path.strategy[cells])),
        span.end - span.start <= settings.short_segment, up, down, min(up, down),
        outcome_of(total, settings.neutral_band), start_type,
        prior_start(path.prior_days, path.prior_lights) if left_truncated else None,
        left_truncated, span.at_window_start and not left_truncated)


def segment_rows(path: WindowPath, settings: PathSettings) -> tuple[SegmentRow, ...]:
    return tuple(segment_row(number, span, path, settings)
                 for number, span in enumerate(non_green_spans(path.lights), start=1))


@dataclass(frozen=True)
class Reconciliation:
    """三项对账；差的绝对值都不得超过容差。"""

    total_gap: float                       # 全窗口 Σ d_j
    segment_gap: float                     # 全部非绿执行段的 D 之和
    class_gap: float | None                # 通道同时出现统计各类的 Σ d_j 之和；对照没有通道，为空
    wealth_gap: float                      # ln(W_策略,末 ÷ W_满仓,末)，两个净值由逐日重建得到

    def differences(self) -> tuple[float, ...]:
        """(1) 段之和 − 全窗口；(2) 五类之和 − 段之和（不适用时为 0）；(3) 全窗口 − 净值比的对数。"""
        second = 0.0 if self.class_gap is None else self.class_gap - self.segment_gap
        return (self.segment_gap - self.total_gap, second, self.total_gap - self.wealth_gap)


def reconcile(path: WindowPath, segments: Sequence[SegmentRow], class_gaps: Sequence[float] | None,
              strategy_wealth: Sequence[float], full_wealth: Sequence[float],
              settings: PathSettings) -> Reconciliation:
    """对账不满足即报错停下。两条净值须由 rebuilt_wealth 在相同日期轴、价格与初始净值上重建。"""
    result = Reconciliation(math.fsum(path.gaps), math.fsum(row.gap for row in segments),
                            None if class_gaps is None else math.fsum(class_gaps),
                            math.log(strategy_wealth[-1] / full_wealth[-1]))
    failed = [index + 1 for index, value in enumerate(result.differences()) if abs(value) > settings.tolerance]
    if failed:
        raise PathError(f"{path.name}：第 {'、'.join(str(item) for item in failed)} 项对账不满足")
    return result
