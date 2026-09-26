"""日期计算测试（SPEC 5.2、9节阶段1验收、5.6第4条）。"""

from __future__ import annotations

import datetime as dt

import pytest

from market_risk import calendar as mcal
from market_risk.config import MarketHolidays, load_holidays

D = dt.date

# SPEC 第9节阶段1表格：基准日, T−5, T−20, 窗口起, 窗口止, O1, O6, T−45, 结果窗口起, 结果窗口止
SAMPLES = [
    (D(2025, 8, 29), D(2025, 8, 22), D(2025, 8, 1), D(2025, 8, 4), D(2025, 8, 29),
     D(2025, 8, 28), D(2025, 8, 21), D(2025, 6, 26), D(2025, 9, 2), D(2025, 9, 29)),
    (D(2025, 9, 26), D(2025, 9, 19), D(2025, 8, 28), D(2025, 8, 29), D(2025, 9, 26),
     D(2025, 9, 25), D(2025, 9, 18), D(2025, 7, 24), D(2025, 9, 29), D(2025, 10, 24)),
    (D(2025, 10, 31), D(2025, 10, 24), D(2025, 10, 3), D(2025, 10, 6), D(2025, 10, 31),
     D(2025, 10, 30), D(2025, 10, 23), D(2025, 8, 28), D(2025, 11, 3), D(2025, 12, 1)),
    (D(2025, 11, 28), D(2025, 11, 20), D(2025, 10, 30), D(2025, 10, 31), D(2025, 11, 28),
     D(2025, 11, 26), D(2025, 11, 19), D(2025, 9, 25), D(2025, 12, 1), D(2025, 12, 29)),
]


@pytest.mark.parametrize(
    ("base", "t5", "t20", "ws", "we", "o1", "o6", "t45", "os_", "oe"), SAMPLES
)
def test_sample_date_references(bond_cal_2025h2, base, t5, t20, ws, we, o1, o6, t45, os_, oe):
    refs = mcal.compute_date_references(base, bond_cal_2025h2)
    assert refs.base_date == base
    assert refs.t_minus_5 == t5
    assert refs.t_minus_20 == t20
    assert (refs.window_start, refs.window_end) == (ws, we)
    assert len(refs.window_days) == 20
    assert refs.window_days[0] == ws and refs.window_days[-1] == we
    assert refs.oas_o1 == o1
    assert refs.oas_o6_v3r1 == o6
    assert refs.oas_o6_v2m is None  # 未提供 FRED 观测列表
    assert refs.three_segment_query_start == t45
    assert (refs.outcome_window_start, refs.outcome_window_end) == (os_, oe)
    assert len(refs.oas_o1_to_o6_sequence) == 6
    assert refs.oas_o1_to_o6_sequence[0] == o1
    assert refs.oas_o1_to_o6_sequence[-1] == o6


def test_sample4_sequence_and_holidays(bond_cal_2025h2):
    refs = mcal.compute_date_references(D(2025, 11, 28), bond_cal_2025h2)
    assert refs.oas_o1_to_o6_sequence == (
        D(2025, 11, 26), D(2025, 11, 25), D(2025, 11, 24),
        D(2025, 11, 21), D(2025, 11, 20), D(2025, 11, 19),
    )
    assert refs.is_early_close is True
    assert refs.stock_holidays_in_window == (D(2025, 11, 27),)
    assert refs.bond_holidays_in_window == (D(2025, 11, 11), D(2025, 11, 27))
    # O1=11-26 之后到基准日只有 11-28 一个交易日（11-27 休市），不滞后
    assert refs.o1_lag_stock_days == 1
    assert refs.is_last_trading_day_of_week is True


def test_sample3_bond_holiday_in_window(bond_cal_2025h2):
    refs = mcal.compute_date_references(D(2025, 10, 31), bond_cal_2025h2)
    assert refs.bond_holidays_in_window == (D(2025, 10, 13),)
    assert refs.stock_holidays_in_window == ()
    assert refs.is_early_close is False


