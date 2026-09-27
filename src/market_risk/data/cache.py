"""下载缓存与网络重试（SPEC 6.1）。

- 缓存文件位置由 storage/paths.py 决定：data/cache/<来源>/<代码>_<起>_<止>.<后缀>，
  旁边的 .meta.json 记录来源 URL（不含密钥）、下载时间、行数、数据起止日期。
- 默认优先使用缓存；refresh=True 强制重新下载。
- 请求区间的终点不早于"今天（美东）"时不使用缓存，避免当天数据未发布时的缓存被一直沿用。
- 网络失败重试（指数退避），仍失败则抛出 DataFetchError。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from market_risk.models import SourceInfo
from market_risk.storage.paths import StoragePaths

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (market-risk research; +https://github.com/stonebridgelin/market-risk)"


class DataFetchError(RuntimeError):
    """数据获取失败（重试后仍失败，或返回内容不合法）。"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def today_new_york(now: dt.datetime | None = None) -> dt.date:
    return (now or utc_now()).astimezone(NEW_YORK).date()


def redact_url(url: str, params: dict[str, Any] | None = None) -> str:
    """生成用于记录的 URL：去掉 api_key 等密钥参数。"""
    if not params:
        return url
    safe = {k: v for k, v in params.items() if k.lower() not in {"api_key", "apikey", "key"}}
    query = "&".join(f"{k}={v}" for k, v in safe.items())
    return f"{url}?{query}" if query else url


_SECRET_PARAM = re.compile(r"(?i)\b(api_key|apikey|key|token)=[^&\s'\")]+")


def redact_text(text: str) -> str:
    """把文字中 URL 查询参数里的密钥替换为 ***（错误信息、日志、data_notes 都经过此函数）。"""
    return _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", text)


def with_retry[T](
    func: Callable[[], T],
    what: str,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """执行 func，失败后按 backoff × 2^n 秒退避重试 max_retries 次。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_seconds * (2**attempt)
                logger.warning("%s 失败（第%d次）：%s；%.1f 秒后重试", what, attempt + 1, redact_text(str(exc)), wait)
                sleep(wait)
    raise DataFetchError(redact_text(f"{what} 在重试 {max_retries} 次后仍失败：{last_exc}")) from None


def http_get_text(
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> str:  # pragma: no cover - 网络请求
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if not resp.ok:  # 不用 raise_for_status：其错误信息含带密钥的完整 URL
        raise DataFetchError(f"HTTP {resp.status_code} {resp.reason}：{redact_text(resp.url)}")
    return resp.text


def cache_usable(
    meta: dict[str, Any], request_end: dt.date, now: dt.datetime | None = None
) -> bool:
    """请求终点早于今天（美东）时缓存可用；否则当天数据可能尚未发布，需重新下载。"""
    return request_end < today_new_york(now) and "downloaded_at_utc" in meta


def read_cache(data_file: Path) -> tuple[str, dict[str, Any]] | None:
    meta_file = StoragePaths.cache_meta_file(data_file)
    if not data_file.exists() or not meta_file.exists():
        return None
    return data_file.read_text(encoding="utf-8"), json.loads(meta_file.read_text(encoding="utf-8"))


def write_cache(data_file: Path, text: str, info: SourceInfo) -> None:
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(text, encoding="utf-8")
    meta = asdict(info)
    for k in ("data_start", "data_end"):
        if meta[k] is not None:
            meta[k] = meta[k].isoformat()
    StoragePaths.cache_meta_file(data_file).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


Series = dict[dt.date, float | None]


def series_to_csv(series: Series, value_name: str = "value") -> str:
    """统一的缓存格式：date,value；缺失值留空（不写 0）。"""
    lines = [f"date,{value_name}"]
    for d in sorted(series):
        v = series[d]
        lines.append(f"{d.isoformat()},{'' if v is None else repr(float(v))}")
    return "\n".join(lines) + "\n"


def series_from_csv(text: str) -> Series:
    result: Series = {}
    rows = text.strip().splitlines()
    for line in rows[1:]:
        if not line.strip():
            continue
        d_str, v_str = line.split(",", 1)
        result[dt.date.fromisoformat(d_str)] = float(v_str) if v_str.strip() else None
    return result


def cached_series(
    paths: StoragePaths,
    source: str,
    key: str,
    start: dt.date,
    end: dt.date,
    url_for_log: str,
    download: Callable[[], Series],
    refresh: bool = False,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
    now: dt.datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Series, SourceInfo]:
    """取得 [start, end] 的逐日序列：优先读缓存，否则下载（含重试）并写入缓存。"""
    data_file = paths.cache_file(source, key, start, end)
    if not refresh:
        cached = read_cache(data_file)
        if cached is not None and cache_usable(cached[1], end, now):
            logger.info("使用缓存：%s", data_file)
            return series_from_csv(cached[0]), source_info_from_meta(cached[1])

    series = with_retry(
        download, f"下载 {source}:{key}", max_retries, backoff_seconds, sleep=sleep
    )
    series = {d: v for d, v in series.items() if start <= d <= end}
    if not series:
        raise DataFetchError(f"{source}:{key} 在 {start} 至 {end} 没有返回任何数据")
    info = SourceInfo(
        source=source,
        key=key,
        url=url_for_log,
        downloaded_at_utc=(now or utc_now()).astimezone(dt.UTC).isoformat(timespec="seconds"),
        rows=len(series),
        data_start=min(series),
        data_end=max(series),
        from_cache=False,
        cache_file=_relative(data_file, paths.root),
    )
    write_cache(data_file, series_to_csv(series), info)
    return series, info


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def source_info_from_meta(meta: dict[str, Any]) -> SourceInfo:
    def _d(v: str | None) -> dt.date | None:
        return dt.date.fromisoformat(v) if v else None

    return SourceInfo(
        source=meta["source"],
        key=meta["key"],
        url=meta["url"],
        downloaded_at_utc=meta["downloaded_at_utc"],
        rows=int(meta["rows"]),
        data_start=_d(meta.get("data_start")),
        data_end=_d(meta.get("data_end")),
        from_cache=True,
        cache_file=meta["cache_file"],
    )
