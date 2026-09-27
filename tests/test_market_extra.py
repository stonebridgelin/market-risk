"""第三部分：指数序列（SPX、NDX）、周频参考序列（STLFSI4、NFCI）与 revisable 规则。只写入临时目录。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from market_risk.config import ConfigError, load_settings, load_symbols
from market_risk.data import cache, market
from market_risk.data.market_build import (
    INDEX_SERIES,
    check_revisable,
    collect_offline,
    scoring_series,
    series_names,
    weekly_series,
)
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

D = dt.date
SETTINGS = load_settings()


def weekly(rows: dict, revisable: bool = True) -> market.NewSeries:
    s = market.value_series("NFCI", rows, "fred", D(2030, 1, 1), [])
    s.frequency, s.revisable = "weekly", revisable
    return s


def test_scoring_series_are_never_revisable():
    """参与评分的序列永远不得设 revisable（登记表与构建前检查）。"""
    symbols = load_symbols()
    revisable = {s.symbol for s in symbols.values() if s.revisable}
    assert revisable == {"STLFSI4", "NFCI"}
    assert not revisable & set(scoring_series(SETTINGS))
    assert all(s.usage == "reference" for s in symbols.values() if s.revisable)
    check_revisable(SETTINGS)
    bad = {k: dataclasses.replace(v, revisable=True) if v.symbol == "SPY" else v for k, v in symbols.items()}
    with pytest.raises(cache.DataFetchError, match="评分序列不得设为 revisable"):
        check_revisable(SETTINGS, bad)


def test_registry_refuses_revisable_non_reference(tmp_path):
    from market_risk.config import load_symbols as load

    p = tmp_path / "s.yaml"
    p.write_text("- {symbol: X, tv_symbol: 'A:X', usage: crosscheck, revisable: true}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="revisable"):
        load(p)
    p.write_text("- {symbol: X, tv_symbol: 'A:X', frequency: monthly}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="frequency"):
        load(p)


def test_revisable_series_is_replaced_whole_and_counted(tmp_path):
    paths = StoragePaths(tmp_path)
    first = market.build_dataset(paths, [weekly({D(2020, 1, 3): 1.0, D(2020, 1, 10): 2.0})])
    assert paths.market_weekly_file("NFCI").exists() and not paths.market_daily_file("NFCI").exists()
    assert first.replaced["NFCI"] == {"revised": 0, "added": 2, "removed": 0}
    # 新版本修订了 01-03、删除 01-10、新增 01-17：整体替换，不与旧版本逐日混合，不列为待确认修订
    second = market.build_dataset(paths, [weekly({D(2020, 1, 3): 1.5, D(2020, 1, 17): 3.0})])
    assert second.replaced["NFCI"] == {"revised": 1, "added": 1, "removed": 1}
    assert not second.revisions
    rows = market.read_series_file(paths.market_weekly_file("NFCI"))[1]
    assert {d: r["value"] for d, r in rows.items()} == {D(2020, 1, 3): 1.5, D(2020, 1, 17): 3.0}
    entry = second.series["NFCI"]
    assert entry["revisable"] and entry["frequency"] == "weekly" and entry["file"] == "data/market/weekly/NFCI.csv"
    assert any("修订 1 条" in n for n in second.notes)
    # 新下载为空（离线无缓存）：保留上一版，不清空
    third = market.build_dataset(paths, [weekly({})])
    assert third.changed == [] and market.read_series_file(paths.market_weekly_file("NFCI"))[1] == rows


def test_non_revisable_series_keeps_old_values():
    old = {D(2020, 1, 3): {"value": 1.0, "source": "fred"}}
    merged, revs = market.merge_rows(weekly({D(2020, 1, 3): 1.5}, revisable=False), old)
    assert merged[D(2020, 1, 3)]["value"] == 1.0 and len(revs) == 1


def test_revisable_series_cannot_be_corrected(tmp_path):
    from decimal import Decimal

    from market_risk.config import DataDecision

    dec = DataDecision(D(2020, 1, 3), "NFCI", "correct", "测试", D(2026, 9, 27), Decimal("1"), "测试")
    with pytest.raises(market.MarketDataError, match="revisable"):
        market.build_dataset(StoragePaths(tmp_path), [weekly({D(2020, 1, 3): 1.0})], decisions=(dec,))


def test_offline_revisable_uses_latest_cache_only(tmp_path):
    """离线生成 revisable 序列只取最新一份缓存，不把旧版本缓存逐日合并进来。"""
    paths = StoragePaths(tmp_path)

    def put(end: dt.date, series: dict, when: str) -> None:
        f = paths.cache_file("fred", "NFCI", D(1970, 1, 1), end)
        cache.write_cache(f, cache.series_to_csv(series), SourceInfo("fred", "NFCI", "u", when, len(series),
                                                                     min(series), max(series), False, ""))

    put(D(2020, 1, 10), {D(2020, 1, 3): 1.0, D(2020, 1, 10): 2.0}, "2020-01-15T00:00:00+00:00")
    put(D(2020, 1, 17), {D(2020, 1, 3): 1.2, D(2020, 1, 17): 3.0}, "2020-01-22T00:00:00+00:00")
    series, _ = collect_offline(paths, SETTINGS, D(2020, 1, 31), only=("NFCI",))
    (nfci,) = series
    assert nfci.revisable and nfci.frequency == "weekly"
    assert {d: r["value"] for d, r in nfci.rows.items()} == {D(2020, 1, 3): 1.2, D(2020, 1, 17): 3.0}


def test_index_series_registered_and_offline(tmp_path):
    paths = StoragePaths(tmp_path)
    assert INDEX_SERIES == {"SPX": "^GSPC", "NDX": "^NDX"}
    assert {"SPX", "NDX", "STLFSI4", "NFCI"} <= set(series_names(SETTINGS))
    assert [s.symbol for s in weekly_series()] == ["NFCI", "STLFSI4"]
    rows = {D(2020, 1, 2): {"open": 1.0, "high": 2.0, "low": 0.5, "close": 3257.85, "volume": 0.0}}
    f = paths.cache_file("yahoo", "SPX_OHLCV", D(1990, 1, 1), D(2020, 1, 2))
    cache.write_cache(f, cache.rows_to_csv(rows, ["open", "high", "low", "close", "volume"]),
                      SourceInfo("yahoo", "SPX_OHLCV", "u", "2020-01-03T00:00:00+00:00", 1, D(2020, 1, 2),
                                 D(2020, 1, 2), False, ""))
    (spx,) = collect_offline(paths, SETTINGS, D(2020, 1, 2), only=("SPX",))[0]
    assert spx.kind == "etf" and spx.rows[D(2020, 1, 2)]["value"] == 3257.85


def test_crosscheck_market_source(tmp_path):
    from market_risk.data.tv_compare import compare_series, fetch_api_series, format_results

    paths = StoragePaths(tmp_path)
    s = market.etf_series("SPX", {D(2021, 8, 11): {"open": 1.0, "high": 1.0, "low": 1.0, "close": 4442.41,
                                                   "volume": 0.0},
                                  D(2021, 8, 12): {"open": 1.0, "high": 1.0, "low": 1.0, "close": 4460.83,
                                                   "volume": 0.0}}, D(2021, 8, 12), [])
    market.build_dataset(paths, [s])
    api = fetch_api_series("market:SPX", D(2021, 8, 1), D(2021, 8, 31), SETTINGS, paths, None)
    info = next(v for v in load_symbols().values() if v.symbol == "SPX")
    r = compare_series(info, {D(2021, 8, 11): 4447.69, D(2021, 8, 12): 4460.84}, api, info.tolerance)
    assert r.nonzero == 2 and len(r.mismatches) == 1 and r.max_abs_diff == 5.28
    assert "| 非零差异 |" in format_results([r], "t", "t")
