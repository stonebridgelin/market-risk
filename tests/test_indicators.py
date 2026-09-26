"""指标测试：均线、窗口最高值、三环节（SPEC 9节阶段2验收）。全部使用合成数据。"""

from __future__ import annotations

import datetime as dt

import pytest

from market_risk import calendar as mcal
from market_risk.indicators import (
    InsufficientDataError,
    ratio,
    simple_moving_average,
    three_segment_both,
    three_segment_candidates,
    three_segment_trace,
    window_max,
)

BASE = dt.date(2025, 11, 28)
DAYS = mcal.stock_trading_days(dt.date(2025, 8, 1), BASE)  # 连续的 NYSE 交易日
B = len(DAYS) - 1  # 基准日下标


def make_closes(overrides: dict[int, float], default: float = 100.0) -> dict[dt.date, float]:
    """overrides 的键为"距基准日的交易日数"（0=基准日，5=T−5）。"""
    closes = dict.fromkeys(DAYS, default)
    for k, v in overrides.items():
        closes[DAYS[B - k]] = v
    return closes


def trace_for(result, k):
    return next(t for t in result.traces if t.d1 == DAYS[B - k])


# ---- 均线 ----


def test_simple_moving_average():
    closes = {d: float(i + 1) for i, d in enumerate(DAYS)}
    n = len(DAYS)
    assert simple_moving_average(closes, BASE, 5) == pytest.approx(n - 2)  # (n+…+n−4)/5
    # 截断到基准日：基准日之后的数据不参与
    closes[dt.date(2025, 12, 1)] = 10_000.0
    assert simple_moving_average(closes, BASE, 1) == float(n)


def test_moving_average_insufficient_data_raises():
    closes = {d: 100.0 for d in DAYS}
    with pytest.raises(InsufficientDataError):
        simple_moving_average(closes, BASE, 200)  # 只有约 85 个交易日
    with pytest.raises(InsufficientDataError):
        simple_moving_average(closes, dt.date(2025, 11, 27), 5)  # 没有该日收盘价


# ---- 窗口最高值（SPEC 5.6 第6条）----


def test_window_max_lists_all_ties():
    values = {dt.date(2025, 11, 5): 4.17, dt.date(2025, 11, 20): 4.1700000001,
              dt.date(2025, 11, 28): 4.02}
    h, dates = window_max(values)
    assert h == 4.17
    assert dates == (dt.date(2025, 11, 5), dt.date(2025, 11, 20))


def test_window_max_base_tied_with_earlier_day_is_max():
    values = {dt.date(2025, 11, 5): 4.17, dt.date(2025, 11, 28): 4.17}
    h, dates = window_max(values)
    assert round(values[dt.date(2025, 11, 28)], 2) == h
    assert dates[0] == dt.date(2025, 11, 5)
    with pytest.raises(InsufficientDataError):
        window_max({})


# ---- 三环节 ----


def test_candidates_range():
    with_t20 = three_segment_candidates(DAYS, BASE, True)
    without = three_segment_candidates(DAYS, BASE, False)
    assert with_t20[0] == DAYS[B - 20] and with_t20[-1] == DAYS[B - 2]
    assert len(with_t20) == 19 and len(without) == 18
    assert without[0] == DAYS[B - 19]
    with pytest.raises(InsufficientDataError):
        three_segment_candidates(DAYS[:-1], BASE, True)
    with pytest.raises(InsufficientDataError):
        three_segment_candidates(DAYS[-10:], BASE, True)


def test_three_segment_all_steps():
    closes = make_closes({5: 95.0, 4: 101.0, 3: 98.0, 2: 101.0, 1: 101.0, 0: 90.0})
    res = three_segment_trace("SPY", closes, BASE, True)
    t = trace_for(res, 5)
    assert (t.d1_close, t.lc) == (95.0, 100.0)
    assert t.lc_date == DAYS[B - 25]  # 并列最低时记录最早日期
    assert t.step1 and t.d2_dates == (DAYS[B - 3],) and t.step3 and t.completed
    assert res.completed