def test_early_close_and_holidays(bond_cal_2025h2):
    assert mcal.is_early_close(D(2025, 11, 28))
    assert not mcal.is_early_close(D(2025, 11, 26))
    # 哥伦布日、退伍军人节：债市休市、股市开市
    for d in (D(2025, 10, 13), D(2025, 11, 11)):
        assert mcal.is_stock_trading_day(d)
        assert not bond_cal_2025h2.is_business_day(d)
    # 感恩节：股债均休市
    assert not mcal.is_stock_trading_day(D(2025, 11, 27))
    assert not bond_cal_2025h2.is_business_day(D(2025, 11, 27))


# ---- SPEC 5.6 第4条：v2-M 的 O1 滞后算法 ----


def test_o1_lag_columbus_day(bond_cal_2025h2):
    """基准日 2025-10-14：O1=10-10（10-13 债市休市），滞后 10-13、10-14 两个交易日。"""
    refs = mcal.compute_date_references(D(2025, 10, 14), bond_cal_2025h2)
    assert refs.oas_o1 == D(2025, 10, 10)
    assert refs.o1_lag_stock_days == 2


def test_o1_lag_normal_and_thanksgiving():
    assert mcal.o1_lag_stock_days(D(2025, 10, 30), D(2025, 10, 31)) == 1
    assert mcal.o1_lag_stock_days(D(2025, 11, 26), D(2025, 11, 28)) == 1
    assert mcal.o1_lag_stock_days(D(2025, 10, 29), D(2025, 10, 31)) == 2
    assert mcal.o1_lag_stock_days(D(2025, 10, 31), D(2025, 10, 31)) == 0


# ---- O6 两种口径 ----


def test_o6_v2m_counts_month_end_weekend_observation(bond_cal_2025h2):
    """2025-08-31 为周日月末观测：v2-M 计入，v3-R1 不计入。"""
    obs = [D(2025, 8, 27), D(2025, 8, 28), D(2025, 8, 29), D(2025, 8, 31),
           D(2025, 9, 2), D(2025, 9, 3), D(2025, 9, 4), D(2025, 9, 5)]
    o1 = D(2025, 9, 5)
    assert mcal.oas_o6_v2m(o1, obs) == D(2025, 8, 29)
    assert mcal.oas_o6_v3r1(o1, bond_cal_2025h2) == D(2025, 8, 28)

    refs = mcal.compute_date_references(D(2025, 9, 8), bond_cal_2025h2, dict.fromkeys(obs, 2.8))
    assert refs.oas_o1 == refs.oas_o1_v2m == o1
    assert refs.oas_o6_v2m == D(2025, 8, 29)
    assert refs.oas_o6_v3r1 == D(2025, 8, 28)
    note = mcal.o6_difference_note(refs)
    assert note is not None and "月末周末观测" in note


def test_o6_v2m_same_as_v3r1_gives_no_note(bond_cal_2025h2):
    obs = dict.fromkeys(bond_cal_2025h2.days, 2.9)
    refs = mcal.compute_date_references(D(2025, 10, 31), bond_cal_2025h2, obs)
    assert refs.oas_o6_v2m == refs.oas_o6_v3r1 == D(2025, 10, 23)
    assert mcal.o6_difference_note(refs) is None


def test_o6_v2m_insufficient_and_missing_o1():
    obs = [D(2025, 9, 2), D(2025, 9, 3), D(2025, 9, 4)]
    assert mcal.oas_o6_v2m(D(2025, 9, 4), obs) is None
    with pytest.raises(mcal.CalendarError):
        mcal.oas_o6_v2m(D(2025, 9, 5), obs)


# ---- SPEC 5.6 第8条：FRED 返回 "." 的行不算观测 ----


