"""广度指标数据质量检查与疑似陈旧值标注（只报告，不改变评分逻辑）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

from conftest import load_sample_raw

from market_risk import calendar as mcal
from market_risk.data.breadth import merge_breadth
from market_risk.data.snapshot import build_snapshot
from market_risk.data.tradingview import Bar
from market_risk.data.tv_quality import analyze, is_single, render, stale_dates
from market_risk.models import BreadthReading

D = dt.date


def bar(d: dt.date, c: float, spread: float = 0.0) -> Bar:
    return Bar(d, c, c + spread, c - spread, c, None)


def test_is_single():
    assert is_single(bar(D(2010, 1, 4), 50.0))
    assert not is_single(bar(D(2010, 1, 4), 50.0, 1.0))
    assert is_single(Bar(D(2010, 1, 4), None, None, None, 50.0, None))


def test_stale_dates_uses_previous_trading_day():
    closes = {D(2009, 7, 2): 39.31, D(2009, 7, 3): 39.31, D(2009, 7, 6): 39.31,   # 07-03 为休市日
              D(2009, 7, 7): 40.0, D(2009, 7, 9): 40.0}                          # 07-08 缺数据
    assert stale_dates(closes) == [D(2009, 7, 6)]
    assert stale_dates({}) == []


def test_analyze_single_phase_jumps_holidays_and_stale():
    days = mcal.stock_trading_days(D(2010, 6, 1), D(2010, 12, 31))
    bars = {}
    for i, d in enumerate(days):
        multi = d > D(2010, 8, 31)
        close = 50.0 + (i % 7) * 0.4
        if d == D(2010, 9, 1):
            close = 80.0                      # 分界次日跳变
        if d == D(2010, 10, 5):
            close = bars[days[i - 1]].close   # 阶段后的相同值
        bars[d] = bar(d, close, 1.0 if multi else 0.0)
    bars[D(2010, 7, 5)] = bar(D(2010, 7, 5), 51.0)        # 独立日（休市）有数据，在单值阶段内
    r = analyze("S5FI", bars)
    assert r.single_phase_end == D(2010, 8, 31)
    assert r.single_after_phase == []
    assert r.holiday_dates == [D(2010, 7, 5)] and r.holidays_outside_phase == []
    assert any(j.date == D(2010, 9, 1) for j in r.jumps)
    assert D(2010, 10, 5) in r.stale_after_phase
    assert r.step == 0.2 and r.zero_after_phase >= 1 and r.expected_zero is not None
    text = render([r], "2026-09-27T00:00:00+00:00")
    assert "### S5FI" in text and "单值阶段结束：2010-08-31" in text and "偶然期望" in text


def test_merge_breadth_marks_stale_fields():
    fi = {D(2009, 7, 2): 39.31, D(2009, 7, 6): 39.31, D(2009, 7, 7): 40.0}
    tw = {D(2009, 7, 2): 23.79, D(2009, 7, 6): 28.23, D(2009, 7, 7): 28.23}
    merged, _ = merge_breadth(fi, tw, {})
    assert merged[D(2009, 7, 6)].stale_fields == ("S5FI",)      # 保留原值
    assert merged[D(2009, 7, 6)].s5fi == 39.31
    assert merged[D(2009, 7, 7)].stale_fields == ("S5TW",)
    assert merged[D(2009, 7, 2)].stale_fields == ()


def test_snapshot_notes_stale_breadth():
    """计分用到疑似陈旧值时，在 data_notes 中标注（F、W、F5、W5 分别标注），分数计算不变。"""
    base, t5 = D(2025, 10, 31), D(2025, 10, 24)
    breadth = {
        base: BreadthReading(base, 40.15, 38.56, "tradingview", "", ("S5FI",)),
        t5: BreadthReading(t5, 52.88, 57.65, "tradingview", "", ("S5FI", "S5TW")),
    }
    snap = build_snapshot(load_sample_raw("2025-10-31", breadth))
    notes = [n for n in snap.data_notes if n.startswith("疑似陈旧值")]
    assert [n.split("（")[0] for n in notes] == ["疑似陈旧值：F", "疑似陈旧值：F5", "疑似陈旧值：W5"]
    assert all("保留原值" in n for n in notes)
    clean = build_snapshot(load_sample_raw("2025-10-31", {
        d: dataclasses.replace(r, stale_fields=()) for d, r in breadth.items()}))
    assert snap.breadth.s5fi == clean.breadth.s5fi and not any("陈旧" in n for n in clean.data_notes)
