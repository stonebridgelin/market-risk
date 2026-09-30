"""v1.2.1 事件账五类与 T0 前信号、执行两个独立时点。"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from market_risk.wavewarn.execution import ExecutionDay
from market_risk.wavewarn.labels_zz import MergedZZEvent, ZZEvent
from market_risk.wavewarn.loss import AssetLossDay
from market_risk.wavewarn.state_machine import Light

EventClass = Literal["新警报", "持续覆盖", "迟到", "中断", "漏报"]
EVENT_CLASSES: tuple[EventClass, ...] = ("新警报", "持续覆盖", "迟到", "中断", "漏报")


@dataclass(frozen=True)
class EventLedgerRow:
    peak_date: dt.date
    t0_date: dt.date
    trough_date: dt.date
    classification: EventClass | None
    signal_before_t0: bool
    reduction_executed_before_t0: bool
    prior_alert: bool
    lead_days: int | None
    alert_start: dt.date | None
    right_censored: bool


def alert_segments(days: Sequence[dt.date], lights: Sequence[Light]) -> tuple[tuple[int, int], ...]:
    """连续非绿日形成闭区间，端点为交易日序号。"""
    if len(days) != len(lights):
        raise ValueError("交易日与灯色数量不一致")
    segments = []
    start: int | None = None
    for index, light in enumerate(lights):
        if light != "绿" and start is None:
            start = index
        if light == "绿" and start is not None:
            segments.append((start, index - 1))
            start = None
    if start is not None:
        segments.append((start, len(days) - 1))
    return tuple(segments)


def classify_event(days: Sequence[dt.date], lights: Sequence[Light], peak_date: dt.date,
                   t0_date: dt.date, trough_date: dt.date,
                   right_censored: bool = False) -> EventLedgerRow:
    """按新警报、持续覆盖、迟到、中断、漏报的优先顺序穷尽分类。"""
    if len(days) != len(lights):
        raise ValueError("交易日与灯色数量不一致")
    positions = {day: index for index, day in enumerate(days)}
    if any(day not in positions for day in (peak_date, t0_date, trough_date)):
        raise ValueError("事件日期须位于同一交易日轴")
    peak, t0, trough = positions[peak_date], positions[t0_date], positions[trough_date]
    if not (peak <= t0 <= trough) or t0 < 2:
        raise ValueError("事件日期顺序错误或缺少 T0 前两个灯色")
    segments = alert_segments(days, lights)
    before = lights[t0 - 1] != "绿"
    executed = lights[t0 - 2] != "绿"
    prior = any(lights[index] != "绿" for index in range(max(0, peak - 20), t0 - 1))
    active_segment = next(((start, stop) for start, stop in segments if start <= t0 - 1 <= stop), None)
    alert_start = days[active_segment[0]] if active_segment else None
    lead: int | None = None
    if active_segment is not None and active_segment[0] >= max(0, t0 - 20):
        lead = t0 - active_segment[0]
    if right_censored:
        # 暂定低点并非已确认结束日；保留已发生的时点事实，不纳入五类归因。
        category: EventClass | None = None
    elif before:
        assert active_segment is not None
        start = active_segment[0]
        if start >= max(0, t0 - 20):
            category = "新警报"
        else:
            category = "持续覆盖"
    elif any(lights[index] != "绿" for index in range(t0, trough)):
        category = "迟到"
    elif prior:
        category = "中断"
    else:
        category = "漏报"
    return EventLedgerRow(peak_date, t0_date, trough_date, category, before, executed,
                          prior, lead, alert_start, right_censored)


def classify_asset_event(days: Sequence[dt.date], lights: Sequence[Light], event: ZZEvent) -> EventLedgerRow:
    """分资产事件保留右截尾标志；五类归因只适用于已确认事件。"""
    return classify_event(days, lights, event.peak_date, event.t0_date,
                          event.trough_date, event.right_censored)


def classify_merged_event(days: Sequence[dt.date], lights: Sequence[Light],
                          event: MergedZZEvent) -> EventLedgerRow:
    """合并事件只复用灯色时点分类，不合并资产价格或低点价格。"""
    return classify_event(days, lights, event.peak_date, event.t0_date, event.trough_date,
                          any(member.right_censored for member in event.members))


@dataclass(frozen=True)
class EventClassSummary:
    confirmed: int
    right_censored: int
    counts: dict[EventClass, int]
    proportions: dict[EventClass, Decimal | None]


def summarize_event_classes(rows: Sequence[EventLedgerRow]) -> EventClassSummary:
    """五类比例仅以已确认事件为分母，右截尾单独计数。"""
    right_censored = sum(row.right_censored for row in rows)
    confirmed = len(rows) - right_censored
    observed = Counter(row.classification for row in rows if not row.right_censored)
    if observed.get(None):
        raise ValueError("已确认事件缺少五类归因")
    counts = {kind: observed[kind] for kind in EVENT_CLASSES}
    proportions = {kind: Decimal(counts[kind]) / confirmed if confirmed else None
                   for kind in EVENT_CLASSES}
    return EventClassSummary(confirmed, right_censored, counts, proportions)


@dataclass(frozen=True)
class AlertLedgerRow:
    start: dt.date
    end: dt.date
    trading_days: int
    active_channels: tuple[str, ...]
    covered_events: tuple[str, ...]
    no_event_alert: bool
    protected_decline: Decimal
    outside_danger_days: int
    net_opportunity_cost: Decimal


def build_alert_ledger(days: Sequence[dt.date], lights: Sequence[Light],
                       active_channels: Sequence[Sequence[str]], events: Sequence[ZZEvent],
                       asset_losses: dict[str, Sequence[AssetLossDay]],
                       weights: dict[str, Decimal]) -> tuple[AlertLedgerRow, ...]:
    """保护效果使用已确认的避险对数跌幅；事件重叠窗为 [P−20,Tr+5]。"""
    if len(days) != len(lights) or len(days) != len(active_channels):
        raise ValueError("警报账输入时间轴不一致")
    if set(asset_losses) != set(weights):
        raise ValueError("资产损失与权重不一致")
    if any(len(rows) != len(days) - 1 for rows in asset_losses.values()):
        raise ValueError("资产损失须按完整区间时间轴排列")
    positions = {day: index for index, day in enumerate(days)}
    result = []
    for start, stop in alert_segments(days, lights):
        overlapping = []
        for event in events:
            if event.peak_date not in positions or event.trough_date not in positions:
                continue
            peak, trough = positions[event.peak_date], positions[event.trough_date]
            if start <= min(len(days) - 1, trough + 5) and stop >= max(0, peak - 20):
                overlapping.append(f"{event.symbol}:{event.peak_date.isoformat()}")
        protected = Decimal(0)
        opportunity = Decimal(0)
        outside_days = sum(all(rows[index].dangerous is False for rows in asset_losses.values())
                           for index in range(start, min(stop, len(days) - 2) + 1))
        for symbol, rows in asset_losses.items():
            weight = weights[symbol]
            for index in range(start, min(stop, len(rows) - 1) + 1):
                row = rows[index]
                if row.excluded_reason:
                    continue
                if row.dangerous:
                    assert row.log_return is not None
                    protected += weight * (1 - row.exposure) * max(-row.log_return, Decimal(0))
                else:
                    opportunity += weight * row.opportunity_loss
        channels = tuple(sorted({name for index in range(start, stop + 1)
                                 for name in active_channels[index]}))
        result.append(AlertLedgerRow(days[start], days[stop], stop - start + 1, channels,
                                     tuple(overlapping), not overlapping, protected,
                                     outside_days, opportunity))
    return tuple(result)


@dataclass(frozen=True)
class YearlyAlertSummary:
    year: int
    trading_days: int
    non_green_days: int
    non_green_ratio: Decimal
    longest_alert_days: int
    executed_switches: int
    active_channel_days: dict[str, int]


def yearly_alert_summary(days: Sequence[dt.date], lights: Sequence[Light],
                         executions: Sequence[ExecutionDay],
                         active_channels: Sequence[Sequence[str]]) -> tuple[YearlyAlertSummary, ...]:
    """跨年警报段按日历年截断；切换归属执行日。"""
    if not (len(days) == len(lights) == len(executions) == len(active_channels)):
        raise ValueError("年度警报汇总输入长度不一致")
    result = []
    for year in sorted({day.year for day in days}):
        positions = [index for index, day in enumerate(days) if day.year == year]
        non_green = sum(lights[index] != "绿" for index in positions)
        longest = current = 0
        counts: dict[str, int] = {}
        for index in positions:
            current = current + 1 if lights[index] != "绿" else 0
            longest = max(longest, current)
            for name in set(active_channels[index]):
                counts[name] = counts.get(name, 0) + 1
        result.append(YearlyAlertSummary(
            year, len(positions), non_green, Decimal(non_green) / len(positions), longest,
            sum(executions[index].switched for index in positions), counts))
    return tuple(result)


@dataclass(frozen=True)
class EventTimingDetails:
    first_alert_date: dt.date | None
    first_green_date: dt.date | None
    green_before_trough: bool | None
    rebound_from_trough_percent: Decimal | None
    decline_after_green_percent: dict[int, Decimal | None]
    reupgraded_after_green: dict[int, bool | None]
    first_alert_channels: tuple[str, ...]
    release_reason: str


def event_timing_details(days: Sequence[dt.date], lights: Sequence[Light],
                         closes: Sequence[Decimal | None], event: ZZEvent,
                         active_channels: Sequence[Sequence[str]],
                         reasons: Sequence[str]) -> EventTimingDetails:
    """首次警报段解除日的事后时点明细；未来窗口不完整或有缺价则留空。"""
    if not (len(days) == len(lights) == len(closes) == len(active_channels) == len(reasons)):
        raise ValueError("事件账时间轴不一致")
    positions = {day: index for index, day in enumerate(days)}
    if event.peak_date not in positions or event.trough_date not in positions:
        raise ValueError("事件不在交易日轴")
    peak, trough = positions[event.peak_date], positions[event.trough_date]
    first_segment = next(((start, stop) for start, stop in alert_segments(days, lights)
                          if start <= trough - 1 and stop >= max(0, peak - 20)), None)
    empty_drops: dict[int, Decimal | None] = {window: None for window in (5, 10, 20)}
    empty_upgrade: dict[int, bool | None] = {window: None for window in (5, 10, 20)}
    if first_segment is None:
        return EventTimingDetails(None, None, None, None, empty_drops, empty_upgrade, (), "")
    start, stop = first_segment
    alert_day = days[start]
    channels = tuple(sorted(set(active_channels[start])))
    if stop + 1 >= len(days):
        return EventTimingDetails(alert_day, None, None, None, empty_drops, empty_upgrade, channels, "")
    green = stop + 1
    green_close = closes[green]
    rebound = (green_close / event.trough_close - 1) * 100 if green_close is not None else None
    drops = {}
    upgrades = {}
    for window in (5, 10, 20):
        if green + window >= len(days):
            drops[window] = None
            upgrades[window] = None
            continue
        later = closes[green + 1:green + window + 1]
        drops[window] = ((min(later) / green_close - 1) * 100
                         if green_close is not None and all(value is not None for value in later) else None)
        upgrades[window] = any(light != "绿" for light in lights[green + 1:green + window + 1])
    return EventTimingDetails(alert_day, days[green], green < trough, rebound,
                              drops, upgrades, channels, reasons[green])
