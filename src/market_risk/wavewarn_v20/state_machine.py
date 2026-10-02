"""两级风险状态与恢复计数（登记第一节第 3 至 5 小节）。纯计算。

每个信号日的顺序：通道更新（channels）→ A₁、A₂ 与截断计数 → 证据级别 L 与七行状态表 → S。
计数只依赖通道状态与输入完整性，与系统状态无关；初始日（t0）只放初始状态、不更新。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum

from market_risk.wavewarn_v20.channels import (
    ChannelDay,
    ChannelState,
    pullback_valid,
    run_pullback,
    run_trend,
    trend_valid,
)
from market_risk.wavewarn_v20.inputs import AssetDay, TrendDay


class StateError(ValueError):
    """状态、计数或参数不在登记允许的范围内。"""


class Risk(IntEnum):
    NORMAL = 0       # 正常
    LEVEL1 = 1       # 一级
    LEVEL2 = 2       # 二级


@dataclass(frozen=True)
class Evidence:
    """某信号日通道更新完成后的全部证据：两资产的输入、均线输入与五个通道的状态。"""

    spx: AssetDay
    qqq: AssetDay
    trend: TrendDay
    p_spx: ChannelDay
    p_qqq: ChannelDay
    pr_spx: ChannelDay
    pr_qqq: ChannelDay
    mr: ChannelDay

    def __post_init__(self) -> None:
        days = {self.spx.day, self.qqq.day, self.trend.day, self.p_spx.day, self.p_qqq.day, self.pr_spx.day,
                self.pr_qqq.day, self.mr.day}
        if len(days) != 1:
            raise StateError("同一份证据里的日期不一致")
        if self.spx.close != self.trend.close:
            raise StateError(f"{self.spx.day} SPX 的收盘价在回调输入与均线输入里不一致")
        if (self.p_spx.valid, self.pr_spx.valid) != (pullback_valid(self.spx),) * 2 or (
                self.p_qqq.valid, self.pr_qqq.valid) != (pullback_valid(self.qqq),) * 2 or (
                self.mr.valid != trend_valid(self.trend)):
            raise StateError(f"{self.spx.day} 通道有效性与输入完整性不一致")
        if self.mr.state is ChannelState.UNARMED:
            raise StateError(f"{self.spx.day} MR 出现了“未武装未激活”状态")

    @property
    def day(self) -> dt.date:
        return self.spx.day

    @property
    def all_valid(self) -> bool:
        """五个通道当日全部有效。"""
        return self.p_spx.valid and self.p_qqq.valid and self.pr_spx.valid and self.pr_qqq.valid and self.mr.valid


@dataclass(frozen=True)
class SystemState:
    risk: Risk
    c1: int
    c2: int


@dataclass(frozen=True)
class SystemDay:
    """某信号日的系统记录。"""

    day: dt.date
    level: int        # 证据级别 L_d
    a1: bool
    a2: bool
    c1: int
    c2: int
    risk: Risk        # S_d

    @property
    def state(self) -> SystemState:
        return SystemState(self.risk, self.c1, self.c2)


def evidence_level(evidence: Evidence) -> int:
    """L_d：任一 PR 激活为 2；否则任一 P 或 MR 激活为 1；否则为 0。"""
    if evidence.pr_spx.active or evidence.pr_qqq.active:
        return 2
    if evidence.p_spx.active or evidence.p_qqq.active or evidence.mr.active:
        return 1
    return 0


def level2_release_checks(evidence: Evidence, k: int) -> tuple[bool, bool, bool, bool]:
    """A₂ 的四项，逐项检查且只检查这四项（不看 MR、200 日均线与 P）。"""
    return (
        evidence.spx.close is not None and evidence.qqq.close is not None,
        evidence.spx.high_complete and evidence.qqq.high_complete,
        not evidence.pr_spx.active and not evidence.pr_qqq.active,
        evidence.spx.q >= k and evidence.qqq.q >= k,
    )


def level1_release_checks(evidence: Evidence, k: int) -> tuple[bool, bool, bool, bool, bool, bool]:
    """A₁ 的六项：A₂ 的四项，另加均线完整且 MR 未激活、两资产 P 未激活。"""
    return (
        *level2_release_checks(evidence, k),
        evidence.trend.complete and not evidence.mr.active,
        not evidence.p_spx.active and not evidence.p_qqq.active,
    )


def next_count(previous: int, condition: bool, h: int) -> int:
    """截断的连续计数：条件为真时 min(h, 前值 + 1)，否则为 0。"""
    return min(h, previous + 1) if condition else 0


def next_risk(previous: Risk, level: int, c1: int, c2: int, h: int) -> Risk:
    """七行状态表，按顺序判断，先满足者生效；降级每个信号日最多一级。"""
    if level == 2:
        return Risk.LEVEL2
    if previous is Risk.NORMAL:
        return Risk.LEVEL1 if level == 1 else Risk.NORMAL
    if previous is Risk.LEVEL1:
        return Risk.NORMAL if c1 >= h else Risk.LEVEL1
    if previous is Risk.LEVEL2:
        return Risk.LEVEL1 if c2 >= h else Risk.LEVEL2
    raise StateError(f"风险状态不在状态域内：{previous!r}")


def _checked_state(state: SystemState, h: int) -> SystemState:
    if h < 1:
        raise StateError("h 必须不小于 1")
    if not isinstance(state.risk, Risk) or not (0 <= state.c1 <= h and 0 <= state.c2 <= h):
        raise StateError(f"系统状态不在允许范围内：{state!r}（h = {h}）")
    return state


def system_step(previous: SystemState, evidence: Evidence, k: int, h: int) -> SystemDay:
    """由前一日的系统状态与当日证据得到当日的计数与风险状态。"""
    previous = _checked_state(previous, h)
    if k < 1:
        raise StateError("K 必须不小于 1")
    a2 = all(level2_release_checks(evidence, k))
    a1 = all(level1_release_checks(evidence, k))
    c1, c2 = next_count(previous.c1, a1, h), next_count(previous.c2, a2, h)
    level = evidence_level(evidence)
    return SystemDay(evidence.day, level, a1, a2, c1, c2, next_risk(previous.risk, level, c1, c2, h))


def run_system(initial: SystemState, evidence: Sequence[Evidence], k: int, h: int) -> tuple[SystemDay, ...]:
    """自初始日的下一日起逐日更新；evidence 不含初始日。"""
    state = _checked_state(initial, h)
    result: list[SystemDay] = []
    for today in evidence:
        updated = system_step(state, today, k, h)
        result.append(updated)
        state = updated.state
    return tuple(result)


def transitions(initial: Risk, path: Sequence[SystemDay]) -> tuple[tuple[dt.date, Risk, Risk], ...]:
    """系统转换日期：S 改变的日期及其前后状态（与通道事件日期分开列出）。"""
    result: list[tuple[dt.date, Risk, Risk]] = []
    previous = initial
    for item in path:
        if item.risk is not previous:
            result.append((item.day, previous, item.risk))
        previous = item.risk
    return tuple(result)


def direct_level2_days(initial: Risk, path: Sequence[SystemDay]) -> tuple[dt.date, ...]:
    """由正常直接进入二级的日期：前一日为正常且当日为二级。"""
    return tuple(day for day, before, after in transitions(initial, path)
                 if before is Risk.NORMAL and after is Risk.LEVEL2)


@dataclass(frozen=True)
class ChannelPaths:
    """五个通道自初始日的下一日起的逐日状态。"""

    p_spx: tuple[ChannelDay, ...]
    p_qqq: tuple[ChannelDay, ...]
    pr_spx: tuple[ChannelDay, ...]
    pr_qqq: tuple[ChannelDay, ...]
    mr: tuple[ChannelDay, ...]


@dataclass(frozen=True)
class ChannelInitial:
    """五个通道在初始日的状态；登记的 t0 初始快照为全部“已武装未激活”。"""

    p_spx: ChannelState
    p_qqq: ChannelState
    pr_spx: ChannelState
    pr_qqq: ChannelState
    mr: ChannelState


def run_channels(initial: ChannelInitial, spx: Sequence[AssetDay], qqq: Sequence[AssetDay],
                 trend: Sequence[TrendDay], k: int, theta: Decimal, average: int) -> ChannelPaths:
    """五个通道各自独立更新；PR 的门槛为 2θ_P。三个输入序列都不含初始日。"""
    return ChannelPaths(run_pullback(initial.p_spx, spx, k, theta), run_pullback(initial.p_qqq, qqq, k, theta),
                        run_pullback(initial.pr_spx, spx, k, 2 * theta),
                        run_pullback(initial.pr_qqq, qqq, k, 2 * theta), run_trend(initial.mr, trend, average))


def evidence_series(spx: Sequence[AssetDay], qqq: Sequence[AssetDay], trend: Sequence[TrendDay],
                    paths: ChannelPaths) -> tuple[Evidence, ...]:
    """把逐日输入与五个通道的状态合成逐日证据；各序列须等长、日期一一对应。"""
    return tuple(Evidence(*items) for items in zip(spx, qqq, trend, paths.p_spx, paths.p_qqq, paths.pr_spx,
                                                    paths.pr_qqq, paths.mr, strict=True))
