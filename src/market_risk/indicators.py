"""指标：简单均线、窗口最高值、三环节遍历（SPEC 5.4、6.2）。

所有函数只使用传入的数据，调用方负责先截断到基准日（含）。
价格比较前统一保留2位小数（SPEC 5.3）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from market_risk.models import ThreeSegmentResult, ThreeSegmentTrace

PRICE_DECIMALS = 2
LC_LOOKBACK = 20          # Lc：d1 之前20个交易日的最低收盘价
MIN_D1_GAP = 2            # d1 至少早于基准日2个交易日
D1_RANGE = 20             # 候选 d1 位于基准日前20个交易日内


class InsufficientDataError(ValueError):
    """数据不足以计算指标（不得用更短的周期代替）。"""


def p2(x: float) -> float:
    """价格按2位小数比较。"""
    return round(x, PRICE_DECIMALS)


def sorted_closes(
    closes: Mapping[dt.date, float], base_date: dt.date
) -> list[tuple[dt.date, float]]:
    """按日期升序排列，并截断到基准日（含）。"""
    return sorted((d, v) for d, v in closes.items() if d <= base_date)


def simple_moving_average(
    closes: Mapping[dt.date, float], base_date: dt.date, period: int
) -> float:
    """截至基准日（含）最近 period 个收盘价的算术平均。数据不足时报错。"""
    series = sorted_closes(closes, base_date)
    if not series or series[-1][0] != base_date:
        raise InsufficientDataError(f"没有基准日 {base_date} 的收盘价")
    if len(series) < period:
        raise InsufficientDataError(f"只有 {len(series)} 个收盘价，不足以计算 MA{period}")
    window = [v for _, v in series[-period:]]
    return sum(window) / period


def window_max(
    values: Mapping[dt.date, float], decimals: int = 2
) -> tuple[float, tuple[dt.date, ...]]:
    """窗口最高值及全部并列最高的日期（按公布精度比较，SPEC 5.6 第6条）。"""
    if not values:
        raise InsufficientDataError("窗口内没有数据")
    rounded = {d: round(v, decimals) for d, v in values.items()}
    h = max(rounded.values())
    return h, tuple(sorted(d for d, v in rounded.items() if v == h))


def three_segment_candidates(
    trading_days: Sequence[dt.date], base_date: dt.date, include_t_minus_20: bool
) -> list[dt.date]:
    """候选 d1：T−20（或 T−19）至 T−2，trading_days 为截至基准日的股票交易日（升序）。"""
    if not trading_days or trading_days[-1] != base_date:
        raise InsufficientDataError(f"交易日序列未以基准日 {base_date} 结尾")
    earliest = D1_RANGE if include_t_minus_20 else D1_RANGE - 1
    if len(trading_days) <= earliest:
        raise InsufficientDataError("交易日序列不足以确定三环节候选范围")
    n = len(trading_days) - 1  # 基准日的位置
    return [trading_days[n - k] for k in range(earliest, MIN_D1_GAP - 1, -1)]


def three_segment_trace(
    symbol: str,
    closes: Mapping[dt.date, float],
    base_date: dt.date,
    include_t_minus_20: bool,
) -> ThreeSegmentResult:
    """对一只 ETF 完整遍历三环节（SPEC 5.4）。

    closes 必须是连续的股票交易日收盘价（调用方已核对无缺日）。
    - Lc：d1 之前20个交易日（d1−20 至 d1−1）的最低收盘价；并列时记录最早日期。
    - 第一步：d1 收盘价 < Lc。
    - 第二步：存在 d1 与基准日之间（不含两端）的 d2，满足 d1 收盘价 < d2 收盘价 ≤ Lc。
    - 第三步：基准日收盘价 < d1 收盘价。
    三步都成立即完成。每个候选都记录全部三步，供人工复查。
    """
    series = [(d, p2(v)) for d, v in sorted_closes(closes, base_date)]
    days = [d for d, _ in series]
    price = dict(series)
    index = {d: i for i, d in enumerate(days)}
    base_close = price.get(base_date)
    if base_close is None:
        raise InsufficientDataError(f"{symbol} 没有基准日 {base_date} 的收盘价")

    traces: list[ThreeSegmentTrace] = []
    for d1 in three_segment_candidates(days, base_date, include_t_minus_20):
        i = index[d1]
        if i < LC_LOOKBACK:
            raise InsufficientDataError(
                f"{symbol} 在 {d1} 之前不足 {LC_LOOKBACK} 个交易日，无法计算 Lc"
            )
        lookback = series[i - LC_LOOKBACK : i]
        lc = min(v for _, v in lookback)
        lc_date = min(d for d, v in lookback if v == lc)
        d1_close = price[d1]
        step1 = d1_close < lc
        d2_dates = tuple(
            d for d, v in series[i + 1 : index[base_date]] if d1_close < v <= lc
        )
        step3 = base_close < d1_close
        traces.append(
            ThreeSegmentTrace(
                symbol=symbol,
                d1=d1,
                d1_close=d1_close,
                lc=lc,
                lc_date=lc_date,
                step1=step1,
                d2_dates=d2_dates,
                step3=step3,
                completed=step1 and bool(d2_dates) and step3,
            )
        )
    return ThreeSegmentResult(
        symbol=symbol, d1_includes_t_minus_20=include_t_minus_20, traces=tuple(traces)
    )


def three_segment_both(
    symbol: str, closes: Mapping[dt.date, float], base_date: dt.date
) -> dict[bool, ThreeSegmentResult]:
    """按两种 d1 口径（含/不含 T−20）同时计算（SPEC 5.6 第1条）。"""
    return {
        flag: three_segment_trace(symbol, closes, base_date, flag) for flag in (True, False)
    }


def ratio(numerator: float, denominator: float, decimals: int = 4) -> float:
    """两个收盘价之比（HYG/LQD），保留4位小数，仅作参考。"""
    if denominator == 0:
        raise ValueError("分母为 0")
    return round(numerator / denominator, decimals)
