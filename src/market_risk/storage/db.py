"""统计数据库（docs/STORAGE.md 第4节）。**所有数据库读写集中在本模块**（storage/）。

约定：
- 使用 SQLAlchemy Core，连接地址来自 .env 的 DATABASE_URL，其次 settings.yaml 的 database.url，
  默认 sqlite:///db/market_risk.sqlite（相对路径以存储根目录为基准）；
- 不使用任何数据库的专有语法；小数用 Numeric；表结构由 Alembic 迁移管理（migrations/）；
- SQLite 开启 WAL 模式（仅在连接 SQLite 时设置）。

数据库内容全部可由文件重建（rebuild）：
results/**/run_*/ 与 official.json → runs、officials、dimension_scores、totals、metrics、near_threshold；
data/manual/outcomes.csv → outcomes；data/materials/index.csv → materials；data/manual/reviews.csv → reviews；
config/symbols.yaml、holidays.yaml、data_decisions.yaml
→ 参考表 symbols、market_holidays、data_decisions（YAML 为源头）。
市场时间序列不存入数据库（在 data/market/）。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import warnings
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, exc, inspect, select, text
from sqlalchemy.engine import Connection, make_url

from market_risk.metrics import metrics_from_snapshot_json
from market_risk.storage import schema
from market_risk.storage.paths import RUN_FILES, StoragePaths
from market_risk.storage.runs import read_json

DEFAULT_URL = "sqlite:///db/market_risk.sqlite"
DIMENSION_KEYS = ("price", "breadth", "vix", "rates", "credit")
TABLE_NAMES = tuple(t.name for t in schema.TABLES)
_ID_COLUMNS = {"id"}

# SQLite 没有原生 DECIMAL 类型，SQLAlchemy 会提示精度转换；写入时已用 Decimal，读取按 Numeric(20, 8) 还原
warnings.filterwarnings("ignore", message=r".*does \*not\* support Decimal objects natively.*",
                        category=exc.SAWarning)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def resolve_database_url(settings: Any, env: dict[str, str] | None = None) -> str:
    """DATABASE_URL（环境变量 / .env）优先，其次 settings.database_url；SQLite 相对路径以存储根目录为基准。"""
    from dotenv import load_dotenv

    from market_risk.config import PROJECT_ROOT

    if env is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        env = dict(os.environ)
    raw = env.get("DATABASE_URL") or getattr(settings, "database_url", None) or DEFAULT_URL
    return absolutize(raw, Path(settings.storage_root))


def absolutize(url: str, root: Path) -> str:
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        p = Path(u.database)
        if not p.is_absolute():
            return sqlite_url(root / p)
    return url


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def default_url(paths: StoragePaths) -> str:
    """不读环境变量的默认地址（测试与离线使用）：<存储根>/db/market_risk.sqlite。"""
    return sqlite_url(paths.db_path)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:  # 仅 SQLite：WAL 与外键约束
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


def _sqlite_file(url: str) -> Path | None:
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        return Path(u.database)
    return None


def upgrade_schema(url: str) -> None:
    """用 Alembic 把表结构迁移到最新版本。"""
    from alembic import command
    from alembic.config import Config

    from market_risk.config import PROJECT_ROOT

    cfg = Config()   # 不读取 alembic.ini（Windows 按系统编码读取），直接设置迁移目录与地址
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def head_revision() -> str:
    """migrations/ 中最新的迁移版本号。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from market_risk.config import PROJECT_ROOT

    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return str(ScriptDirectory.from_config(cfg).get_current_head())


def _reset(url: str) -> None:
    """清空数据库：SQLite 文件直接删除；其他数据库删除全部表（含 alembic_version）。"""
    file = _sqlite_file(url)
    if file is not None:
        for p in (file, Path(f"{file}-wal"), Path(f"{file}-shm")):
            if p.exists():
                p.unlink()
        file.parent.mkdir(parents=True, exist_ok=True)
        return
    engine = make_engine(url)
    try:
        schema.metadata.drop_all(engine)
        with engine.begin() as conn:
            if inspect(conn).has_table("alembic_version"):
                conn.execute(text("DROP TABLE alembic_version"))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 写入（重建）
