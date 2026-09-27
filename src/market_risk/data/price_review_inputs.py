"""残差审查的数据获取：ETF 读市场数据集，指数及除息日仅进入审计缓存。"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from market_risk import calendar as cal
from market_risk.config import Settings
from market_risk.data import cache, prices, tradingview
from market_risk.data.corrections import restore_originals
from market_risk.data.market import read_manifest, read_series_file
from market_risk.data.price_review import ReviewConfig, published_prices
from market_risk.storage.paths import StoragePaths


@dataclass(frozen=True)
class ReviewInputs:
    tv: Mapping[str, Mapping[dt.date, Decimal]]
    yahoo: Mapping[str, Mapping[dt.date, Decimal]]
    yahoo_indices: Mapping[str, Mapping[dt.date, Decimal]]
    dividends: Mapping[str, frozenset[dt.date]]
    errors: Mapping[str, str]
    provenance: tuple[str, ...]


def dividend_series_from_frame(frame: object) -> cache.Series:
    """保留无分红日的零值，以验证请求窗口完整；仅数值>0的日期为除息日。"""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame) or frame.empty or "Dividends" not in frame:
        raise cache.DataFetchError("yfinance 未返回分红记录，不能把获取失败当成无除息日")
    values = frame["Dividends"]
    if values.isna().any() or (values < 0).any():
        raise cache.DataFetchError("yfinance 分红记录存在无效值")
    return {ts.date(): float(v) for ts, v in values.items()}


def download_dividends(symbol: str, start: dt.date, end: dt.date) -> cache.Series:  # pragma: no cover - 联网
    import yfinance as yf

    frame = yf.download(symbol, start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                        auto_adjust=False, actions=True, progress=False, multi_level_index=False, threads=False)
    return dividend_series_from_frame(frame)


def collect_review_inputs(paths: StoragePaths, settings: Settings, config: ReviewConfig,
                          refresh: bool = False) -> ReviewInputs:
    start = cal.shift_trading_days(min(d for _, d in config.cases), -config.window_sessions - 1)
    end = cal.shift_trading_days(max(d for _, d in config.cases), config.window_sessions)
    symbols = sorted({s for s, _ in config.cases})
    benchmarks = sorted({config.benchmarks[s] for s in symbols})
    tv = {s: published_prices(tradingview.read_processed(paths, s)) for s in [*symbols, *benchmarks]}
    manifest = read_manifest(paths)
    yahoo = {}
    provenance = []
    for symbol in symbols:
        rows = restore_originals(read_series_file(paths.market_daily_file(symbol))[1],
                                 manifest.get("series", {}).get(symbol, {}).get("corrections", []))
        yahoo[symbol] = published_prices({d: r["value"] for d, r in rows.items()})
    files = [paths.market_manifest, *[paths.market_daily_file(s) for s in symbols],
             *[paths.tv_processed_file(s) for s in [*symbols, *benchmarks]]]
    indices: dict[str, dict[dt.date, Decimal]] = {}
    dividends: dict[str, frozenset[dt.date]] = {}
    errors: dict[str, str] = {}
    retry = {"max_retries": settings.max_retries, "backoff_seconds": settings.backoff_seconds}
    for name in [*benchmarks, *symbols]:
        is_index = name in benchmarks
        ticker = config.index_tickers[name] if is_index else name
        key = name + ("_dispute_index" if is_index else "_dispute_dividends")

        def download(ticker: str = ticker, is_index: bool = is_index) -> cache.Series:
            if is_index:
                return prices.close_series_from_frame(
                    prices.yfinance_download(ticker, start, end + dt.timedelta(days=1)), ticker)
            return download_dividends(ticker, start, end)

        description = f"yfinance {ticker} " + ("auto_adjust=False Close" if is_index else "actions=True Dividends")
        try:
            values, info = cache.cached_series(paths, "yahoo", key, start, end, description,
                                                download, refresh=refresh, **retry)
            if is_index:
                indices[name] = published_prices(values)
            else:
                required = cal.stock_trading_days(start, end)
                if any(d not in values or values[d] is None for d in required):
                    raise cache.DataFetchError("除息数据未覆盖完整请求窗口")
                dividends[name] = frozenset(d for d, v in values.items() if v and v > 0)
            files.append(paths.cache_file("yahoo", key, start, end))
            provenance.append(f"{description}；下载时间 {info.downloaded_at_utc}")
        except cache.DataFetchError as exc:
            errors[name] = cache.redact_text(str(exc))
    for path in files:
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            provenance.append(f"`{path.relative_to(paths.root).as_posix()}` sha256={digest}")
    return ReviewInputs(tv, yahoo, indices, dividends, errors, tuple(provenance))
