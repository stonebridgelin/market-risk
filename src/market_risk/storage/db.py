"""SQLite 统计数据库（docs/STORAGE.md 第4节）。

数据库内容全部可由文件重建（rebuild）：
- results/**/run_*/（meta.json、scores.json、snapshot.json 或 metrics.json）与 official.json → runs、dimension_scores、
  totals、metrics、near_threshold；
- data/manual/outcomes.csv → outcomes；data/materials/index.csv → materials；data/manual/reviews.csv → reviews。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from market_risk.metrics import metrics_from_snapshot_json
from market_risk.storage.paths import RUN_FILES, StoragePaths
from market_risk.storage.runs import read_json

SCHEMA = """
CREATE TABLE runs (
    run_key TEXT PRIMARY KEY, run_id TEXT, subject TEXT, framework TEXT, base_date TEXT, mode TEXT,
    created_at_utc TEXT, created_at_local TEXT, git_commit TEXT, git_dirty INTEGER,
    data_source_type TEXT, status TEXT, run_dir TEXT, is_official INTEGER, reviewed INTEGER,
    official_set_by TEXT
);
CREATE TABLE dimension_scores (
    run_key TEXT, version TEXT, dimension TEXT, score INTEGER, possible_scores TEXT,
    triggered_conditions TEXT, pending_reason TEXT, calculation TEXT
);
CREATE TABLE totals (
    run_key TEXT, version TEXT, total INTEGER, total_min INTEGER, total_max INTEGER, stage TEXT,
    clear_deterioration TEXT, alert TEXT, review_flags TEXT, notes TEXT
);
CREATE TABLE metrics (run_key TEXT, key TEXT, value REAL, text TEXT);
CREATE TABLE near_threshold (run_key TEXT, item TEXT, value REAL, threshold REAL, gap REAL, unit TEXT);
CREATE TABLE outcomes (
    subject TEXT, base_date TEXT, window_start TEXT, window_end TEXT, spx_min_close_drawdown REAL,
    qqq_min_close_drawdown REAL, is_event TEXT, event_date TEXT, source TEXT, entered_at TEXT
);
CREATE TABLE materials (
    subject TEXT, base_date TEXT, type TEXT, file_path TEXT, source TEXT, note TEXT, added_at TEXT
);
CREATE TABLE reviews (
    subject TEXT, base_date TEXT, reviewer TEXT, category TEXT, content TEXT, impact TEXT,
    run_key TEXT, other_run_key TEXT, created_at TEXT
);
CREATE UNIQUE INDEX one_official ON runs(subject, framework, base_date) WHERE is_official = 1;
"""
TABLES = ("runs", "dimension_scores", "totals", "metrics", "near_threshold", "outcomes", "materials", "reviews")
DIMENSION_KEYS = ("price", "breadth", "vix", "rates", "credit")


def run_key(meta: dict[str, Any]) -> str:
    return f"{meta['subject']}/{meta['framework']}/{meta['base_date']}/{meta['run_id']}"


def alert_status(total: int | None, lo: int, hi: int) -> str:
    """SOP 9.4：总分下限≥3 视为预警；上限<3 视为未预警；否则状态未知。"""
    if total is not None:
        return "是" if total >= 3 else "否"
    if lo >= 3:
        return "是"
    return "否" if hi < 3 else "未知"


def iter_run_dirs(paths: StoragePaths) -> Iterator[Path]:
    root = paths.results_root
    if not root.exists():
        return
    for meta in sorted(root.glob("**/run_*/meta.json")):
        yield meta.parent


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _insert_run(conn: sqlite3.Connection, paths: StoragePaths, run_dir: Path) -> None:
    meta = read_json(run_dir / RUN_FILES["meta"])
    key = run_key(meta)
    official = run_dir.parent / "official.json"
    pointer = read_json(official) if official.exists() else None
    is_official = bool(pointer and pointer.get("run_id") == meta["run_id"])
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, meta["run_id"], meta["subject"], meta["framework"], meta["base_date"], meta.get("mode"),
         meta.get("created_at_utc"), meta.get("created_at_local"), meta.get("git_commit"),
         None if meta.get("git_dirty") is None else int(bool(meta["git_dirty"])),
         meta.get("data_source_type"), meta.get("status"), run_dir.relative_to(paths.root).as_posix(),
         int(is_official), int(bool(is_official and pointer and pointer.get("reviewed"))),
         pointer.get("set_by") if is_official and pointer else None),
    )
    scores_path = run_dir / RUN_FILES["scores"]
    if scores_path.exists():
        scores = read_json(scores_path)
        for r in scores.get("results", []):
            for dk in DIMENSION_KEYS:
                d = r[dk]
                conn.execute(
                    "INSERT INTO dimension_scores VALUES (?,?,?,?,?,?,?,?)",
                    (key, r["version"], d["name"], d["score"], json.dumps(d["possible_scores"]),
                     json.dumps(d["triggered_conditions"], ensure_ascii=False), d.get("pending_reason"),
                     d.get("calculation")),
                )
            lo, hi = r["total_range"]
            conn.execute(
                "INSERT INTO totals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, r["version"], r["total"], lo, hi, r["stage"],
                 dict(r["clear_deterioration"]).get("大盘明确恶化"), alert_status(r["total"], lo, hi),
                 json.dumps(r.get("review_flags", []), ensure_ascii=False),
                 json.dumps(r.get("notes", []), ensure_ascii=False)),
            )
        for n in scores.get("near_threshold", []):
            conn.execute("INSERT INTO near_threshold VALUES (?,?,?,?,?,?)",
                         (key, n["item"], n.get("value"), n.get("threshold"), n.get("gap"), n.get("unit")))
    metrics_path, snap_path = run_dir / RUN_FILES["metrics"], run_dir / RUN_FILES["snapshot"]
    if metrics_path.exists():
        metrics = read_json(metrics_path)
    elif snap_path.exists():
        metrics = metrics_from_snapshot_json(read_json(snap_path))
    else:
        metrics = {}
    for k, v in sorted(metrics.items()):
        if isinstance(v, int | float) or v is None:
            conn.execute("INSERT INTO metrics VALUES (?,?,?,?)", (key, k, v, None))
        else:
            conn.execute("INSERT INTO metrics VALUES (?,?,?,?)", (key, k, None, str(v)))


def rebuild(paths: StoragePaths, db_path: Path | None = None) -> Path:
    """删除并重建数据库（STORAGE 第4节 rebuild-db）。"""
    db_path = db_path or paths.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for run_dir in iter_run_dirs(paths):
            _insert_run(conn, paths, run_dir)
        for r in _csv_rows(paths.outcomes_csv):
            conn.execute("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (r["subject"], r["base_date"], r["window_start"], r["window_end"],
                          _float(r["spx_min_close_drawdown"]), _float(r["qqq_min_close_drawdown"]),
                          r["is_event"], r.get("event_date") or None, r["source"], r["entered_at"]))
        for r in _csv_rows(paths.materials_index_csv):
            conn.execute("INSERT INTO materials VALUES (?,?,?,?,?,?,?)",
                         (r["subject"], r["base_date"], r["type"], r["file_path"], r["source"], r["note"],
                          r["added_at"]))
        for r in _csv_rows(paths.reviews_csv):
            conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["subject"], r["base_date"], r["reviewer"], r["category"], r["content"],
                          r["impact"], r.get("run_key") or None, r.get("other_run_key") or None, r["created_at"]))
        conn.commit()
    finally:
        conn.close()
    return db_path


def _float(v: str | None) -> float | None:
    return float(v) if v not in (None, "") else None


def dump(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """全部表的内容（排序后），用于比较两次重建是否一致。"""
    conn = sqlite3.connect(db_path)
    try:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TABLES}
    finally:
        conn.close()


def query(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()
