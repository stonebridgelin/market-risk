"""参考表、db/sql/ 导出与数据库兼容性验证（B2–B4）。只写入临时目录；MySQL 集成测试默认跳过。"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from market_risk import services
from market_risk.config import PROJECT_ROOT, get_optional_secret, load_data_decisions, load_settings, load_symbols
from market_risk.storage import db, mysql_verify, schema, sql_export
from market_risk.storage.paths import StoragePaths

D = dt.date
SQL_DIR = PROJECT_ROOT / "db" / "sql"


@pytest.fixture()
def paths(tmp_path) -> StoragePaths:
    return StoragePaths(tmp_path)


def test_schema_matches_alembic_head():
    """表结构以 Alembic 为唯一源头：storage/schema.py 必须与迁移结果一致。"""
    assert sql_export.schema_differences() == []


def test_committed_sql_files_are_up_to_date():
    """表结构或 YAML 变化后未重新导出时失败：请运行 uv run market-risk db export-sql。"""
    assert sql_export.stale_files(SQL_DIR) == [], "db/sql/ 未重新导出：请运行 uv run market-risk db export-sql"


def test_stale_detection(tmp_path):
    sql_export.export_sql(tmp_path)
    assert sql_export.stale_files(tmp_path) == []
    (tmp_path / "02_base_data.sql").write_text("-- 手工修改\n", encoding="utf-8")
    assert sql_export.stale_files(tmp_path) == ["02_base_data.sql"]


def test_sql_files_have_no_secrets_or_local_paths():
    for f in SQL_DIR.glob("*.sql"):
        body = f.read_text(encoding="utf-8")
        for bad in ("://", "password", "Users", "C:\\", "@localhost", "api_key"):
            assert bad not in body, (f.name, bad)


def test_reference_tables_from_yaml(paths):
    url = db.rebuild(paths, db.default_url(paths))
    rows = mysql_verify.table_rows(url)
    assert len(rows["symbols"]) == len(load_symbols())
    assert len(rows["data_decisions"]) == len(load_data_decisions())
    engine = db.make_engine(url)
    try:
        with engine.connect() as conn:
            dec = conn.execute(schema.data_decisions.select().where(schema.data_decisions.c.symbol == "PCCE")).all()
            hol = conn.execute(schema.market_holidays.select().where(
                schema.market_holidays.c.date == D(2012, 10, 30), schema.market_holidays.c.market == "bond")).one()
            spy = conn.execute(schema.symbols.select().where(schema.symbols.c.symbol == "SPY")).one()
    finally:
        engine.dispose()
    assert {r.decision for r in dec} == {"invalid"}
    assert hol.kind == "holiday" and "飓风桑迪" in hol.note
    assert spy.tv_symbol == "BATS:SPY" and '"2025-10-31": 682.06' in spy.known_values


def test_exported_sqlite_sql_builds_same_database(paths, tmp_path):
    """01_schema_sqlite.sql + 02_base_data.sql 建出的库与 Alembic + rebuild 的参考表一致。"""
    reference = mysql_verify.table_rows(db.rebuild(paths, db.default_url(paths)))
    target = db.sqlite_url(tmp_path / "from_sql.sqlite")
    assert mysql_verify.check_exported_sql(target, SQL_DIR, "sqlite", reference) == []
    con = sqlite3.connect(tmp_path / "from_sql.sqlite")
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == db.head_revision()
    finally:
        con.close()


def test_verify_logic_with_sqlite_target(paths, tmp_path):
    """验证流程与数据库无关：以另一个 SQLite 为目标库时逐表一致。"""
    report = mysql_verify.verify(paths, db.sqlite_url(tmp_path / "target.sqlite"), SQL_DIR)
    assert report.ok and all(t.equal for t in report.tables) and report.exported_sql_ok
    assert {t.table for t in report.tables} == set(db.TABLE_NAMES)


def test_verify_refuses_foreign_tables(paths, tmp_path):
    target = tmp_path / "other.sqlite"
    con = sqlite3.connect(target)
    con.execute("CREATE TABLE customers (id INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(mysql_verify.VerifyError, match="拒绝清空"):
        mysql_verify.verify(paths, db.sqlite_url(target))
    assert target.exists()


def test_compare_normalizes_types():
    from decimal import Decimal

    a = {"t": [(Decimal("682.0600000000"), True, "2025-10-31")]}
    b = {"t": [(Decimal("682.06"), True, "2025-10-31")]}
    na = {"t": [tuple(mysql_verify._normalize(v) for v in r) for r in a["t"]]}
    nb = {"t": [tuple(mysql_verify._normalize(v) for v in r) for r in b["t"]]}
    assert mysql_verify.compare(na, nb)[0].equal
    diff = mysql_verify.compare(na, {"t": []})[0]
    assert not diff.equal and diff.sqlite_rows == 1 and diff.target_rows == 0


def test_redact_and_driver():
    url = "mysql://verify_user:s3cret-pw@db.example.internal:3306/verify_db"
    msg = f"(1045, \"Access denied for user 'verify_user'@'db.example.internal'\") [{url}] verify_db s3cret-pw"
    out = mysql_verify.redact(msg, url)
    for secret in ("verify_user", "s3cret-pw", "db.example.internal", "verify_db"):
        assert secret not in out
    full = mysql_verify.with_driver(url)
    assert full.startswith("mysql+pymysql://") and "charset=utf8mb4" in full
    assert mysql_verify.with_driver("sqlite:///x.sqlite") == "sqlite:///x.sqlite"


def test_verify_mysql_skips_without_url(paths, monkeypatch):
    monkeypatch.setattr("market_risk.config.get_optional_secret", lambda *a, **k: None)
    ctx = services.Context(load_settings(), paths, db.default_url(paths))
    assert services.db_verify_mysql(ctx) is None


@pytest.mark.mysql
def test_mysql_compatibility(tmp_path):
    """集成测试（默认跳过）：uv run --extra mysql pytest -m mysql。

    需要 .env 的 MYSQL_VERIFY_URL（专用、可清空的库）。
    """
    url = get_optional_secret("MYSQL_VERIFY_URL")
    if not url:
        pytest.skip("未设置 MYSQL_VERIFY_URL")
    settings = load_settings()
    ctx = services.Context(settings, StoragePaths(settings.storage_root), db.default_url(StoragePaths(tmp_path)))
    report = services.db_verify_mysql(ctx)
    assert report is not None and report.ok, [t for t in report.tables if not t.equal]
