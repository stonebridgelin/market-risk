"""争议收盘价的实质影响检验：两种价格分别计分并比较。只写入临时目录。"""

from __future__ import annotations

import datetime as dt

from test_market import SETTINGS, _sample, dataset_from_raw

from market_risk import calendar as mcal
from market_risk.config import DataDecision
from market_risk.data import market
from market_risk.price_impact import compare_scores, dispute_impact, render_impact, score_versions
from market_risk.storage.paths import StoragePaths

D = dt.date


def _series(tmp_path):
    raw, breadth = _sample("2025-11-28")
    paths = StoragePaths(tmp_path)
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    return paths, market.load_market_series(paths, SETTINGS)


def test_identical_prices_have_no_impact(tmp_path):
    paths, series = _series(tmp_path)
    day = D(2025, 11, 20)
    close = series.rows["SPY"][day]["value"]
    r = dispute_impact(series, paths, SETTINGS, "SPY", day, close, close)
    assert r.verdict == "无实质影响" and not r.errors
    assert r.first_base == day and r.last_base == max(series.dates["SPY"])   # 数据末尾截止
    assert r.checked == len(mcal.stock_trading_days(day, r.last_base))


def test_extreme_price_is_detected_and_rendered(tmp_path):
    paths, series = _series(tmp_path)
    day = D(2025, 11, 28)
    r = dispute_impact(series, paths, SETTINGS, "SPY", day, series.rows["SPY"][day]["value"], 1.0)
    assert r.verdict == "有实质影响" and r.affected_dates == [day]
    assert any(d.dimension == "价格" for d in r.diffs)
    text = "\n".join(render_impact([r]))
    assert "有实质影响" in text and "| 2025-11-28 |" in text


def test_replacement_does_not_touch_other_dates_or_files(tmp_path):
    paths, series = _series(tmp_path)
    day = D(2025, 11, 26)
    before = paths.market_daily_file("SPY").read_bytes()
    alt = series.replace_values("SPY", {day: 1.0})
    assert alt.rows["SPY"][day]["close"] == 1.0 and series.rows["SPY"][day]["close"] != 1.0
    assert paths.market_daily_file("SPY").read_bytes() == before
    base = D(2025, 11, 25)      # 争议日之前的基准日不受影响（截断到基准日）
    a = score_versions(market.raw_inputs_from_series(series, paths, SETTINGS, base, revision_check=False))
    b = score_versions(market.raw_inputs_from_series(alt, paths, SETTINGS, base, revision_check=False))
    assert compare_scores(base, a, b) == []


def test_impact_passes_adjudicated_dates_to_each_single_day_score(tmp_path):
    """H-02：争议价格影响检验两种来源都继承单日评分的裁定日期表。"""
    paths, series = _series(tmp_path)
    day = D(2025, 11, 28)
    decision = DataDecision(D(2025, 11, 26), SETTINGS.oas_series, "exclude", "测试排除", day)
    seen = []

    def scorer(raw):
        seen.append(raw.decisions)
        return score_versions(raw)

    close = series.rows["SPY"][day]["value"]
    result = dispute_impact(series, paths, SETTINGS, "SPY", day, close, close,
                            scorer=scorer, decisions=(decision,))
    assert result.checked == 1 and seen == [(decision,), (decision,)]
