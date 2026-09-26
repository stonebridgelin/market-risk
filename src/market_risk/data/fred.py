"""FRED / ALFRED（SPEC 6.3）：VIXCLS、BAMLH0A0HYM2、DGS10（财政部备用源）。

- 接口：https://api.stlouisfed.org/fred/series/observations，参数 api_key、file_type=json、
  observation_start、observation_end；ALFRED 版本数据另加 realtime_start / realtime_end。
- 值为 "." 表示缺失，转换为 None，不得当作 0；这类行不算观测（SPEC 5.6 第8条）。
- 自2026年4月起 FRED 只提供 ICE 系列最近三年的观测，更早的日期取不到时给出明确提示。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

from market_risk.data.cache import (
    DataFetchError,
    Series,
    cached_series,
    http_get_text,
    redact_url,
)
from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
ICE_SERIES_PREFIX = "BAML"
ICE_HISTORY_NOTE_GAP_DAYS = 7

HttpGet = Callable[[str, dict[str, Any]], str]


def _default_get(url: str, params: dict[str, Any]) -> str:  # pragma: no cover - 网络请求
    return http_get_text(url, params)


def parse_observations(payload: str) -> Series:
    """解析 FRED JSON；"." 转为 None。"""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DataFetchError(f"FRED 返回内容不是 JSON：{payload[:200]}") from exc
    if "observations" not in data:
        msg = data.get("error_message") or str(data)[:200]
        raise DataFetchError(f"FRED 返回错误：{msg}")
    result: Series = {}
    for obs in data["observations"]:
        value = obs["value"].strip()
        result[dt.date.fromisoformat(obs["date"])] = None if value in {".", ""} else float(value)
    return result


def fetch_series(
    paths: StoragePaths,
    series_id: str,
    start: dt.date,
    end: dt.date,
    api_key: str,
    realtime: dt.date | None = None,
    refresh: bool = False,
    http_get: HttpGet = _default_get,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[Series, SourceInfo]:
    """取得 FRED 系列 [start, end] 的观测；realtime 不为空时取该日的 ALFRED 版本。"""
    params: dict[str, Any] = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
    }
    key = series_id
    if realtime is not None:
        params["realtime_start"] = realtime.isoformat()
        params["realtime_end"] = realtime.isoformat()
        key = f"{series_id}_vintage{realtime:%Y%m%d}"

    def _download() -> Series:
        return parse_observations(http_get(FRED_OBSERVATIONS_URL, params))

    return cached_series(
        paths,
        "fred",
        key,
        start,
        end,
        url_for_log=redact_url(FRED_OBSERVATIONS_URL, params),
        download=_download,
        refresh=refresh,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
    )


def ice_history_note(series_id: str, requested_start: dt.date, series: Series) -> str | None:
    """ICE 系列只提供最近三年：返回数据起点明显晚于请求起点时给出提示。"""
    if not series_id.startswith(ICE_SERIES_PREFIX) or not series:
        return None
    first = min(series)
    if (first - requested_start).days > ICE_HISTORY_NOTE_GAP_DAYS:
        return (
            f"{series_id} 请求自 {requested_start} 起，但 FRED 只返回 {first} 之后的观测"
            "（自2026年4月起 FRED 只提供 ICE 系列最近三年的数据，更早的需向 ICE 购买）"
        )
    return None
