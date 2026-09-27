"""运行目录、meta.json、official.json（docs/STORAGE.md 第2、3节，2.1节）。

每次运行一个独立目录，不覆盖；official.json 指向正式记录。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_risk.storage.paths import RUN_FILES, StoragePaths, make_run_id, unique_run_id

STATUS_COMPLETE, STATUS_PENDING, STATUS_FAILED = "complete", "pending", "failed"
UNKNOWN_COMMIT = "0000000"
# 本机 git 可能不在 PATH 中（CLAUDE.md 第11条）
_GIT_CANDIDATES = ("git", r"C:\Execute\Git\bin\git.exe", r"C:\Program Files\Git\bin\git.exe")


@dataclass(frozen=True)
class GitInfo:
    commit: str          # 完整哈希；取不到时为 0000000
    dirty: bool | None   # 工作区是否有未提交的修改；取不到时为 None

    @property
    def short(self) -> str:
        return self.commit[:7]


def _find_git() -> str | None:
    for c in _GIT_CANDIDATES:
        found = shutil.which(c) or (c if Path(c).exists() else None)
        if found:
            return found
    return None


def git_info(repo: Path) -> GitInfo:  # pragma: no cover - 依赖本机 git
    git = _find_git()
    if git is None:
        return GitInfo(UNKNOWN_COMMIT, None)
    try:
        commit = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            [git, "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return GitInfo(UNKNOWN_COMMIT, None)
    return GitInfo(commit, bool(status.strip()))


def to_jsonable(obj: Any) -> Any:
    """dataclass / 日期 / Decimal / tuple / 以 bool 为键的 dict 转为 JSON 可序列化对象。"""
    import dataclasses
    from decimal import Decimal

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dt.datetime | dt.date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {str(k).lower() if isinstance(k, bool) else str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [to_jsonable(x) for x in obj]
    return obj


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(data), ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def new_run(
    paths: StoragePaths,
    subject: str,
    framework: str,
    base_date: dt.date,
    git: GitInfo,
    now: dt.datetime | None = None,
) -> tuple[str, Path, dt.datetime]:
    """创建新的运行目录（UTC 时间命名，同一秒重复加 _2），返回 (run_id, 目录, 创建时间)。"""
    created = now or dt.datetime.now(dt.UTC)
    date_dir = paths.results_date_dir(subject, framework, base_date)
    existing = [p.name for p in date_dir.iterdir()] if date_dir.exists() else []
    run_id = unique_run_id(make_run_id(created, git.commit if git.commit else UNKNOWN_COMMIT), existing)
    run_dir = paths.run_dir(subject, framework, base_date, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir, created


def base_meta(
    run_id: str,
    created: dt.datetime,
    subject: str,
    framework: str,
    base_date: dt.date,
    mode: str,
    git: GitInfo,
    data_source_type: str = "api",
    rule_versions: tuple[str, ...] = ("v2-M", "v3-R1"),
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "created_at_utc": created.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "created_at_local": created.astimezone().isoformat(timespec="seconds"),
        "subject": subject,
        "framework": framework,
        "base_date": base_date.isoformat(),
        "mode": mode,
        "rule_versions": list(rule_versions),
        "git_commit": git.commit,
        "git_dirty": git.dirty,
        "data_source_type": data_source_type,
    }


def read_official(paths: StoragePaths, subject: str, framework: str, base_date: dt.date) -> dict | None:
    p = paths.official_pointer(subject, framework, base_date)
    return read_json(p) if p.exists() else None


def set_official(
    paths: StoragePaths,
    subject: str,
    framework: str,
    base_date: dt.date,
    run_id: str,
    set_by: str,
    reviewed: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """写 official.json（STORAGE 2.1）。指定的运行目录必须存在。"""
    if not paths.run_dir(subject, framework, base_date, run_id).is_dir():
        raise FileNotFoundError(f"运行目录不存在：{run_id}")
    when = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")
    pointer = {
        "run_id": run_id,
        "set_at_utc": when,
        "set_by": set_by,
        "reviewed": reviewed,
        "reviewed_at_utc": when if reviewed else None,
    }
    write_json(paths.official_pointer(subject, framework, base_date), pointer)
    return pointer


def confirm_official(
    paths: StoragePaths, subject: str, framework: str, base_date: dt.date, now: dt.datetime | None = None
) -> dict[str, Any]:
    pointer = read_official(paths, subject, framework, base_date)
    if pointer is None:
        raise FileNotFoundError(f"{base_date} 尚无正式记录")
    pointer["reviewed"] = True
    pointer["reviewed_at_utc"] = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")
    write_json(paths.official_pointer(subject, framework, base_date), pointer)
    return pointer


def maybe_auto_official(
    paths: StoragePaths,
    subject: str,
    framework: str,
    base_date: dt.date,
    run_id: str,
    status: str,
    git: GitInfo,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    """首次运行的自动设定（STORAGE 2.1）：尚无正式记录、状态为 complete/pending、git 工作区干净。"""
    if read_official(paths, subject, framework, base_date) is not None:
        return False, "该基准日已有正式记录，本次运行只新增目录（更换请用 official set）"
    if status not in (STATUS_COMPLETE, STATUS_PENDING):
        return False, f"运行状态为 {status}，不自动设为正式记录"
    if git.dirty is not False:
        reason = "git 工作区有未提交的修改" if git.dirty else "无法确定 git 工作区状态"
        return False, f"{reason}，不自动设为正式记录"
    set_official(paths, subject, framework, base_date, run_id, "auto", reviewed=False, now=now)
    return True, "已自动设为正式记录（reviewed=false，复核后用 official confirm 确认）"


def list_runs(paths: StoragePaths, subject: str, framework: str, base_date: dt.date) -> list[str]:
    d = paths.results_date_dir(subject, framework, base_date)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and p.name.startswith("run_"))


__all__ = ["RUN_FILES"]
