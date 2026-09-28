"""评分准备层：ETF 缺失时价格维度记待补、SPY 缺失时 v2-M 广度按两种假设取并集（阶段6，2026-09-27 确认）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import load_sample_raw

from market_risk import calendar as mcal
from market_risk.backtest.engine import day_flags
from market_risk.backtest.settings import load_backtest_config
from market_risk.config import load_settings
from market_risk.data.market import MarketSeries
from market_risk.data.snapshot import DataIntegrityError, build_snapshot
from market_risk.metrics import format_metric, metric_values
from market_risk.models import BreadthReading
from market_risk.prepare import missing_etf_message, score_versions
from market_risk.report import snapshot_metrics_input
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
        assert r.price.score is None and r.price.possible_scores == (1, 2)
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
    assert a.total is None and a.total_range[1] - a.total_range[0] >= 2


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


def test_spy_missing_two_other_etfs_confirm_v2_and_v3_price() -> None:
    """H-03 场景1：QQQ、RSP 的收盘与 MA5 均低于各自 MA50，两个版本确定为2。"""
    raw = raw_with((55.0, 60.0, 52.0, 57.0), {"SPY": [BASE]})
    closes = {sym: dict(values) for sym, values in raw.closes.items()}
    for sym in ("QQQ", "RSP"):
        for day in sorted(closes[sym])[-5:]:
            closes[sym][day] = 100.0
    snapshot = snap(dataclasses.replace(raw, closes=closes))
    assert all(snapshot.etfs[sym].close < snapshot.etfs[sym].ma50
               and snapshot.etfs[sym].ma5 < snapshot.etfs[sym].ma50 for sym in ("QQQ", "RSP"))
    a, b = score_versions(snapshot)
    assert (a.price.score, a.price.possible_scores) == (2, (2,))
    assert (b.price.score, b.price.possible_scores) == (2, (2,))


def test_spy_missing_two_close_below_ma50_without_ma5_confirmation() -> None:
    """H-03 场景2：v2-M 确定2；v3-R1 的 SPY<MA200 未知，价格待补 {1,2}。"""
    raw = raw_with((55.0, 60.0, 52.0, 57.0), {"SPY": [BASE]})
    closes = {sym: dict(values) for sym, values in raw.closes.items()}
    closes["QQQ"][BASE] = 590.0
    closes["RSP"][BASE] = 188.80
    snapshot = snap(dataclasses.replace(raw, closes=closes))
    assert all(snapshot.etfs[sym].close < snapshot.etfs[sym].ma50
               and snapshot.etfs[sym].ma5 >= snapshot.etfs[sym].ma50 for sym in ("QQQ", "RSP"))
    a, b = score_versions(snapshot)
    assert (a.price.score, a.price.possible_scores) == (2, (2,))
    assert (b.price.score, b.price.possible_scores) == (None, (1, 2))


def test_qqq_missing_t_minus_100_only_blanks_reference_ma200() -> None:
    """H-04：QQQ 必需窗口仅 T−49 至 T；较早缺价只使展示 MA200 留空。"""
    missing = mcal.shift_trading_days(BASE, -100)
    snapshot = snap(raw_with((55.0, 60.0, 52.0, 57.0), {"QQQ": [missing]}))
    assert "QQQ" not in dict(snapshot.missing_etfs)
    assert snapshot.etfs["QQQ"].ma200 is None
    assert any("QQQ MA200 仅作展示：回看窗口内缺价" in note for note in snapshot.data_notes)
    assert format_metric(metric_values(snapshot_metrics_input(snapshot))["QQQ.ma200"]) == ""
    assert all(result.price.score is not None for result in score_versions(snapshot))


def test_spy_missing_t_minus_100_is_enumerated() -> None:
    """H-04：SPY 的必需窗口为 T−199 至 T，缺 T−100 应进入 H-03 枚举。"""
    missing = mcal.shift_trading_days(BASE, -100)
    snapshot = snap(raw_with((55.0, 60.0, 52.0, 57.0), {"SPY": [missing]}))
    assert "SPY" in dict(snapshot.missing_etfs)
    assert all(result.price.possible_scores for result in score_versions(snapshot))


def test_extra_non_trading_price_never_fills_missing_stock_day() -> None:
    """相邻周日多余行只做提示和 flag，不能替代缺失的周一收盘。"""
    missing, extra = D(2025, 10, 20), D(2025, 10, 19)
    raw = raw_with((55.0, 60.0, 52.0, 57.0), {"SPY": [missing]})
    closes = {sym: dict(values) for sym, values in raw.closes.items()}
    closes["SPY"][extra] = closes["SPY"][D(2025, 10, 17)]
    snapshot = snap(dataclasses.replace(raw, closes=closes))
    assert "SPY" in dict(snapshot.missing_etfs)
    assert snapshot.non_trading_prices == (("SPY", extra),)
    assert any("不得代替缺失交易日" in note for note in snapshot.data_notes)
    market = MarketSeries(rows={"BAMLH0A0HYM2": {}}, dates={}, manifest={}, breadth={})
    result = score_versions(snapshot)[0]
    flags = day_flags(result.version, snapshot, result, market, load_settings(), load_backtest_config(),
                      {BASE: 0}, ())
    assert f"价格序列含非交易日行|SPY|{extra}|评分回看窗口" in flags


def test_one_breadth_value_retains_known_constraint() -> None:
    """H-05：F 已知且 W 缺失时，不能把 F 也视为未知。"""
    raw = raw_with((55.0, 60.0, 50.0, 50.0))
    breadth = {**raw.breadth, BASE: BreadthReading(BASE, 55.0, None)}
    snapshot = snap(dataclasses.replace(raw, breadth=breadth))
    a, b = score_versions(snapshot)
    assert snapshot.breadth.s5fi == 55 and snapshot.breadth.s5tw is None
    assert a.breadth.possible_scores == (0, 1)
    assert b.breadth.possible_scores == (0, 1)
    assert "S5FI=55.00" in a.breadth.calculation


def test_non_month_end_weekend_oas_excluded_with_manual_note() -> None:
    """H-06：非月末周末 OAS 不参与 v2-M 计数，并提示需人工判断。"""
    raw = raw_with((55.0, 60.0, 50.0, 50.0))
    weekend = D(2025, 10, 25)
    snapshot = snap(dataclasses.replace(raw, oas={**raw.oas, weekend: 4.5}))
    assert snapshot.refs.oas_o6_v2m == D(2025, 10, 23)
    assert any("需人工判断" in note and str(weekend) in note and "v2-M 不计入" in note
               for note in snapshot.data_notes)
