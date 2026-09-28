"""数据获取模块测试（离线：网络请求一律用假函数替代）。"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from market_risk.data import breadth as br
from market_risk.data import cache, cboe, fred, prices, treasury
from market_risk.data.cache import DataFetchError
from market_risk.storage.paths import StoragePaths

FIXTURES = Path(__file__).resolve().parent / "fixtures"
D = dt.date
NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def paths(tmp_path):
    return StoragePaths(tmp_path)


def no_sleep(_: float) -> None:
    pass


# ---- cache ----


def test_redact_url_removes_api_key():
    url = cache.redact_url("https://x/obs", {"series_id": "VIXCLS", "api_key": "SECRET"})
    assert "SECRET" not in url and "series_id=VIXCLS" in url
    assert cache.redact_url("https://x", None) == "https://x"
    assert cache.redact_url("https://x", {"api_key": "S"}) == "https://x"


def test_with_retry_backoff_then_success():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "ok"

    assert cache.with_retry(flaky, "测试", 3, 1.0, sleep=waits.append) == "ok"
    assert waits == [1.0, 2.0]


def test_with_retry_gives_up():
    def always_fail():
        raise ConnectionError("down")

    with pytest.raises(DataFetchError, match="重试 3 次后仍失败"):
        cache.with_retry(always_fail, "测试", 3, 0.5, sleep=no_sleep)


def test_cached_series_uses_cache_and_refresh(paths):
    calls = []

    def download():
        calls.append(1)
        return {D(2025, 11, 26): 1.0, D(2025, 11, 27): None, D(2025, 11, 30): 9.0}

    kwargs = {"url_for_log": "u", "download": download, "now": NOW, "sleep": no_sleep}
    s1, info1 = cache.cached_series(paths, "fred", "X", D(2025, 11, 1), D(2025, 11, 28), **kwargs)
    assert s1 == {D(2025, 11, 26): 1.0, D(2025, 11, 27): None}  # 超出区间的数据被丢弃
    assert not info1.from_cache and info1.rows == 2
    assert info1.cache_file == "data/cache/fred/X_2025-11-01_2025-11-28.csv"
    meta = json.loads(paths.cache_meta_file(paths.root / info1.cache_file).read_text("utf-8"))
    assert meta["url"] == "u" and meta["data_end"] == "2025-11-27"

    s2, info2 = cache.cached_series(paths, "fred", "X", D(2025, 11, 1), D(2025, 11, 28), **kwargs)
    assert s2 == s1 and info2.from_cache and len(calls) == 1

    cache.cached_series(paths, "fred", "X", D(2025, 11, 1), D(2025, 11, 28), refresh=True, **kwargs)
    assert len(calls) == 2


def test_cache_not_used_when_request_reaches_today(paths):
    calls = []

    def download():
        calls.append(1)
        return {D(2026, 9, 25): 1.0}

    for _ in range(2):
        cache.cached_series(paths, "fred", "X", D(2026, 9, 1), D(2026, 9, 26),
                            url_for_log="u", download=download, now=NOW, sleep=no_sleep)
    assert len(calls) == 2


def test_cached_series_empty_raises(paths):
    with pytest.raises(DataFetchError, match="没有返回任何数据"):
        cache.cached_series(paths, "fred", "X", D(2025, 1, 1), D(2025, 1, 31),
                            url_for_log="u", download=dict, now=NOW, sleep=no_sleep)


def test_series_csv_roundtrip_keeps_missing_as_blank():
    s = {D(2025, 10, 13): None, D(2025, 10, 10): 3.18}
    text = cache.series_to_csv(s)
    assert "2025-10-13,\n" in text
    assert cache.series_from_csv(text) == s


# ---- prices ----


def _frame(rows: dict[dt.date, float], adj_factor: float = 0.99) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in rows])
    return pd.DataFrame(
        {"Close": list(rows.values()), "Adj Close": [v * adj_factor for v in rows.values()]},
        index=idx,
    )


def test_prices_use_unadjusted_close_and_end_is_next_day(paths):
    seen = {}

    def downloader(symbol, start, end_exclusive):
        seen.update(symbol=symbol, start=start, end=end_exclusive)
        return _frame({D(2025, 11, 26): 679.679993, D(2025, 11, 28): 683.390015})

    series, info = prices.fetch_closes(paths, "spy", D(2025, 11, 28), 420, downloader=downloader)
    assert seen == {"symbol": "SPY", "start": D(2024, 10, 4), "end": D(2025, 11, 29)}
    assert series == {D(2025, 11, 26): 679.68, D(2025, 11, 28): 683.39}  # Close，不是 Adj Close
    assert info.source == "yahoo" and "auto_adjust=False" in info.url


def test_prices_reject_missing_close_and_empty(paths):
    with pytest.raises(DataFetchError):
        prices.close_series_from_frame(_frame({D(2025, 11, 26): math.nan}), "SPY")
    with pytest.raises(DataFetchError):
        prices.close_series_from_frame(pd.DataFrame(), "SPY")
    with pytest.raises(DataFetchError):
        prices.close_series_from_frame(pd.DataFrame({"Adj Close": [1.0]}), "SPY")


def test_prices_multiindex_columns():
    f = _frame({D(2025, 11, 28): 683.39})
    f.columns = pd.MultiIndex.from_product([f.columns, ["SPY"]])
    assert prices.close_series_from_frame(f, "SPY") == {D(2025, 11, 28): 683.39}


# ---- FRED ----


def _fred_json(rows):
    return json.dumps({"observations": [{"date": d, "value": v} for d, v in rows]})


def test_fred_dot_is_missing_not_zero():
    s = fred.parse_observations(_fred_json([("2025-10-10", "3.18"), ("2025-10-13", ".")]))
    assert s == {D(2025, 10, 10): 3.18, D(2025, 10, 13): None}


def test_fred_error_payloads():
    with pytest.raises(DataFetchError, match="不是 JSON"):
        fred.parse_observations("<html>")
    with pytest.raises(DataFetchError, match="Bad Request"):
        fred.parse_observations(json.dumps({"error_message": "Bad Request. api_key"}))


def test_fred_fetch_series_and_vintage_params(paths):
    captured = []

    def http_get(url, params):
        captured.append(dict(params))
        return _fred_json([("2025-11-26", "3.00")])

    s, info = fred.fetch_series(paths, "BAMLH0A0HYM2", D(2025, 11, 1), D(2025, 11, 28), "SECRET",
                                realtime=D(2025, 11, 28), http_get=http_get)
    assert s == {D(2025, 11, 26): 3.0}
    assert captured[0]["realtime_start"] == captured[0]["realtime_end"] == "2025-11-28"
    assert "SECRET" not in info.url
    assert "vintage20251128" in info.key


def test_ice_history_note():
    s = {D(2023, 9, 26): 4.04}
    note = fred.ice_history_note("BAMLH0A0HYM2", D(2023, 1, 1), s)
    assert note is not None and "三年" in note
    assert fred.ice_history_note("BAMLH0A0HYM2", D(2023, 9, 25), s) is None
    assert fred.ice_history_note("VIXCLS", D(2023, 1, 1), s) is None


# ---- 财政部 ----

TREASURY_CSV = (
    'Date,"1 Mo","10 Yr","30 Yr"\n'
    "11/28/2025,3.90,4.02,4.66\n"
    "11/26/2025,3.91,4.00,4.64\n"
    "11/25/2025,3.92,,4.65\n"
)


def test_parse_treasury_csv():
    s = treasury.parse_treasury_csv(TREASURY_CSV)
    assert s == {D(2025, 11, 28): 4.02, D(2025, 11, 26): 4.00, D(2025, 11, 25): None}
    with pytest.raises(DataFetchError):
        treasury.parse_treasury_csv("Date,1 Mo\n11/28/2025,3.9\n")


def test_treasury_url_and_fetch(paths):
    urls = []

    def http_get(url):
        urls.append(url)
        return TREASURY_CSV

    s, _, notes, fallback = treasury.fetch_ten_year(paths, D(2025, 11, 20), D(2025, 11, 28), None,
                                              http_get=http_get)
    assert s == {D(2025, 11, 28): 4.02, D(2025, 11, 26): 4.00}  # 空值不计入
    assert notes == []
    assert fallback == set()
    assert "daily-treasury-rates.csv/2025/all" in urls[0] and "_format=csv" in urls[0]


def test_treasury_falls_back_to_dgs10(paths):
    def broken(url):
        raise ConnectionError("treasury down")

    def fred_get(url, params):
        assert params["series_id"] == "DGS10"
        return _fred_json([("2025-10-10", "4.05"), ("2025-10-13", "."), ("2025-10-14", "4.03")])

    s, infos, notes, fallback = treasury.fetch_ten_year(
        paths, D(2025, 10, 1), D(2025, 10, 14), "KEY", http_get=broken, fred_get=fred_get,
        max_retries=0,
    )
    assert s == {D(2025, 10, 10): 4.05, D(2025, 10, 14): 4.03}
    assert infos[0].key == "DGS10"
    assert fallback == {D(2025, 10, 10), D(2025, 10, 14)}
    assert any("DGS10" in n for n in notes)


def test_treasury_failure_without_fallback_raises(paths):
    def broken(url):
        raise ConnectionError("down")

    with pytest.raises(DataFetchError):
        treasury.fetch_ten_year(paths, D(2025, 10, 1), D(2025, 10, 14), None,
                                http_get=broken, max_retries=0)


def test_treasury_fallback_only_failed_year(paths, monkeypatch):
    """M-03：一个年份失败不影响其他年份的财政部主源，备用源仅请求失败年份。"""
    from market_risk.models import SourceInfo

    def year_loader(_paths, year, _end, *_args):
        if year == 2024:
            raise DataFetchError("2024 下载失败")
        return {D(2025, 1, 2): 4.01}, SourceInfo("treasury", "10Y", "", "", 1, None, None, False, "")

    calls = []

    def fred_loader(_paths, symbol, start, end, _key, **_kwargs):
        calls.append((symbol, start, end))
        return {D(2024, 12, 31): 4.02}, SourceInfo("fred", "DGS10", "", "", 1, None, None, False, "")

    monkeypatch.setattr(treasury, "fetch_treasury_year", year_loader)
    monkeypatch.setattr(treasury.fred, "fetch_series", fred_loader)
    values, infos, notes, fallback = treasury.fetch_ten_year(paths, D(2024, 12, 31), D(2025, 1, 2), "KEY")
    assert values == {D(2024, 12, 31): 4.02, D(2025, 1, 2): 4.01}
    assert calls == [("DGS10", D(2024, 1, 1), D(2024, 12, 31))]
    assert {info.source for info in infos} == {"fred", "treasury"}
    assert fallback == {D(2024, 12, 31)} and len(notes) == 1


@pytest.mark.parametrize("sample", ["2025-08-29", "2025-09-26", "2025-10-31", "2025-11-28"])
def test_treasury_fixture_matches_sop_appendix_a(appendix_a_yields, sample):
    """SPEC 6.4：程序取得的财政部数值与 SOP 附录A 完全一致（日期集合与数值都相同）。"""
    text = (FIXTURES / "raw" / sample / "treasury_10y.csv").read_text("utf-8")
    fetched = {d: v for d, v in cache.series_from_csv(text).items() if v is not None}
    lo, hi = max(min(fetched), D(2025, 8, 1)), max(fetched)
    got = {d: v for d, v in fetched.items() if lo <= d <= hi}
    expected = {d: v for d, v in appendix_a_yields.items() if lo <= d <= hi}
    assert got == expected
    assert len(expected) > 10


# ---- Cboe 与 VIX 取数顺序（SPEC 5.6 第7条）----


def test_parse_vix_history():
    text = "DATE,OPEN,HIGH,LOW,CLOSE\n11/20/2025,20.78,28.27,19.28,26.420000\n11/21/2025,1,1,1,\n"
    assert cboe.parse_vix_history(text) == {D(2025, 11, 20): 26.42, D(2025, 11, 21): None}
    with pytest.raises(DataFetchError):
        cboe.parse_vix_history("DATE,OPEN\n")


def test_fetch_vix_history(paths):
    text = "DATE,OPEN,HIGH,LOW,CLOSE\n11/20/2025,1,1,1,26.42\n11/28/2025,1,1,1,16.35\n"
    s, info = cboe.fetch_vix_history(paths, D(2025, 11, 1), D(2025, 11, 28),
                                     http_get=lambda url: text)
    assert s[D(2025, 11, 28)] == 16.35 and info.source == "cboe"


def test_resolve_vix_prefers_fred():
    d = D(2025, 11, 28)
    v, notes = cboe.resolve_vix(d, {d: 16.35}, {d: 16.35})
    assert v == 16.35 and notes == []


def test_resolve_vix_fallback_to_cboe_when_fred_missing():
    d = D(2025, 11, 28)
    v, notes = cboe.resolve_vix(d, {d: None}, {d: 16.35})
    assert v == 16.35 and "Cboe" in notes[0]
    v, notes = cboe.resolve_vix(d, {}, {d: 16.35})
    assert v == 16.35


def test_resolve_vix_disagreement_reports_and_uses_fred():
    d = D(2025, 11, 28)
    v, notes = cboe.resolve_vix(d, {d: 16.35}, {d: 16.40})
    assert v == 16.35 and "不一致" in notes[0] and "以 FRED 为准" in notes[0]


def test_resolve_vix_both_missing_is_pending():
    d = D(2025, 11, 28)
    v, notes = cboe.resolve_vix(d, {d: None}, {})
    assert v is None and "记待补" in notes[0]
    v, notes = cboe.resolve_vix(d, {d: float("nan")}, None)
    assert v is None and "Cboe 未取得" in notes[0]


# ---- 广度 ----


def test_breadth_read_write_and_validation(tmp_path):
    path = tmp_path / "breadth.csv"
    assert br.read_breadth(path) == {}
    r = br.make_reading(D(2025, 11, 28), 58.44, 76.73)
    assert br.upsert_breadth(path, r) is True
    assert br.upsert_breadth(path, r) is False  # 相同数值，无变更
    assert br.read_breadth(path)[D(2025, 11, 28)].s5tw == 76.73
    with pytest.raises(br.BreadthError, match="确认后再覆盖"):
        br.upsert_breadth(path, br.make_reading(D(2025, 11, 28), 58.0, 76.73))
    assert br.upsert_breadth(path, br.make_reading(D(2025, 11, 28), 58.0, 76.73), overwrite=True)
    with pytest.raises(br.BreadthError, match="0–100"):
        br.make_reading(D(2025, 11, 28), 0.5844 * 1000, 76.73)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("date,s5fi,s5tw,note\n2025-11-28,58.44,76.73,\n2025-11-28,58.44,76.73,\n", "两条记录"),
        ("date,s5fi,s5tw,note\n2025-11-28,158.44,76.73,\n", "0–100"),
        ("date,s5fi,s5tw,note\n2025-11-28,abc,76.73,\n", "格式错误"),
        ("date,s5fi\n2025-11-28,58.44\n", "缺少列"),
    ],
)
def test_breadth_invalid_files(tmp_path, content, match):
    path = tmp_path / "breadth.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(br.BreadthError, match=match):
        br.read_breadth(path)


def test_errors_never_contain_api_key(caplog):
    """接口错误信息（含 URL）写入日志、data_notes 前必须脱敏。"""
    url = "https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key=abc123SECRET&file_type=json"
    assert cache.redact_text(f"400 Client Error: Bad Request for url: {url}") == (
        "400 Client Error: Bad Request for url: "
        "https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key=***&file_type=json"
    )

    def failing():
        raise RuntimeError(f"400 Client Error for url: {url}")

    with pytest.raises(DataFetchError) as info:
        cache.with_retry(failing, "下载", 1, 0.0, sleep=no_sleep)
    assert "abc123SECRET" not in str(info.value) and "api_key=***" in str(info.value)
    assert info.value.__cause__ is None
    assert "abc123SECRET" not in caplog.text
