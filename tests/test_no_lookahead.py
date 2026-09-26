"""防未来信息测试（SPEC 0.6、9节阶段2验收）。

构造包含基准日之后数据的输入，确认截断后不参与任何计算；并确认断言能捕获越界。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import SAMPLE_DATES, load_sample_raw

from market_risk import calendar as mcal
from market_risk.data.snapshot import (
    LookaheadError,
    assert_no_lookahead,
    build_snapshot,
    truncate,
)
from market_risk.models import BreadthReading

FUTURE_DAYS = 25


def _with_future(raw):
    """在每个序列后追加基准日之后的极端数值；并把基准日当天的 OAS 改成极端值。"""
    base = raw.base_date
    future = mcal.stock_trading_days(base + dt.timedelta(days=1), base + dt.timedelta(days=45))
    future = future[:FUTURE_DAYS]
    closes = {s: {**c, **dict.fromkeys(future, 1.0)} for s, c in raw.closes.items()}
    oas = {**raw.oas, **dict.fromkeys(future, 9.99)}
    oas[base] = 9.99  # 基准日当天的 OAS 观测不得使用（O1 在基准日之前）
    breadth = {
        **raw.breadth,
        future[0]: BreadthReading(future[0], 1.0, 1.0),
    }
    return dataclasses.replace(
        raw,
        closes=closes,
        vix_fred={**raw.vix_fred, **dict.fromkeys(future, 99.0)},
        vix_cboe={**(raw.vix_cboe or {}), **dict.fromkeys(future, 99.0)},
        treasury={**raw.treasury, **dict.fromkeys(future, 9.99)},
        oas=oas,
        oas_vintage={**(raw.oas_vintage or {}), **dict.fromkeys(future, 9.99)},
        breadth=breadth,
    )


@pytest.mark.parametrize("sample", SAMPLE_DATES)
def test_future_data_does_not_change_snapshot(sample):
    base = dt.date.fromisoformat(sample)
    breadth = {base: BreadthReading(base, 55.0, 60.0)}
    raw = load_sample_raw(sample, breadth)
    clean = build_snapshot(raw)
    polluted = build_snapshot(_with_future(raw))
    assert polluted.etfs == clean.etfs
    assert polluted.three_segment == clean.three_segment
    assert (polluted.vix, polluted.vix_t5) == (clean.vix, clean.vix_t5)
    assert (polluted.y, polluted.h, polluted.h_dates, polluted.y_t20) == (
        clean.y, clean.h, clean.h_dates, clean.y_t20
    )
    assert polluted.yields == clean.yields
    assert polluted.breadth == clean.breadth
    assert polluted.spy_window_max_close == clean.spy_window_max_close
    # OAS：基准日当天被改成 9.99，O1/O6 不受影响
    assert polluted.oas_o1 == clean.oas_o1 != 9.99
    assert polluted.oas_o1_v2m != 9.99
    assert polluted.refs.oas_o1 < base and polluted.refs.oas_o1_v2m < base
    assert polluted.refs == clean.refs


def test_truncate_keeps_base_date_inclusive():
    base = dt.date(2025, 11, 28)
    s = {base - dt.timedelta(days=1): 1.0, base: 2.0, base + dt.timedelta(days=1): 3.0}
    assert truncate(s, base) == {base - dt.timedelta(days=1): 1.0, base: 2.0}


def test_assert_catches_future_yield():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    bad = dataclasses.replace(snap, yields={**snap.yields, dt.date(2025, 12, 1): 4.09})
    with pytest.raises(LookaheadError, match="yields=2025-12-01"):
        assert_no_lookahead(bad)


def test_assert_catches_future_close_and_breadth_and_refs():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    spy = snap.etfs["SPY"]
    bad_spy = dataclasses.replace(spy, closes=(*spy.closes, (dt.date(2025, 12, 1), 680.0)))
    with pytest.raises(LookaheadError, match=r"SPY\.closes"):
        assert_no_lookahead(dataclasses.replace(snap, etfs={**snap.etfs, "SPY": bad_spy}))
    future_reading = BreadthReading(dt.date(2025, 12, 1), 50.0, 50.0)
    with pytest.raises(LookaheadError, match="breadth"):
        assert_no_lookahead(dataclasses.replace(snap, breadth_t5=future_reading))
    bad_refs = dataclasses.replace(snap.refs, oas_o1=dt.date(2025, 11, 28) + dt.timedelta(days=3))
    with pytest.raises(LookaheadError, match=r"refs\.oas_o1"):
        assert_no_lookahead(dataclasses.replace(snap, refs=bad_refs))


def test_assert_catches_future_three_segment_dates():
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    res = snap.three_segment[True][0]
    bad_trace = dataclasses.replace(res.traces[0], d2_dates=(dt.date(2025, 12, 2),))
    bad_res = dataclasses.replace(res, traces=(bad_trace, *res.traces[1:]))
    bad = dataclasses.replace(
        snap, three_segment={True: (bad_res, *snap.three_segment[True][1:]), False: ()}
    )
    with pytest.raises(LookaheadError, match=r"three_segment\.d2"):
        assert_no_lookahead(bad)


def test_outcome_window_dates_are_allowed():
    """结果窗口只计算日期、不含行情，不视为越界。"""
    snap = build_snapshot(load_sample_raw("2025-11-28"))
    assert snap.refs.outcome_window_end > snap.refs.base_date
    assert_no_lookahead(snap)


def test_snapshot_module_does_not_import_network_or_outcomes():
    """snapshot.py 是纯计算：不导入网络模块，不引用结果标签。"""
    import inspect

    from market_risk.data import snapshot

    source = inspect.getsource(snapshot)
    for forbidden in ("requests", "yfinance", "outcomes", "open(", "read_text"):
        assert forbidden not in source, forbidden
