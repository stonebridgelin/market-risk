"""v1.4 的转绿延迟、可行条件与三级选择程序（纯计算）。

可行条件：非绿占比 ≤ 上限；SPX、QQQ 的转绿延迟中位数各 ≤ 上限（只对类别②事件；无样本视为不满足）。
选择：按顺序取第一个非空的候选范围，在其中取择时得分 T 最小者；并列依次比较执行非绿天数、
计费切换次数、登记顺序。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.wavewarn.calibration import Distribution, distribution
from market_risk.wavewarn.config_v14 import DELAY_QQQ, DELAY_SPX, NON_GREEN, SelectionTier, V14Limits
from market_risk.wavewarn.exit_costs import ExitCostEvent

DELAY_CONDITION = {"SPX": DELAY_SPX, "QQQ": DELAY_QQQ}


@dataclass(frozen=True)
class GreenDelay:
    """一个设定在一个资产上的转绿延迟：首个绿灯执行日 g 与最终低点 Tr 之间的交易日数（只含类别②）。"""

    symbol: str
    included: int
    class_1: int
    class_2: int
    class_3: int
    delays: Distribution


def green_delay(symbol: str, axis: Sequence[dt.date], costs: Sequence[ExitCostEvent]) -> GreenDelay:
    """纳入门槛与分类口径同 v1.3 的退出代价；g=Tr 时延迟为 0。"""
    position = {day: index for index, day in enumerate(axis)}
    included = [row for row in costs if row.inclusion == "纳入"]
    counts = [sum(row.rebound_class == name for row in included)
              for name in ("①低点前已绿", "②低点或之后转绿", "③下一事件前未转绿")]
    days = []
    for row in included:
        if row.rebound_class != "②低点或之后转绿":
            continue
        if row.first_green_from_trough is None:
            raise ValueError("类别②事件缺少首个绿灯执行日")
        days.append(Decimal(position[row.first_green_from_trough] - position[row.trough_date]))
    if sum(counts) != len(included) or len(days) != counts[1]:
        raise ValueError("转绿延迟的三类未穷尽纳入事件")
    return GreenDelay(symbol, len(included), counts[0], counts[1], counts[2], distribution(days))


def condition_flags(non_green_share: Decimal, delays: Mapping[str, GreenDelay],
                    limits: V14Limits) -> dict[str, bool]:
    """三项条件各自是否满足；某资产类别②无样本时该资产的转绿条件不满足。"""
    flags = {NON_GREEN: non_green_share <= limits.non_green_share_max}
    for symbol, name in DELAY_CONDITION.items():
        median = delays[symbol].delays.median
        flags[name] = median is not None and median <= limits.green_delay_median_max
    return flags


@dataclass(frozen=True)
class TierItem:
    """选择程序只需要的字段；key 由调用方给出，用来对回完整结果。"""

    key: str
    score: Decimal
    executed_non_green_days: int
    billed_switches: int
    order: int
    flags: Mapping[str, bool]


@dataclass(frozen=True)
class TierTrace:
    """三级选择程序的逐步追踪。"""

    sizes: tuple[int, ...]       # 每一级的候选数（各自按该级条件计，不互斥）
    tier: int                    # 最终所在级别，从 1 起
    note: str                    # 该级的标注；第一级为空
    selected: TierItem


def tier_key(item: TierItem) -> tuple[Decimal, int, int, int]:
    """T 最小；并列依次比较执行非绿天数、计费切换次数、登记顺序。"""
    return item.score, item.executed_non_green_days, item.billed_switches, item.order


def in_tier(item: TierItem, tier: SelectionTier) -> bool:
    return all(item.flags[name] for name in tier.conditions)


def select_by_tiers(items: Sequence[TierItem], tiers: Sequence[SelectionTier]) -> TierTrace:
    """按顺序取第一个非空的候选范围；最后一级不设条件，所以只要有候选就一定选得出。"""
    if not items:
        raise ValueError("没有候选设定")
    pools = [[item for item in items if in_tier(item, tier)] for tier in tiers]
    for index, pool in enumerate(pools):
        if pool:
            return TierTrace(tuple(len(group) for group in pools), index + 1, tiers[index].note,
                             min(pool, key=tier_key))
    raise ValueError("终止规则的最后一级必须不设条件")