# ---------------------------------------------------------------------------


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
    """单日运行目录（不含逐日回测的运行目录 results/…/backtests/，后者由 _insert_backtests 处理）。"""
    root = paths.results_root
    if root.exists():
        skip = paths.backtests_root
        yield from (m.parent for m in sorted(root.glob("**/run_*/meta.json")) if skip not in m.parents)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return v if isinstance(v, Decimal) else Decimal(repr(float(v)) if isinstance(v, float) else str(v))


def _date(v: Any) -> dt.date | None:
    return dt.date.fromisoformat(v) if v else None


def _yes(v: str | None) -> bool | None:
    return None if v in (None, "") else v in ("是", "true", "True", "1")


def _insert_run(conn: Connection, paths: StoragePaths, run_dir: Path) -> None:
    meta = read_json(run_dir / RUN_FILES["meta"])
    key = run_key(meta)
    official = run_dir.parent / "official.json"
    pointer = read_json(official) if official.exists() else None
    is_official = bool(pointer and pointer.get("run_id") == meta["run_id"])
    conn.execute(schema.runs.insert().values(
        run_key=key, run_id=meta["run_id"], subject=meta["subject"], framework=meta["framework"],
        base_date=_date(meta["base_date"]), mode=meta.get("mode"), created_at_utc=meta.get("created_at_utc"),
        created_at_local=meta.get("created_at_local"), git_commit=meta.get("git_commit"),
        git_dirty=meta.get("git_dirty"), data_source_type=meta.get("data_source_type"),
        status=meta.get("status"), run_dir=run_dir.relative_to(paths.root).as_posix(),
        is_official=is_official, reviewed=bool(is_official and pointer and pointer.get("reviewed")),
        official_set_by=pointer.get("set_by") if is_official and pointer else None,
    ))
    if is_official and pointer:
        conn.execute(schema.officials.insert().values(
            subject=meta["subject"], framework=meta["framework"], base_date=_date(meta["base_date"]),
            run_key=key, set_by=pointer.get("set_by"), reviewed=bool(pointer.get("reviewed")),
            set_at_utc=pointer.get("set_at_utc"), reviewed_at_utc=pointer.get("reviewed_at_utc"),
        ))
    scores_path = run_dir / RUN_FILES["scores"]
    if scores_path.exists():
        scores = read_json(scores_path)
        for r in scores.get("results", []):
            for dk in DIMENSION_KEYS:
                d = r[dk]
                conn.execute(schema.dimension_scores.insert().values(
                    run_key=key, version=r["version"], dimension=d["name"], score=d["score"],
                    possible_scores=json.dumps(d["possible_scores"]),
                    triggered_conditions=json.dumps(d["triggered_conditions"], ensure_ascii=False),
                    pending_reason=d.get("pending_reason"), calculation=d.get("calculation"),
                ))
            lo, hi = r["total_range"]
            conn.execute(schema.totals.insert().values(
                run_key=key, version=r["version"], total=r["total"], total_min=lo, total_max=hi, stage=r["stage"],
                clear_deterioration=dict(r["clear_deterioration"]).get("大盘明确恶化"),
                alert=alert_status(r["total"], lo, hi),
                review_flags=json.dumps(r.get("review_flags", []), ensure_ascii=False),
                notes=json.dumps(r.get("notes", []), ensure_ascii=False),
            ))
        for n in scores.get("near_threshold", []):
            conn.execute(schema.near_threshold.insert().values(
                run_key=key, item=n["item"], value=_dec(n.get("value")), threshold=_dec(n.get("threshold")),
                gap=_dec(n.get("gap")), unit=n.get("unit"),
            ))
    metrics_path, snap_path = run_dir / RUN_FILES["metrics"], run_dir / RUN_FILES["snapshot"]
    if metrics_path.exists():
        metrics = read_json(metrics_path)
    elif snap_path.exists():
        metrics = metrics_from_snapshot_json(read_json(snap_path))
    else:
        metrics = {}
    for k, v in sorted(metrics.items()):
        numeric = isinstance(v, int | float) and not isinstance(v, bool)
        conn.execute(schema.metrics.insert().values(
            run_key=key, key=k, value=_dec(v) if numeric else None,
            text=None if numeric or v is None else str(v),
        ))


