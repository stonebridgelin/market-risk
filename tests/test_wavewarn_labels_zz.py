"""v1.2.1 ZZ 标签的合成路径；阈值及日期由注释手推。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from market_risk.wavewarn.labels_zz import (
    ZZEvent,
    dangerous_interval,
    find_zz_events,
    merge_zz_events,
    right_censored_unknown,
)


def _days(n: int) -> tuple[dt.date, ...]:
    return tuple(dt.date(2010, 1, 4) + dt.timedelta(days=i) for i in range(n))


def test_zz_spx_exact_threshold_and_end_day_no_second_t0() -> None:
    days = _days(5)
    prices = {day: value for day, value in zip(days,
              (Decimal("100"), Decimal("96"), Decimal("90"), Decimal("94.5"), Decimal("90")), strict=True)}
    events = find_zz_events("SPX", days, prices, days[-1])
    # 100×(1−4%)=96，day1 等号触及；90×(1+5%)=94.5，day3 等号结束。
    # day3 只重置候选高点，不在同日判新的 T0；day4 从94.5跌至90，形成右截尾事件。
    assert len(events) == 2
    first, second = events
    assert (first.peak_date, first.t0_date, first.trough_date, first.end_date) == (
        days[0], days[1], days[2], days[3])
    assert first.right_censored is False
    assert (second.peak_date, second.t0_date, second.end_date) == (days[3], days[4], None)
    assert second.right_censored is True
    assert dangerous_interval(first, days) == days[:2]  # [P,Tr)，不含低点日。


def test_zz_qqq_missing_price_holds_state_and_right_censors() -> None:
    days = _days(6)
    prices = {days[0]: Decimal("100"), days[1]: None, days[2]: Decimal("95"),
              days[3]: Decimal("90"), days[4]: None, days[5]: Decimal("94")}
    events = find_zz_events("QQQ", days, prices, days[-1])
    # QQQ 下跌门槛5%：day1 缺价不触及；day2=95 才等号触及；低点90后需95.85才结束。
    assert len(events) == 1
    event = events[0]
    assert (event.t0_date, event.trough_date, event.end_date) == (days[2], days[3], None)
    assert event.right_censored is True
    # 暂定低点 day3 后的区间 day3→day4 与 day4→day5 危险归属不可判定。
    assert right_censored_unknown(event, days, days[-1]) == (days[3], days[4])


def test_zz_transitive_closed_interval_merge_keeps_individual_events() -> None:
    days = _days(8)
    events = (
        ZZEvent("SPX", days[0], days[1], days[2], days[3], Decimal("100"), Decimal("90"), False),
        ZZEvent("QQQ", days[2], days[3], days[4], days[5], Decimal("200"), Decimal("180"), False),
        ZZEvent("SPX", days[4], days[5], days[6], days[7], Decimal("100"), Decimal("90"), False),
    )
    merged = merge_zz_events(events)
    # 第一与第二段在 day2 端点相接，第二与第三段在 day4 相接，传递合并为一段。
    assert len(merged) == 1
    assert (merged[0].peak_date, merged[0].t0_date, merged[0].trough_date) == (
        days[0], days[1], days[6])
    assert merged[0].source == "SPX+QQQ"
    assert merged[0].members == events
