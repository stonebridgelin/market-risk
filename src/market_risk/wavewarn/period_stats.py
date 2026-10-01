"""分期统计（纯计算，描述性诊断）：逐年的主损失、ē、择时得分、非绿占比，以及指定期间的执行暴露与回撤。

口径（负责人 2026-10-01 确认）：区间按起点日期归年；逐年 T 用当年自己的平均执行暴露与当年的 L_G、L_R，
所以各年 T 之和不等于全期 T。期间回撤按每个资产的累计对数收益 Σ e_j·r_j 计算。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.evaluation import SYMBOLS, CandidateEvaluation
from market_risk.wavewarn.execution import exposure

Row = tuple[object, ...]


@dataclass(frozen=True)
class IntervalStats:
    intervals: int
    loss: Decimal
    mean_exposure: Decimal
    non_green: int


def interval_stats(evaluated: CandidateEvaluation, eta: Decimal, indices: Sequence[int]) -> IntervalStats:
    """给定区间下标（按起点）的主损失、平均执行暴露与执行非绿天数。"""
    if not indices or max(indices) >= len(evaluated.days) - 1:
        raise ValueError("区间下标为空或超出计入区间")
    lights = [evaluated.system_executed[index] for index in indices]
    return IntervalStats(
        len(indices), sum((evaluated.daily_losses[index].total for index in indices), Decimal(0)),
        sum((exposure(light, eta) for light in lights), Decimal(0)) / len(indices),  # type: ignore[arg-type]
        sum(light != "绿" for light in lights))


def year_indices(days: Sequence[dt.date]) -> dict[int, list[int]]:
    """计入区间按起点日期归年；窗口末日没有后续区间，不归入任何一年。"""
    result: dict[int, list[int]] = {}
    for index, day in enumerate(days[:-1]):
        result.setdefault(day.year, []).append(index)
    return result


YEARLY_HEADER = ("row", "year", "intervals", "total_loss", "mean_exposure", "timing_score", "non_green_share")


def yearly_rows(name: str, evaluated: CandidateEvaluation, green: CandidateEvaluation,
                red: CandidateEvaluation, eta: Decimal) -> tuple[Row, ...]:
    """逐年一行，末尾附“全期”一行。T = L − [ē·L_G + (1−ē)·L_R]，三者都取同一批区间。"""
    groups: list[tuple[object, list[int]]] = list(year_indices(evaluated.days).items())
    groups.append(("全期", list(range(len(evaluated.days) - 1))))
    rows: list[Row] = []
    for label, indices in groups:
        own = interval_stats(evaluated, eta, indices)
        green_loss = interval_stats(green, eta, indices).loss
        red_loss = interval_stats(red, eta, indices).loss
        score = own.loss - (own.mean_exposure * green_loss + (1 - own.mean_exposure) * red_loss)
        rows.append((name, label, own.intervals, own.loss, own.mean_exposure, score,
                     Decimal(own.non_green) / own.intervals))
    return tuple(rows)


def max_drawdown(increments: Sequence[Decimal]) -> Decimal:
    """累计曲线（自 0 起）的最大回撤：max_k(此前最高点 − 当前值)，不小于 0。"""
    level = peak = worst = Decimal(0)
    for step in increments:
        level += step
        peak = max(peak, level)
        worst = max(worst, peak - level)
    return worst


PERIOD_HEADER = ("period", "row", "intervals", "mean_exposure", "total_loss",
                 *(f"{symbol.lower()}_{name}" for symbol in SYMBOLS
                   for name in ("exposed_log_return", "max_drawdown")))


def period_row(period: str, name: str, evaluated: CandidateEvaluation, eta: Decimal, start: dt.date,
               end: dt.date) -> Row:
    """期间为区间起点在 [start, end) 的全部区间（最后一个区间止于 end 当日收盘）。"""
    indices = [index for index, day in enumerate(evaluated.days[:-1]) if start <= day < end]
    stats = interval_stats(evaluated, eta, indices)
    cells: list[object] = []
    for symbol in SYMBOLS:
        steps = []
        for index in indices:
            loss = evaluated.asset_losses[symbol][index]
            if loss.log_return is None:
                raise ValueError(f"{symbol} {loss.start} 区间无对数收益，不能计算期间回撤")
            steps.append(evaluated.executions[symbol][index].exposure * loss.log_return)
        cells.extend((sum(steps, Decimal(0)), max_drawdown(steps)))
    return (period, name, stats.intervals, stats.mean_exposure, stats.loss, *cells)
