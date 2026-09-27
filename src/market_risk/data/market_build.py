"""data build 的输入收集：接口缓存（按需下载）、TradingView 清洗结果、手工录入（SPEC 6.6）。

- 在线：按各序列起点下载至 end（已完整收盘的交易日），写入 data/cache/ 后使用；
- 离线（--offline）：只用 data/cache/ 中已有的缓存（同一代码的全部缓存按下载时间合并，新下载优先）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection

from market_risk.config import Settings, SymbolInfo, load_symbols
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

# 指数（用于结果标签与回调事件标签，不参与评分）：数据集序列名 → Yahoo 代码
INDEX_SERIES = {"SPX": "^GSPC", "NDX": "^NDX"}
WEEKLY_START = dt.date(1970, 1, 1)      # 周频参考序列：从来源最早的观测开始（NFCI 1971、STLFSI4 1993）


def scoring_series(settings: Settings) -> list[str]:
    """评分输入序列：永远不得设为 revisable。"""
    return [*settings.scored_symbols, *settings.reference_symbols, settings.vix_series, VIX_CBOE,
            settings.oas_series, TREASURY_SERIES, "S5FI", "S5TW"]


def weekly_series(symbols: dict[str, SymbolInfo] | None = None) -> list[SymbolInfo]:
    """symbols.yaml 中登记的周频 FRED 参考序列（如 STLFSI4、NFCI）。"""
    infos = (symbols or load_symbols()).values()
    return sorted((s for s in infos if s.frequency == "weekly" and (s.api_source or "").startswith("fred:")),
                  key=lambda s: s.symbol)


def check_revisable(settings: Settings, symbols: dict[str, SymbolInfo] | None = None) -> None:
    """参与评分的序列不得设 revisable（CLAUDE.md 第13条例外只适用于 reference 序列）。"""
    infos = (symbols or load_symbols()).values()
    bad = sorted({s.symbol for s in infos if s.revisable} & set(scoring_series(settings)))
    if bad:
        raise cache.DataFetchError(f"评分序列不得设为 revisable：{bad}")


def series_names(settings: Settings, symbols: dict[str, SymbolInfo] | None = None) -> list[str]:
    return [*scoring_series(settings), *INDEX_SERIES, *(s.symbol for s in weekly_series(symbols))]


def _weekly(info: SymbolInfo, values: dict[dt.date, float | None], end: dt.date,
            inputs: list[SourceInfo]) -> NewSeries:
    s = value_series(info.symbol, values, "fred", end, inputs)
    s.frequency, s.revisable = "weekly", info.revisable
    return s


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
    check_revisable(settings)
    want = set(only or series_names(settings))
    out: list[NewSeries] = []
    for sym in (*settings.scored_symbols, *settings.reference_symbols):
        if sym in want:
            rows, info = prices.fetch_ohlcv(paths, sym, ETF_START, end, refresh, **retry)
            out.append(etf_series(sym, rows, end, [info]))
    for name, ticker in INDEX_SERIES.items():
        if name in want:
            rows, info = prices.fetch_ohlcv(paths, ticker, ETF_START, end, refresh, key=name, **retry)
            out.append(etf_series(name, rows, end, [info]))
    for w in weekly_series():
        if w.symbol in want:
            values, info = fred.fetch_series(paths, (w.api_source or "").split(":", 1)[1], WEEKLY_START, end,
                                             api_key, refresh=refresh, **retry)
            out.append(_weekly(w, values, end, [info]))
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
    """只用现有缓存生成（不联网）。没有缓存的序列跳过。

    revisable 序列只取最新的一份缓存（整体替换，不与旧版本缓存逐日合并）。
    """
    check_revisable(settings)
    want = set(only or series_names(settings))
    out: list[NewSeries] = []
    for w in weekly_series():
        files = cache.cached_files(paths, "fred", (w.api_source or "").split(":", 1)[1])
        if w.symbol in want and files:
            chosen = files[-1:] if w.revisable else files
            values: dict[dt.date, float | None] = {}
            for f, _ in chosen:
                values.update(cache.series_from_csv(f.read_text(encoding="utf-8")))
            out.append(_weekly(w, values, end, [cache.source_info_from_meta(m) for _, m in chosen]))
    for sym in (*settings.scored_symbols, *settings.reference_symbols, *INDEX_SERIES):
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
