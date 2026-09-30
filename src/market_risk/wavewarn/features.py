"""v1.2.1 前瞻特征：严格按交易日索引，仅使用当日及更早输入。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from market_risk.wavewarn.config import FixedParameters


@dataclass(frozen=True)
class AssetFeatures:
    date: dt.date
    close: Decimal | None
    high63: Decimal | None
    drawdown63: Decimal | None
    new_low20: int | None
    q: int
    breadth: Decimal | None
    delta_w20: Decimal | None
    delta_w10: Decimal | None
    quantile_reference: Decimal | None
    median60: Decimal | None
    ma50: Decimal | None
    ma200: Decimal | None
    close_t5: Decimal | None
    close_t10: Decimal | None


def new_low20(closes: Sequence[Decimal | None], index: int,
              prior_days: int = 19, min_valid: int = 15) -> int | None:
    """负责人确认的保守三值逻辑：缺口下仅能证明“未创新低”。"""
    if index < 0 or index >= len(closes):
        raise IndexError(index)
    today = closes[index]
    if today is None:
        return None
    prior = closes[max(0, index - prior_days):index]
    good = [value for value in prior if value is not None]
    if len(good) < min_valid:
        return None
    if today >= min(good):
        return 0
    return 1 if len(prior) == prior_days and len(good) == prior_days else None


def q_from_new_lows(values: Sequence[int | None]) -> tuple[int, ...]:
    """NL 为1或未知时归零；完整历史顺序计算，不从状态机初始化日重新计数。"""
    q = 0
    result = []
    for value in values:
        q = q + 1 if value == 0 else 0
        result.append(q)
    return tuple(result)


def rolling_high(closes: Sequence[Decimal | None], index: int, window: int) -> Decimal | None:
    if index + 1 < window:
        return None
    values = closes[index - window + 1:index + 1]
    return max(values) if all(value is not None for value in values) else None


def moving_average(values: Sequence[Decimal | None], index: int, window: int) -> Decimal | None:
    if index + 1 < window:
        return None
    observed = values[index - window + 1:index + 1]
    return sum(observed) / Decimal(window) if all(value is not None for value in observed) else None


def breadth_median60(values: Sequence[Decimal | None], index: int,
                     window: int = 60, min_valid: int = 50) -> Decimal | None:
    """60日窗口内有效读数至少50，偶数个取两个中值的平均。"""
    if index + 1 < window:
        return None
    good = sorted(value for value in values[index - window + 1:index + 1] if value is not None)
    if len(good) < min_valid:
        return None
    middle = len(good) // 2
    return good[middle] if len(good) % 2 else (good[middle - 1] + good[middle]) / 2


def breadth_change(values: Sequence[Decimal | None], index: int, lag: int) -> Decimal | None:
    if index < lag or values[index] is None or values[index - lag] is None:
        return None
    return values[index] - values[index - lag]


def breadth_quantile_reference(values: Sequence[Decimal | None], index: int,
                               q: Decimal, reference_window: int = 252,
                               min_valid: int = 200) -> Decimal | None:
    """t−252 至 t−1 的有效 ΔW20 至少200个；取第 ceil(q*n) 小值。"""
    if not (0 < q <= 1):
        raise ValueError("分位数 q 必须在 (0,1] 内")
    if index < reference_window:
        return None
    changes = sorted(change for past in range(index - reference_window, index)
                     if (change := breadth_change(values, past, 20)) is not None)
    if len(changes) < min_valid:
        return None
    rank = int((q * len(changes)).to_integral_value(rounding=ROUND_CEILING))
    return changes[rank - 1]


def vix_term_ratio(vix: Decimal | None, vix3m: Decimal | None) -> Decimal | None:
    return vix / vix3m if vix is not None and vix3m not in (None, Decimal(0)) else None


def asset_features(days: Sequence[dt.date], prices: Mapping[dt.date, Decimal | None],
                   breadth: Mapping[dt.date, Decimal | None], q: Decimal,
                   fixed: FixedParameters | None = None) -> tuple[AssetFeatures, ...]:
    """只按指定 NYSE 日历取值，额外日期不能填补缺价。"""
    if tuple(days) != tuple(sorted(set(days))):
        raise ValueError("交易日必须唯一且升序")
    closes = tuple(prices.get(day) for day in days)
    breadth_values = tuple(breadth.get(day) for day in days)
    prior_days = fixed.new_low_prior if fixed else 19
    min_new_low = fixed.new_low_min_valid if fixed else 15
    anchor = fixed.anchor if fixed else 63
    reference = fixed.breadth_reference if fixed else 252
    reference_min = fixed.breadth_reference_min_valid if fixed else 200
    median_window = fixed.breadth_median if fixed else 60
    median_min = fixed.breadth_median_min_valid if fixed else 50
    ma50 = fixed.ma50 if fixed else 50
    ma200 = fixed.ma200 if fixed else 200
    new_lows = tuple(new_low20(closes, index, prior_days, min_new_low) for index in range(len(days)))
    q_values = q_from_new_lows(new_lows)
    result = []
    for index, day in enumerate(days):
        high = rolling_high(closes, index, anchor)
        close = closes[index]
        result.append(AssetFeatures(
            day, close, high, Decimal(1) - close / high if high is not None and close is not None else None,
            new_lows[index], q_values[index], breadth_values[index],
            breadth_change(breadth_values, index, 20), breadth_change(breadth_values, index, 10),
            breadth_quantile_reference(breadth_values, index, q, reference, reference_min),
            breadth_median60(breadth_values, index, median_window, median_min),
            moving_average(closes, index, ma50), moving_average(closes, index, ma200),
            closes[index - 5] if index >= 5 else None,
            closes[index - 10] if index >= 10 else None))
    return tuple(result)


def anchor_126_audit(days: Sequence[dt.date], prices: Mapping[dt.date, Decimal | None],
                     theta: Decimal, fixed: FixedParameters | None = None
                     ) -> tuple[tuple[dt.date, Decimal, Decimal], ...]:
    """仅列63日锚点未触发而126日锚点会触发的日期，不改变实时通道。"""
    closes = tuple(prices.get(day) for day in days)
    result = []
    for index, day in enumerate(days):
        high63 = rolling_high(closes, index, fixed.anchor if fixed else 63)
        high126 = rolling_high(closes, index, fixed.anchor_audit if fixed else 126)
        close = closes[index]
        if close is None or high63 is None or high126 is None:
            continue
        short = 1 - close / high63
        long = 1 - close / high126
        if short < theta <= long:
            result.append((day, short, long))
    return tuple(result)
