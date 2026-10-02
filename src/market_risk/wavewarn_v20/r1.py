"""R1 与回撤（登记第五节、第七节第 4 小节；产品规格第四节）。纯计算，float64。

回撤只从净值序列独立重建；净值序列以窗口起点的 1 开头。
R1 满足 ⇔ 信号模拟的最大回撤 ≤ ratio × 同一组合一直持有的最大回撤（等号算满足）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


class R1Error(ValueError):
    """净值序列不合法。"""


@dataclass(frozen=True)
class R1Result:
    """computable 为假即“R1 无法计算”（净值无法计算），此时不给回撤，也不判断是否满足。"""

    computable: bool
    signal_drawdown: float | None
    hold_drawdown: float | None
    satisfied: bool | None


def _checked(wealth: Sequence[float]) -> Sequence[float]:
    if not wealth or any(not math.isfinite(value) or value <= 0 for value in wealth):
        raise R1Error("净值序列须非空，且每个值都是正的有限数")
    return wealth


def max_drawdown(wealth: Sequence[float]) -> float:
    """MDD = max_t (1 − W_t ÷ max_{s≤t} W_s)，含起点。"""
    peak = worst = None
    for value in _checked(wealth):
        peak = value if peak is None else max(peak, value)
        drawdown = 1.0 - value / peak
        worst = drawdown if worst is None else max(worst, drawdown)
    return float(worst)     # type: ignore[arg-type]


def r1_result(signal: Sequence[float] | None, hold: Sequence[float] | None, ratio: float) -> R1Result:
    """signal、hold 为信号模拟与一直持有的净值序列；任一为 None 表示净值无法计算。"""
    if signal is None or hold is None:
        return R1Result(False, None, None, None)
    for wealth in (signal, hold):
        if _checked(wealth)[0] != 1.0:
            raise R1Error("净值序列须以窗口起点的 1 开头")
    if len(signal) != len(hold):
        raise R1Error("两条净值序列须等长")
    signal_drawdown, hold_drawdown = max_drawdown(signal), max_drawdown(hold)
    return R1Result(True, signal_drawdown, hold_drawdown, signal_drawdown <= ratio * hold_drawdown)


def segment_drawdown_ratio(signal: Sequence[float], hold: Sequence[float], start: int, stop: int) -> float | None:
    """分段回撤比（只作描述）：段 [start, stop] 内以段首净值为起点重建回撤。

    一直持有在该段的最大回撤为 0 时，比值“无定义”（None）。
    """
    if not 0 <= start <= stop < len(signal) or len(signal) != len(hold):
        raise R1Error("分段的起止位置不合法")
    hold_drawdown = max_drawdown(hold[start:stop + 1])
    if hold_drawdown == 0:
        return None
    return max_drawdown(signal[start:stop + 1]) / hold_drawdown
