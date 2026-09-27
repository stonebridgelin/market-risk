"""data build 的输入收集：接口缓存（按需下载）、TradingView 清洗结果、手工录入（SPEC 6.6）。

- 在线：按各序列起点下载至 end（已完整收盘的交易日），写入 data/cache/ 后使用；
- 离线（--offline）：只用 data/cache/ 中已有的缓存（同一代码的全部缓存按下载时间合并，新下载优先）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection

from market_risk.config import Settings
from market_risk.data import cache, cboe, fred, prices, treasury
from market_risk.data.breadth import load_breadth
from market_risk.data.market import (
    ETF_START,
    OAS_START,
    TREASURY_SERIES,
    TREASURY_START,
    VIX_CBOE,
    VIX_START,
    NewSeries,
    breadth_series,
    etf_series,
    local_input,
    oas_series,
    value_series,
    vix_series,
)
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths


def series_names(settings: Settings) -> list[str]:
    return [*settings.scored_symbols, *settings.reference_symbols, settings.vix_series, VIX_CBOE,
            settings.oas_series, TREASURY_SERIES, "S5FI", "S5TW"]


def _tv_oas(paths: StoragePaths, settings: Settings) -> dict[dt.date, float] | None:
    if settings.oas_long_history_source != "tradingview":
        return None
    from market_risk.data.tradingview import read_processed

    return read_processed(paths, settings.oas_series)


def collect_online(
    paths: StoragePaths, settings: Settings, end: dt.date, api_key: str, refresh: bool = False,
    only: Collection[str] | None = None,
) -> tuple[list[NewSeries], dict[dt.date, str]]:  # pragma: no cover - 网络请求
    retry = {"max_retries": settings.max_retries, "backoff_seconds": settings.backoff_seconds}
    want = set(only or series_names(settings))
    out: list[NewSeries] = []
    for sym in (*settings.scored_symbols, *settings.reference_symbols):
        if sym in want:
            rows, info = prices.fetch_ohlcv(paths, sym, ETF_START, end, refresh, **retry)
            out.append(etf_series(sym, rows, end, [info]))
    if settings.vix_series in want or VIX_CBOE in want:
        vix, vinfo = fred.fetch_series(paths, settings.vix_series, VIX_START, end, api_key, refresh=refresh, **retry)
        cb, cinfo = cboe.fetch_vix_history(paths, VIX_START, end, settings.cboe_vix_history_url, refresh, **retry)
        if settings.vix_series in want:
            out.append(vix_series(settings.vix_series, vix, cb, end, [vinfo, cinfo]))
        if VIX_CBOE in want:
            out.append(value_series(VIX_CBOE, cb, "cboe", end, [cinfo]))
    if settings.oas_series in want:
        try:
            oas, oinfo = fred.fetch_series(paths, settings.oas_series, OAS_START, end, api_key, refresh=refresh,
                                           **retry)
            infos = [oinfo]
        except cache.DataFetchError:
            if settings.oas_long_history_source != "tradingview":
                raise
            oas, infos = {}, []
        tv = _tv_oas(paths, settings)
        if tv is not None:
            infos.append(local_input(paths, paths.tv_processed_file(settings.oas_series), "tradingview"))
        out.append(oas_series(settings.oas_series, oas, tv, end, infos))
    if TREASURY_SERIES in want:
        values, tinfos, tnotes = treasury.fetch_ten_year(paths, TREASURY_START, end, api_key,
                                                         refresh, **retry)
        source = "fred:DGS10" if tnotes else "treasury"
        s = value_series(TREASURY_SERIES, values, source, end, tinfos)
        s.primary, s.notes = "treasury", list(tnotes)
        out.append(s)
    conflicts: dict[dt.date, str] = {}
    if want & {"S5FI", "S5TW"}:
        readings, conflicts = load_breadth(paths)
        out += [s for s in breadth_series(readings, end, paths) if s.name in want]
    return out, conflicts


def _merged_cache(paths: StoragePaths, source: str, key: str) -> tuple[dict[dt.date, float | None], list[SourceInfo]]:
    merged: dict[dt.date, float | None] = {}
    infos = []
    for f, meta in cache.cached_files(paths, source, key):
        merged.update(cache.series_from_csv(f.read_text(encoding="utf-8")))
        infos.append(cache.source_info_from_meta(meta))
    return merged, infos


def collect_offline(
    paths: StoragePaths, settings: Settings, end: dt.date, only: Collection[str] | None = None,
) -> tuple[list[NewSeries], dict[dt.date, str]]:
    """只用现有缓存生成（不联网）。没有缓存的序列跳过。"""
    want = set(only or series_names(settings))
    out: list[NewSeries] = []
    for sym in (*settings.scored_symbols, *settings.reference_symbols):
        if sym not in want:
            continue
        rows: dict[dt.date, dict[str, float | None]] = {}
        infos: list[SourceInfo] = []
        files = cache.cached_files(paths, "yahoo", f"{sym}_OHLCV")
        if files:
            for f, meta in files:
                rows.update(cache.rows_from_csv(f.read_text(encoding="utf-8")))
                infos.append(cache.source_info_from_meta(meta))
        else:   # 只有按基准日下载的收盘价缓存：开高低量留空
            closes, infos = _merged_cache(paths, "yahoo", sym)
            rows = {d: {"open": None, "high": None, "low": None, "close": v, "volume": None}
                    for d, v in closes.items() if v is not None}
        if rows:
            out.append(etf_series(sym, rows, end, infos))
    vix, vinfos = _merged_cache(paths, "fred", settings.vix_series)
    cb, cinfos = _merged_cache(paths, "cboe", "VIX")
    if settings.vix_series in want and vix:
        out.append(vix_series(settings.vix_series, vix, cb, end, vinfos + cinfos))
    if VIX_CBOE in want and cb:
        out.append(value_series(VIX_CBOE, cb, "cboe", end, cinfos))
    if settings.oas_series in want:
        oas, oinfos = _merged_cache(paths, "fred", settings.oas_series)
        tv = _tv_oas(paths, settings)
        if tv is not None:
            oinfos.append(local_input(paths, paths.tv_processed_file(settings.oas_series), "tradingview"))
        if oas or tv:
            out.append(oas_series(settings.oas_series, oas, tv, end, oinfos))
    if TREASURY_SERIES in want:
        t, tinfos = _merged_cache(paths, "treasury", "10Y")
        if t:
            out.append(value_series(TREASURY_SERIES, {d: v for d, v in t.items() if v is not None}, "treasury",
                                    end, tinfos))
    conflicts: dict[dt.date, str] = {}
    if want & {"S5FI", "S5TW"}:
        readings, conflicts = load_breadth(paths)
        out += [s for s in breadth_series(readings, end, paths) if s.name in want]
    return out, conflicts
