"""多层级回调识别（ZigZag）：构造价格序列验证高低点、门槛边界、序列末尾与并列（2026-09-27 确认的口径）。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from market_risk.backtest.settings import Grades, load_backtest_config
from market_risk.backtest.zigzag import CONFIRMED, UNCONFIRMED, zigzag

START = dt.date(2020, 1, 1)


def series(values: list[str]) -> list[tuple[dt.date, D]]:
    return [(START + dt.timedelta(days=i), D(v)) for i, v in enumerate(values)]


def day(i: int) -> dt.date:
    return START + dt.timedelta(days=i)


def test_multilevel_rebound_between_5_and_10_percent():
    """下跌中途反弹超过5%但小于10%：5%层级识别为两段，10%与20%层级识别为一段。"""
    s = series(["100", "85", "91", "70", "90"])        # 85→91 反弹 7.06%
    five = zigzag(s, D("0.05"))
    assert [(x.high_date, x.low_date, x.status) for x in five] == [
        (day(0), day(1), CONFIRMED), (day(2), day(3), CONFIRMED)]
    for level in ("0.10", "0.20"):
        (one,) = zigzag(s, D(level))
        assert (one.high_date, one.low_date, one.low_close, one.confirm_date) == (day(0), day(3), D("70"), day(4))
        assert one.drawdown == D("-0.3") and one.trading_days == 3


@pytest.mark.parametrize(("low", "found"), [("95", True), ("95.01", False)])
def test_drop_threshold_is_inclusive(low, found):
    s = series(["100", low, "200"])
    swings = zigzag(s, D("0.05"))
    assert bool(swings) is found


@pytest.mark.parametrize(("rebound", "status"), [("105", CONFIRMED), ("104.99", UNCONFIRMED)])
def test_rebound_threshold_is_inclusive(rebound, status):
    """从低点 100 反弹 ≥5%（含等号）才确认低点；不足时到序列末尾为未确认。"""
    s = series(["120", "100", rebound])
    (x,) = zigzag(s, D("0.05"))
    assert x.status == status and x.low_close == D("100")
    assert (x.confirm_date == day(2)) is (status == CONFIRMED)


@pytest.mark.parametrize("level", ["0.05", "0.10", "0.20"])
def test_level_boundaries_each_level(level):
    lv = D(level)
    hit = series(["100", str(100 * (1 - lv)), str(100 * (1 - lv) * (1 + lv))])
    miss = series(["100", str(100 * (1 - lv) + D("0.01")), "100"])
    (x,) = zigzag(hit, lv)
    assert x.status == CONFIRMED and x.drawdown == -lv
    assert zigzag(miss, lv) == []


def test_series_end_unconfirmed_uses_lowest_so_far():
    s = series(["100", "110", "95", "90", "93", "92"])
    (x,) = zigzag(s, D("0.10"))
    assert (x.high_date, x.low_date, x.low_close, x.status) == (day(1), day(3), D("90"), UNCONFIRMED)
    assert x.recovery_date is None


def test_ties_take_earliest_high_and_low():
    s = series(["100", "100", "90", "90", "100", "100"])
    (x,) = zigzag(s, D("0.10"))
    assert (x.high_date, x.low_date) == (day(0), day(2))
    assert x.recovery_date == day(4)          # 首个收盘价 ≥ 高点的日期


def test_recovery_date_after_confirmation():
    s = series(["100", "80", "90", "95", "101"])
    (x,) = zigzag(s, D("0.10"))
    assert x.confirm_date == day(2) and x.recovery_date == day(4)


def test_grades_left_closed_right_open():
    spx, qqq = Grades(D("0.05"), D("0.10"), D("0.20")), Grades(D("0.07"), D("0.10"), D("0.20"))
    assert [spx.grade(D(x)) for x in ("0.05", "0.0999", "0.10", "0.1999", "0.20")] == [
        "小回调", "小回调", "修正", "修正", "熊市"]
    assert [qqq.grade(D(x)) for x in ("0.07", "0.0999", "0.10", "0.20")] == ["小回调", "小回调", "修正", "熊市"]


def test_config_levels_and_periods():
    cfg = load_backtest_config()
    assert cfg.levels["SPX"] == (D("0.05"), D("0.10"), D("0.20"))
    assert cfg.levels["QQQ"] == (D("0.07"), D("0.10"), D("0.20"))
    assert cfg.start == dt.date(2008, 8, 11) and cfg.holdout_start == dt.date(2023, 1, 1)
    assert cfg.period_of(dt.date(2016, 12, 30)) == "开发期" and cfg.period_of(dt.date(2017, 1, 3)) == "验证期"
    assert cfg.period_of(dt.date(2023, 1, 3)) == "保留期" and cfg.period_of(dt.date(2008, 8, 8)) == "起点之前"
