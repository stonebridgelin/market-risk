"""切换诊断（纯计算，描述性，不参与任何判定）：补充登记 D。

输入是评价窗口内逐日的执行灯色 L_0 … L_N（L_j 为第 j 天收盘执行后的灯色，持有于区间 j→j+1）。
切换口径与登记一致：只计窗口内相邻两日执行灯色不同的切换，即第 j 天（1 ≤ j ≤ N−1）L_j ≠ L_{j−1}；
初始建仓（j₀ 当日继承的灯色）不计，窗口末日（第 N 天）的切换不计费、不计入。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from market_risk.wavewarn.calibration import linear_percentile

Lights = Sequence[str]
TRANSITION_TYPES = (("绿", "黄"), ("黄", "绿"), ("黄", "红"), ("红", "黄"), ("绿", "红"), ("红", "绿"))
UP, DOWN = "暴露上升", "暴露下降"


@dataclass(frozen=True)
class Switch:
    index: int                 # 执行日在窗口内的行号 j（1 ≤ j ≤ N−1）
    date: dt.date              # 执行日
    before: str
    after: str
    change: Decimal            # e_j − e_{j−1}

    @property
    def direction(self) -> str:
        return UP if self.change > 0 else DOWN


def window_switches(days: Sequence[dt.date], lights: Lights, exposures: Mapping[str, Decimal]) -> tuple[Switch, ...]:
    """窗口内计费的切换；days 与 lights 等长（N+1），首日与末日的切换都不在其中。"""
    if len(days) != len(lights) or len(days) < 2:
        raise ValueError("切换诊断要求日期与执行灯色等长，且至少有一个区间")
    return tuple(Switch(index, days[index], lights[index - 1], lights[index],
                        exposures[lights[index]] - exposures[lights[index - 1]])
                 for index in range(1, len(days) - 1) if lights[index] != lights[index - 1])


def switches_by_year(switches: Sequence[Switch]) -> dict[int, int]:
    """按执行日所在年份计数。"""
    return dict(sorted(Counter(item.date.year for item in switches).items()))


def switches_by_type(switches: Sequence[Switch]) -> dict[tuple[str, str], int]:
    """按转换类型计数；登记列出的五类之外另列红→绿（只可能出现在没有黄灯的参照行）。"""
    counts = Counter((item.before, item.after) for item in switches)
    if set(counts) - set(TRANSITION_TYPES):
        raise ValueError("出现未登记的转换类型")
    return {kind: counts.get(kind, 0) for kind in TRANSITION_TYPES}


def reversal_count(switches: Sequence[Switch], window: int) -> int:
    """随后 window 个交易日内（执行日行号在 (j, j+window]）至少出现一次反方向切换的起始切换数。

    每次起始切换在一个窗口内最多计一次。
    """
    return sum(any(item.index < other.index <= item.index + window and other.direction != item.direction
                   for other in switches) for item in switches)


def exposure_change(switches: Sequence[Switch]) -> Decimal:
    """目标暴露变化量 Σ|Δe|：不含每日再平衡交易与初始建仓。"""
    return sum((abs(item.change) for item in switches), Decimal(0))


@dataclass(frozen=True)
class Segment:
    """一段连续相同执行灯色的区间。"""

    light: str
    start: dt.date             # 第一个区间的起点
    end: dt.date               # 最后一个区间的起点
    length: int                # 区间数（持有的交易日数）
    truncated: bool            # 首段或末段被评价窗口截断（段的真实起点在窗口之前，或终点未观察到）


def holding_segments(days: Sequence[dt.date], lights: Lights, starts_with_switch: bool,
                     ends_with_switch: bool) -> tuple[Segment, ...]:
    """按区间 0…N−1 的执行灯色分段。

    首段在 j₀ 当日恰有一次切换（starts_with_switch）时不算截断；末段在窗口末日恰有一次切换
    （ends_with_switch，该切换不计费）时不算截断。
    """
    held = list(lights[:-1])
    if len(days) != len(lights) or not held:
        raise ValueError("持有时长要求日期与执行灯色等长，且至少有一个区间")
    bounds = [0, *(index for index in range(1, len(held)) if held[index] != held[index - 1]), len(held)]
    segments = []
    for order, (start, stop) in enumerate(pairwise(bounds)):
        cut = (order == 0 and not starts_with_switch) or (stop == len(held) and not ends_with_switch)
        segments.append(Segment(held[start], days[start], days[stop - 1], stop - start, cut))
    return tuple(segments)


@dataclass(frozen=True)
class HoldingSummary:
    light: str
    count: int
    median: Decimal | None
    p25: Decimal | None
    p75: Decimal | None
    maximum: int | None
    truncated: int             # 其中被窗口截断的段数


def holding_summary(segments: Sequence[Segment], light: str) -> HoldingSummary:
    """某一颜色全部执行段（含被截断的首末段）的时长分布；线性插值分位数。"""
    own = [item for item in segments if item.light == light]
    lengths = [Decimal(item.length) for item in own]
    return HoldingSummary(light, len(own), linear_percentile(lengths, Decimal("0.5")),
                          linear_percentile(lengths, Decimal("0.25")), linear_percentile(lengths, Decimal("0.75")),
                          max((item.length for item in own), default=None), sum(item.truncated for item in own))


@dataclass(frozen=True)
class TimingSplit:
    """T = T价格 + 切换项。"""

    score: Decimal
    switch_cost: Decimal
    switches: int

    @property
    def price_score(self) -> Decimal:
        return self.score - self.switch_cost


def break_even_gamma(model: TimingSplit, baseline: TimingSplit) -> Decimal | None:
    """使两者 T 相等的每次切换罚分 γ*：T价格_M + γ*·n_M = T价格_B + γ*·n_B。

    它只是比较结果的翻转点，不是推荐参数，也不是实测交易成本；切换次数相同时无解。
    """
    if model.switches == baseline.switches:
        return None
    return (baseline.price_score - model.price_score) / (model.switches - baseline.switches)
