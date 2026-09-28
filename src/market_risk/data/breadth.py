"""手工录入的广度数据（SPEC 6.5）：data/manual/breadth.csv，列为 date,s5fi,s5tw,note。

读取时校验：数值在 0–100 之间；同一日期不能有两条记录。
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from market_risk.models import BreadthReading
from market_risk.storage.paths import StoragePaths

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


# ---------------------------------------------------------------------------
# 读取顺序（SPEC 6.5 / TRADINGVIEW 6.1）：先 TradingView 导出数据，再手工录入
# ---------------------------------------------------------------------------

TV_TOLERANCE = 0.005


def merge_breadth(
    tv_s5fi: dict[dt.date, float],
    tv_s5tw: dict[dt.date, float],
    manual: dict[dt.date, BreadthReading],
) -> tuple[dict[dt.date, BreadthReading], dict[dt.date, str]]:
    """合并两个来源。同一日期都有数值但不一致时记录差异，以 TradingView 为准。

    TradingView 只有 S5FI、S5TW 其中之一的日期保留已知项，另一项为 None（审查 H-05，2026-09-28 负责人确认）。
    该日期若同时有手工录入，两个来源不混合拼成一条读数，报错要求人工裁定。
    返回 (读数, {日期: 差异说明})。

    S5FI、S5TW 与前一交易日相同属离散取值下的偶然（TRADINGVIEW 7.2），不标注疑似陈旧值（stale_fields 为空）。
    """
    result: dict[dt.date, BreadthReading] = dict(manual)
    conflicts: dict[dt.date, str] = {}
    for d in sorted(set(tv_s5fi) ^ set(tv_s5tw)):
        if d in manual:
            raise BreadthError(f"{d}：TradingView 只有 S5FI、S5TW 其中一项，同日又有手工录入；"
                               "两个来源不混合，请人工裁定")
        f = _check_value("S5FI", tv_s5fi[d], d) if d in tv_s5fi else None
        w = _check_value("S5TW", tv_s5tw[d], d) if d in tv_s5tw else None
        result[d] = BreadthReading(d, f, w, "tradingview", "TradingView 导出（只有一项）")
    for d in sorted(set(tv_s5fi) & set(tv_s5tw)):
        tv = make_reading(d, tv_s5fi[d], tv_s5tw[d], "TradingView 导出")
        tv = BreadthReading(tv.date, tv.s5fi, tv.s5tw, "tradingview", tv.note)
        old = manual.get(d)
        if old is not None and (abs(old.s5fi - tv.s5fi) > TV_TOLERANCE or abs(old.s5tw - tv.s5tw) > TV_TOLERANCE):
            conflicts[d] = (
                f"广度 {d}：TradingView 导出 S5FI={tv.s5fi}、S5TW={tv.s5tw}，"
                f"手工录入 S5FI={old.s5fi}、S5TW={old.s5tw}，不一致，以 TradingView 为准"
            )
        result[d] = tv
    return result, conflicts


def load_breadth(paths: StoragePaths) -> tuple[dict[dt.date, BreadthReading], dict[dt.date, str]]:
    """按读取顺序加载全部广度读数：TradingView 清洗结果（S5FI.csv、S5TW.csv）优先，手工录入补充。"""
    from market_risk.data.tradingview import read_processed

    return merge_breadth(read_processed(paths, "S5FI"), read_processed(paths, "S5TW"),
                         read_breadth(paths.breadth_csv))