def _weekday_obs(start: dt.date, end: dt.date, missing: set[dt.date]) -> dict:
    """构造 FRED 观测：每个工作日一行，missing 中的日期值为 None（即 "."）。"""
    obs: dict[dt.date, float | None] = {}
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            obs[cur] = None if cur in missing else 2.9
        cur += dt.timedelta(days=1)
    return obs


def test_fred_dot_row_is_not_o1(bond_cal_2025h2):
    """基准日 2025-10-14：FRED 在 10-13 返回 "."，v2-M 的 O1 应为 10-10，不得取 10-13。"""
    obs = _weekday_obs(D(2025, 9, 25), D(2025, 10, 14), {D(2025, 10, 13)})
    refs = mcal.compute_date_references(D(2025, 10, 14), bond_cal_2025h2, obs)
    assert refs.oas_o1_v2m == D(2025, 10, 10)
    assert refs.oas_o1 == D(2025, 10, 10)
    assert mcal.o1_lag_stock_days(refs.oas_o1_v2m, refs.base_date) == 2


def test_fred_dot_row_skipped_in_o6_v2m(bond_cal_2025h2):
    """基准日 2025-10-17：O1=10-16，v2-M 的 O6 为 10-08（跳过 10-13），与 v3-R1 相同。"""
    obs = _weekday_obs(D(2025, 9, 25), D(2025, 10, 17), {D(2025, 10, 13)})
    refs = mcal.compute_date_references(D(2025, 10, 17), bond_cal_2025h2, obs)
    assert refs.oas_o1 == refs.oas_o1_v2m == D(2025, 10, 16)
    assert refs.oas_o6_v2m == D(2025, 10, 8)
    assert refs.oas_o6_v3r1 == D(2025, 10, 8)
    assert mcal.o6_difference_note(refs) is None


def test_valued_observation_dates_filters_none_and_nan():
    obs = {D(2025, 10, 10): 2.9, D(2025, 10, 13): None, D(2025, 10, 14): float("nan")}
    assert mcal.valued_observation_dates(obs) == [D(2025, 10, 10)]
    assert mcal.oas_o1_v2m(D(2025, 10, 10), [D(2025, 10, 10)]) is None


def test_o1_difference_is_reported(bond_cal_2025h2):
    """FRED 在 O1 当天缺值（数据滞后）：v2-M 的 O1 回退到更早观测，并说明原因。"""
    obs = _weekday_obs(D(2025, 10, 1), D(2025, 10, 31), {D(2025, 10, 30), D(2025, 10, 31)})
    refs = mcal.compute_date_references(D(2025, 10, 31), bond_cal_2025h2, obs)
    assert refs.oas_o1 == D(2025, 10, 30)
    assert refs.oas_o1_v2m == D(2025, 10, 29)
    assert refs.oas_o6_v2m == D(2025, 10, 22)
    note = mcal.o6_difference_note(refs)
    assert note is not None and "O1 不同" in note


def test_no_valued_observation_before_base(bond_cal_2025h2):
    refs = mcal.compute_date_references(D(2025, 10, 31), bond_cal_2025h2, {})
    assert refs.oas_o1_v2m is None and refs.oas_o6_v2m is None


# ---- SPEC 5.6 第9条：股市休市、债市开市（2026-04-03）----


@pytest.fixture(scope="module")
def bond_cal_2026_spring():
    return mcal.bond_calendar_from_holidays(load_holidays(), D(2026, 2, 1), D(2026, 5, 29))


def test_good_friday_2026_is_stock_holiday_bond_open(bond_cal_2026_spring):
    assert not mcal.is_stock_trading_day(D(2026, 4, 3))
    assert bond_cal_2026_spring.is_business_day(D(2026, 4, 3))
    assert D(2026, 4, 3) in load_holidays().bond_early_closes


