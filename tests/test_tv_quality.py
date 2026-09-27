"""广度指标数据质量检查（只报告，不改变评分逻辑；S5FI、S5TW 不标注疑似陈旧值）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

from conftest import load_sample_raw

from market_risk import calendar as mcal
from market_risk.data.breadth import merge_breadth
from market_risk.data.snapshot import build_snapshot
from market_risk.data.tradingview import Bar
from market_risk.data.tv_quality import (
    analyze,
    compare_versions,
    is_single,
    missing_trading_days,
    render,
    stale_dates,
)
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


def test_merge_breadth_does_not_mark_stale():
    """S5FI、S5TW 与前一交易日相同属偶然（2026-09-27 撤回疑似陈旧值标注）：不触发 stale_fields，保留原值。"""
    fi = {D(2009, 7, 2): 39.31, D(2009, 7, 6): 39.31, D(2009, 7, 7): 40.0}
    tw = {D(2009, 7, 2): 23.79, D(2009, 7, 6): 28.23, D(2009, 7, 7): 28.23}
    merged, _ = merge_breadth(fi, tw, {})
    assert all(r.stale_fields == () for r in merged.values())
    assert merged[D(2009, 7, 6)].s5fi == 39.31


def test_snapshot_has_no_stale_breadth_notes():
    """F、W、F5、W5 用到与前一交易日相同的读数时，data_notes 不标注疑似陈旧值。"""
    base, t5 = D(2025, 10, 31), D(2025, 10, 24)
    breadth = {
        base: BreadthReading(base, 40.15, 38.56, "tradingview", "", ("S5FI",)),
        t5: BreadthReading(t5, 52.88, 57.65, "tradingview", "", ("S5FI", "S5TW")),
    }
    snap = build_snapshot(load_sample_raw("2025-10-31", breadth))
    assert not any("陈旧" in n for n in snap.data_notes)
    clean = build_snapshot(load_sample_raw("2025-10-31", {
        d: dataclasses.replace(r, stale_fields=()) for d, r in breadth.items()}))
    assert snap.data_notes == clean.data_notes


def test_repeat_verdict_and_conclusion():
    days = mcal.stock_trading_days(D(2020, 1, 2), D(2020, 3, 31))
    # NC 系列（步长 0.03）：每两天才变化一次（变化 0.5），重复值远多于偶然期望 → 明显偏多
    closes = [50.0 + (i // 2) * 0.5 for i in range(len(days))]
    bars = {d: bar(d, c, 1.0) for d, c in zip(days, closes, strict=True)}
    r = analyze("NCFI", bars)
    assert r.repeats_excessive and r.repeat_verdict.startswith("明显偏多")
    text = render([r], "t")
    assert "S5FI、S5TW 的重复值属偶然" in text and "NC 系列重复值明显偏多" in text and "重复值判断" in text
    assert analyze("XXXX", bars).repeat_verdict == "无法判断"


def test_move_versions_and_bond_holidays():
    days = mcal.stock_trading_days(D(2025, 10, 1), D(2025, 11, 28))
    ice = {d: 80.0 for d in days if d not in (D(2025, 10, 13), D(2025, 11, 11), D(2025, 11, 5))}
    tvc = {d: 80.0 for d in days if d != D(2025, 10, 20)}
    ice[D(2025, 10, 22)] = 98.3
    c = compare_versions("ICE_DLY:MOVE", "TVC:MOVE", ice, tvc, {D(2025, 10, 13), D(2025, 11, 11)})
    assert [m[0] for m in c.mismatches] == [D(2025, 10, 22)] and c.max_abs_diff == 18.3
    assert c.primary_missing_bond_holiday == [D(2025, 10, 13), D(2025, 11, 11)]
    assert c.primary_missing_other == [D(2025, 11, 5)] and c.alternative_missing == [D(2025, 10, 20)]
    assert D(2025, 10, 20) in c.only_primary
    gaps = {"HIGN": [D(2011, 8, 8)], "LOWN": [D(2010, 3, d) for d in (2, 3, 8)]}
    text = render([], "t", c, gaps)
    assert "## MOVE 两个版本比对" in text and "其他 1 个" in text
    assert "2011-08-08  **剧烈波动日" in text and "**集中缺口**（同月 ≥3 个）：2010-03（3 个）" in text
    assert "## 缺口清单（HIGN、LOWN）" in text
    assert missing_trading_days({}) == []
