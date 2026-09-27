"""阶段4.5 存储测试（docs/STORAGE.md 第9节验收）。只写入临时目录。"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import shutil
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from conftest import load_sample_raw

from market_risk.config import PROJECT_ROOT, load_settings
from market_risk.legacy import import_legacy, read_excel, recompute_summary
from market_risk.materials import MaterialError, Review, add_material, add_reviews, list_materials, read_reviews
from market_risk.models import BreadthReading
from market_risk.outcomes import (
    Outcome,
    OutcomeError,
    compute_outcome,
    effective_outcomes,
    read_outcomes,
    record_outcome,
)
from market_risk.pipeline import run_scoring
from market_risk.stats import run_stats
from market_risk.storage import db, runs
from market_risk.storage.paths import MARKET, RISK_SCORING, StoragePaths

D = dt.date
SETTINGS = load_settings()
GIT = runs.GitInfo("b" * 40, False)
EXCEL = PROJECT_ROOT / "data" / "legacy" / "backtest_record_legacy.xlsx"
SAMPLE_BREADTH = {
    "2025-08-29": {D(2025, 8, 29): (63.22, 67.59)},
    "2025-09-26": {D(2025, 9, 26): (54.67, 51.09)},
    "2025-10-31": {D(2025, 10, 31): (40.15, 38.56), D(2025, 10, 24): (52.88, 57.65)},
    "2025-11-28": {D(2025, 11, 28): (58.44, 76.73)},
}


@pytest.fixture()
def paths(tmp_path):
    return StoragePaths(tmp_path)


def program_runs(paths: StoragePaths, now: dt.datetime | None = None) -> dict[str, str]:
    out = {}
    for sample, readings in SAMPLE_BREADTH.items():
        breadth = {d: BreadthReading(d, f, w) for d, (f, w) in readings.items()}
        out[sample] = run_scoring(load_sample_raw(sample, breadth), SETTINGS, paths, GIT, now=now).run_id
    return out


def copy_excel(paths: StoragePaths) -> Path:
    target = paths.legacy_excel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXCEL, target)
    return target


# ---------------------------------------------------------------------------
# 数据库：删除后重建，内容一致；同一基准日只有一个正式记录
# ---------------------------------------------------------------------------


def test_rebuild_db_is_reproducible(paths):
    program_runs(paths)
    run_scoring(load_sample_raw("2025-11-28", {D(2025, 11, 28): BreadthReading(D(2025, 11, 28), 58.44, 76.73)}),
                SETTINGS, paths, GIT)   # 同一基准日第二次运行
    record_outcome(paths.outcomes_csv, Outcome("MARKET", D(2025, 8, 29), D(2025, 9, 2), D(2025, 9, 29),
                                               -1.2, -2.0, False, None, "manual", "2026-09-27T00:00:00+00:00"))
    note = paths.root / "note.md"
    note.write_text("笔记", encoding="utf-8")
    add_material(paths, "MARKET", D(2025, 11, 28), "notes", note)
    add_reviews(paths, [Review("MARKET", D(2025, 11, 28), "claude", "核查", "无问题")])

    url = db.default_url(paths)
    first = db.dump(db.rebuild(paths, url))
    shutil.rmtree(paths.db_path.parent)      # 删除 db/ 后重建
    second = db.dump(db.rebuild(paths, url))
    assert first == second
    assert len(first["runs"]) == 5 and len(first["outcomes"]) == 1
    assert len(first["materials"]) == 1 and len(first["reviews"]) == 1
    assert len(first["dimension_scores"]) == 5 * 2 * 5 and len(first["totals"]) == 5 * 2
    # 同一基准日只有一个正式记录（officials 表的复合主键保证）
    officials = {row[2]: row[3] for row in first["officials"]}
    assert set(officials) == {D.fromisoformat(s) for s in SAMPLE_BREADTH}
    key = next(r["run_key"] for r in db.official_runs(url, RISK_SCORING) if r["base_date"] == D(2025, 10, 31))
    metrics = db.metrics_for(url, key)
    assert metrics["SPY.close"] == Decimal("682.06") and metrics["L"] == Decimal("38.56")
    assert metrics["doas_v3r1_bp"] == Decimal("-11")


def test_database_url_resolution(tmp_path):
    settings = dataclasses.replace(load_settings(), storage_root=tmp_path)
    url = db.resolve_database_url(settings, env={})
    assert url == db.sqlite_url(tmp_path / "db" / "market_risk.sqlite")
    assert db.resolve_database_url(settings, env={"DATABASE_URL": "postgresql://u:p@h/db"}) == "postgresql://u:p@h/db"
    assert db.absolutize("sqlite:///:memory:", tmp_path) == "sqlite:///:memory:"


def test_sqlite_wal_and_migration(paths):
    import sqlalchemy as sa

    url = db.rebuild(paths, db.default_url(paths))
    engine = db.make_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("PRAGMA journal_mode")).scalar() == "wal"
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert version == db.head_revision()
    finally:
        engine.dispose()


def test_official_unique_per_date(paths):
    import sqlalchemy as sa

    from market_risk.storage import schema

    url = db.rebuild(paths, db.default_url(paths))
    engine = db.make_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(schema.runs.insert().values(run_key="k1", run_id="r1", subject="MARKET", framework="f",
                                                     base_date=D(2025, 1, 2), is_official=True, reviewed=False))
            conn.execute(schema.runs.insert().values(run_key="k2", run_id="r2", subject="MARKET", framework="f",
                                                     base_date=D(2025, 1, 2), is_official=True, reviewed=False))
            conn.execute(schema.officials.insert().values(subject="MARKET", framework="f",
                                                          base_date=D(2025, 1, 2), run_key="k1", reviewed=False))
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.execute(schema.officials.insert().values(subject="MARKET", framework="f",
                                                                  base_date=D(2025, 1, 2), run_key="k2",
                                                                  reviewed=False))
    finally:
        engine.dispose()


def test_alert_status():
    assert db.alert_status(3, 3, 3) == "是" and db.alert_status(2, 2, 2) == "否"
    assert db.alert_status(None, 3, 5) == "是" and db.alert_status(None, 0, 2) == "否"
    assert db.alert_status(None, 2, 4) == "未知"


# ---------------------------------------------------------------------------
# 结果标签与隔离（STORAGE 第5节）
# ---------------------------------------------------------------------------

ISOLATED = [
    *sorted((PROJECT_ROOT / "src" / "market_risk" / "scoring").glob("*.py")),
    PROJECT_ROOT / "src" / "market_risk" / "report.py",
    PROJECT_ROOT / "src" / "market_risk" / "data" / "snapshot.py",
    PROJECT_ROOT / "templates" / "prompt_backtest.md.j2",
]


@pytest.mark.parametrize("path", ISOLATED, ids=lambda p: p.name)
def test_label_isolation(path):
    """评分、prompt 生成与快照不得引用结果标签（文件、表、模块）。"""
    text = path.read_text(encoding="utf-8")
    for forbidden in ("outcomes", "outcomes_csv", "market_risk.outcomes", "is_event", "drawdown",
                      "market_risk.stats", "FROM outcomes"):
        assert forbidden not in text, f"{path.name} 引用了 {forbidden}"
    if path.suffix == ".py":
        tree = ast.parse(text)
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(m.endswith(("outcomes", "stats", "storage.db", "legacy")) for m in modules), modules


def _closes(base: dt.date, path: list[float]) -> dict[dt.date, float]:
    from market_risk import calendar as mcal

    days = [base, *mcal.stock_trading_days(base + dt.timedelta(days=1), base + dt.timedelta(days=40))[:20]]
    return dict(zip(days, path, strict=True))


def test_compute_outcome():
    base = D(2025, 10, 31)
    spx = _closes(base, [100.0] + [99.0] * 5 + [94.9] + [98.0] * 14)     # 第6个交易日跌 5.1%
    qqq = _closes(base, [100.0] + [97.0] * 20)
    o = compute_outcome(base, spx, qqq, today_new_york=D(2026, 1, 1))
    assert o.is_event and o.spx_drawdown_from_base == pytest.approx(-5.1)
    assert o.event_date == sorted(spx)[6] and (o.window_start, o.window_end) == (D(2025, 11, 3), D(2025, 12, 1))
    calm = compute_outcome(base, _closes(base, [100.0] + [95.01] * 20), _closes(base, [100.0] + [93.01] * 20),
                           D(2026, 1, 1))
    assert not calm.is_event and calm.event_date is None
    with pytest.raises(OutcomeError, match="才结束"):
        compute_outcome(base, spx, qqq, today_new_york=D(2025, 12, 1))   # 窗口最后一天当天不得计算
    del qqq[D(2025, 11, 14)]
    with pytest.raises(OutcomeError, match="缺少收盘价"):
        compute_outcome(base, spx, qqq, D(2026, 1, 1))


def test_manual_and_computed_outcomes(paths):
    manual = Outcome("MARKET", D(2025, 10, 31), D(2025, 11, 3), D(2025, 12, 1), -4.0, -6.0, False, None,
                     "manual", "t")
    computed = dataclasses.replace(manual, spx_drawdown_from_base=-5.2, is_event=True, source="computed")
    assert record_outcome(paths.outcomes_csv, manual) == []
    diffs = record_outcome(paths.outcomes_csv, computed)
    assert len(diffs) == 1 and "标签不一致" in diffs[0]
    rows = read_outcomes(paths.outcomes_csv)
    assert len(rows) == 2
    assert effective_outcomes(rows)[("MARKET", D(2025, 10, 31))].source == "manual"
    record_outcome(paths.outcomes_csv, computed)      # 同一来源重复写入不重复
    assert len(read_outcomes(paths.outcomes_csv)) == 2


# ---------------------------------------------------------------------------
# 资料
# ---------------------------------------------------------------------------


def test_materials(paths, tmp_path):
    f = tmp_path / "chatgpt.md"
    f.write_text("第一版", encoding="utf-8")
    t1 = add_material(paths, "market", D(2025, 11, 28), "chatgpt_response", f, "ChatGPT", "样本4 打分")
    assert t1 == paths.materials_dir("MARKET", D(2025, 11, 28)) / "chatgpt.md"
    f.write_text("第二版", encoding="utf-8")
    t2 = add_material(paths, "MARKET", D(2025, 11, 28), "chatgpt_response", f)
    assert t2.name == "chatgpt_2.md" and t1.read_text("utf-8") == "第一版"   # 不覆盖
    shot = tmp_path / "spy.png"
    shot.write_bytes(b"png")
    assert "screenshots" in add_material(paths, "MARKET", D(2025, 11, 28), "screenshot", shot).parts
    assert len(list_materials(paths, "MARKET", D(2025, 11, 1))) == 3
    assert list_materials(paths, "MARKET", D(2025, 12, 1)) == []
    with pytest.raises(MaterialError):
        add_material(paths, "MARKET", D(2025, 11, 28), "video", f)
    with pytest.raises(MaterialError):
        add_material(paths, "MARKET", D(2025, 11, 28), "notes", tmp_path / "missing.md")


# ---------------------------------------------------------------------------
# import-legacy
# ---------------------------------------------------------------------------


def test_read_excel_and_recompute_formulas():
    samples, changelog = read_excel(EXCEL)
    done = [s for s in samples if s.complete]
    assert [s.base_date for s in done] == [D(2025, 8, 29), D(2025, 9, 26), D(2025, 10, 31), D(2025, 11, 28)]
    assert done[2].scores == {"v2-M": [1, 2, 0, 0, 0], "v3-R1": [1, 2, 0, 0, 0]}
    assert recompute_summary(done[2])["U"] == "v2-M:是 / v3-R1:是"
    assert done[2].raw["F"] == pytest.approx(40.15) and done[2].raw["SPY.ma50"] == 662.17
    assert len(changelog) == 12


def test_import_legacy_end_to_end(paths):
    prog = program_runs(paths)
    excel = copy_excel(paths)
    now = dt.datetime(2026, 9, 27, 3, 0, 0, tzinfo=dt.UTC)
    result = import_legacy(excel, paths, GIT, now=now)
    assert [d for d, _ in result.imported] == [D(2025, 8, 29), D(2025, 9, 26), D(2025, 10, 31), D(2025, 11, 28)]
    assert result.formula_checks == []          # 公式重算与 Excel 缓存值一致
    assert result.score_differences == []       # 截图与程序分数一致
    assert result.metric_differences == []
    assert len(result.skipped) == 9             # 待做的样本5至13
    for d, run_id in result.imported:
        pointer = runs.read_official(paths, MARKET, RISK_SCORING, d)
        assert pointer == {**pointer, "run_id": run_id, "set_by": "import-legacy", "reviewed": True}
        meta = json.loads((paths.run_dir(MARKET, RISK_SCORING, d, run_id) / "meta.json").read_text("utf-8"))
        assert meta["data_source_type"] == "screenshot" and meta["legacy"]["sha256"]
        # 程序记录作为对照保留
        assert prog[d.isoformat()] in runs.list_runs(paths, MARKET, RISK_SCORING, d)
    reviews = read_reviews(paths)
    compare = [r for r in reviews if r["category"] == "截图与程序对照"]
    assert len(compare) == 4 and all(r["impact"] == "分数一致" for r in compare)
    assert all(r["other_run_key"].endswith(prog[r["base_date"]]) for r in compare)
    assert sum(r["reviewer"] == "legacy_changelog" for r in reviews) == result.changelog_rows >= 12
    resolved = [r for r in reviews if r["category"] == "变更记录：规则口径（已确定）"]
    assert len(resolved) == 2 and all("T−20 至 T−2" in r["impact"] for r in resolved)
    assert not any("待定" in r["category"] for r in reviews)

    # 重复导入：不新建运行目录，不重复复核记录
    pointers = {d: runs.read_official(paths, MARKET, RISK_SCORING, d) for d, _ in result.imported}
    again = import_legacy(excel, paths, GIT, now=now + dt.timedelta(hours=1))
    assert {d: runs.read_official(paths, MARKET, RISK_SCORING, d) for d in pointers} == pointers
    assert again.imported == [] and any("已导入" in s for s in again.skipped)
    assert len(read_reviews(paths)) == len(reviews)


def test_import_legacy_reports_score_difference(paths):
    prog = program_runs(paths)
    scores_path = paths.run_dir(MARKET, RISK_SCORING, D(2025, 10, 31), prog["2025-10-31"]) / "scores.json"
    scores = json.loads(scores_path.read_text("utf-8"))
    scores["results"][1]["price"]["score"] = 2          # 人为制造差异
    scores_path.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
    result = import_legacy(copy_excel(paths), paths, GIT)
    assert result.score_differences == ["2025-10-31 v3-R1 价格：截图 1，程序 2"]
    row = next(r for r in read_reviews(paths) if r["base_date"] == "2025-10-31" and r["category"] == "截图与程序对照")
    assert "需用户判断" in row["impact"]


def test_import_legacy_without_program_runs(paths):
    result = import_legacy(copy_excel(paths), paths, GIT)
    assert len(result.imported) == 4 and result.score_differences == []
    assert any("没有程序运行记录" in r["content"] for r in read_reviews(paths))


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_with_legacy_samples(paths):
    program_runs(paths)
    import_legacy(copy_excel(paths), paths, GIT)
    record_outcome(paths.outcomes_csv, Outcome("MARKET", D(2025, 10, 31), D(2025, 11, 3), D(2025, 12, 1),
                                               -5.3, -7.9, True, D(2025, 11, 20), "manual", "t"))
    record_outcome(paths.outcomes_csv, Outcome("MARKET", D(2025, 8, 29), D(2025, 9, 2), D(2025, 9, 29),
                                               -1.0, -1.5, False, None, "manual", "t"))
    text, samples = run_stats(paths, db.default_url(paths), now=dt.datetime(2026, 9, 27, tzinfo=dt.UTC))
    assert len(samples) == 4 and all(s.data_source_type == "screenshot" for s in samples)
    assert "正式记录样本数：4（已复核 4，未复核 0）" in text
    assert "| 命中 / 误报 / 漏报 / 正确静默 | 1 / 0 / 0 / 1 | 1 / 0 / 0 / 1 |" in text
    assert "**不确定**" in text
    assert "所有样本两个版本的各维度分数相同" in text
    assert "| 命中样本提前量（交易日，均值） | 14.0 | 14.0 |" in text
    assert paths.backtest_stats_md.read_text("utf-8") == text

    # 样本汇总表的分数与旧 Excel 一致
    old = openpyxl.load_workbook(EXCEL, data_only=True)["样本汇总"]
    new = openpyxl.load_workbook(paths.backtest_history_xlsx)["样本汇总"]
    for i in range(4):
        old_row = [old.cell(5 + i, c).value for c in range(5, 21)]
        new_row = [new.cell(5 + i, c).value for c in range(5, 21)]
        assert new_row == old_row
    assert new.cell(7, 23).value == "是" and new.cell(5, 26).value == "截图"


def test_stats_unreviewed_and_mixed(paths):
    program_runs(paths)     # 自动设为正式记录，reviewed=false
    text, _ = run_stats(paths, db.default_url(paths))
    assert "已复核 0，未复核 4" in text and "程序 4" in text


# ---------------------------------------------------------------------------
# 结果标签辅助字段（SOP 9.3，2026-09-27）
# ---------------------------------------------------------------------------


def test_max_drawdown_and_near_event(paths):
    from market_risk.outcomes import is_near_event, peak_to_trough_drawdown

    assert peak_to_trough_drawdown([100, 110, 99, 105]) == pytest.approx(-10.0)     # 110 → 99
    assert peak_to_trough_drawdown([100, 101, 102]) == 0.0
    base = D(2025, 10, 31)
    # 先涨后跌：基准日口径 −4.5%（接近事件），最大收盘跌幅 −9.5%（105 → 95.5）
    spx = _closes(base, [100.0, 105.0] + [100.0] * 5 + [95.5] + [99.0] * 13)
    qqq = _closes(base, [100.0] + [98.0] * 20)
    o = compute_outcome(base, spx, qqq, D(2026, 1, 1))
    assert not o.is_event and o.near_event
    assert o.spx_drawdown_from_base == pytest.approx(-4.5)
    assert o.spx_peak_to_trough_drawdown == pytest.approx(-9.0476, abs=1e-3)
    assert o.qqq_peak_to_trough_drawdown == pytest.approx(-2.0)
    assert not is_near_event(-5.5, -1.0, True)            # 已是事件，不再标"接近"
    assert is_near_event(-1.0, -6.0, False) and not is_near_event(-3.99, -5.99, False)

    record_outcome(paths.outcomes_csv, o)
    (back,) = read_outcomes(paths.outcomes_csv)
    assert back == o and back.near_event
    url = db.rebuild(paths, db.default_url(paths))
    row = db.dump(url)["outcomes"][0]
    cols = [c.name for c in db.schema.outcomes.c]
    rec = dict(zip(cols, row, strict=True))
    assert rec["near_event"] is True and rec["spx_peak_to_trough_drawdown"] == Decimal("-9.0476")


def test_stats_show_near_events(paths):
    program_runs(paths)
    record_outcome(paths.outcomes_csv, Outcome("MARKET", D(2025, 10, 31), D(2025, 11, 3), D(2025, 12, 1),
                                               -4.41, -6.90, False, None, "computed", "t", -5.1, -8.2))
    text, _ = run_stats(paths, db.default_url(paths))
    assert "接近事件（仅参考，不改变风险事件定义）：1 个（2025-10-31）" in text
    assert "| -4.41% / -5.10% | -6.90% / -8.20% | 是 |" in text
    sheet = openpyxl.load_workbook(paths.backtest_history_xlsx)["样本汇总"]
    headers = [sheet.cell(4, c).value for c in range(1, sheet.max_column + 1)]
    assert headers[-1] == "接近事件(仅参考)"
    row = next(r for r in range(5, sheet.max_row + 1) if str(sheet.cell(r, 2).value).startswith("2025-10-31"))
    assert sheet.cell(row, len(headers)).value == "是"


def test_peak_to_trough_peak_starts_at_base():
    """峰值起点包含基准日：之后没有更高的收盘价时，峰值即基准日，峰谷回撤等于基准日口径跌幅。"""
    from market_risk.outcomes import peak_to_trough_drawdown

    assert peak_to_trough_drawdown([100.0, 98.0, 95.0, 99.0]) == pytest.approx(-5.0)
    base = D(2025, 10, 31)
    spx = _closes(base, [100.0] + [99.0] * 9 + [96.5] + [98.0] * 10)
    qqq = _closes(base, [100.0] + [99.5] * 20)
    o = compute_outcome(base, spx, qqq, D(2026, 1, 1))
    assert o.spx_peak_to_trough_drawdown == pytest.approx(o.spx_drawdown_from_base) == pytest.approx(-3.5)
    assert o.qqq_peak_to_trough_drawdown == pytest.approx(-0.5)


def test_outcomes_csv_old_column_names(paths):
    """2026-09-27 改名前的 outcomes.csv 仍可读取与重建数据库。"""
    paths.outcomes_csv.parent.mkdir(parents=True)
    paths.outcomes_csv.write_text(
        "subject,base_date,window_start,window_end,spx_min_close_drawdown,qqq_min_close_drawdown,is_event,"
        "event_date,spx_max_drawdown,qqq_max_drawdown,near_event,source,entered_at\n"
        "MARKET,2025-10-31,2025-11-03,2025-12-01,-4.4069,-6.8991,否,,-4.5711,-7.3424,是,computed,t\n",
        encoding="utf-8")
    (o,) = read_outcomes(paths.outcomes_csv)
    assert o.qqq_drawdown_from_base == -6.8991 and o.qqq_peak_to_trough_drawdown == -7.3424 and o.near_event
    rows = db.dump(db.rebuild(paths, db.default_url(paths)))["outcomes"]
    assert len(rows) == 1


def test_migration_0003_keeps_data(paths):
    """从 0002 升级到 0003：列改名，已有数据保留。"""
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    url = db.default_url(paths)
    paths.db_path.parent.mkdir(parents=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0002")
    engine = db.make_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO outcomes (subject, base_date, source, window_start, window_end, spx_min_close_drawdown,"
                " qqq_min_close_drawdown, is_event, spx_max_drawdown, qqq_max_drawdown, near_event)"
                " VALUES ('MARKET', '2025-10-31', 'computed', '2025-11-03', '2025-12-01', -4.4069, -6.8991,"
                " 0, -4.5711, -7.3424, 1)"))
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT qqq_drawdown_from_base, qqq_peak_to_trough_drawdown FROM outcomes")).one()
        assert [round(float(x), 4) for x in row] == [-6.8991, -7.3424]
    finally:
        engine.dispose()
