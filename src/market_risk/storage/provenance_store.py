"""数据留痕的读写（docs/research/数据留痕设计说明.md）：只追加的记录文件、原始快照与数据库行。

以 data/manual/provenance/ 下的文件为准：records.csv 与 confirmations.csv 只追加、不改写已有的行；
数据库的三张表由 rebuild-db 从这些文件重建。本模块不接入任何评分、信号或研究计算。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import shutil
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_risk.provenance import Confirmation, ProvenanceError, ProvenanceRecord, Snapshot
from market_risk.storage.paths import StoragePaths

RECORD_FIELDS = (
    "record_id", "indicator", "trade_date", "raw_value", "raw_unit", "raw_basis", "raw_precision",
    "normalized_value", "source", "acquisition_method", "first_obtained_at_et", "entered_at_utc", "entered_by",
    "is_late", "source_published_at", "snapshot_path", "snapshot_sha256", "data_version", "code_version",
    "code_dirty", "revises_record_id", "revision_kind", "correction_original_value", "correction_corrected_value",
    "correction_evidence", "historical_backfill")
CONFIRMATION_FIELDS = ("record_id", "confirmed_by", "confirmed_at_utc", "self_confirmed")
SIGNAL_INPUT_FIELDS = ("signal_key", "record_id", "note")
YES, NO = "是", "否"


def _flag(value: bool | None) -> str:
    return "" if value is None else YES if value else NO


def _bool(text: str) -> bool | None:
    if text == "":
        return None
    if text not in (YES, NO):
        raise ProvenanceError(f"留痕文件里的是否字段不合法：{text!r}")
    return text == YES


def _time(value: dt.datetime | None) -> str:
    return "" if value is None else value.isoformat(timespec="seconds")


def _parse_time(text: str) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


def record_row(record: ProvenanceRecord) -> dict[str, str]:
    """记录 → 文件里的一行（全部为文本；空值写空串）。"""
    return {
        "record_id": record.record_id, "indicator": record.indicator, "trade_date": record.trade_date.isoformat(),
        "raw_value": record.raw_value, "raw_unit": record.raw_unit, "raw_basis": record.raw_basis,
        "raw_precision": str(record.raw_precision), "normalized_value": str(record.normalized_value),
        "source": record.source, "acquisition_method": record.acquisition_method,
        "first_obtained_at_et": _time(record.first_obtained_at_et),
        "entered_at_utc": _time(record.entered_at_utc), "entered_by": record.entered_by,
        "is_late": _flag(record.is_late), "source_published_at": _time(record.source_published_at),
        "snapshot_path": record.snapshot_path or "", "snapshot_sha256": record.snapshot_sha256 or "",
        "data_version": record.data_version or "", "code_version": record.code_version,
        "code_dirty": _flag(record.code_dirty), "revises_record_id": record.revises_record_id or "",
        "revision_kind": record.revision_kind or "",
        "correction_original_value": record.correction_original_value or "",
        "correction_corrected_value": record.correction_corrected_value or "",
        "correction_evidence": record.correction_evidence or "",
        "historical_backfill": _flag(record.historical_backfill),
    }


def record_from_row(row: dict[str, str]) -> ProvenanceRecord:
    entered = _parse_time(row["entered_at_utc"])
    backfill = _bool(row["historical_backfill"])
    if entered is None or backfill is None:
        raise ProvenanceError(f"{row['record_id']} 缺少录入时间或历史补录标记")
    return ProvenanceRecord(
        row["record_id"], row["indicator"], dt.date.fromisoformat(row["trade_date"]), row["raw_value"],
        row["raw_unit"], row["raw_basis"], int(row["raw_precision"]), Decimal(row["normalized_value"]),
        row["source"], row["acquisition_method"], _parse_time(row["first_obtained_at_et"]), entered,
        row["entered_by"], _bool(row["is_late"]), _parse_time(row["source_published_at"]),
        row["snapshot_path"] or None, row["snapshot_sha256"] or None, row["data_version"] or None,
        row["code_version"], _bool(row["code_dirty"]), row["revises_record_id"] or None,
        row["revision_kind"] or None, row["correction_original_value"] or None,
        row["correction_corrected_value"] or None, row["correction_evidence"] or None, backfill)


def _read(path: Path, fields: Sequence[str]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise ProvenanceError(f"留痕文件的表头与设计不一致：{path.name}")
        return list(reader)


def _append(path: Path, fields: Sequence[str], row: dict[str, str]) -> None:
    """只追加一行；文件不存在时先写表头。已有的行不读入、不改写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(fields), lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerow(row)


