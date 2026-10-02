"""主参照“200 日均线二级版”的状态（登记第六节第 1 小节）。纯计算。

参照不使用 h：均线不完整时保持前一日状态；均线完整且 SPX 收盘价低于均线时为二级；
否则从二级降到一级、从一级降到正常，每个信号日一级。阶段恢复只在这里发生一次，执行层直接映射。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from market_risk.wavewarn_v20.inputs import TrendDay, below_average
from market_risk.wavewarn_v20.state_machine import Risk, StateError


@dataclass(frozen=True)
class ReferenceDay:
    """主参照在某信号日的记录。"""

    day: dt.date
    level: int | None      # L_参照：2、0，或均线不完整时无定义（None）
    risk: Risk             # S_参照

    @property
    def complete(self) -> bool:
        return self.level is not None


def reference_level(today: TrendDay, length: int) -> int | None:
    """L_参照：均线完整且收盘价低于均线为 2；均线完整且不低于均线为 0；均线不完整时无定义。"""
    if today.complete != (today.total is not None) or (today.complete and today.close is None):
        raise StateError(f"{today.day} 的均线完整性与均线数值不一致")
    if not today.complete or today.close is None or today.total is None:
        return None
    return 2 if below_average(today.close, today.total, length) else 0


def next_reference(previous: Risk, level: int | None) -> Risk:
    """参照的状态表，按顺序判断。"""
    if not isinstance(previous, Risk):
        raise StateError(f"参照的风险状态不在状态域内：{previous!r}")
    if level is None:
        return previous
    if level == 2:
        return Risk.LEVEL2
    if level != 0:
        raise StateError(f"L_参照 只能是 2、0 或无定义：{level!r}")
    if previous is Risk.LEVEL2:
        return Risk.LEVEL1
    return Risk.NORMAL


def run_reference(initial: Risk, inputs: Sequence[TrendDay], length: int) -> tuple[ReferenceDay, ...]:
    """自初始日的下一日起逐日更新；inputs 不含初始日。"""
    if not isinstance(initial, Risk):
        raise StateError(f"参照的风险状态不在状态域内：{initial!r}")
    state = initial
    result: list[ReferenceDay] = []
    for today in inputs:
        level = reference_level(today, length)
        state = next_reference(state, level)
        result.append(ReferenceDay(today.day, level, state))
    return tuple(result)