def test_window_2026_04_24_excludes_good_friday(bond_cal_2026_spring):
    refs = mcal.compute_date_references(D(2026, 4, 24), bond_cal_2026_spring)
    assert (refs.window_start, refs.window_end) == (D(2026, 3, 27), D(2026, 4, 24))
    assert len(refs.window_days) == 20
    assert D(2026, 4, 3) not in refs.window_days
    assert refs.stock_holidays_in_window == (D(2026, 4, 3),)
    assert refs.bond_holidays_in_window == ()


def test_rate_window_excludes_treasury_value_on_stock_holiday(bond_cal_2026_spring):
    """基准日 2026-04-24：利率窗口不得包含 04-03 的财政部数值。"""
    refs = mcal.compute_date_references(D(2026, 4, 24), bond_cal_2026_spring)
    treasury = dict.fromkeys(sorted(bond_cal_2026_spring.days), 4.2)
    treasury[D(2026, 4, 3)] = 9.99  # 若被计入，会成为窗口最高值
    included, excluded = mcal.rate_window_observations(treasury, refs.window_days)
    assert D(2026, 4, 3) not in included
    assert excluded == [D(2026, 4, 3)]
    assert len(included) == 20
    assert max(included.values()) == 4.2


def test_rate_window_bond_holiday_is_missing_not_filled(bond_cal_2025h2, appendix_a_yields):
    refs = mcal.compute_date_references(D(2025, 10, 31), bond_cal_2025h2)
    included, excluded = mcal.rate_window_observations(appendix_a_yields, refs.window_days)
    assert D(2025, 10, 13) not in included
    assert len(included) == 19
    assert excluded == []
    assert max(included.values()) == 4.18  # H=4.18（2025-10-06）
    assert mcal.rate_window_observations(appendix_a_yields, []) == ({}, [])


def test_v3r1_o6_counts_good_friday_2026(bond_cal_2026_spring):
    """v3-R1 按债市营业日计数，包含 04-03：基准日 2026-04-13 的 O6 正好是 04-03。"""
    refs = mcal.compute_date_references(D(2026, 4, 13), bond_cal_2026_spring)
    assert refs.oas_o1 == D(2026, 4, 10)
    assert refs.oas_o6_v3r1 == D(2026, 4, 3)
    # v2-M：FRED 若在 04-03 无数值，只数有数值的观测，O6 顺延到 04-02
    obs = _weekday_obs(D(2026, 3, 16), D(2026, 4, 13), {D(2026, 4, 3)})
    refs2 = mcal.compute_date_references(D(2026, 4, 13), bond_cal_2026_spring, obs)
    assert refs2.oas_o6_v2m == D(2026, 4, 2)
    assert refs2.oas_o6_v3r1 == D(2026, 4, 3)


def test_o1_on_good_friday_2026(bond_cal_2026_spring):
    """基准日 2026-04-06：v3-R1 的 O1 为 04-03（债市营业日），滞后1个股票交易日。"""
    refs = mcal.compute_date_references(D(2026, 4, 6), bond_cal_2026_spring)
    assert refs.oas_o1 == D(2026, 4, 3)
    assert refs.o1_lag_stock_days == 1


def test_o1_sequence_requires_business_day(bond_cal_2025h2):
    with pytest.raises(mcal.CalendarError):
        mcal.oas_o1_to_o6_sequence(D(2025, 10, 13), bond_cal_2025h2)


# ---- 边界与错误 ----


def test_base_date_must_be_trading_day(bond_cal_2025h2):
    with pytest.raises(mcal.CalendarError):
        mcal.compute_date_references(D(2025, 11, 27), bond_cal_2025h2)
    with pytest.raises(mcal.CalendarError):
        mcal.shift_trading_days(D(2025, 11, 29), -1)


def test_bond_calendar_coverage_is_enforced(bond_cal_2025h2):
    with pytest.raises(mcal.CalendarError):
        bond_cal_2025h2.is_business_day(D(2025, 7, 31))
    with pytest.raises(mcal.CalendarError):
        mcal.bond_calendar_from_dates([], D(2025, 9, 1), D(2025, 8, 1))


