"""资料管理与复核记录（docs/STORAGE.md 第2、4、7节）。

- 资料文件复制到 data/materials/<对象>/<年>/<日期>/，索引写入 data/materials/index.csv；
- 复核记录写入 data/manual/reviews.csv。
两者都是数据库 materials、reviews 表的来源（可由文件重建）。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from market_risk.storage.paths import StoragePaths, normalize_subject

MATERIAL_FIELDS = ["subject", "base_date", "type", "file_path", "source", "note", "added_at"]
REVIEW_FIELDS = ["subject", "base_date", "reviewer", "category", "content", "impact", "run_key",
                 "other_run_key", "created_at"]
MATERIAL_TYPES = ("tiger_ai_background", "chatgpt_response", "claude_review", "notes", "screenshot", "other")


class MaterialError(ValueError):
    pass


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _now(now: dt.datetime | None) -> str:
    return (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")


def add_material(
    paths: StoragePaths,
    subject: str,
    base_date: dt.date,
    kind: str,
    source_file: Path,
    source: str = "",
    note: str = "",
    now: dt.datetime | None = None,
) -> Path:
    """复制资料文件并登记索引。同名文件内容不同时加后缀，不覆盖。"""
    if kind not in MATERIAL_TYPES:
        raise MaterialError(f"资料类型应为 {MATERIAL_TYPES}")
    if not source_file.is_file():
        raise MaterialError(f"文件不存在：{source_file}")
    target_dir = paths.materials_dir(subject, base_date)
    if kind == "screenshot":
        target_dir = target_dir / "screenshots"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source_file.name
    data = source_file.read_bytes()
    n = 2
    while target.exists() and hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest():
        target = target_dir / f"{source_file.stem}_{n}{source_file.suffix}"
        n += 1
    if not target.exists():
        shutil.copyfile(source_file, target)
    rel = target.relative_to(paths.root).as_posix()
    rows = [r for r in _read(paths.materials_index_csv) if r["file_path"] != rel]
    rows.append({"subject": normalize_subject(subject), "base_date": base_date.isoformat(), "type": kind,
                 "file_path": rel, "source": source, "note": note, "added_at": _now(now)})
    _write(paths.materials_index_csv, MATERIAL_FIELDS, sorted(rows, key=lambda r: (r["base_date"], r["file_path"])))
    return target


def list_materials(
    paths: StoragePaths, subject: str | None = None, start: dt.date | None = None, end: dt.date | None = None
) -> list[dict[str, str]]:
    rows = _read(paths.materials_index_csv)
    s = normalize_subject(subject) if subject else None
    return [
        r for r in rows
        if (s is None or r["subject"] == s)
        and (start is None or r["base_date"] >= start.isoformat())
        and (end is None or r["base_date"] <= end.isoformat())
    ]


@dataclass(frozen=True)
class Review:
    subject: str
    base_date: dt.date | None      # None 表示不针对某个样本（如"全部"）
    reviewer: str          # chatgpt / claude / user / program / legacy_changelog
    category: str
    content: str
    impact: str = ""
    run_key: str = ""
    other_run_key: str = ""


def add_reviews(
    paths: StoragePaths,
    reviews: list[Review],
    now: dt.datetime | None = None,
    replace: Callable[[dict[str, str]], bool] | None = None,
) -> None:
    """追加复核记录。replace 不为空时，先删除满足条件的旧记录（重复导入时不重复）。"""
    rows = [r for r in _read(paths.reviews_csv) if replace is None or not replace(r)]
    ts = _now(now)
    for r in reviews:
        rows.append({"subject": normalize_subject(r.subject),
                     "base_date": r.base_date.isoformat() if r.base_date else "",
                     "reviewer": r.reviewer, "category": r.category, "content": r.content, "impact": r.impact,
                     "run_key": r.run_key, "other_run_key": r.other_run_key, "created_at": ts})
    _write(paths.reviews_csv, REVIEW_FIELDS, rows)


def read_reviews(paths: StoragePaths) -> list[dict[str, str]]:
    return _read(paths.reviews_csv)
