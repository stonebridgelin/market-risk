"""双指数危险时段及冻结评分的事后表现。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

START = dt.date(2008, 8, 11)
DEVELOPMENT_END = dt.date(2016, 12, 31)
END = dt.date(2022, 12, 31)


@dataclass(frozen=True)
class Episode:
    symbol: str
    high_date: dt.date
    low_date: dt.date
    high_close: Decimal


@dataclass(frozen=True)
class DangerPeriod:
    start: dt.date
    end: dt.date
    start_symbol: str
    end_symbol: str
    members: tuple[Episode, ...]
    period: str
    sessions: int
    spx_top: dt.date | None
    qqq_top: dt.date | None
    spx_bottom: dt.date | None
    qqq_bottom: dt.date | None
    spx_drawdown_pct: Decimal | None
    qqq_drawdown_pct: Decimal | None

    @property
    def source(self) -> str:
        return "双指数" if len({e.symbol for e in self.members}) == 2 else self.members[0].symbol


def _period_of(date: dt.date) -> str:
    return "开发期" if date <= DEVELOPMENT_END else "验证期"


def _earliest_low(prices: Mapping[dt.date, Decimal], dates: Sequence[dt.date]) -> dt.date:
    return min(dates, key=lambda d: (prices[d], d))


def build_danger_periods(episodes: Sequence[Episode], prices: Mapping[str, Mapping[dt.date, Decimal]],
                         days: Sequence[dt.date]) -> tuple[DangerPeriod, ...]:
    """闭区间有交集即传递合并；合并后仍保留各指数原回调。"""
    valid = sorted((e for e in episodes if START <= e.high_date <= END and e.low_date <= END),
                   key=lambda e: (e.high_date, e.low_date, e.symbol))
    groups: list[list[Episode]] = []
    ends: list[dt.date] = []
    for episode in valid:
        if groups and episode.high_date <= ends[-1]:
            groups[-1].append(episode)
            ends[-1] = max(ends[-1], episode.low_date)
        else:
            groups.append([episode])
            ends.append(episode.low_date)
    index = {day: i for i, day in enumerate(days)}
    result = []
    for group, end in zip(groups, ends, strict=True):
        start = min(e.high_date for e in group)
        start_symbols = "+".join(symbol for symbol in ("SPX", "QQQ")
                                 if any(e.symbol == symbol and e.high_date == start for e in group))
        end_symbols = "+".join(symbol for symbol in ("SPX", "QQQ")
                               if any(e.symbol == symbol and e.low_date == end for e in group))
        tops: dict[str, dt.date] = {}
        bottoms: dict[str, dt.date] = {}
        falls: dict[str, Decimal] = {}
        span = [day for day in days if start <= day <= end]
        for symbol in {e.symbol for e in group}:
            top = min(e.high_date for e in group if e.symbol == symbol)
            if any(day not in prices[symbol] for day in span):
                raise ValueError(f"危险时段 {start} 至 {end} 的 {symbol} 收盘价缺失")
            bottom = _earliest_low(prices[symbol], span)
            top_close = prices[symbol][top]
            tops[symbol], bottoms[symbol] = top, bottom
            falls[symbol] = (prices[symbol][bottom] / top_close - 1) * 100
        result.append(DangerPeriod(start, end, start_symbols, end_symbols, tuple(group),
                                   _period_of(start), index[end] - index[start] + 1,
                                   tops.get("SPX"), tops.get("QQQ"), bottoms.get("SPX"), bottoms.get("QQQ"),
                                   falls.get("SPX"), falls.get("QQQ")))
    return tuple(result)


def _warning(row: Mapping[str, str]) -> bool:
    return int(row["total_min"]) >= 3


def _downgraded(row: Mapping[str, str]) -> bool:
    return int(row["total_max"]) < 3


def _progress(period: DangerPeriod, symbol: str, day: dt.date,
              prices: Mapping[str, Mapping[dt.date, Decimal]]) -> Decimal | None:
    top = period.spx_top if symbol == "SPX" else period.qqq_top
    bottom = period.spx_bottom if symbol == "SPX" else period.qqq_bottom
    if top is None or bottom is None:
        return None
    high, low = prices[symbol][top], prices[symbol][bottom]
    return None if high == low else (high - prices[symbol][day]) / (high - low)


def rule_performance(period: DangerPeriod, version: str, scores: Mapping[tuple[dt.date, str], Mapping[str, str]],
                     prices: Mapping[str, Mapping[dt.date, Decimal]], days: Sequence[dt.date]) -> dict[str, object]:
    """首次预警、覆盖率、首次明确降级；未知总分单列。"""
    index = {day: i for i, day in enumerate(days)}
    start_i, end_i = index[period.start], index[period.end]
    first_i = max(0, start_i - 20)
    candidates = days[first_i:end_i + 1]
    first = next((day for day in candidates if _warning(scores[day, version])), None)
    active = days[start_i:end_i + 1]
    warned = sum(_warning(scores[day, version]) for day in active)
    unknown = sum(int(scores[day, version]["total_min"]) < 3 <= int(scores[day, version]["total_max"])
                  for day in active)
    downgrade = next((day for day in days[index[first] + 1:end_i + 1] if _downgraded(scores[day, version])), None) \
        if first is not None else None
    row: dict[str, object] = {
        "start": period.start, "end": period.end, "period": period.period, "version": version,
        "source": period.source, "first_warning": first,
        "warning_offset": None if first is None else index[first] - start_i,
        "warning_window_incomplete": start_i < 20,
        "already_warning_at_window_start": first == days[first_i] and start_i >= 20 if first is not None else False,
        "unwarned": first is None, "coverage_days": warned, "unknown_days": unknown,
        "sessions": len(active), "coverage": Decimal(warned) / Decimal(len(active)),
        "early_downgrade": downgrade is not None and downgrade < period.end,
        "first_downgrade": downgrade,
    }
    for symbol in ("SPX", "QQQ"):
        top = period.spx_top if symbol == "SPX" else period.qqq_top
        row[f"{symbol.lower()}_first_warning_progress"] = _progress(period, symbol, first, prices) if first else None
        row[f"{symbol.lower()}_before_top"] = first < top if first and top else None
        if downgrade and top is not None:
            tail = days[index[downgrade]:end_i + 1]
            base = prices[symbol][downgrade]
            row[f"{symbol.lower()}_downgrade_to_min_pct"] = (min(prices[symbol][day] for day in tail) / base - 1) * 100
            row[f"{symbol.lower()}_downgrade_to_end_pct"] = (prices[symbol][period.end] / base - 1) * 100
        else:
            row[f"{symbol.lower()}_downgrade_to_min_pct"] = None
            row[f"{symbol.lower()}_downgrade_to_end_pct"] = None
    fall = period.spx_drawdown_pct if period.spx_top else period.qqq_drawdown_pct
    assert fall is not None
    depth = -fall
    row["grade"] = "熊市" if depth >= 20 else "修正" if depth >= 10 else "小回调"
    return row


def lead_lag(period: DangerPeriod, prices: Mapping[str, Mapping[dt.date, Decimal]],
             days: Sequence[dt.date]) -> dict[str, object] | None:
    """双指数见顶、见底先后与取晚机会成本。"""
    if period.source != "双指数":
        return None
    index = {day: i for i, day in enumerate(days)}
    assert period.spx_top and period.qqq_top and period.spx_bottom and period.qqq_bottom
    top_delta = index[period.spx_top] - index[period.qqq_top]
    bottom_delta = index[period.spx_bottom] - index[period.qqq_bottom]
    early_symbol = "SPX" if bottom_delta < 0 else "QQQ" if bottom_delta > 0 else None
    late_day = max(period.spx_bottom, period.qqq_bottom)
    opportunity = ((prices[early_symbol][late_day] / prices[early_symbol][min(period.spx_bottom, period.qqq_bottom)]
                    - 1) * 100) if early_symbol else None
    return {"start": period.start, "end": period.end, "period": period.period,
            "top_first": "SPX" if top_delta < 0 else "QQQ" if top_delta > 0 else "同日",
            "top_gap_days": abs(top_delta), "bottom_first": early_symbol or "同日",
            "bottom_gap_days": abs(bottom_delta), "early_bottom_rebound_pct": opportunity,
            "multi_episodes": sum(e.symbol == "SPX" for e in period.members) > 1
            or sum(e.symbol == "QQQ" for e in period.members) > 1}


def summary(values: Sequence[Decimal | int]) -> dict[str, object]:
    """描述性中位数与范围，空样本保留缺失。"""
    ordered = sorted(values)
    return {"count": len(ordered), "median": median(ordered) if ordered else None,
            "min": ordered[0] if ordered else None, "max": ordered[-1] if ordered else None}