def test_bond_calendar_ignores_weekends_and_out_of_range():
    cal = mcal.bond_calendar_from_dates(
        [D(2025, 8, 30), D(2025, 9, 2), D(2025, 12, 1)], D(2025, 8, 1), D(2025, 9, 30)
    )
    assert cal.days == frozenset({D(2025, 9, 2)})


def test_shift_trading_days_across_year():
    assert mcal.shift_trading_days(D(2026, 1, 2), -1) == D(2025, 12, 31)
    assert mcal.shift_trading_days(D(2025, 12, 31), 1) == D(2026, 1, 2)
    assert mcal.shift_trading_days(D(2025, 11, 28), 0) == D(2025, 11, 28)
    assert mcal.count_trading_days_after(D(2025, 11, 28), D(2025, 11, 1)) == 0


def test_last_trading_day_of_week():
    assert mcal.is_last_trading_day_of_week(D(2025, 11, 28))
    assert not mcal.is_last_trading_day_of_week(D(2025, 11, 26))
    assert mcal.is_last_trading_day_of_week(D(2025, 4, 17))  # 次日耶稣受难日休市
    assert not mcal.is_last_trading_day_of_week(D(2025, 11, 29))


def test_monthly_sample_dates():
    s2025 = mcal.monthly_sample_dates(2025)
    assert s2025[7:12] == [D(2025, 8, 29), D(2025, 9, 26), D(2025, 10, 31),
                          D(2025, 11, 28), D(2025, 12, 26)]
    s2026 = mcal.monthly_sample_dates(2026)
    assert s2026[:8] == [D(2026, 1, 30), D(2026, 2, 27), D(2026, 3, 27), D(2026, 4, 24),
                         D(2026, 5, 29), D(2026, 6, 26), D(2026, 7, 31), D(2026, 8, 28)]
    # 2020-12-25（最后一个周五）圣诞休市 → 取当月最后一个交易日
    assert mcal.monthly_sample_dates(2020)[11] == D(2020, 12, 31)


# ---- 与 holidays.yaml 核对 ----


def test_holidays_yaml_matches_nyse_2025_2026():
    notes = mcal.check_stock_calendar(load_holidays(), D(2025, 1, 1), D(2026, 12, 31))
    assert notes == []


def test_holidays_yaml_matches_appendix_a(bond_cal_2025h2):
    holidays = load_holidays()
    assert mcal.check_bond_calendar(
        bond_cal_2025h2, holidays, D(2025, 8, 1), D(2025, 12, 31)
    ) == []
    from_yaml = mcal.bond_calendar_from_holidays(holidays, D(2025, 8, 1), D(2025, 12, 31))
    assert from_yaml.days == bond_cal_2025h2.days


def test_calendar_mismatch_is_reported_not_fixed(bond_cal_2025h2):
    empty = MarketHolidays(frozenset(), frozenset(), frozenset({D(2025, 10, 14)}), frozenset())
    stock_notes = mcal.check_stock_calendar(empty, D(2025, 11, 26), D(2025, 11, 28))
    assert any("2025-11-27" in n for n in stock_notes)
    assert any("提前收盘" in n for n in stock_notes)
    wrong = MarketHolidays(
        frozenset({D(2025, 11, 26)}), frozenset(), frozenset(), frozenset()
    )
    assert any("NYSE 日历开市" in n for n in
               mcal.check_stock_calendar(wrong, D(2025, 11, 26), D(2025, 11, 26)))
    bond_notes = mcal.check_bond_calendar(bond_cal_2025h2, empty, D(2025, 10, 13), D(2025, 10, 14))
    assert len(bond_notes) == 2
    # 核对不改变日历本身
    assert not bond_cal_2025h2.is_business_day(D(2025, 10, 13))
