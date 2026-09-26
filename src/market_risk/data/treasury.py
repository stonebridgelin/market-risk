"""财政部每日收益率（SPEC 6.4）：Daily Treasury Par Yield Curve Rates 的 10 Yr 列。

下载 URL（2026-09-26 已联网确认可用，按年份导出 CSV）：
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
  daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve
  &field_tdr_date_value={year}&page&_format=csv
CSV 列：Date（MM/DD/YYYY）、"1 Mo" … "10 Yr" … "30 Yr"，按日期降序。

备用源：FRED DGS10（数值与财政部相同）。主源失败时使用，并在说明中标注。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Callable
from typing import Any

from market_risk.data import fred
from market_risk.data.cache import (
    DataFetchError,
    Series,
    cached_series,
    http_get_text,
)
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
TEN_YEAR_COLUMN = "10 Yr"

TextGet = Callable[[str], str]


def _default_get(url: str) -> str:  # pragma: no cover - 网络请求
    return http_get_text(url)


def parse_treasury_csv(text: str) -> Series:
    """解析财政部年度 CSV，只取 Date 与 10 Yr；空值为 None。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None or "Date" not in reader.fieldnames \
            or TEN_YEAR_COLUMN not in reader.fieldnames:
        raise DataFetchError(f"财政部 CSV 缺少 Date 或 {TEN_YEAR_COLUMN} 列：{reader.fieldnames}")
    result: Series = {}
    for row in reader:
        d = dt.datetime.strptime(row["Date"].strip(), "%m/%d/%Y").date()
        v = (row[TEN_YEAR_COLUMN] or "").strip()
        result[d] = float(v) if v else None
    return result


def fetch_treasury_year(
    paths: StoragePaths,
    year: int,
    end: dt.date,
    refresh: bool = False,
    http_get: TextGet = _default_get,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Series, SourceInfo]:
    """取得某年 1月1日 至 min(12月31日, end) 的财政部 10 年期数值。"""
    start = dt.date(year, 1, 1)
    stop = min(dt.date(year, 12, 31), end)
    url = TREASURY_CSV_URL.format(year=year)
    return cached_series(
        paths,
        "treasury",
        "10Y",
        start,
        stop,
        url_for_log=url,
        download=lambda: parse_treasury_csv(http_get(url)),
        refresh=refresh,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
    )


def fetch_ten_year(
    paths: StoragePaths,
    start: dt.date,
    end: dt.date,
    api_key: str | None,
    refresh: bool = False,
    http_get: TextGet = _default_get,
    fred_get: Any = None,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Series, list[SourceInfo], list[str]]:
    """取得 [start, end] 的 10 年期收益率。返回 (数值, 来源信息, 说明)。

    主源（财政部）任一年份失败时，整段改用 FRED DGS10，并在说明中标注。
    """
    notes: list[str] = []
    infos: list[SourceInfo] = []
    combined: Series = {}
    try:
        for year in range(start.year, end.year + 1):
            values, info = fetch_treasury_year(
                paths, year, end, refresh, http_get, max_retries, backoff_seconds
            )
            combined.update(values)
            infos.append(info)
    except DataFetchError as exc:
        if not api_key:
            raise
        notes.append(f"财政部收益率主源获取失败（{exc}），改用备用源 FRED DGS10")
        kwargs: dict[str, Any] = {"refresh": refresh, "max_retries": max_retries,
                                  "backoff_seconds": backoff_seconds}
        if fred_get is not None:
            kwargs["http_get"] = fred_get
        values, info = fred.fetch_series(paths, "DGS10", start, end, api_key, **kwargs)
        return {d: v for d, v in values.items() if v is not None}, [info], notes
    result = {d: v for d, v in combined.items() if start <= d <= end and v is not None}
    return result, infos, notes
