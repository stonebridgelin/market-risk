"""回测运行目录的文件读写（STORAGE 2.3）：results/MARKET/risk_scoring/backtests/<run_id>/。

- 每次回测一个运行目录，不为每个交易日单独建目录；不覆盖已有目录；
- 正式回测指针 backtests/official.json（仿照单日 official.json）；
- backtests/.gitignore 由程序生成并提交：只放行正式回测的运行目录，其他运行目录保留在本地、不提交；
- outcomes.csv、pullback_episodes.csv、episode_windows.csv 为隔离的标签文件，评分代码不得读取。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from market_risk.storage.paths import BACKTEST_FILES, StoragePaths
from market_risk.storage.runs import to_jsonable

DAILY_SCORE_FIELDS = ["date", "version", "price", "breadth", "vix", "rates", "credit", "total", "total_min",
                      "total_max", "stage", "pending_dimensions", "clear_deterioration", "clear_deterioration_chain",
                      "alert", "flags", "alt_source_scores"]
OUTCOME_FIELDS = ["base_date", "window_start", "window_end", "spx_drawdown_from_base", "qqq_drawdown_from_base",
                  "spx_peak_to_trough_drawdown", "qqq_peak_to_trough_drawdown", "is_event", "event_date",
                  "is_near_event", "period", "crosses_period", "data_note"]
EPISODE_FIELDS = ["symbol", "level", "high_date", "high_close", "low_date", "low_close", "drawdown_pct",
                  "trading_days", "grade", "status", "confirm_date", "recovery_date", "recovery_note", "period",
                  "before_start", "crosses_boundary", "counted"]
WINDOW_FIELDS = ["symbol", "level", "high_date", "date", "offset", "offset_from_trough", "drawdown_from_peak",
                 "decline_progress", "rebound_from_trough", "v2m_total", "v2m_total_min", "v2m_total_max",
                 "v2m_stage", "v3r1_total", "v3r1_total_min", "v3r1_total_max", "v3r1_stage"]


def run_file(paths: StoragePaths, run_id: str, name: str) -> Path:
    return paths.backtest_run_dir(run_id) / BACKTEST_FILES[name]


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if r.get(k) is None else r[k] for k in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_meta(paths: StoragePaths, run_id: str, meta: Mapping[str, Any]) -> None:
    p = run_file(paths, run_id, "meta")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(to_jsonable(dict(meta)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_meta(paths: StoragePaths, run_id: str) -> dict[str, Any]:
    return json.loads(run_file(paths, run_id, "meta").read_text(encoding="utf-8"))


def list_runs(paths: StoragePaths) -> list[str]:
    root = paths.backtests_root
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / BACKTEST_FILES["meta"]).exists())


def read_official(paths: StoragePaths) -> dict[str, Any] | None:
    p = paths.backtest_official
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def gitignore_text(official_run_id: str | None) -> str:
    lines = ["# 由 market-risk backtest official 生成，不得手工修改：只提交正式回测的运行目录，",
             "# 其他回测运行目录保留在本地、不提交（docs/STORAGE.md 2.3）。", "/*/"]
    if official_run_id:
        lines.append(f"!/{official_run_id}/")
    return "\n".join(lines) + "\n"


def set_official(paths: StoragePaths, run_id: str, set_by: str = "manual", now: dt.datetime | None = None
                 ) -> dict[str, Any]:
    """设置正式回测指针，并重写 backtests/.gitignore（只放行该运行目录）。"""
    if not run_file(paths, run_id, "meta").exists():
        raise FileNotFoundError(f"回测运行目录不存在：{run_id}")
    when = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")
    pointer = {"run_id": run_id, "set_at_utc": when, "set_by": set_by}
    paths.backtest_official.write_text(json.dumps(pointer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths.backtest_gitignore.write_text(gitignore_text(run_id), encoding="utf-8")
    return pointer
