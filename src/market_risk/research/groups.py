"""开发期高位候选日的事后分组与独立观测单位。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from market_risk.research.features import FeatureEngine, high_position_days
from market_risk.research.pullback import DangerPeriod


@dataclass(frozen=True)
class Candidate:
    date: dt.date
    category: str
    target_starts: tuple[dt.date, ...]
    in_danger: bool
    complete_5: bool
    complete_20: bool


@dataclass(frozen=True)
class Observation:
    key: str
    kind: str
    dates: tuple[dt.date, ...]
    period_start: dt.date | None
    top_first: str | None
    values: Mapping[str, Decimal | None]
    effective_days: Mapping[str, int]


def classify_candidates(engine: FeatureEngine, periods: Sequence[DangerPeriod],
                        development_days: Sequence[dt.date], threshold: Decimal,
                        symbol: str = "SPX", unknown_days: frozenset[dt.date] = frozenset()
                        ) -> tuple[Candidate, ...]:
    """优先排除窗口不完整与起点当天；样本允许映射至多个危险时段。"""
    candidates = high_position_days(engine, development_days, threshold, symbol)
    index = {day: i for i, day in enumerate(development_days)}
    starts = {period.start for period in periods if period.period == "开发期"}
    result = []
    for day in candidates:
        i = index[day]
        complete5, complete20 = i + 5 < len(development_days), i + 20 < len(development_days)
        after5 = tuple(d for d in development_days[i + 1:i + 6] if d in starts)
        after20 = tuple(d for d in development_days[i + 1:i + 21] if d in starts)
        inside = any(period.start <= day <= period.end for period in periods if period.period == "开发期")
        if day in unknown_days:
            category = "期末尾段危险归属未定"
        elif not complete20:
            category = "后续20日窗口不完整"
        elif day in starts:
            category = "起点当天"
        elif after5:
            category = "样本组"
        elif after20:
            category = "灰色区"
        elif inside:
            category = "危险时段内且20日无新起点"
        else:
            category = "对照组"
        result.append(Candidate(day, category, after5 if category == "样本组" else (),
                                inside, complete5, complete20))
    return tuple(result)


def _average_feature_values(engine: FeatureEngine, dates: Sequence[dt.date]) -> tuple[dict[str, Decimal | None],
                                                                                     dict[str, int]]:
    rows = [engine.values(day) for day in dates]
    names = rows[0].keys()
    values: dict[str, Decimal | None] = {}
    counts: dict[str, int] = {}
    for name in names:
        good = [row[name] for row in rows if row[name] is not None]
        counts[name] = len(good)
        values[name] = sum(good) / Decimal(len(good)) if good else None
    return values, counts


def observations(engine: FeatureEngine, periods: Sequence[DangerPeriod], candidates: Sequence[Candidate],
                 development_days: Sequence[dt.date]) -> tuple[Observation, ...]:
    """样本按危险时段、对照按相邻交易日序号差不超过5的簇平均。"""
    result = []
    by_day = {candidate.date: candidate for candidate in candidates}
    index = {day: i for i, day in enumerate(development_days)}
    for period in periods:
        if period.period != "开发期":
            continue
        dates = tuple(day for day, candidate in by_day.items()
                      if candidate.category == "样本组" and period.start in candidate.target_starts)
        if not dates:
            continue
        values, counts = _average_feature_values(engine, dates)
        if period.spx_top and period.qqq_top:
            first = "SPX" if period.spx_top < period.qqq_top else "QQQ" if period.qqq_top < period.spx_top else "同日"
        else:
            first = period.source
        result.append(Observation(period.start.isoformat(), "样本组", dates, period.start, first, values, counts))
    controls = sorted(day for day, candidate in by_day.items() if candidate.category == "对照组")
    clusters: list[list[dt.date]] = []
    for day in controls:
        if clusters and index[day] - index[clusters[-1][-1]] <= 5:
            clusters[-1].append(day)
        else:
            clusters.append([day])
    for cluster in clusters:
        values, counts = _average_feature_values(engine, cluster)
        result.append(Observation(f"cluster_{cluster[0].isoformat()}", "对照组", tuple(cluster),
                                  None, None, values, counts))
    return tuple(result)


def uncovered_before_starts(engine: FeatureEngine, periods: Sequence[DangerPeriod],
                            development_days: Sequence[dt.date], high_days: Sequence[dt.date]) -> tuple[int, int]:
    """开发期危险起点前1至5日的全部日期，以及其中不满足高位条件的日期。"""
    index = {day: i for i, day in enumerate(development_days)}
    high = set(high_days)
    dates = {day for period in periods if period.period == "开发期" and period.start in index
             for day in development_days[max(0, index[period.start] - 5):index[period.start]]}
    return len(dates), sum(day not in high for day in dates)
