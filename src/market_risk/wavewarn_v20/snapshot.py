"""数据快照（登记第一节第 6 小节第 1 步）：只含快照日及以前数据的不可变结构。纯计算，不读写文件。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType


class SnapshotError(ValueError):
    """快照的输入不合法，或访问了快照日之后的数据。"""


@dataclass(frozen=True)
class Snapshot:
    """第 day 日的数据快照：交易日轴与各资产收盘价都截到 day（含）。"""

    day: dt.date
    days: tuple[dt.date, ...]                         # 交易日轴，严格升序，末日为 day
    closes: Mapping[str, Mapping[dt.date, Decimal]]   # 资产 → 日期 → 收盘价；缺价的日期不在映射里
    acquired_at: dt.datetime                          # 实际取得时间，带时区
    source_hashes: Mapping[str, str]                  # 资产 → 来源文件的哈希

    def close(self, asset: str, day: dt.date) -> Decimal | None:
        """某资产某日的收盘价；缺价返回 None。访问快照日之后或日期轴之外的日期即报错。"""
        if day > self.day:
            raise SnapshotError(f"访问了快照日 {self.day} 之后的数据：{day}")
        if asset not in self.closes:
            raise SnapshotError(f"快照里没有资产 {asset}")
        if day not in self._axis:
            raise SnapshotError(f"{day} 不在快照的交易日轴上")
        return self.closes[asset].get(day)

    def series(self, asset: str) -> tuple[Decimal | None, ...]:
        """与交易日轴对齐的收盘价序列；缺价为 None。"""
        if asset not in self.closes:
            raise SnapshotError(f"快照里没有资产 {asset}")
        values = self.closes[asset]
        return tuple(values.get(day) for day in self.days)

    @property
    def _axis(self) -> frozenset[dt.date]:
        return frozenset(self.days)


def make_snapshot(days: Sequence[dt.date], closes: Mapping[str, Mapping[dt.date, Decimal]], day: dt.date,
                  acquired_at: dt.datetime, source_hashes: Mapping[str, str]) -> Snapshot:
    """由交易日轴与收盘价生成第 day 日的快照：day 之后的日期与价格一律不带入。

    日期轴之前的日期没有价格可言；日期轴上没有价格的日期即缺价。价格落在日期轴之外属于未覆盖的输入状态，报错。
    """
    if any(later <= earlier for earlier, later in pairwise(days)):
        raise SnapshotError("交易日轴必须严格升序")
    kept = tuple(item for item in days if item <= day)
    if not kept or kept[-1] != day:
        raise SnapshotError(f"快照日 {day} 不在交易日轴上")
    if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
        raise SnapshotError("实际取得时间必须带时区")
    if set(source_hashes) != set(closes):
        raise SnapshotError("来源哈希必须与资产一一对应")
    axis = frozenset(kept)
    frozen: dict[str, Mapping[dt.date, Decimal]] = {}
    for asset, values in closes.items():
        within = {item: value for item, value in values.items() if item <= day}
        outside = sorted(item for item in within if item not in axis)
        if outside:
            raise SnapshotError(f"{asset} 有不在交易日轴上的价格日期：{outside[0]}")
        if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in within.values()):
            raise SnapshotError(f"{asset} 的收盘价必须是正的有限 Decimal")
        frozen[asset] = MappingProxyType(within)
    return Snapshot(day, kept, MappingProxyType(frozen), acquired_at, MappingProxyType(dict(source_hashes)))
