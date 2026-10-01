"""完整净值（纯计算，描述性，不参与任何判定）：补充登记 D。

单资产当日收益 R_{a,j} = e_j·(exp(r_{a,j}) − 1)，其中 exp(r_{a,j}) − 1 = 收盘价_{j+1} ÷ 收盘价_j − 1，直接由收盘价计算；
双资产每日收盘按“总暴露 e_j、两资产按权重”再平衡，R_j = Σ_a w_a·e_j·(exp(r_{a,j}) − 1)（登记权重各半）；
现金收益为 0；W_{j+1} = W_j·(1 + R_j)，W_0 = 1。
不含分红、未计真实交易费用与再平衡交易；γ 是代理损失罚分，不扣入净值。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal


def simple_returns(days: Sequence[dt.date], closes: Mapping[dt.date, Decimal | None]) -> tuple[Decimal, ...]:
    """各区间的简单收益 收盘价_{j+1} ÷ 收盘价_j − 1；任一端缺价即报错，不填补。"""
    values = [closes.get(day) for day in days]
    if any(value is None for value in values):
        raise ValueError("完整净值要求评价窗口内每个交易日都有收盘价；缺失记待补，不插值")
    return tuple(values[index + 1] / values[index] - 1 for index in range(len(values) - 1))  # type: ignore[operator]


def exposed_returns(exposures: Sequence[Decimal], returns: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """单资产：R_{a,j} = e_j·(exp(r_{a,j}) − 1)。"""
    if len(exposures) != len(returns):
        raise ValueError("暴露与收益的区间数不一致")
    return tuple(level * value for level, value in zip(exposures, returns, strict=True))


def portfolio_returns(exposures: Sequence[Decimal], returns: Mapping[str, Sequence[Decimal]],
                      weights: Mapping[str, Decimal]) -> tuple[Decimal, ...]:
    """双资产：每日收盘再平衡到“总暴露 e_j、各资产占 w_a”，R_j = Σ_a w_a·e_j·(exp(r_{a,j}) − 1)。"""
    if set(returns) != set(weights) or sum(weights.values(), Decimal(0)) != 1:
        raise ValueError("资产与权重不一致，或权重之和不为 1")
    if any(len(values) != len(exposures) for values in returns.values()):
        raise ValueError("暴露与收益的区间数不一致")
    return tuple(sum((weights[symbol] * exposures[index] * returns[symbol][index] for symbol in weights), Decimal(0))
                 for index in range(len(exposures)))


def wealth_path(returns: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """W_0 = 1，W_{j+1} = W_j·(1 + R_j)；长度比区间数多 1。"""
    path = [Decimal(1)]
    for value in returns:
        path.append(path[-1] * (1 + value))
    return tuple(path)


@dataclass(frozen=True)
class Drawdown:
    depth: Decimal                 # 1 − 谷值 ÷ 此前最高值，不小于 0
    peak_date: dt.date             # 回撤起点（此前最高值所在日）
    trough_date: dt.date           # 回撤终点（谷值所在日）
    decline_days: int              # 峰值到谷底的交易日数
    recovery_date: dt.date | None  # 谷底之后净值首次回到或超过峰值的日期；所给区间内未恢复为空
    recovery_days: int | None      # 谷底到恢复日的交易日数；未恢复为空
    days_after_trough: int         # 谷底到所给区间最后一日的交易日数（未恢复时用来注明已过去多久）


def running_drawdowns(wealth: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """每一日相对此前（含当日）最高净值的回撤 1 − W_t ÷ max_{s≤t} W_s。"""
    result, peak = [], wealth[0]
    for value in wealth:
        peak = max(peak, value)
        result.append(1 - value / peak)
    return tuple(result)


def max_drawdown(days: Sequence[dt.date], wealth: Sequence[Decimal]) -> Drawdown:
    """所给净值序列上的最大回撤、起止日、下跌与恢复的交易日数。

    深度相同时取最早出现的一次；从未回撤时深度为 0，起止日与恢复日同为首日。
    恢复只在所给的区间内寻找：谷底之后净值首次回到或超过峰值的那一天。
    """
    if len(days) != len(wealth) or not days:
        raise ValueError("回撤要求日期与净值等长且非空")
    best, peak_index, span = Decimal(0), 0, (0, 0)
    for index, value in enumerate(wealth):
        if value > wealth[peak_index]:
            peak_index = index
        depth = 1 - value / wealth[peak_index]
        if depth > best:
            best, span = depth, (peak_index, index)
    start, low = span
    recovered = next((index for index in range(low, len(wealth)) if wealth[index] >= wealth[start]), None)
    return Drawdown(best, days[start], days[low], low - start,
                    days[recovered] if recovered is not None else None,
                    recovered - low if recovered is not None else None, len(wealth) - 1 - low)


@dataclass(frozen=True)
class RollingWorst:
    window: int
    value: Decimal | None          # 最差的 window 个区间的累计收益；区间数不足时为空
    start: dt.date | None
    end: dt.date | None


def worst_rolling(days: Sequence[dt.date], wealth: Sequence[Decimal], window: int) -> RollingWorst:
    """最差滚动收益：min_t W_{t+window} ÷ W_t − 1（t = 0 … N−window）；相同时取最早的一段。"""
    candidates = [(wealth[index + window] / wealth[index] - 1, index) for index in range(len(wealth) - window)]
    if not candidates:
        return RollingWorst(window, None, None, None)
    value, index = min(candidates)
    return RollingWorst(window, value, days[index], days[index + window])


@dataclass(frozen=True)
class NavMetrics:
    intervals: int
    cumulative: Decimal            # W_N − 1
    annualized: Decimal            # W_N^(年交易日数 ÷ N) − 1
    drawdown: Drawdown
    rolling: tuple[RollingWorst, ...]
    volatility: Decimal            # 日收益的样本标准差（分母 N−1）× √年交易日数


def nav_metrics(days: Sequence[dt.date], returns: Sequence[Decimal], windows: Sequence[int],
                year_days: int) -> NavMetrics:
    """days 为 N+1 个日期（各区间的端点），returns 为 N 个区间的当日收益。"""
    count = len(returns)
    if len(days) != count + 1 or count < 2:
        raise ValueError("净值指标要求至少两个区间，且日期比区间多一个")
    wealth = wealth_path(returns)
    mean = sum(returns, Decimal(0)) / count
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / (count - 1)
    return NavMetrics(count, wealth[-1] - 1, wealth[-1] ** (Decimal(year_days) / count) - 1,
                      max_drawdown(days, wealth), tuple(worst_rolling(days, wealth, window) for window in windows),
                      variance.sqrt() * Decimal(year_days).sqrt())
