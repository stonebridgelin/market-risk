"""一次评分运行：快照 → 两个版本评分 → 贴近门槛 → 写运行目录 → 自动设定正式记录。

输入为已获取的原始数据（RawInputs），便于离线测试与以后接入 Agent。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from market_risk.config import MarketHolidays, Settings
from market_risk.data.snapshot import RawInputs, build_snapshot
from market_risk.models import MarketSnapshot, NearThresholdItem, ScoreResult
from market_risk.near_threshold import near_threshold_items
from market_risk.report import ReportContext, write_run_outputs
from market_risk.scoring import v2m, v3r1
from market_risk.storage.paths import MARKET, RISK_SCORING, RUN_FILES, StoragePaths
from market_risk.storage.runs import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
    GitInfo,
    base_meta,
    maybe_auto_official,
    new_run,
    to_jsonable,
    write_json,
)


@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    status: str
    snapshot: MarketSnapshot
    results: tuple[ScoreResult, ...]
    near: tuple[NearThresholdItem, ...]
    official_set: bool
    official_note: str
    messages: list[str] = field(default_factory=list)


def score_snapshot(
    snapshot: MarketSnapshot, settings: Settings
) -> tuple[tuple[ScoreResult, ...], tuple[NearThresholdItem, ...]]:
    results = (v2m.score(snapshot, settings.d1_includes_t_minus_20), v3r1.score(snapshot))
    near = tuple(near_threshold_items(snapshot, settings.near_threshold))
    return results, near


def run_status(results: tuple[ScoreResult, ...]) -> str:
    return STATUS_PENDING if any(r.pending for r in results) else STATUS_COMPLETE


def required_breadth_messages(snapshot: MarketSnapshot, results: tuple[ScoreResult, ...]) -> list[str]:
    """SPEC 5.6 第2条：规则需要 F5/W5 而缺失时，提示需要补录哪一天。"""
    msgs = []
    refs = snapshot.refs
    if snapshot.breadth is None:
        msgs.append(f"需要补录 {refs.base_date} 的 S5FI、S5TW 读数")
    if snapshot.breadth_t5 is None and any(
        r.breadth.score is None and "5日前" in (r.breadth.pending_reason or "") for r in results
    ):
        msgs.append(f"需要补录 {refs.t_minus_5} 的 S5FI、S5TW 读数")
    return msgs


def run_scoring(
    raw: RawInputs,
    settings: Settings,
    paths: StoragePaths,
    git: GitInfo,
    holidays: MarketHolidays | None = None,
    now: dt.datetime | None = None,
    breadth_source: str | None = None,
) -> RunOutcome:
    base = raw.base_date
    run_id, run_dir, created = new_run(paths, MARKET, RISK_SCORING, base, git, now)
    meta = base_meta(run_id, created, MARKET, RISK_SCORING, base, raw.mode, git)
    meta["config"] = {
        "three_segment.d1_includes_t_minus_20": settings.d1_includes_t_minus_20,
        "oas.revision_check": settings.oas_revision_check,
        "oas.long_history_source": settings.oas_long_history_source,
    }
    meta["sources"] = to_jsonable(list(raw.sources))
    if git.dirty:
        meta["warnings"] = ["git 工作区有未提交的修改，结果对应的代码版本不完全等于 git_commit"]
    try:
        snapshot = build_snapshot(
            raw, settings.scored_symbols, tuple(settings.reference_symbols[:2]), holidays  # type: ignore[arg-type]
        )
        results, near = score_snapshot(snapshot, settings)
        status = run_status(results)
        ctx_kwargs = {"breadth_source": breadth_source} if breadth_source else {}
        ctx = ReportContext(raw, snapshot, results, near, settings.d1_includes_t_minus_20, **ctx_kwargs)
        write_run_outputs(run_dir, ctx, status, settings.reference_symbols)
    except Exception as exc:
        meta.update(status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
        write_json(run_dir / RUN_FILES["meta"], meta)
        raise
    meta["status"] = status
    meta["pending_dimensions"] = {r.version: list(r.pending) for r in results}
    meta["review_flags"] = [f for r in results for f in r.review_flags]
    meta["data_notes"] = list(snapshot.data_notes)
    write_json(run_dir / RUN_FILES["meta"], meta)

    official_set, note = maybe_auto_official(paths, MARKET, RISK_SCORING, base, run_id, status, git, now)
    summary = run_dir / RUN_FILES["summary"]
    summary.write_text(summary.read_text(encoding="utf-8") + f"\n正式记录：{note}\n", encoding="utf-8")
    return RunOutcome(
        run_id, run_dir, status, snapshot, results, near, official_set, note,
        required_breadth_messages(snapshot, results),
    )