def read_records(paths: StoragePaths) -> tuple[ProvenanceRecord, ...]:
    records = tuple(record_from_row(row) for row in _read(paths.provenance_records_csv, RECORD_FIELDS))
    if len({record.record_id for record in records}) != len(records):
        raise ProvenanceError("留痕记录文件里有重复的记录编号")
    return records


def append_record(paths: StoragePaths, record: ProvenanceRecord) -> None:
    _append(paths.provenance_records_csv, RECORD_FIELDS, record_row(record))


def read_confirmations(paths: StoragePaths) -> tuple[Confirmation, ...]:
    result = []
    for row in _read(paths.provenance_confirmations_csv, CONFIRMATION_FIELDS):
        at, own = _parse_time(row["confirmed_at_utc"]), _bool(row["self_confirmed"])
        if at is None or own is None:
            raise ProvenanceError(f"{row['record_id']} 的确认记录缺少时间或自确认标记")
        result.append(Confirmation(row["record_id"], row["confirmed_by"], at, own))
    return tuple(result)


def append_confirmation(paths: StoragePaths, item: Confirmation) -> None:
    _append(paths.provenance_confirmations_csv, CONFIRMATION_FIELDS,
            {"record_id": item.record_id, "confirmed_by": item.confirmed_by,
             "confirmed_at_utc": _time(item.confirmed_at_utc), "self_confirmed": _flag(item.self_confirmed)})


# ---------------------------------------------------------------------------
# 原始快照
# ---------------------------------------------------------------------------

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def store_snapshot(paths: StoragePaths, indicator: str, trade_date: dt.date, source: Path) -> Snapshot:
    """把原始快照文件复制到 snapshots/<指标>/<交易日>/<SHA-256 前 12 位>_<原文件名>，返回相对路径与 SHA-256。

    目标已存在且内容相同时直接沿用；不覆盖任何已有的文件。
    """
    if not source.is_file():
        raise ProvenanceError(f"原始快照文件不存在：{source}")
    digest = file_sha256(source)
    target = paths.provenance_snapshot_dir(indicator, trade_date) / f"{digest[:12]}_{source.name}"
    if target.exists():
        if file_sha256(target) != digest:
            raise ProvenanceError(f"已有的快照文件与其文件名里的哈希不符：{target.name}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return Snapshot(target.relative_to(paths.root).as_posix(), digest)


def snapshot_problems(paths: StoragePaths, records: Sequence[ProvenanceRecord]) -> list[str]:
    """原始快照哈希核对：逐条重算快照文件的 SHA-256，与记录比较。返回问题清单（空即全部一致）。"""
    problems = []
    for record in records:
        if record.snapshot_path is None:
            continue
        target = paths.root / record.snapshot_path
        if not target.is_file():
            problems.append(f"{record.record_id}：快照文件不存在（{record.snapshot_path}）")
        elif file_sha256(target) != record.snapshot_sha256:
            problems.append(f"{record.record_id}：快照文件的 SHA-256 与记录不符（{record.snapshot_path}）")
    return problems


# ---------------------------------------------------------------------------
# 数据库行（rebuild-db 用）
# ---------------------------------------------------------------------------

def database_rows(paths: StoragePaths) -> dict[str, list[dict[str, Any]]]:
    """三张表的全部行，由文件生成。信号输入关联表在本批没有来源文件时为空。"""
    records = read_records(paths)
    known = {record.record_id for record in records}
    record_rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = dict(record_row(record))
        row.update(trade_date=record.trade_date, raw_precision=record.raw_precision,
                   normalized_value=record.normalized_value, is_late=record.is_late, code_dirty=record.code_dirty,
                   historical_backfill=record.historical_backfill)
        record_rows.append({key: (None if value == "" else value) for key, value in row.items()})
    confirmations = read_confirmations(paths)
    links = _read(paths.provenance_signal_inputs_csv, SIGNAL_INPUT_FIELDS)
    for name, ids in (("确认记录", [item.record_id for item in confirmations]),
                      ("修订关系", [record.revises_record_id for record in records if record.revises_record_id]),
                      ("信号输入关联", [row["record_id"] for row in links])):
        missing = sorted(set(ids) - known)
        if missing:
            raise ProvenanceError(f"{name}指向不存在的记录：{'、'.join(missing)}")
    return {
        "provenance_records": record_rows,
        "provenance_confirmations": [
            {"record_id": item.record_id, "confirmed_by": item.confirmed_by,
             "confirmed_at_utc": _time(item.confirmed_at_utc), "self_confirmed": item.self_confirmed}
            for item in confirmations],
        "signal_input_links": [{"signal_key": row["signal_key"], "record_id": row["record_id"],
                                "note": row["note"] or None} for row in links],
    }
