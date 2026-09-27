"""市场数据集 data/market/（B1）：生成、修订检查、manifest、从数据集组装评分输入。只写入临时目录。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest
from conftest import load_sample_raw

from market_risk import services
from market_risk.config import load_settings
from market_risk.data import cache, market
from market_risk.data.market import NewSeries
from market_risk.data.market_build import collect_offline
from market_risk.data.snapshot import build_snapshot
from market_risk.models import BreadthReading, SourceInfo
from market_risk.pipeline import score_snapshot
from market_risk.storage import db
from market_risk.storage.paths import StoragePaths
from market_risk.validation import EXPECTED_PATH, load_expected, sample_breadth

D = dt.date
SETTINGS = load_settings()


@pytest.fixture()
def paths(tmp_path) -> StoragePaths:
    return StoragePaths(tmp_path)


@pytest.fixture()
def ctx(paths):
    return services.Context(SETTINGS, paths, db.sqlite_url(paths.root / "db" / "m.sqlite"))


def _info(name: str) -> SourceInfo:
    return SourceInfo("test", name, f"test://{name}", "2026-09-27T00:00:00+00:00", 1, None, None, False,
                      f"data/cache/test/{name}.csv")


def dataset_from_raw(raw, breadth: dict[dt.date, BreadthReading]) -> list[NewSeries]:
    """把离线样本的原始数据转成数据集的序列（ETF 只有收盘价）。"""
    out = []
    for sym, closes in raw.closes.items():
        rows = {d: {"open": None, "high": None, "low": None, "close": v, "volume": None} for d, v in closes.items()}
        out.append(market.etf_series(sym, rows, raw.base_date + dt.timedelta(days=60), [_info(sym)]))
    end = raw.base_date + dt.timedelta(days=60)
    out.append(market.vix_series("VIXCLS", raw.vix_fred, raw.vix_cboe, end, [_info("VIXCLS")]))
    out.append(market.value_series(market.VIX_CBOE, raw.vix_cboe or {}, "cboe", end, [_info("VIX")]))
    out.append(market.oas_series("BAMLH0A0HYM2", raw.oas, None, end, [_info("OAS")]))
    out.append(market.value_series(market.TREASURY_SERIES, raw.treasury, "treasury", end, [_info("10Y")]))
    out += market.breadth_series(breadth, end)
    return out


def _sample(sample: str = "2025-11-28"):
    exp = load_expected(EXPECTED_PATH)["samples"][sample]
    breadth = sample_breadth(exp)
    return load_sample_raw(sample, breadth), breadth


# ---------------------------------------------------------------------------
# 合并与修订
# ---------------------------------------------------------------------------


def test_merge_rows_revisions_are_not_overwritten():
    old = {D(2025, 1, 2): {"value": 3.0, "source": "fred"}, D(2025, 1, 3): {"value": 3.1, "source": "fred"},
           D(2020, 1, 2): {"value": 5.0, "source": "fred"}}
    new = NewSeries("OAS", "value", {D(2025, 1, 2): {"value": 3.0, "source": "fred"},
                                     D(2025, 1, 3): {"value": 3.2, "source": "fred"},
                                     D(2025, 1, 6): {"value": 3.3, "source": "fred"}}, "fred")
    merged, revs = market.merge_rows(new, old)
    assert [(r.date, r.column, r.old, r.new) for r in revs] == [(D(2025, 1, 3), "value", 3.1, 3.2)]
    assert merged[D(2025, 1, 3)]["value"] == 3.1                  # 保留旧值
    assert merged[D(2025, 1, 6)]["value"] == 3.3                  # 新日期加入
    assert D(2020, 1, 2) in merged                                # 新数据没有的旧日期保留（FRED 只提供最近三年）
    accepted, _ = market.merge_rows(new, old, accept_revisions=True)
    assert accepted[D(2025, 1, 3)]["value"] == 3.2


def test_merge_rows_tolerances_and_source_priority():
    old = {D(2025, 1, 2): {"value": 683.39, "close": 683.39, "open": 1.0, "high": 1.0, "low": 1.0,
                           "volume": 100.0, "source": "yahoo"},
           D(2025, 1, 3): {"value": 16.35, "source": "cboe"}}
    etf = NewSeries("SPY", "etf", {D(2025, 1, 2): {"value": 683.3900146484375, "close": 683.3900146484375,
                                                   "open": 1.0, "high": 1.0, "low": 1.0, "volume": 100.0,
                                                   "source": "yahoo"}}, "yahoo")
    assert market.merge_rows(etf, old)[1] == []                   # 4 位小数后相同：不算修订
    vol = dataclasses.replace(etf, rows={D(2025, 1, 2): {**etf.rows[D(2025, 1, 2)], "volume": 101.0}})
    assert [r.column for r in market.merge_rows(vol, old)[1]] == ["volume"]
    vix = NewSeries("VIXCLS", "value", {D(2025, 1, 3): {"value": 16.35, "source": "fred"}}, "fred")
    merged, revs = market.merge_rows(vix, old)
    assert revs == [] and merged[D(2025, 1, 3)]["source"] == "fred"   # 数值相同，来源改为主来源


def test_build_dataset_writes_files_manifest_and_is_stable(paths):
    raw, breadth = _sample()
    now = dt.datetime(2026, 9, 27, tzinfo=dt.UTC)
    r1 = market.build_dataset(paths, dataset_from_raw(raw, breadth), now=now)
    assert set(r1.changed) >= {"SPY", "VIXCLS", "UST10Y", "S5FI"} and not r1.revisions
    manifest = json.loads(paths.market_manifest.read_text(encoding="utf-8"))
    entry = manifest["series"]["SPY"]
    assert entry["file"] == "data/market/daily/SPY.csv" and entry["sources"] == {"yahoo": len(raw.closes["SPY"])}
    assert entry["sha256"] == market.sha256_text(paths.market_daily_file("SPY").read_text(encoding="utf-8"))
    header = paths.market_daily_file("SPY").read_text(encoding="utf-8").splitlines()[0]
    assert header == "date,value,open,high,low,close,volume,source"
    assert paths.market_daily_file("VIXCLS").read_text(encoding="utf-8").startswith("date,value,source\n")
    before = paths.market_manifest.read_text(encoding="utf-8")
    r2 = market.build_dataset(paths, dataset_from_raw(raw, breadth), now=now + dt.timedelta(days=1))
    assert r2.changed == [] and paths.market_manifest.read_text(encoding="utf-8") == before


def test_data_build_reports_revisions(ctx, paths):
    raw, breadth = _sample()
    first = dataset_from_raw(raw, breadth)
    services.data_build(ctx, end=D(2025, 11, 28), collect=lambda e: (first, {}))
    revised = dataset_from_raw(dataclasses.replace(
        raw, closes={**raw.closes, "SPY": {**raw.closes["SPY"], D(2025, 11, 26): 999.0}}), breadth)
    report = services.data_build(ctx, end=D(2025, 11, 28), collect=lambda e: (revised, {}))
    assert {(r.series, r.date, r.column) for r in report.result.revisions} == {
        ("SPY", D(2025, 11, 26), "value"), ("SPY", D(2025, 11, 26), "close")}
    assert report.revisions_path == paths.market_revisions_md
    text = paths.market_revisions_md.read_text(encoding="utf-8")
    assert "未自动覆盖" in text and "| SPY | 2025-11-26 | close |" in text
    spy = market.read_series_file(paths.market_daily_file("SPY"))[1]
    assert spy[D(2025, 11, 26)]["value"] != 999.0
    services.data_build(ctx, end=D(2025, 11, 28), accept_revisions=True, collect=lambda e: (revised, {}))
    assert market.read_series_file(paths.market_daily_file("SPY"))[1][D(2025, 11, 26)]["value"] == 999.0


# ---------------------------------------------------------------------------
# 从数据集组装评分输入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sample", ["2025-08-29", "2025-11-28"])
def test_scoring_from_dataset_matches_offline_sample(paths, sample):
    raw, breadth = _sample(sample)
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    loaded = market.load_raw_inputs(paths, SETTINGS, raw.base_date, revision_check=False)
    a = build_snapshot(dataclasses.replace(raw, oas_vintage=None))
    b = build_snapshot(loaded)
    assert (a.etfs, a.refs, a.vix, a.vix_t5, a.y, a.h, a.y_t20) == (b.etfs, b.refs, b.vix, b.vix_t5, b.y, b.h, b.y_t20)
    assert (a.oas_o1, a.oas_o6_v3r1, a.oas_o1_v2m, a.oas_o6_v2m) == (b.oas_o1, b.oas_o6_v3r1, b.oas_o1_v2m,
                                                                     b.oas_o6_v2m)
    assert a.breadth == b.breadth or (a.breadth.s5fi, a.breadth.s5tw) == (b.breadth.s5fi, b.breadth.s5tw)
    ra, _ = score_snapshot(a, SETTINGS)
    rb, _ = score_snapshot(b, SETTINGS)
    assert [[d.score for d in r.dimensions] for r in ra] == [[d.score for d in r.dimensions] for r in rb]
    assert all(s.source == "market" and s.cache_file.startswith("data/market/") for s in loaded.sources)


def test_dataset_future_rows_do_not_leak(paths):
    """防未来信息：数据集包含基准日之后的数据，组装输入时截断到基准日（含）。"""
    raw, breadth = _sample()
    series = dataset_from_raw(raw, breadth)
    future = [D(2025, 12, 1), D(2025, 12, 2)]
    for s in series:
        for d in future:
            s.rows[d] = ({"value": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                          "source": s.primary} if s.kind == "etf" else {"value": 9.99, "source": s.primary})
    market.build_dataset(paths, series)
    loaded = market.load_raw_inputs(paths, SETTINGS, raw.base_date, revision_check=False)
    for name, values in (("closes", [d for c in loaded.closes.values() for d in c]), ("vix", loaded.vix_fred),
                         ("cboe", loaded.vix_cboe or {}), ("treasury", loaded.treasury), ("oas", loaded.oas),
                         ("breadth", loaded.breadth)):
        assert max(values) <= raw.base_date, name
    clean = build_snapshot(dataclasses.replace(raw, oas_vintage=None))
    assert build_snapshot(loaded).etfs == clean.etfs


def test_coverage_error_and_score_requires_dataset(ctx, paths):
    with pytest.raises(services.ServiceError, match="请先运行 market-risk fetch"):
        services.load_market_inputs(ctx, D(2025, 11, 28))
    raw, breadth = _sample()
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    assert market.coverage_error(paths, SETTINGS, D(2025, 11, 28)) is None
    assert "未覆盖基准日 2026-01-02" in market.coverage_error(paths, SETTINGS, D(2026, 1, 2))


def test_score_date_reads_dataset_and_records_hash(ctx, paths):
    raw, breadth = _sample()
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    outcome = services.score_date(ctx, D(2025, 11, 28))
    meta = json.loads((outcome.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["market_manifest_sha256"] == market.manifest_sha256(paths)
    assert [d.score for d in outcome.results[0].dimensions] == [0, 0, 0, 0, 0]


def test_vintage_file_used_and_listed_in_manifest(paths):
    raw, breadth = _sample()
    market.build_dataset(paths, dataset_from_raw(raw, breadth))
    base = raw.base_date
    market.write_vintage(paths, "BAMLH0A0HYM2", base, {**raw.oas, base + dt.timedelta(days=3): 9.9}, _info("v"))
    assert paths.market_vintage_file("BAMLH0A0HYM2", base).exists()
    manifest = market.read_manifest(paths)
    assert "BAMLH0A0HYM2_2025-11-28.csv" in manifest["vintage"]
    loaded = market.load_raw_inputs(paths, SETTINGS, base, revision_check=True)
    assert loaded.oas_vintage and max(loaded.oas_vintage) <= base
    other = market.load_raw_inputs(paths, SETTINGS, D(2025, 11, 26), revision_check=True)
    assert other.oas_vintage is None      # 没有该基准日的版本文件：不做版本比对


def test_breadth_conflict_notes_window(paths):
    raw, breadth = _sample()
    conflicts = {D(2025, 11, 20): "广度 2025-11-20：不一致", D(2025, 9, 1): "广度 2025-09-01：不一致"}
    market.build_dataset(paths, dataset_from_raw(raw, breadth), breadth_conflicts=conflicts)
    loaded = market.load_raw_inputs(paths, SETTINGS, raw.base_date, revision_check=False)
    assert "广度 2025-11-20：不一致" in loaded.notes and "广度 2025-09-01：不一致" not in loaded.notes


def test_oas_series_fills_from_tradingview_only_before_fred():
    fred = {D(2023, 9, 26): 4.0, D(2023, 9, 27): 4.1}
    tv = {D(2023, 9, 25): 3.9, D(2023, 9, 26): 7.7, D(1990, 1, 2): 1.0}
    s = market.oas_series("BAMLH0A0HYM2", fred, tv, D(2025, 1, 1), [])
    assert {d: r["source"] for d, r in s.rows.items()} == {
        D(2023, 9, 25): "tradingview", D(2023, 9, 26): "fred", D(2023, 9, 27): "fred"}
    assert s.rows[D(2023, 9, 26)]["value"] == 4.0


def test_vix_series_fills_missing_fred_from_cboe():
    s = market.vix_series("VIXCLS", {D(2025, 1, 2): 15.0, D(2025, 1, 3): None}, {D(2025, 1, 3): 16.0,
                                                                                  D(2025, 1, 6): 17.0},
                          D(2025, 1, 6), [])
    assert s.rows[D(2025, 1, 3)] == {"value": 16.0, "source": "cboe"}
    assert s.rows[D(2025, 1, 6)] == {"value": 17.0, "source": "cboe"}


def test_last_completed_trading_day():
    ny = market.NEW_YORK
    assert market.last_completed_trading_day(dt.datetime(2025, 11, 26, 16, 20, tzinfo=ny)) == D(2025, 11, 26)
    assert market.last_completed_trading_day(dt.datetime(2025, 11, 26, 16, 5, tzinfo=ny)) == D(2025, 11, 25)
    assert market.last_completed_trading_day(dt.datetime(2025, 11, 28, 13, 20, tzinfo=ny)) == D(2025, 11, 28)
    assert market.last_completed_trading_day(dt.datetime(2025, 11, 29, 10, 0, tzinfo=ny)) == D(2025, 11, 28)


def test_collect_offline_from_caches(paths):
    """离线生成：同一代码的多个缓存按下载时间合并；只有收盘价缓存时开高低量留空。"""
    def put(source, key, start, end, series, when):
        f = paths.cache_file(source, key, start, end)
        info = SourceInfo(source, key, "u", when, len(series), start, end, False, "")
        cache.write_cache(f, cache.series_to_csv(series), info)

    put("yahoo", "SPY", D(2025, 1, 1), D(2025, 1, 3), {D(2025, 1, 2): 590.0, D(2025, 1, 3): 591.0}, "2026-01-01")
    put("yahoo", "SPY", D(2025, 1, 3), D(2025, 1, 6), {D(2025, 1, 3): 591.5, D(2025, 1, 6): 592.0}, "2026-02-01")
    put("fred", "VIXCLS", D(2025, 1, 1), D(2025, 1, 6), {D(2025, 1, 2): 15.0, D(2025, 1, 3): None}, "2026-01-01")
    put("fred", "VIXCLS_vintage20250106", D(2025, 1, 1), D(2025, 1, 6), {D(2025, 1, 2): 1.0}, "2026-01-01")
    put("cboe", "VIX", D(2025, 1, 1), D(2025, 1, 6), {D(2025, 1, 3): 16.0}, "2026-01-01")
    series, _ = collect_offline(paths, SETTINGS, D(2025, 1, 6), only=("SPY", "VIXCLS", "VIX_CBOE"))
    by = {s.name: s for s in series}
    assert {d: r["value"] for d, r in by["SPY"].rows.items()} == {
        D(2025, 1, 2): 590.0, D(2025, 1, 3): 591.5, D(2025, 1, 6): 592.0}
    assert by["SPY"].rows[D(2025, 1, 2)]["open"] is None
    assert by["VIXCLS"].rows[D(2025, 1, 2)] == {"value": 15.0, "source": "fred"}     # 版本缓存不混入
    assert by["VIXCLS"].rows[D(2025, 1, 3)] == {"value": 16.0, "source": "cboe"}
