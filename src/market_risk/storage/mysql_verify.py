"""MySQL 兼容性验证（docs/decisions/0001，B4）：db verify-mysql。

1. 连接地址来自 .env 的 MYSQL_VERIFY_URL（未设置时跳过）；地址、账户、密码、主机、库名都不输出到任何地方。
2. 验证库必须是专用库：库中只允许本项目的表（及 alembic_version），否则拒绝清空。
3. 在验证库上执行 Alembic 迁移与 rebuild-db；另在临时 SQLite 上 rebuild；两者逐表逐行比对。
4. 再用 db/sql/ 导出的 01_schema_mysql.sql、02_base_data.sql 建库，核对参考表内容；最后恢复为 rebuild 后的状态。
日常的 DATABASE_URL 仍指向 SQLite。
"""

from __future__ import annotations

import datetime as dt
import re
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from market_risk.storage import db, schema
from market_risk.storage.paths import StoragePaths
from market_risk.storage.sql_export import BASE_DATA_FILE, SCHEMA_FILES

ALLOWED_EXTRA = {"alembic_version"}


class VerifyError(RuntimeError):
    """验证无法进行（地址不可用、验证库不是专用库等）。信息中不含连接地址。"""


@dataclass(frozen=True)
class TableCheck:
    table: str
    sqlite_rows: int
    target_rows: int
    equal: bool
    samples: tuple[str, ...] = ()           # 不一致的示例（只含数据，不含连接信息）


@dataclass
class VerifyReport:
    dialect: str
    alembic_version: str | None
    tables: list[TableCheck]
    exported_sql_ok: bool | None = None
    exported_sql_notes: list[str] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)   # 字符集、排序规则、大小写敏感性等（不含连接信息）

    @property
    def ok(self) -> bool:
        return all(t.equal for t in self.tables) and self.exported_sql_ok is not False \
            and self.alembic_version == db.head_revision()


def redact(message: str, url: str) -> str:
    """去掉错误信息中的连接地址及其各部分（账户、密码、主机、库名）。"""
    out = message.replace(url, "***")
    u = make_url(url)
    for part in (u.password, u.username, u.host, u.database):
        if part:
            out = re.sub(re.escape(str(part)), "***", out)
    return out


def with_driver(url: str) -> str:
    """mysql:// 未指定驱动时使用 pymysql（可选依赖 mysql 组），并默认 utf8mb4。"""
    try:
        u = make_url(url)
    except Exception:
        raise VerifyError("MYSQL_VERIFY_URL 格式不正确（应为 mysql+pymysql://用户:密码@主机:端口/库名）") from None
    if u.get_backend_name() == "mysql":
        if u.drivername == "mysql":
            u = u.set(drivername="mysql+pymysql")
        if "charset" not in u.query:
            u = u.update_query_dict({"charset": "utf8mb4"})
        return u.render_as_string(hide_password=False)
    return url


def _normalize(v: Any) -> Any:
    """跨数据库比较的规范化：小数按数值（去掉末尾 0），布尔统一为 bool，日期为 ISO 字符串。"""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v else "NaN"
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, float):
        return format(Decimal(repr(v)).normalize(), "f")
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    return v


def table_rows(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """全部表的规范化内容（去掉自增 id，排序后）。"""
    engine = db.make_engine(url)
    out = {}
    try:
        with engine.connect() as conn:
            for table in schema.TABLES:
                cols = [c for c in table.c if c.name != "id"]
                rows = [tuple(_normalize(v) for v in r) for r in conn.execute(select(*cols))]
                out[table.name] = sorted(rows, key=repr)
    finally:
        engine.dispose()
    return out


def compare(reference: dict[str, list[tuple]], target: dict[str, list[tuple]]) -> list[TableCheck]:
    checks = []
    for name in reference:
        a, b = reference[name], target.get(name, [])
        diff = [repr(r)[:160] for r in sorted(set(a) ^ set(b), key=repr)[:3]] if a != b else []
        checks.append(TableCheck(name, len(a), len(b), a == b, tuple(diff)))
    return checks


def ensure_dedicated(url: str) -> None:
    """验证库只能含本项目的表（或为空），否则拒绝清空。"""
    engine = db.make_engine(url)
    try:
        with engine.connect() as conn:
            names = set(inspect(conn).get_table_names())
    finally:
        engine.dispose()
    foreign = names - set(db.TABLE_NAMES) - ALLOWED_EXTRA
    if foreign:
        raise VerifyError(f"验证库中有 {len(foreign)} 张非本项目的表，拒绝清空；请使用专用于验证的空库")


def _server_settings(url: str) -> dict[str, str]:
    engine = db.make_engine(url)
    try:
        with engine.connect() as conn:
            if engine.dialect.name != "mysql":
                return {"dialect": engine.dialect.name}
            row = conn.execute(text(
                "SELECT @@character_set_database, @@collation_database, @@lower_case_table_names, VERSION()")).one()
            case = conn.execute(text("SELECT 'A' = 'a'")).scalar()
            return {"character_set_database": str(row[0]), "collation_database": str(row[1]),
                    "lower_case_table_names": str(row[2]), "server_version": str(row[3]).split("-")[0],
                    "string_compare_case_insensitive": "是" if case else "否"}
    finally:
        engine.dispose()


def _alembic_version(url: str) -> str | None:
    engine = db.make_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";")]


def check_exported_sql(url: str, sql_dir: Path, dialect: str, reference: dict[str, list[tuple]]) -> list[str]:
    """用导出的 SQL 在目标库建表并写入参考表，核对与 SQLite rebuild 的参考表内容一致。返回问题（空为通过）。"""
    db._reset(url)
    engine = db.make_engine(url)
    try:
        with engine.begin() as conn:
            for name in (SCHEMA_FILES[dialect], BASE_DATA_FILE):
                for stmt in _statements((sql_dir / name).read_text(encoding="utf-8")):
                    conn.exec_driver_sql(stmt.rstrip(";"))
    finally:
        engine.dispose()
    got = table_rows(url)
    problems = []
    for t in schema.TABLES:
        want = reference[t.name] if t in schema.REFERENCE_TABLES else []
        if got[t.name] != want:
            problems.append(f"{t.name}：导出 SQL 建库后 {len(got[t.name])} 行，期望 {len(want)} 行")
    return problems


def verify(paths: StoragePaths, target_url: str, sql_dir: Path | None = None) -> VerifyReport:
    """在目标库上迁移并 rebuild，与临时 SQLite 的 rebuild 逐表逐行比对；再核对导出的 SQL。"""
    try:
        ensure_dedicated(target_url)
        settings = _server_settings(target_url)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_url = db.sqlite_url(Path(tmp) / "reference.sqlite")
            db.rebuild(paths, sqlite_url)
            reference = table_rows(sqlite_url)
        db.rebuild(paths, target_url)
        dialect = make_url(target_url).get_backend_name()
        report = VerifyReport(dialect, _alembic_version(target_url), compare(reference, table_rows(target_url)),
                              settings=settings)
        if sql_dir is not None and dialect in SCHEMA_FILES:
            report.exported_sql_notes = check_exported_sql(target_url, sql_dir, dialect, reference)
            report.exported_sql_ok = not report.exported_sql_notes
            db.rebuild(paths, target_url)           # 恢复为 rebuild 后的状态
    except VerifyError:
        raise
    except Exception as exc:  # 数据库错误信息可能含连接信息：脱敏后抛出
        raise VerifyError(f"{type(exc).__name__}：{redact(str(exc), target_url)}") from None
    return report