def test_three_segment_lists_every_qualifying_d2():
    """d1 与基准日之间所有满足 d1 < d2 ≤ Lc 的日期都列出。"""
    closes = make_closes({5: 95.0, 3: 98.0, 0: 90.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert t.d2_dates == (DAYS[B - 4], DAYS[B - 3], DAYS[B - 2], DAYS[B - 1])


def test_three_segment_only_step1():
    closes = make_closes({5: 95.0, 4: 101.0, 3: 101.0, 2: 101.0, 1: 101.0, 0: 101.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert t.step1 and t.d2_dates == () and not t.step3 and not t.completed


def test_three_segment_step3_fails():
    closes = make_closes({5: 95.0, 4: 101.0, 3: 98.0, 2: 101.0, 1: 101.0, 0: 97.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert t.step1 and t.d2_dates and not t.step3 and not t.completed


def test_three_segment_d2_equal_to_lc_counts():
    """d2 收盘价恰好等于 Lc：满足 d2 ≤ Lc。"""
    closes = make_closes({5: 95.0, 4: 101.0, 3: 100.0, 2: 101.0, 1: 101.0, 0: 90.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert t.lc == 100.0
    assert t.d2_dates == (DAYS[B - 3],)
    assert t.completed


def test_three_segment_d2_equal_to_d1_does_not_count():
    """d2 收盘价等于 d1 收盘价：不满足 d1 < d2。"""
    closes = make_closes({5: 95.0, 4: 101.0, 3: 95.0, 2: 101.0, 1: 101.0, 0: 90.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert t.d2_dates == () and not t.completed


def test_three_segment_d1_one_day_before_base_excluded():
    """d1 距基准日仅1个交易日：不在候选范围内；距2个交易日时可以完成。"""
    closes = make_closes({1: 95.0, 0: 90.0})
    res = three_segment_trace("SPY", closes, BASE, True)
    assert DAYS[B - 1] not in {t.d1 for t in res.traces}
    assert not res.completed

    closes2 = make_closes({2: 95.0, 1: 98.0, 0: 90.0})
    res2 = three_segment_trace("SPY", closes2, BASE, True)
    assert trace_for(res2, 2).completed


def test_three_segment_t_minus_20_dependency():
    """只有 d1=T−20 能完成：含 T−20 口径完成，不含口径不完成。"""
    closes = make_closes({20: 95.0, 10: 98.0, 0: 90.0})
    both = three_segment_both("SPY", closes, BASE)
    assert both[True].completed is True
    assert both[False].completed is False
    assert trace_for(both[True], 20).completed


def test_three_segment_uses_two_decimal_prices():
    """d1 收盘价 99.999 按2位小数为 100.00，不小于 Lc=100。"""
    closes = make_closes({5: 99.999, 0: 90.0})
    t = trace_for(three_segment_trace("SPY", closes, BASE, True), 5)
    assert not t.step1


def test_three_segment_ignores_data_after_base():
    closes = make_closes({5: 95.0, 4: 101.0, 3: 98.0, 2: 101.0, 1: 101.0, 0: 90.0})
    closes[dt.date(2025, 12, 1)] = 50.0
    closes[dt.date(2025, 12, 2)] = 99.0
    res = three_segment_trace("SPY", closes, BASE, True)
    assert all(t.d1 <= BASE and all(d < BASE for d in t.d2_dates) for t in res.traces)
    assert trace_for(res, 5).d2_dates == (DAYS[B - 3],)


def test_three_segment_insufficient_history():
    short = {d: 100.0 for d in DAYS[-30:]}
    with pytest.raises(InsufficientDataError):
        three_segment_trace("SPY", short, BASE, True)
    no_base = {d: 100.0 for d in DAYS[:-1]}
    with pytest.raises(InsufficientDataError):
        three_segment_trace("SPY", no_base, BASE, True)


def test_ratio():
    assert ratio(80.123, 110.5) == round(80.123 / 110.5, 4)
    with pytest.raises(ValueError):
        ratio(1.0, 0.0)
