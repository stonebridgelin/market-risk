"""通道（登记第一节第 1 小节；第八节第 2 小节的 v2.0 通道更新规则，v2.0 唯一缺值入口）。纯计算。

- P、PR 的状态域有三种；MR 只有“激活”“已武装未激活”两种，收到其他状态即报错。
- 每日更新顺序为退出 > 重新武装 > 进入；同一通道不得同日退出后又进入。
- 初始快照日（t0）只放初始状态、不更新；run_* 的输入是初始日之后的各日。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from market_risk.wavewarn_v20.inputs import AssetDay, NewLow, TrendDay, below_average, drawdown_reaches


class ChannelError(ValueError):
    """通道状态不在其状态域内，或出现登记没有覆盖的输入状态。"""


class ChannelState(Enum):
    ACTIVE = "激活"
    ARMED = "已武装未激活"
    UNARMED = "未武装未激活"


PULLBACK_DOMAIN: frozenset[ChannelState] = frozenset(ChannelState)                          # P、PR
TREND_DOMAIN: frozenset[ChannelState] = frozenset({ChannelState.ACTIVE, ChannelState.ARMED})   # MR


class ChannelEvent(Enum):
    EXIT = "退出"
    REARM = "重新武装"
    ENTER = "进入"


@dataclass(frozen=True)
class ChannelDay:
    """通道在某日更新后的状态、当日是否有效，以及当日发生的通道事件（按发生顺序）。"""

    day: dt.date
    state: ChannelState
    valid: bool
    events: tuple[ChannelEvent, ...]

    @property
    def active(self) -> bool:
        return self.state is ChannelState.ACTIVE


def _checked_asset(today: AssetDay) -> AssetDay:
    """登记输入表之外的组合一律报错，不转换为其他状态。"""
    if today.close is None and (today.high_complete or today.new_low is not NewLow.UNKNOWN or today.q != 0):
        raise ChannelError(f"{today.day} 当日缺价，但 H 完整、NL 不是未知或 Q 不为 0")
    if today.close is not None and (today.high is None or today.high < today.close):
        raise ChannelError(f"{today.day} 当日有价，但窗口最高价缺失或低于当日收盘价")
    if today.q < 0 or (today.new_low is not NewLow.NO) != (today.q == 0):
        raise ChannelError(f"{today.day} 的 Q 与 NL 不一致")
    return today


def pullback_valid(today: AssetDay) -> bool:
    """P、PR 当日有效 ⇔ 当日收盘价存在且 H 完整。"""
    return today.close is not None and today.high_complete


def pullback_step(previous: ChannelState, today: AssetDay, k: int, threshold: Decimal) -> ChannelDay:
    """P 或 PR 的每日更新；threshold 为该通道的进入门槛（P 为 θ_P，PR 为 2θ_P）。"""
    if previous not in PULLBACK_DOMAIN:
        raise ChannelError(f"P、PR 的状态不在状态域内：{previous!r}")
    today = _checked_asset(today)
    valid = pullback_valid(today)
    if previous is ChannelState.ACTIVE:
        if valid and today.q >= k:
            return ChannelDay(today.day, ChannelState.UNARMED, valid, (ChannelEvent.EXIT,))
        return ChannelDay(today.day, ChannelState.ACTIVE, valid, ())
    events: tuple[ChannelEvent, ...] = ()
    if previous is ChannelState.UNARMED:
        if today.new_low in (NewLow.YES, NewLow.UNKNOWN):
            events = (ChannelEvent.REARM,)          # 立即武装，并进入下一步
        elif valid and not _reaches(today, threshold):
            return ChannelDay(today.day, ChannelState.ARMED, valid, (ChannelEvent.REARM,))   # 当日不进入
        else:
            return ChannelDay(today.day, ChannelState.UNARMED, valid, ())
    if today.close is not None and _reaches(today, threshold):
        return ChannelDay(today.day, ChannelState.ACTIVE, valid, (*events, ChannelEvent.ENTER))
    return ChannelDay(today.day, ChannelState.ARMED, valid, events)


def _reaches(today: AssetDay, threshold: Decimal) -> bool:
    if today.close is None or today.high is None:
        raise ChannelError(f"{today.day} 缺少判定回撤所需的价格")
    return drawdown_reaches(today.close, today.high, threshold)


def trend_valid(today: TrendDay) -> bool:
    """MR 当日有效 ⇔ 当日 SPX 收盘价存在且均线完整。"""
    return today.close is not None and today.complete


def trend_step(previous: ChannelState, today: TrendDay, length: int) -> ChannelDay:
    """MR 的每日更新；length 为均线窗口长度。MR 没有未武装状态，退出即回到“已武装未激活”。"""
    if previous not in TREND_DOMAIN:
        raise ChannelError(f"MR 的状态只能是“激活”或“已武装未激活”：{previous!r}")
    if today.complete != (today.total is not None) or (today.complete and today.close is None):
        raise ChannelError(f"{today.day} 的均线完整性与均线数值不一致")
    valid = trend_valid(today)
    below = valid and today.close is not None and today.total is not None and below_average(
        today.close, today.total, length)
    if previous is ChannelState.ACTIVE:
        if valid and not below:
            return ChannelDay(today.day, ChannelState.ARMED, valid, (ChannelEvent.EXIT,))
        return ChannelDay(today.day, ChannelState.ACTIVE, valid, ())
    if valid and below:
        return ChannelDay(today.day, ChannelState.ACTIVE, valid, (ChannelEvent.ENTER,))
    return ChannelDay(today.day, ChannelState.ARMED, valid, ())


def run_pullback(initial: ChannelState, inputs: Sequence[AssetDay], k: int, threshold: Decimal
                 ) -> tuple[ChannelDay, ...]:
    """自初始日的下一日起逐日更新 P 或 PR；inputs 不含初始日。"""
    if initial not in PULLBACK_DOMAIN:
        raise ChannelError(f"P、PR 的状态不在状态域内：{initial!r}")
    state = initial
    result: list[ChannelDay] = []
    for today in inputs:
        updated = pullback_step(state, today, k, threshold)
        result.append(updated)
        state = updated.state
    return tuple(result)


def run_trend(initial: ChannelState, inputs: Sequence[TrendDay], length: int) -> tuple[ChannelDay, ...]:
    """自初始日的下一日起逐日更新 MR；inputs 不含初始日。"""
    if initial not in TREND_DOMAIN:
        raise ChannelError(f"MR 的状态只能是“激活”或“已武装未激活”：{initial!r}")
    state = initial
    result: list[ChannelDay] = []
    for today in inputs:
        updated = trend_step(state, today, length)
        result.append(updated)
        state = updated.state
    return tuple(result)


def event_dates(path: Sequence[ChannelDay]) -> tuple[tuple[dt.date, ChannelEvent], ...]:
    """通道事件日期：只由该通道自身的输入与 K、θ_P 决定，与 h 和系统状态无关。"""
    return tuple((item.day, event) for item in path for event in item.events)
