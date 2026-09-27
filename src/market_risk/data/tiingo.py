"""Tiingo 日线（只用于 tv crosscheck 的第三方核对，不参与评分）。

- 取 `close` 字段：不复权收盘价（`adjClose` 为复权价，不使用）。使用前用已知读数验证确实不复权。
- 密钥 TIINGO_API_KEY 只放在 .env；请求时放在 Authorization 头中，不出现在 URL、缓存元数据、日志和报告里。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping

from market_risk.data.cache import DataFetchError, Series, cached_series, redact_text
from market_risk.storage.paths import StoragePaths

BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
TOLERANCE = 0.005


def parse_prices(text: str) -> Series:
    """解析 Tiingo 的 JSON：[{"date": "2025-10-31T00:00:00.000Z", "close": 682.06, ...}, ...] → {日期: close}。"""
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataFetchError(f"Tiingo 返回的不是 JSON：{redact_text(text[:200])}") from exc
    if not isinstance(rows, list):
        raise DataFetchError(f"Tiingo 返回格式错误：{redact_text(str(rows)[:200])}")
    return {dt.date.fromisoformat(r["date"][:10]): float(r["close"]) for r in rows if r.get("close") is not None}


def fetch_closes(
    paths: StoragePaths, ticker: str, start: dt.date, end: dt.date, api_key: str, refresh: bool = False,
    max_retries: int = 3, backoff_seconds: float = 1.0,
) -> Series:  # pragma: no cover - 网络请求
    import requests

    from market_risk.data.cache import USER_AGENT

    url = BASE_URL.format(ticker=ticker.lower())
    params = {"startDate": start.isoformat(), "endDate": end.isoformat(), "format": "json"}

    def download() -> Series:
        resp = requests.get(url, params=params, timeout=60,
                            headers={"User-Agent": USER_AGENT, "Authorization": f"Token {api_key}"})
        if not resp.ok:
            raise DataFetchError(f"HTTP {resp.status_code} {resp.reason}：{redact_text(resp.url)}")
        return parse_prices(resp.text)

    series, _ = cached_series(paths, "tiingo", ticker.upper(), start, end,
                              f"{url}?startDate={start}&endDate={end}", download, refresh,
                              max_retries, backoff_seconds)
    return series


def verify_unadjusted(closes: Mapping[dt.date, float | None], known: Mapping[dt.date, float]) -> list[str]:
    """用已知读数验证数据确实不复权；返回不符的说明（空列表表示通过）。已知读数不在数据中也算不通过。"""
    problems = []
    for d, k in sorted(known.items()):
        v = closes.get(d)
        if v is None:
            problems.append(f"{d} 无数据")
        elif abs(round(v, 2) - k) > TOLERANCE:
            problems.append(f"{d} 为 {v}，已知读数 {k}")
    return problems
