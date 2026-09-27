"""ETF 日线（SPEC 6.2）：yfinance，auto_adjust=False，只取不复权 Close。

不得使用 Adj Close：它按基准日之后的股息调整历史价格，属于未来信息。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable

import pandas as pd

from market_risk.data.cache import DataFetchError, Series, cached_series
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

PRICE_DECIMALS = 2
STORE_DECIMALS = 4          # data/market 中的价格：四舍五入到4位小数（去掉浮点表示误差，保留全部真实价格）
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

Downloader = Callable[[str, dt.date, dt.date], pd.DataFrame]


def yfinance_download(
    symbol: str, start: dt.date, end_exclusive: dt.date
) -> pd.DataFrame:  # pragma: no cover - 网络请求
    import yfinance as yf

    return yf.download(
        symbol,
        start=start.isoformat(),
        end=end_exclusive.isoformat(),
        auto_adjust=False,
        progress=False,
        actions=False,
        multi_level_index=False,
    )


def close_series_from_frame(frame: pd.DataFrame, symbol: str) -> Series:
    """从 yfinance 结果中取 Close 列（不复权），保留2位小数；缺失值报错，不插值。"""
    if frame is None or frame.empty:
        raise DataFetchError(f"{symbol}：yfinance 没有返回数据")
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs(symbol, axis=1, level=-1) if symbol in frame.columns.get_level_values(-1) \
            else frame.droplevel(-1, axis=1)
    if "Close" not in frame.columns:
        raise DataFetchError(f"{symbol}：返回数据中没有 Close 列")
    result: Series = {}
    for ts, value in frame["Close"].items():
        d = pd.Timestamp(ts).date()
        if value is None or (isinstance(value, float) and math.isnan(value)):
            raise DataFetchError(f"{symbol}：{d} 的 Close 缺失")
        result[d] = round(float(value), PRICE_DECIMALS)
    return result


def ohlcv_rows_from_frame(frame: pd.DataFrame, symbol: str) -> dict[dt.date, dict[str, float | None]]:
    """从 yfinance 结果中取开高低收量（不复权 Close），价格四舍五入到4位小数；Close 缺失报错，不插值。"""
    if frame is None or frame.empty:
        raise DataFetchError(f"{symbol}：yfinance 没有返回数据")
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.xs(symbol, axis=1, level=-1) if symbol in frame.columns.get_level_values(-1) \
            else frame.droplevel(-1, axis=1)
    missing = [c for c in ("Open", "High", "Low", "Close", "Volume") if c not in frame.columns]
    if missing:
        raise DataFetchError(f"{symbol}：返回数据中没有 {missing} 列")

    def num(v: object, decimals: int | None) -> float | None:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        x = float(v)  # type: ignore[arg-type]
        return round(x, decimals) if decimals is not None else float(int(x))

    rows: dict[dt.date, dict[str, float | None]] = {}
    for ts, r in frame.iterrows():
        d = pd.Timestamp(ts).date()
        close = num(r["Close"], STORE_DECIMALS)
        if close is None:
            raise DataFetchError(f"{symbol}：{d} 的 Close 缺失")
        rows[d] = {"open": num(r["Open"], STORE_DECIMALS), "high": num(r["High"], STORE_DECIMALS),
                   "low": num(r["Low"], STORE_DECIMALS), "close": close, "volume": num(r["Volume"], None)}
    return rows


def fetch_ohlcv(
    paths: StoragePaths,
    symbol: str,
    start: dt.date,
    end: dt.date,
    refresh: bool = False,
    downloader: Downloader = yfinance_download,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[dict[dt.date, dict[str, float | None]], SourceInfo]:
    """下载 [start, end] 的日线开高低收量（data build 使用；缓存代码 <标的>_OHLCV）。"""
    from market_risk.data.cache import cached_rows

    symbol = symbol.upper()
    end_exclusive = end + dt.timedelta(days=1)
    return cached_rows(
        paths, "yahoo", f"{symbol}_OHLCV", start, end, OHLCV_COLUMNS,
        url_for_log=f"yfinance.download({symbol!r}, start={start}, end={end_exclusive}, auto_adjust=False) → OHLCV",
        download=lambda: ohlcv_rows_from_frame(downloader(symbol, start, end_exclusive), symbol),
        refresh=refresh, max_retries=max_retries, backoff_seconds=backoff_seconds,
    )


def fetch_closes(
    paths: StoragePaths,
    symbol: str,
    base_date: dt.date,
    lookback_calendar_days: int = 420,
    refresh: bool = False,
    downloader: Downloader = yfinance_download,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Series, SourceInfo]:
    """下载基准日往前 lookback 个自然日至基准日（含）的不复权收盘价。

    yfinance 的 end 参数不含当天，所以传入基准日次日。
    """
    symbol = symbol.upper()
    start = base_date - dt.timedelta(days=lookback_calendar_days)
    end_exclusive = base_date + dt.timedelta(days=1)

    def _download() -> Series:
        return close_series_from_frame(downloader(symbol, start, end_exclusive), symbol)

    return cached_series(
        paths,
        "yahoo",
        symbol,
        start,
        base_date,
        url_for_log=(
            f"yfinance.download({symbol!r}, start={start}, end={end_exclusive}, "
            "auto_adjust=False) → Close"
        ),
        download=_download,
        refresh=refresh,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
    )
