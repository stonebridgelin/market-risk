"""v1.2.1 通道谓词与每日武装、激活、退出状态。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.config import FixedParameters
from market_risk.wavewarn.features import AssetFeatures

ChannelStatus = Literal["armed", "active", "unarmed"]


@dataclass(frozen=True)
class ChannelPredicate:
    entry: bool | None
    exit: bool | None
    rearm: bool | None
    immediate_rearm: bool = False
    evidence: str = ""


@dataclass(frozen=True)
class ChannelDay:
    date: dt.date
    status: ChannelStatus
    valid: bool
    reason: str


def update_channel(day: dt.date, status: ChannelStatus, predicate: ChannelPredicate) -> ChannelDay:
    """优先级：退出→重新武装→进入；同日退出后不可重新进入。"""
    if status == "active":
        if predicate.exit is None:
            return ChannelDay(day, status, False, "退出输入缺失，状态沿用")
        if predicate.exit:
            return ChannelDay(day, "unarmed", True, "退出；当日不再检查进入")
        return ChannelDay(day, status, True, "维持激活")
    if status == "unarmed":
        if predicate.rearm is None:
            return ChannelDay(day, status, False, "重新武装输入缺失，状态沿用")
        if not predicate.rearm:
            return ChannelDay(day, status, True, "等待重新武装")
        if not predicate.immediate_rearm:
            return ChannelDay(day, "armed", True, "进入谓词为假，重新武装；当日不进入")
        if predicate.entry is None:
            # 负责人确认：NL 未知仍可重新武装；D 缺失不能作为进入证据。
            return ChannelDay(day, "armed", False, "NL为1或未知，重新武装；进入输入缺失")
        if predicate.entry:
            return ChannelDay(day, "active", True, f"重新武装并进入；{predicate.evidence}")
        return ChannelDay(day, "armed", True, "重新武装；进入谓词为假")
    if predicate.entry is None:
        return ChannelDay(day, status, False, "进入输入缺失，状态沿用")
    if predicate.entry:
        return ChannelDay(day, "active", True, f"进入；{predicate.evidence}")
    return ChannelDay(day, status, True, "保持已武装")


def run_channel(days: Sequence[dt.date], predicates: Sequence[ChannelPredicate],
                initial: ChannelStatus = "armed") -> tuple[ChannelDay, ...]:
    if len(days) != len(predicates):
        raise ValueError("通道谓词与交易日数量不一致")
    state = initial
    result = []
    for day, predicate in zip(days, predicates, strict=True):
        update = update_channel(day, state, predicate)
        result.append(update)
        state = update.status
    return tuple(result)


def _both(a: bool | None, b: bool | None) -> bool | None:
    if a is None or b is None:
        return None
    return a and b


def _all(*values: bool | None) -> bool | None:
    result: bool | None = True
    for value in values:
        result = _both(result, value)
    return result


def _compare(a: Decimal | None, op: str, b: Decimal | None) -> bool | None:
    if a is None or b is None:
        return None
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    raise ValueError(op)


def _streak(values: Sequence[bool | None], index: int, length: int) -> bool | None:
    if values[index] is None:
        return None
    if index + 1 < length:
        return False
    return all(value is True for value in values[index - length + 1:index + 1])


def price_predicates(features: Sequence[AssetFeatures], theta: Decimal,
                     k: int, red: bool = False) -> tuple[ChannelPredicate, ...]:
    if theta <= 0 or k <= 0:
        raise ValueError("价格通道门槛与 K 必须为正")
    threshold = theta * (2 if red else 1)
    result = []
    for item in features:
        entry = _compare(item.drawdown63, ">=", threshold)
        immediate = item.new_low20 in (1, None)
        rearm = True if immediate else entry is False if entry is not None else None
        result.append(ChannelPredicate(entry, item.q >= k, rearm, immediate,
                                       f"D≥{threshold}；NL={item.new_low20}；Q={item.q}"))
    return tuple(result)


def breadth_predicates(features: Sequence[AssetFeatures], channel: Literal["B", "DV", "BW"],
                       fixed: FixedParameters | None = None
                       ) -> tuple[ChannelPredicate, ...]:
    breadth_min = fixed.breadth_min_drawdown if fixed else Decimal("0.01")
    divergence_max = fixed.divergence_max_drawdown if fixed else Decimal("0.01")
    divergence_change = fixed.divergence_delta10_max if fixed else Decimal("-10")
    collapse_min = fixed.collapse_min_drawdown if fixed else Decimal("0.02")
    collapse_max_breadth = fixed.collapse_breadth_max if fixed else Decimal("30")
    collapse_repair_breadth = fixed.collapse_repair_breadth if fixed else Decimal("40")
    repair_days = fixed.breadth_repair if fixed else 3
    repair = [_compare(item.breadth, ">=", item.median60) for item in features]
    collapse_repair = [_all(_compare(item.breadth, ">", collapse_repair_breadth),
                            _compare(item.close, ">", item.close_t5)) for item in features]
    result = []
    for index, item in enumerate(features):
        if channel == "B":
            entry = _all(_compare(item.drawdown63, ">=", breadth_min),
                         _compare(item.delta_w20, "<=", item.quantile_reference))
            exit = _streak(repair, index, repair_days)
        elif channel == "DV":
            entry = _all(_compare(item.drawdown63, "<=", divergence_max),
                         _compare(item.delta_w10, "<=", divergence_change),
                         _compare(item.close, ">=", item.close_t10))
            exit = _streak(repair, index, repair_days)
        elif channel == "BW":
            entry = _all(_compare(item.drawdown63, ">=", collapse_min),
                         _compare(item.breadth, "<=", collapse_max_breadth))
            exit = _streak(collapse_repair, index, repair_days)
        else:
            raise ValueError(channel)
        result.append(ChannelPredicate(entry, exit, entry is False if entry is not None else None,
                                       False, f"{channel} 进入条件成立"))
    return tuple(result)


def volatility_predicates(ratios: Sequence[Decimal | None],
                          fixed: FixedParameters | None = None) -> tuple[ChannelPredicate, ...]:
    enter = fixed.volatility_enter_ratio if fixed else Decimal(1)
    exit_ratio = fixed.volatility_exit_ratio if fixed else Decimal("0.95")
    short_days = fixed.volatility_exit_short if fixed else 3
    long_days = fixed.volatility_exit_long if fixed else 10
    below_095 = [_compare(value, "<", exit_ratio) for value in ratios]
    at_most_one = [_compare(value, "<=", enter) for value in ratios]
    result = []
    for index, ratio in enumerate(ratios):
        entry = _compare(ratio, ">", enter)
        exit3 = _streak(below_095, index, short_days)
        exit10 = _streak(at_most_one, index, long_days)
        exit = None if ratio is None else bool(exit3 or exit10)
        result.append(ChannelPredicate(entry, exit, entry is False if entry is not None else None,
                                       False, "VIX/VIX3M>1"))
    return tuple(result)


def cmap_predicate(channel: Literal["CY", "CR"], minimum: int, maximum: int,
                   deterioration: str) -> ChannelPredicate:
    if channel == "CY":
        entry, exit = minimum >= 3, maximum <= 2
    elif channel == "CR":
        entry = minimum >= 6 or deterioration == "是"
        exit = maximum <= 5 and deterioration == "否" if deterioration in ("是", "否") else None
    else:
        raise ValueError(channel)
    return ChannelPredicate(entry, exit, not entry, False, f"{channel} 评分映射")
