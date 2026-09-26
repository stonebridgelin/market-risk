"""VIX 备用源：Cboe 官方 VIX_History.csv 的 CLOSE 列（SPEC 5.6 第7条，仅用于 VIX）。

URL：https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
列：DATE（MM/DD/YYYY）、OPEN、HIGH、LOW、CLOSE（2026-09-26 已联网确认）。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
from collections.abc import Callable, Mapping

from market_risk.data.cache import (
    DataFetchError,
    Series,
    cached_series,
    http_get_text,
)
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

DEFAULT_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_DECIMALS = 2

TextGet = Callable[[str], str]


def _default_get(url: str) -> str:  # pragma: no cover - 网络请求
    return http_get_text(url)


def parse_vix_history(text: str) -> Series:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None or "DATE" not in reader.fieldnames \
            or "CLOSE" not in reader.fieldnames:
        raise DataFetchError(f"Cboe VIX_History.csv 缺少 DATE 或 CLOSE 列：{reader.fieldnames}")
    result: Series = {}
    for row in reader:
        d = dt.datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").date()
        v = (row["CLOSE"] or "").strip()
        result[d] = round(float(v), VIX_DECIMALS) if v else None
    return result


def fetch_vix_history(
    paths: StoragePaths,
    start: dt.date,
    end: dt.date,
    url: str = DEFAULT_URL,
    refresh: bool = False,
    http_get: TextGet = _default_get,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Series, SourceInfo]:
    return cached_series(
        paths,
        "cboe",
        "VIX",
        start,
        end,
        url_for_log=url,
        download=lambda: parse_vix_history(http_get(url)),
        refresh=refresh,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
    )


def _valid(v: float | None) -> bool:
    return v is not None and not math.isnan(v)


def resolve_vix(
    day: dt.date,
    fred_values: Mapping[dt.date, float | None],
    cboe_values: Mapping[dt.date, float | None] | None,
) -> tuple[float | None, list[str]]:
    """按 SPEC 5.6 第7条取某日 VIX：先 FRED VIXCLS，缺失时用 Cboe CLOSE；两者都缺才返回 None。

    两个来源都有值但不一致时报告差异，以 FRED 为准。
    """
    notes: list[str] = []
    f = fred_values.get(day)
    c = cboe_values.get(day) if cboe_values is not None else None
    if _valid(f):
        if _valid(c) and round(f, VIX_DECIMALS) != round(c, VIX_DECIMALS):  # type: ignore[arg-type]
            notes.append(f"VIX {day}：FRED VIXCLS={f} 与 Cboe CLOSE={c} 不一致，以 FRED 为准")
        return f, notes
    if _valid(c):
        notes.append(f"VIX {day}：FRED VIXCLS 缺失，改用 Cboe 官方 VIX_History.csv 的 CLOSE={c}")
        return c, notes
    src = "FRED VIXCLS 与 Cboe CLOSE" if cboe_values is not None else "FRED VIXCLS（Cboe 未取得）"
    notes.append(f"VIX {day}：{src} 均缺失，记待补")
    return None, notes
