"""v1.4 验证期所需的 Cboe 官方 VIX3M 副本：只保存 2009-09-18 至 2022-12-30，不读取或保存其后的行。

做法同 v1.2.1 的开发期副本（data.py）：流式读取官方 CSV，遇到截止日之后的第一行即停止。
另核对 2016-12-30 及以前的部分与已入库的开发期副本逐行相同，不同则报错、不写文件。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.request
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from market_risk.wavewarn.data import DEVELOPMENT_END, FIRST_DATE, VIX3M_URL, CboeSubset

HEADER = "DATE,OPEN,HIGH,LOW,CLOSE"


def parse_cboe_until(lines: Iterable[bytes], end: dt.date) -> CboeSubset:
    """只解析官方 CSV 中至 end 的行；遇到更晚日期立即停止迭代，其后的内容不再读取。"""
    source = iter(lines)
    header = next(source).decode("utf-8-sig").strip()
    if header != HEADER:
        raise ValueError(f"Cboe VIX3M 表头不符：{header}")
    rows: list[tuple[dt.date, str]] = []
    for raw in source:
        fields = raw.decode("utf-8").strip().split(",")
        day = dt.datetime.strptime(fields[0], "%m/%d/%Y").date()
        if rows and day <= rows[-1][0]:
            raise ValueError("Cboe VIX3M 日期未严格升序")
        if day > end:
            break
        if len(fields) != 5 or Decimal(fields[4]) <= 0:
            raise ValueError(f"Cboe VIX3M 行格式或收盘值无效：{day}")
        rows.append((day, fields[4]))
    if not rows or rows[0][0] != FIRST_DATE:
        raise ValueError("Cboe VIX3M 起始日与规格不符")
    return CboeSubset(tuple(rows), rows[0][0], rows[-1][0])


def subset_text(subset: CboeSubset) -> str:
    return "date,close\n" + "".join(f"{day.isoformat()},{close}\n" for day, close in subset.rows)


def development_prefix_matches(subset: CboeSubset, development_text: str) -> bool:
    """新副本中 2016-12-30 及以前的部分须与开发期副本逐行相同。"""
    prefix = CboeSubset(tuple(row for row in subset.rows if row[0] <= DEVELOPMENT_END), subset.first,
                        DEVELOPMENT_END)
    return subset_text(prefix) == development_text.replace("\r\n", "\n")


def fetch_cboe_validation(destination: Path, development_copy: Path, end: dt.date,
                          fetched_at: dt.datetime) -> dict[str, object]:
    """下载并保存截至 end 的官方收盘价与来源信息；目标文件已存在时拒绝覆盖。"""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("获取时间必须包含时区")
    if destination.exists():
        raise ValueError(f"VIX3M 副本已存在，拒绝覆盖：{destination}")
    with urllib.request.urlopen(VIX3M_URL, timeout=30) as response:
        subset = parse_cboe_until(iter(response.readline, b""), end)
    if subset.last != end:
        raise ValueError(f"Cboe VIX3M 最后一行为 {subset.last}，未到截止日 {end}")
    if not development_prefix_matches(subset, development_copy.read_text(encoding="utf-8")):
        raise ValueError("新下载的 VIX3M 在 2016-12-30 及以前与开发期副本不同，需核对历史修订")
    encoded = subset_text(subset).encode("utf-8")
    metadata: dict[str, object] = {
        "source_url": VIX3M_URL, "scope": f"{subset.first.isoformat()} 至 {end.isoformat()}（不含其后的行）",
        "first_date": subset.first.isoformat(), "last_date": subset.last.isoformat(), "rows": len(subset.rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "fetched_at_utc": fetched_at.astimezone(dt.UTC).isoformat(),
        "development_prefix_identical": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    destination.with_suffix(".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
    return metadata
