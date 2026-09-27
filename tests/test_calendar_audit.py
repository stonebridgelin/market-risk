"""休市日历审计与债市休市日 OAS 清单（纯函数，离线）。"""

from __future__ import annotations

import datetime as dt

import yaml
from conftest import parse_sop_appendix_a

from market_risk.config import DataDecision
from market_risk.data import calendar_audit as ca

D = dt.date


def test_easter_and_rule_holidays():
    assert ca.easter(2025) == D(2025, 4, 20) and ca.easter(2026) == D(2026, 4, 5)
    rules = ca.sifma_rule_holidays(2025)
    assert rules[D(2025, 10, 13)] == "哥伦布日" and rules[D(2025, 11, 11)] == "退伍军人节"
    assert rules[D(2025, 4, 18)] == "耶稣受难日" and rules[D(2025, 6, 19)] == "六月节"
    assert D(2021, 6, 18) not in ca.sifma_rule_holidays(2021)              # 六月节 2022 年起
    assert D(2021, 12, 24) in ca.sifma_rule_holidays(2021)                 # 圣诞节周六 → 周五
    assert D(2022, 1, 1) not in ca.sifma_rule_holidays(2022)               # 元旦周六不补
    assert D(2023, 11, 10) not in ca.sifma_rule_holidays(2023)             # 退伍军人节周六不补


def test_early_closes_only_before_actual_holidays():
    # 2026：耶稣受难日（04-03）债市实际开市（提前收盘），不推断 04-02
    holidays = {D(2026, 1, 19), D(2026, 9, 7)}
    early = ca.sifma_rule_early_closes([2026], holidays)
    assert early[D(2026, 1, 16)].startswith("马丁") and early[D(2026, 9, 4)].startswith("劳动节")
    assert D(2026, 4, 2) not in early
    assert early[D(2026, 11, 27)] == "感恩节次日" and early[D(2026, 12, 24)] == "平安夜"


def _treasury_2025h2() -> set[dt.date]:
    return set(parse_sop_appendix_a())


def test_audit_calendar_2025h2_matches_appendix():
    a = ca.audit_calendar(_treasury_2025h2(), D(2025, 8, 1), D(2025, 12, 31))
    assert a.bond_holidays == [D(2025, 9, 1), D(2025, 10, 13), D(2025, 11, 11), D(2025, 11, 27), D(2025, 12, 25)]
    assert a.only_treasury == [] and a.only_rule == []
    assert a.stock_holidays == [D(2025, 9, 1), D(2025, 11, 27), D(2025, 12, 25)]
    assert a.stock_early_closes == [D(2025, 11, 28), D(2025, 12, 24)]
    assert D(2025, 10, 10) in a.bond_early_closes and D(2025, 8, 29) in a.bond_early_closes


def test_audit_reports_mismatches_without_fixing():
    days = _treasury_2025h2() - {D(2025, 10, 1)}      # 财政部多缺一天
    days |= {D(2025, 10, 13)}                         # 哥伦布日反而有数据
    a = ca.audit_calendar(days, D(2025, 8, 1), D(2025, 12, 31))
    assert a.only_treasury == [D(2025, 10, 1)] and a.only_rule == [D(2025, 10, 13)]
    assert D(2025, 10, 1) in a.bond_holidays and D(2025, 10, 13) not in a.bond_holidays   # 以财政部为准
    text = ca.render_audit(a, [], "t", D(1997, 1, 1))
    assert "| 2025-10-01 | 周三 | - |" in text and "| 2025-10-13 | 周一 | 哥伦布日 |" in text


def test_render_holidays_yaml_keeps_manual_after_end_and_verified_early_closes():
    a = ca.audit_calendar(_treasury_2025h2(), D(2025, 8, 1), D(2025, 12, 31))
    manual = {"bond_holidays": [D(2026, 10, 12), D(2025, 3, 1)], "bond_early_closes": [D(2026, 4, 3)],
              "stock_holidays": [D(2026, 12, 25)], "stock_early_closes": []}
    data = yaml.safe_load(ca.render_holidays_yaml(a, manual))
    assert D(2026, 10, 12) in data["bond"]["holidays"] and D(2025, 3, 1) not in data["bond"]["holidays"]
    assert D(2026, 4, 3) in data["bond"]["early_closes"]
    assert D(2026, 12, 25) in data["stock"]["holidays"] and D(2025, 11, 27) in data["stock"]["holidays"]


def test_oas_holiday_differences_and_decisions():
    treasury = _treasury_2025h2()
    series = {d: 3.0 for d in treasury}
    series[D(2025, 10, 13)] = 3.0          # 沿用值：不列出
    series[D(2025, 11, 11)] = 3.5          # 与前一观测不同：列出
    decision = DataDecision(D(2025, 11, 11), "BAMLH0A0HYM2", "keep", "测试", D(2026, 9, 27))
    diffs = ca.oas_holiday_differences("BAMLH0A0HYM2", series, treasury, D(2025, 8, 1), D(2025, 12, 31),
                                       [decision])
    assert [(x.date, x.value, x.previous_value) for x in diffs] == [(D(2025, 11, 11), 3.5, 3.0)]
    assert diffs[0].decision == decision
    text = ca.render_audit(ca.audit_calendar(treasury, D(2025, 8, 1), D(2025, 12, 31)), diffs, "t", D(1997, 1, 1))
    assert "keep（2026-09-27；与规则不同，按裁定）" in text
    assert "排除：两个版本都不计入" in text and "月末例外 0 处，排除 1 处" in text


def test_oas_holiday_month_end_classification():
    """债市休市日恰为自然月末（2021-05-31）：月末例外；一般休市日（2021-07-05）：排除。"""
    start, end = D(2021, 5, 1), D(2021, 7, 31)
    treasury = {start + dt.timedelta(days=i) for i in range((end - start).days + 1)}
    treasury = {d for d in treasury if d.weekday() < 5} - {D(2021, 5, 31), D(2021, 7, 5)}
    series = dict.fromkeys(treasury, 3.0) | {D(2021, 5, 31): 3.34, D(2021, 7, 5): 3.1}
    exclude = DataDecision(D(2021, 7, 5), "BAMLH0A0HYM2", "exclude", "测试", D(2026, 9, 27))
    diffs = ca.oas_holiday_differences("BAMLH0A0HYM2", series, treasury, start, end, [exclude])
    assert [(x.date, x.month_end) for x in diffs] == [(D(2021, 5, 31), True), (D(2021, 7, 5), False)]
    assert diffs[0].treatment.startswith("月末例外") and diffs[1].treatment.startswith("排除")
    text = ca.render_audit(ca.audit_calendar(treasury, start, end), diffs, "t", D(1997, 1, 1))
    assert "月末例外 1 处，排除 1 处" in text and "exclude（2026-09-27；与规则一致）" in text


def test_calendar_resolutions_follow_treasury():
    """8 个不一致日期已裁定以财政部数据为准：桑迪、老布什国丧日按债市休市；6 个耶稣受难日按开市（提前收盘）。"""
    assert len(ca.CALENDAR_RESOLUTIONS) == 8
    start, end = D(2012, 1, 1), D(2012, 12, 31)
    days = {start + dt.timedelta(days=i) for i in range((end - start).days + 1)}
    rule = set(ca.sifma_rule_holidays(2012))
    # 10-29 债市提前收盘，财政部有数据；10-30 无数据
    treasury = {d for d in days if d.weekday() < 5 and d not in rule} - {D(2012, 10, 30)}
    treasury |= {D(2012, 4, 6)}                                   # 耶稣受难日财政部有数据
    a = ca.audit_calendar(treasury, start, end)
    assert D(2012, 10, 30) in a.only_treasury and D(2012, 4, 6) in a.only_rule
    assert a.unresolved == []
    assert D(2012, 10, 30) in a.bond_holidays and D(2012, 4, 6) not in a.bond_holidays
    assert "提前收盘" in a.bond_early_closes[D(2012, 4, 6)]
    text = ca.render_audit(a, [], "t", D(1997, 1, 1))
    assert "待判断：0 个" in text and "飓风桑迪：财政部无数据，按债市休市" in text
    yml = ca.render_holidays_yaml(a, {})
    assert "2012-10-30   # 飓风桑迪：财政部无数据，按债市休市（2026-09-27 裁定以财政部数据为准）" in yml
    data = yaml.safe_load(yml)
    assert D(2012, 4, 6) in data["bond"]["early_closes"] and D(2012, 4, 6) not in data["bond"]["holidays"]
