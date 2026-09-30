"""v1.2.1 独立 ZigZag 研究标签；只接收截止评价期末的价格。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

LABEL_VERSION = "zz-v121"
LEVELS = {"SPX": (Decimal("0.04"), Decimal("0.05")),
          "QQQ": (Decimal("0.05"), Decimal("0.065"))}


@dataclass(frozen=True)
class ZZEvent:
    symbol: str
    peak_date: dt.date
    t0_date: dt.date
    trough_date: dt.date
    end_date: dt.date | None
    peak_close: Decimal
    trough_close: Decimal
    right_censored: bool


@dataclass(frozen=True)
class MergedZZEvent:
    peak_date: dt.date
    t0_date: dt.date
    trough_date: dt.date
    members: tuple[ZZEvent, ...]

    @property
    def source(self) -> str:
        return "SPX+QQQ" if len({event.symbol for event in self.members}) == 2 else self.members[0].symbol


def find_zz_events(symbol: str, days: Sequence[dt.date], prices: Mapping[dt.date, Decimal | None],
                   end: dt.date, thresholds: Mapping[str, tuple[Decimal, Decimal]] = LEVELS) -> tuple[ZZEvent, ...]:
    """价格缺失时保持寻峰／寻底状态；只用 end 及以前数据。"""
    if symbol not in thresholds:
        raise ValueError(f"不支持的 ZZ 标的：{symbol}")
    if tuple(days) != tuple(sorted(set(days))):
        raise ValueError("交易日必须唯一且升序")
    d, u = thresholds[symbol]
    if d <= 0 or u <= 0:
        raise ValueError("ZZ 下跌与反弹门槛必须为正")
    events: list[ZZEvent] = []
    peak_date: dt.date | None = None
    peak_close: Decimal | None = None
    t0_date: dt.date | None = None
    trough_date: dt.date | None = None
    trough_close: Decimal | None = None
    for day in days:
        if day > end:
            break
        close = prices.get(day)
        if close is None:
            continue
        if close <= 0:
            raise ValueError(f"{symbol} {day} 收盘价必须大于零")
        if peak_close is None:
            peak_date, peak_close = day, close
            continue
        if t0_date is None:
            if close > peak_close:
                peak_date, peak_close = day, close
            elif close <= peak_close * (1 - d):
                t0_date, trough_date, trough_close = day, day, close
        else:
            assert trough_close is not None and trough_date is not None and peak_date is not None
            if close < trough_close:
                trough_date, trough_close = day, close
            elif close >= trough_close * (1 + u):
                events.append(ZZEvent(symbol, peak_date, t0_date, trough_date, day,
                                      peak_close, trough_close, False))
                peak_date, peak_close = day, close
                t0_date = trough_date = trough_close = None
                # 结束日只重置候选高点，不再判定新的首次触及日。
    if t0_date is not None:
        assert peak_date is not None and peak_close is not None
        assert trough_date is not None and trough_close is not None
        events.append(ZZEvent(symbol, peak_date, t0_date, trough_date, None,
                              peak_close, trough_close, True))
    return tuple(events)


def dangerous_interval(event: ZZEvent, days: Sequence[dt.date]) -> tuple[dt.date, ...]:
    """该资产的已判定危险区间是 [P, Tr)。"""
    return tuple(day for day in days if event.peak_date <= day < event.trough_date)


def right_censored_unknown(event: ZZEvent, days: Sequence[dt.date], end: dt.date) -> tuple[dt.date, ...]:
    """期末仍在寻底时，暂定低点之后的价格区间危险归属未知。"""
    if not event.right_censored:
        return ()
    return tuple(day for day in days if event.trough_date <= day < end)


def merge_zz_events(events: Sequence[ZZEvent]) -> tuple[MergedZZEvent, ...]:
    """按闭区间 [P, Tr] 的交集传递合并，保留每个资产的原事件。"""
    ordered = sorted(events, key=lambda event: (event.peak_date, event.trough_date, event.symbol))
    groups: list[list[ZZEvent]] = []
    ends: list[dt.date] = []
    for event in ordered:
        if groups and event.peak_date <= ends[-1]:
            groups[-1].append(event)
            ends[-1] = max(ends[-1], event.trough_date)
        else:
            groups.append([event])
            ends.append(event.trough_date)
    return tuple(MergedZZEvent(min(event.peak_date for event in group),
                               min(event.t0_date for event in group), end, tuple(group))
                 for group, end in zip(groups, ends, strict=True))
