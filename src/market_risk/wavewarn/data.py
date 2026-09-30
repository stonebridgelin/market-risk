"""v1.2.1 研究专用 Cboe VIX3M 开发期数据，不触及保留期。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

VIX3M_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv"
FIRST_DATE = dt.date(2009, 9, 18)
DEVELOPMENT_END = dt.date(2016, 12, 30)


@dataclass(frozen=True)
class CboeSubset:
    rows: tuple[tuple[dt.date, str], ...]
    first: dt.date
    last: dt.date


def parse_cboe_development(lines: Iterable[bytes]) -> CboeSubset:
    """只解析官方 CSV 中至开发期末的行，遇到更晚日期立即停止迭代。"""
    source = iter(lines)
    header = next(source).decode("utf-8-sig").strip()
    if header != "DATE,OPEN,HIGH,LOW,CLOSE":
        raise ValueError(f"Cboe VIX3M 表头不符：{header}")
    rows: list[tuple[dt.date, str]] = []
    previous: dt.date | None = None
    for raw in source:
        fields = raw.decode("utf-8").strip().split(",")
        if len(fields) != 5:
            raise ValueError("Cboe VIX3M 行格式不符")
        day = dt.datetime.strptime(fields[0], "%m/%d/%Y").date()
        if previous is not None and day <= previous:
            raise ValueError("Cboe VIX3M 日期未严格升序")
        if day > DEVELOPMENT_END:
            break
        close = Decimal(fields[4])
        if close <= 0:
            raise ValueError(f"Cboe VIX3M 收盘值无效：{day}")
        rows.append((day, fields[4]))
        previous = day
    if not rows or rows[0][0] != FIRST_DATE:
        raise ValueError("Cboe VIX3M 起始日与规格不符")
    return CboeSubset(tuple(rows), rows[0][0], rows[-1][0])


def fetch_cboe_development(destination: Path, fetched_at: dt.datetime) -> dict[str, object]:
    """保存仅含开发期的官方收盘价和来源哈希；源值变化时拒绝覆盖。"""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("获取时间必须包含时区")
    with urllib.request.urlopen(VIX3M_URL, timeout=30) as response:
        subset = parse_cboe_development(iter(response.readline, b""))
    text = "date,close\n" + "".join(f"{day.isoformat()},{close}\n" for day, close in subset.rows)
    encoded = text.encode("utf-8")
    sha = hashlib.sha256(encoded).hexdigest()
    if destination.exists() and destination.read_bytes() != encoded:
        raise ValueError(f"Cboe VIX3M 已存开发期值与新下载值不同，需核对历史修订：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(encoded)
    metadata: dict[str, object] = {"source_url": VIX3M_URL, "scope": "仅开发期",
                                   "first_date": subset.first.isoformat(), "last_date": subset.last.isoformat(),
                                   "rows": len(subset.rows), "sha256": sha,
                                   "fetched_at_utc": fetched_at.astimezone(dt.UTC).isoformat()}
    metadata_path = destination.with_suffix(".meta.json")
    if not metadata_path.exists():
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata
