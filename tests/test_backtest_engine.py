"""阶段6 验收：回测与单日评分流程逐项一致、daily_metrics 与单日指标一致、样本1至4、防未来信息、flags。

读取仓库中的真实数据集 data/market/ 与正式记录（只读）；单日运行只写入临时目录。
"""

from __future__ import annotations

import datetime as dt
import json
import random

import pytest

from market_risk import calendar as mcal
from market_risk.backtest.engine import run_backtest
from market_risk.backtest.settings import load_backtest_config
from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.data.market import load_market_series, load_raw_inputs
from market_risk.metrics import format_metric
from market_risk.pipeline import run_scoring
from market_risk.storage.paths import MARKET, RISK_SCORING, StoragePaths
from market_risk.storage.runs import GitInfo, read_official

D = dt.date
SETTINGS = load_settings()
REAL = StoragePaths(PROJECT_ROOT)
CFG = load_backtest_config()
CLEAN = GitInfo("a" * 40, False)

# 固定选取的日期：节假日前后、月末、债市休市日次日、人工修正日、区间边界
SPECIAL = [D(2008, 8, 11), D(2008, 9, 8), D(2009, 7, 16), D(2011, 9, 30), D(2012, 11, 23), D(2014, 10, 14),
           D(2015, 7, 6), D(2016, 12, 30), D(2017, 1, 3), D(2019, 12, 31), D(2021, 6, 1), D(2022, 11, 25)]


def _random_days(n: int, seed: int) -> list[dt.date]:
    days = mcal.stock_trading_days(CFG.start, D(2025, 12, 31))
    return sorted(random.Random(seed).sample(days, n))


SAMPLE_DAYS = sorted(set(SPECIAL + _random_days(12, 20260927)))      # 至少20个，覆盖各年份


@pytest.fixture(scope="module")
def series():
    return load_market_series(REAL, SETTINGS)


@pytest.fixture(scope="module")
def engine_days(series):
    out = {}
    for d in SAMPLE_DAYS:
        (res,) = run_backtest(series, REAL, SETTINGS, CFG, start=d, end=d, corrections=()).days
        out[d] = res
    return out


def _single_day(tmp_path, base):
    raw = load_raw_inputs(REAL, SETTINGS, base, revision_check=False)
    return run_scoring(raw, SETTINGS, StoragePaths(tmp_path / str(base)), CLEAN)


def _key(r):
    return ([(d.name, d.score, d.possible_scores) for d in r.dimensions], r.total, r.total_range, r.stage,
            r.clear_deterioration)


def test_sample_days_cover_requirements():
    assert len(SAMPLE_DAYS) >= 20
    assert len({d.year for d in SAMPLE_DAYS}) >= 10


def test_engine_matches_single_day_score(engine_days, tmp_path):
    """随机与特殊日期（≥20个）：回测结果与单日 score 流程逐项一致。"""
    for base in SAMPLE_DAYS:
        single = _single_day(tmp_path, base)
        eng = engine_days[base]
        for r in single.results:
            assert _key(eng.results[r.version]) == _key(r), (base, r.version)


def test_daily_metrics_match_single_day_metrics(engine_days, tmp_path):
    """随机至少10个交易日：daily_metrics 与单日运行的 metrics.json 逐项一致（数值按 6 位小数）。"""
    for base in SAMPLE_DAYS[::2][:12]:
        single = _single_day(tmp_path, base)
        metrics = json.loads((single.run_dir / "metrics.json").read_text(encoding="utf-8"))
        eng = engine_days[base].metrics
        assert set(eng) == set(metrics), base
        for k, v in metrics.items():
            got = format_metric(eng[k])  # type: ignore[arg-type]
            if isinstance(v, bool):
                assert got == ("是" if v else "否"), (base, k)
            elif isinstance(v, int | float):
                assert float(got) == pytest.approx(v, abs=1e-9), (base, k)
            else:
                assert got == ("" if v is None else str(v)), (base, k)


def test_samples_match_official_records(series):
    """样本1至4：回测分数与现有正式记录逐项一致。"""
    for base in (D(2025, 8, 29), D(2025, 9, 26), D(2025, 10, 31), D(2025, 11, 28)):
        pointer = read_official(REAL, MARKET, RISK_SCORING, base)
        run_dir = REAL.run_dir(MARKET, RISK_SCORING, base, pointer["run_id"])
        official = {r["version"]: r for r in json.loads((run_dir / "scores.json").read_text("utf-8"))["results"]}
        (res,) = run_backtest(series, REAL, SETTINGS, CFG, start=base, end=base).days
        for version, r in res.results.items():
            o = official[version]
            dims = [o[k]["score"] for k in ("price", "breadth", "vix", "rates", "credit")]
            assert [d.score for d in r.dimensions] == dims, (base, version)
            assert (r.total, list(r.total_range), r.stage) == (o["total"], o["total_range"], o["stage"])


@pytest.mark.parametrize("base", [D(2011, 8, 5), D(2020, 3, 16)])
def test_future_extremes_do_not_change_scores(series, base):
    """防未来信息：把基准日之后的全部数据改成极端值，已计算的分数与指标不变。"""
    (clean,) = run_backtest(series, REAL, SETTINGS, CFG, start=base, end=base).days
    polluted = series
    for name, dates in series.dates.items():
        polluted = polluted.replace_values(name, {d: 99999.0 for d in dates if d > base})
    (dirty,) = run_backtest(polluted, REAL, SETTINGS, CFG, start=base, end=base).days
    assert {v: _key(r) for v, r in dirty.results.items()} == {v: _key(r) for v, r in clean.results.items()}
    assert dirty.metrics == clean.metrics


def test_flags_by_actual_usage(series):
    corrections = [("SPY", D(2009, 7, 16))]
    days = {d.date: d for d in run_backtest(series, REAL, SETTINGS, CFG, start=D(2009, 7, 16),
                                            end=D(2009, 9, 30), corrections=corrections).days}
    first = days[D(2009, 7, 16)].flags
    assert "使用人工修正值|SPY|2009-07-16|当日收盘比较" in first["v2-M"]
    assert "使用人工修正值|SPY|2009-07-16|MA5" in first["v3-R1"]
    later = days[mcal.shift_trading_days(D(2009, 7, 16), 30)].flags
    used = {f.rsplit("|", 1)[1] for f in later["v3-R1"] if f.startswith("使用人工修正值")}
    assert used == {"MA50", "MA200"}                  # 30个交易日后：只在 MA50、MA200 的回看范围内
    assert any(f.endswith("|MA50") for f in later["v3-R1"])
    assert any(f.startswith("广度单值K线阶段|S5FI") for f in first["v2-M"])
    assert any(f.startswith("OAS来自TradingView长历史") for f in first["v3-R1"])
    late = run_backtest(series, REAL, SETTINGS, CFG, start=D(2010, 10, 4), end=D(2010, 10, 4)).days[0].flags
    # 基准日已过两个单值阶段；T−5=2010-09-27 仍在 S5TW 的单值阶段内（至 2010-10-01，含当天）
    assert [f for f in late["v2-M"] if f.startswith("广度单值K线阶段")] == [
        "广度单值K线阶段|S5TW|2010-09-27|T−5 广度"]


def test_configured_start_is_earliest_complete_day(series):
    """配置的起点 2008-08-11 = 第一个基准日与其 T−5 都有广度读数的交易日。"""
    breadth = series.breadth
    first = next(d for d in mcal.stock_trading_days(D(2008, 7, 1), D(2008, 9, 30))
                 if d in breadth and mcal.shift_trading_days(d, -5) in breadth)
    assert first == CFG.start == D(2008, 8, 11)
