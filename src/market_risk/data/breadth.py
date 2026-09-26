"""手工录入的广度数据（SPEC 6.5）：data/manual/breadth.csv，列为 date,s5fi,s5tw,note。

读取时校验：数值在 0–100 之间；同一日期不能有两条记录。
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from market_risk.models import BreadthReading

COLUMNS = ["date", "s5fi", "s5tw", "note"]


class BreadthError(ValueError):
    """广度数据不合法。"""


def _check_value(name: str, value: float, d: dt.date) -> float:
    if not 0 <= value <= 100:
        raise BreadthError(f"{d} 的 {name}={value} 不在 0–100 之间（应为百分数，如 58.44）")
    return round(value, 2)


def make_reading(d: dt.date, s5fi: float, s5tw: float, note: str = "") -> BreadthReading:
    return BreadthReading(
        date=d,
        s5fi=_check_value("S5FI", s5fi, d),
        s5tw=_check_value("S5TW", s5tw, d),
        source="manual",
        note=note,
    )


def read_breadth(path: Path) -> dict[dt.date, BreadthReading]:
    """读取广度 CSV；文件不存在时返回空字典。"""
    if not path.exists():
        return {}
    result: dict[dt.date, BreadthReading] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(COLUMNS[:3]) - set(reader.fieldnames or [])
        if missing:
            raise BreadthError(f"{path} 缺少列：{sorted(missing)}")
        for lineno, row in enumerate(reader, start=2):
            try:
                d = dt.date.fromisoformat(row["date"].strip())
                reading = make_reading(
                    d, float(row["s5fi"]), float(row["s5tw"]), (row.get("note") or "").strip()
                )
            except (ValueError, TypeError) as exc:
                if isinstance(exc, BreadthError):
                    raise
                raise BreadthError(f"{path} 第{lineno}行格式错误：{row}") from exc
            if d in result:
                raise BreadthError(f"{path} 中 {d} 有两条记录")
            result[d] = reading
    return result


def write_breadth(path: Path, readings: dict[dt.date, BreadthReading]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(COLUMNS)
        for d in sorted(readings):
            r = readings[d]
            writer.writerow([d.isoformat(), f"{r.s5fi:.2f}", f"{r.s5tw:.2f}", r.note])


def upsert_breadth(path: Path, reading: BreadthReading, overwrite: bool = False) -> bool:
    """写入一条读数。已有同日记录且数值不同时，overwrite=False 则报错。返回是否有变更。"""
    readings = read_breadth(path)
    old = readings.get(reading.date)
    if old is not None:
        if (old.s5fi, old.s5tw) == (reading.s5fi, reading.s5tw):
            return False
        if not overwrite:
            raise BreadthError(
                f"{reading.date} 已有读数 S5FI={old.s5fi}、S5TW={old.s5tw}，"
                f"与新录入的 S5FI={reading.s5fi}、S5TW={reading.s5tw} 不同；确认后再覆盖"
            )
    readings[reading.date] = reading
    write_breadth(path, readings)
    return True