def _insert_files(conn: Connection, paths: StoragePaths) -> None:
    from market_risk.outcomes import compat_row

    for r in map(compat_row, _csv_rows(paths.outcomes_csv)):
        values: dict[str, Any] = {
            "subject": r["subject"], "base_date": _date(r["base_date"]), "source": r["source"],
            "window_start": _date(r["window_start"]), "window_end": _date(r["window_end"]),
            "spx_drawdown_from_base": _dec(r["spx_drawdown_from_base"]),
            "qqq_drawdown_from_base": _dec(r["qqq_drawdown_from_base"]),
            "is_event": bool(_yes(r["is_event"])), "event_date": _date(r.get("event_date")),
            "entered_at": r["entered_at"],
        }
        for col in ("spx_peak_to_trough_drawdown", "qqq_peak_to_trough_drawdown"):
            if col in schema.outcomes.c:
                values[col] = _dec(r.get(col))
        if "near_event" in schema.outcomes.c:
            values["near_event"] = _yes(r.get("near_event"))
        conn.execute(schema.outcomes.insert().values(**values))
    for r in _csv_rows(paths.materials_index_csv):
        conn.execute(schema.materials.insert().values(
            subject=r["subject"], base_date=_date(r["base_date"]), type=r["type"], file_path=r["file_path"],
            source=r["source"], note=r["note"], added_at=r["added_at"]))
    for r in _csv_rows(paths.reviews_csv):
        conn.execute(schema.reviews.insert().values(
            subject=r["subject"], base_date=_date(r.get("base_date")), reviewer=r["reviewer"],
            category=r["category"], content=r["content"], impact=r["impact"], run_key=r.get("run_key") or None,
            other_run_key=r.get("other_run_key") or None, created_at=r["created_at"]))


def holiday_rows(holidays_path: Path) -> list[dict[str, Any]]:
    """holidays.yaml → market_holidays 的行（含每个日期的行内注释）。"""
    import re

    from market_risk.config import load_holidays

    h = load_holidays(holidays_path)
    notes: dict[tuple[str, str, str], str] = {}
    market = kind = ""
    for line in holidays_path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^(stock|bond):", line):
            market = line.split(":")[0]
        elif m := re.match(r"^\s+(holidays|early_closes):", line):
            kind = "holiday" if m.group(1) == "holidays" else "early_close"
        elif m := re.match(r"^\s+-\s*(\d{4}-\d{2}-\d{2})\s*(?:#\s*(.*))?$", line):
            if m.group(2):
                notes[(market, kind, m.group(1))] = m.group(2).strip()
    rows = []
    for market, kind, dates in (("stock", "holiday", h.stock_holidays), ("stock", "early_close", h.stock_early_closes),
                                ("bond", "holiday", h.bond_holidays), ("bond", "early_close", h.bond_early_closes)):
        rows += [{"market": market, "date": d, "kind": kind, "note": notes.get((market, kind, d.isoformat()))}
                 for d in sorted(dates)]
    return rows


def reference_rows(config_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """参考表的内容，由 config/ 下的 YAML 生成（db export-sql 与 rebuild-db 共用）。"""
    from market_risk.config import PROJECT_ROOT, load_data_decisions, load_symbols

    cfg = config_dir or PROJECT_ROOT / "config"
    symbols = [{
        "symbol": s.symbol, "tv_symbol": s.tv_symbol, "name": s.name, "category": s.category, "usage": s.usage,
        "unit": s.unit, "timezone": s.timezone, "calendar": s.calendar, "inception": s.inception,
        "api_source": s.api_source, "tolerance": _dec(s.tolerance), "crosscheck_note": s.crosscheck_note,
        "filename_aliases": json.dumps(list(s.filename_aliases), ensure_ascii=False),
        "known_values": json.dumps({d.isoformat(): v for d, v in sorted((s.known_values or {}).items())}),
    } for s in sorted(load_symbols(cfg / "symbols.yaml").values(), key=lambda s: s.symbol)]
    decisions = [{"date": d.date, "symbol": d.symbol, "decision": d.decision, "reason": d.reason,
                  "decided_on": d.decided_on, "corrected_value": d.corrected_value,
                  "evidence_source": d.evidence_source}
                 for d in sorted(load_data_decisions(cfg / "data_decisions.yaml"), key=lambda d: (d.date, d.symbol))]
    return {"symbols": symbols, "market_holidays": holiday_rows(cfg / "holidays.yaml"),
            "data_decisions": decisions}


def _insert_reference(conn: Connection, config_dir: Path | None = None) -> None:
    rows = reference_rows(config_dir)
    for table in schema.REFERENCE_TABLES:
        if rows[table.name]:
            conn.execute(table.insert(), rows[table.name])


def _dim_parts(text: str) -> tuple[int | None, str]:
    """daily_scores.csv 的维度写法："2" 或 "待补[1, 2]" → (分数, 可能取值)。"""
    if text.startswith("待补"):
        return None, text.removeprefix("待补")
    return int(text), f"[{int(text)}]"


def _insert_backtests(conn: Connection, paths: StoragePaths) -> None:
    """由回测运行目录重建：backtest_runs、backtest_daily_scores、backtest_outcomes、pullback_episodes。"""
    from market_risk.storage import backtests

    official = (backtests.read_official(paths) or {}).get("run_id")
    for run_id in backtests.list_runs(paths):
        meta = backtests.read_meta(paths, run_id)
        conn.execute(schema.backtest_runs.insert().values(
            run_id=run_id, created_at_utc=meta.get("created_at_utc"), git_commit=meta.get("git_commit"),
            git_dirty=meta.get("git_dirty"), market_manifest_sha256=meta.get("market_manifest_sha256"),
            config_sha256=meta.get("config", {}).get("sha256"), start_date=_date(meta["start"]),
            end_date=_date(meta["end"]), versions=",".join(meta.get("versions", [])), days=int(meta["days"]),
            runtime_seconds=_dec(meta.get("runtime_seconds")), holdout_unlocked_at=meta.get("holdout_unlocked_at"),
            is_official=run_id == official,
            run_dir=paths.backtest_run_dir(run_id).relative_to(paths.root).as_posix()))
        rows = []
        for r in backtests.read_csv(backtests.run_file(paths, run_id, "daily_scores")):
            dims = {d: _dim_parts(r[d]) for d in schema.BACKTEST_DIMS}
            rows.append({"run_id": run_id, "base_date": _date(r["date"]), "version": r["version"],
                         **{d: v[0] for d, v in dims.items()}, **{f"{d}_possible": v[1] for d, v in dims.items()},
                         "total": int(r["total"]) if r["total"] else None, "total_min": int(r["total_min"]),
                         "total_max": int(r["total_max"]), "stage": r["stage"],
                         "pending_dimensions": r["pending_dimensions"] or None,
                         "clear_deterioration": r["clear_deterioration"], "alert": r["alert"],
                         "flags": r["flags"] or None})
        if rows:
            conn.execute(schema.backtest_daily_scores.insert(), rows)
        rows = [{"run_id": run_id, "base_date": _date(r["base_date"]), "window_start": _date(r["window_start"]),
                 "window_end": _date(r["window_end"]),
                 **{k: _dec(r[k]) for k in ("spx_drawdown_from_base", "qqq_drawdown_from_base",
                                            "spx_peak_to_trough_drawdown", "qqq_peak_to_trough_drawdown")},
                 "is_event": r["is_event"] == "是", "event_date": _date(r.get("event_date")),
                 "is_near_event": r["is_near_event"] == "是", "period": r["period"],
                 "crosses_period": r["crosses_period"] == "是"}
                for r in backtests.read_csv(backtests.run_file(paths, run_id, "outcomes"))]
        if rows:
            conn.execute(schema.backtest_outcomes.insert(), rows)
        rows = [{"run_id": run_id, "symbol": r["symbol"], "level": _dec(Decimal(r["level"]) / 100),
                 "high_date": _date(r["high_date"]), "high_close": _dec(r["high_close"]),
                 "low_date": _date(r["low_date"]), "low_close": _dec(r["low_close"]),
                 "drawdown_pct": _dec(r["drawdown_pct"]),
                 "trading_days": int(r["trading_days"]) if r["trading_days"] else None,
                 "grade": r["grade"] or None, "status": r["status"], "confirm_date": _date(r["confirm_date"]),
                 "recovery_date": _date(r["recovery_date"]), "recovery_note": r["recovery_note"] or None,
                 "period": r["period"], "before_start": r["before_start"] == "是",
                 "crosses_boundary": r["crosses_boundary"] == "是", "counted": r["counted"] == "是"}
                for r in backtests.read_csv(backtests.run_file(paths, run_id, "episodes"))]
        if rows:
            conn.execute(schema.pullback_episodes.insert(), rows)


def rebuild(paths: StoragePaths, url: str | None = None) -> str:
    """清空并重建数据库（rebuild-db）：Alembic 迁移到最新结构后，由文件写入全部内容。返回连接地址。"""
    url = url or default_url(paths)
    _reset(url)
    upgrade_schema(url)
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            for run_dir in iter_run_dirs(paths):
                _insert_run(conn, paths, run_dir)
            _insert_files(conn, paths)
            _insert_reference(conn)
            _insert_backtests(conn, paths)
    finally:
        engine.dispose()
    return url


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _rows(url: str, stmt: Any) -> list[dict[str, Any]]:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]
    finally:
        engine.dispose()


