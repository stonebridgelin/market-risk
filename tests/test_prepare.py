"""评分准备层：ETF 缺失时价格维度记待补、SPY 缺失时 v2-M 广度按两种假设取并集（阶段6，2026-09-27 确认）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import load_sample_raw

from market_risk.data.snapshot import DataIntegrityError, build_snapshot
from market_risk.models import BreadthReading
from market_risk.prepare import missing_etf_message, score_versions
from market_risk.scoring import v2m, v3r1

D = dt.date
BASE = D(2025, 10, 31)
T5 = D(2025, 10, 24)


def raw_with(breadth: tuple[float, float, float, float], drop: dict[str, list[dt.date]] | None = None):
    f, w, f5, w5 = breadth
    raw = load_sample_raw("2025-10-31", {BASE: BreadthReading(BASE, f, w), T5: BreadthReading(T5, f5, w5)})
    closes = {s: {d: v for d, v in c.items() if d not in (drop or {}).get(s, [])} for s, c in raw.closes.items()}
    return dataclasses.replace(raw, closes=closes)


def snap(raw):
    return build_snapshot(raw, allow_missing_etfs=True)


def test_complete_data_is_identical_to_frozen_rules():
    raw = raw_with((40.15, 38.56, 52.88, 57.65))
    s = build_snapshot(raw)
    assert score_versions(s) == (v2m.score(s), v3r1.score(s))
    assert missing_etf_message(s) is None


def test_strict_mode_still_raises():
    with pytest.raises(DataIntegrityError, match="SPY"):
        build_snapshot(raw_with((40.15, 38.56, 52.88, 57.65), {"SPY": [BASE]}))


@pytest.mark.parametrize("missing_day", [BASE, D(2025, 10, 20)])    # SPY 当日缺失 / 20日窗口内缺失
def test_spy_missing_same_under_both_hypotheses(missing_day):
    """L≥50%：条件 (b) 成立与否都不改变结果 → v2-M 广度给确定分数 0。"""
    s = snap(raw_with((55.0, 60.0, 52.0, 57.0), {"SPY": [missing_day]}))
    assert "SPY" in dict(s.missing_etfs) and "SPY" not in s.etfs and s.spy_window_max_close is None
    a, b = score_versions(s)
    for r in (a, b):
        assert r.price.score is None and r.price.possible_scores == (0, 1, 2)
    assert a.breadth.score == 0 and "两种假设结果相同" in a.breadth.calculation
    assert b.breadth == v3r1.score_breadth(s)                     # v3-R1 广度不受影响
    assert any("【数据源可能有问题】" in n for n in a.notes)


@pytest.mark.parametrize("missing_day", [BASE, D(2025, 10, 20)])
def test_spy_missing_differs_under_hypotheses_is_pending(missing_day):
    """F<40%、条件 (a) 不成立：条件 (b) 成立为2分、不成立为1分 → v2-M 广度待补 {1,2}。"""
    s = snap(raw_with((39.0, 45.0, 30.0, 50.0), {"SPY": [missing_day]}))
    a, _ = score_versions(s)
    assert a.breadth.score is None and a.breadth.possible_scores == (1, 2)
    assert "成立时 2" in a.breadth.calculation and "不成立时 1" in a.breadth.calculation
    assert a.total is None and a.total_range[1] - a.total_range[0] >= 3


def test_qqq_missing_does_not_touch_breadth():
    raw = raw_with((39.0, 45.0, 30.0, 50.0), {"QQQ": [D(2025, 10, 20)]})
    s = snap(raw)
    full = build_snapshot(raw_with((39.0, 45.0, 30.0, 50.0)))
    a, b = score_versions(s)
    assert dict(s.missing_etfs).keys() == {"QQQ"}
    assert a.breadth == v2m.score_breadth(full) and b.breadth == v3r1.score_breadth(full)
    assert (a.vix, a.rates, a.credit) == (v2m.score(full).vix, v2m.score(full).rates, v2m.score(full).credit)
    assert a.price.score is None


def test_single_day_path_distinguishes_not_covered_from_missing(tmp_path):
    """数据集未更新到基准日 → 报错提示 fetch；已覆盖但 SPY 缺当天 → 组装输入成功（价格维度待补）。"""
    from test_market import SETTINGS, _sample, dataset_from_raw

    from market_risk.data import market
    from market_risk.storage.paths import StoragePaths

    raw, breadth = _sample("2025-11-28")
    paths = StoragePaths(tmp_path)
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    with pytest.raises(market.MarketDataError, match="未覆盖基准日"):
        market.load_raw_inputs(paths, SETTINGS, D(2025, 12, 1), revision_check=False)
    spy = market.read_series_file(paths.market_daily_file("SPY"))[1]
    del spy[D(2025, 11, 28)]
    paths.market_daily_file("SPY").write_text(market.series_text(spy, market.ETF_COLUMNS), encoding="utf-8")
    loaded = market.load_raw_inputs(paths, SETTINGS, D(2025, 11, 28), revision_check=False)
    s = build_snapshot(loaded, allow_missing_etfs=True)
    assert dict(s.missing_etfs).keys() == {"SPY"}
    assert all(r.price.score is None for r in score_versions(s))