def official_runs(url: str, framework: str) -> list[dict[str, Any]]:
    """正式记录（按基准日升序）。"""
    r = schema.runs
    return _rows(url, select(r).where(r.c.is_official.is_(True), r.c.framework == framework)
                 .order_by(r.c.base_date))


def totals_by_version(url: str, key: str) -> dict[str, dict[str, Any]]:
    t = schema.totals
    return {row["version"]: row for row in _rows(url, select(t).where(t.c.run_key == key))}


def dimensions_by_version(url: str, key: str) -> dict[str, dict[str, dict[str, Any]]]:
    d = schema.dimension_scores
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in _rows(url, select(d).where(d.c.run_key == key)):
        out.setdefault(row["version"], {})[row["dimension"]] = row
    return out


def near_threshold_for(url: str, key: str) -> list[dict[str, Any]]:
    n = schema.near_threshold
    return _rows(url, select(n).where(n.c.run_key == key).order_by(n.c.id))


def metrics_for(url: str, key: str) -> dict[str, Any]:
    m = schema.metrics
    return {row["key"]: row["value"] if row["value"] is not None else row["text"]
            for row in _rows(url, select(m).where(m.c.run_key == key))}


def runs_table(url: str) -> list[dict[str, Any]]:
    r = schema.runs
    return _rows(url, select(r).order_by(r.c.base_date, r.c.data_source_type, r.c.run_id))


def dump(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """全部表的内容（去掉自增 id、排序后），用于比较两次重建是否一致。"""
    out = {}
    for table in schema.TABLES:
        cols = [c for c in table.c if c.name not in _ID_COLUMNS]
        rows = _rows(url, select(*cols))
        out[table.name] = sorted((tuple(r.values()) for r in rows), key=repr)
    return out

