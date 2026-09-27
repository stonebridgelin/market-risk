"""db/sql/ 下的 SQL 文件（docs/decisions/0001，STORAGE 4.2）：由程序导出，不得手工修改。

- 01_schema_sqlite.sql、01_schema_mysql.sql：当前 Alembic 最新版本的表结构（导出前核对 schema.py 与迁移结果一致）；
- 02_base_data.sql：参考表（标的登记、休市日历、裁定日期表）的 INSERT 语句，由 config/ 下的 YAML 生成；
  标识符按 MySQL 规则加反引号（SQLite 同样接受），两种数据库都可执行。
- 文件中不含账户、密码或本机路径；不含生成时间，内容只随表结构与 YAML 变化（测试据此检查是否已重新导出）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from market_risk.storage import db, schema

SCHEMA_FILES = {"sqlite": "01_schema_sqlite.sql", "mysql": "01_schema_mysql.sql"}
BASE_DATA_FILE = "02_base_data.sql"
_DIALECTS = {"sqlite": sqlite.dialect(), "mysql": mysql.dialect()}
_HEADER = "-- 由 `market-risk db export-sql` 自动生成，不得手工修改。"


class SchemaMismatchError(RuntimeError):
    """storage/schema.py 与 Alembic 最新迁移的表结构不一致。"""


def schema_differences() -> list[Any]:
    """在临时的内存 SQLite 上执行全部迁移，与 schema.metadata 比较（Alembic autogenerate 的比较逻辑）。"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            _upgrade_on(conn)
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diffs = compare_metadata(ctx, schema.metadata)
    finally:
        engine.dispose()
    return [d for d in diffs if not (isinstance(d, tuple) and d and d[0] == "remove_table"
                                     and getattr(d[1], "name", "") == "alembic_version")]


def _upgrade_on(conn: Any) -> None:
    from alembic import command
    from alembic.config import Config

    from market_risk.config import PROJECT_ROOT

    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "head")


def schema_sql(dialect: str) -> str:
    """表结构的 CREATE 语句（按依赖顺序），另建 alembic_version 并写入最新版本号，便于以后继续迁移。"""
    d = _DIALECTS[dialect]
    head = db.head_revision()
    lines = [_HEADER, f"-- 表结构：Alembic 版本 {head}（migrations/versions/），数据库：{dialect}。",
             "-- 具体数据不在此导出，见 db/sql/README.md。"]
    if dialect == "mysql":
        lines.append("-- 建库时使用 utf8mb4 字符集（CREATE DATABASE ... CHARACTER SET utf8mb4）。")
    lines.append("")
    for table in schema.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=d)).strip()
        lines.append("\n".join(x.rstrip() for x in ddl.splitlines()) + ";")
        lines += [str(CreateIndex(ix).compile(dialect=d)).strip() + ";" for ix in sorted(table.indexes, key=str)]
        lines.append("")
    q = d.identifier_preparer.quote
    lines += [f"CREATE TABLE {q('alembic_version')} ({q('version_num')} VARCHAR(32) NOT NULL, "
              f"PRIMARY KEY ({q('version_num')}));",
              f"INSERT INTO {q('alembic_version')} ({q('version_num')}) VALUES ('{head}');", ""]
    return "\n".join(lines)


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, dt.date):
        return f"'{v.isoformat()}'"
    if isinstance(v, Decimal | int | float):
        return format(Decimal(str(v)).normalize(), "f")
    s = str(v)
    if "\\" in s:   # 反斜杠在 MySQL 中是转义符、在 SQLite 中不是：同一文件无法兼容，要求 YAML 不含反斜杠
        raise ValueError(f"参考数据含反斜杠，无法生成两种数据库通用的 INSERT：{s[:60]!r}")
    return "'" + s.replace("'", "''") + "'"


def base_data_sql(config_dir: Path | None = None) -> str:
    """参考表的 INSERT 语句（由 YAML 生成）。"""
    rows = db.reference_rows(config_dir)
    q = _DIALECTS["mysql"].identifier_preparer.quote   # 反引号：MySQL 需要（如 usage 为保留字），SQLite 同样接受
    lines = [_HEADER, "-- 参考表：标的登记（config/symbols.yaml）、休市日历（config/holidays.yaml）、"
             "裁定日期表（config/data_decisions.yaml）。YAML 为源头。", ""]
    for table in schema.REFERENCE_TABLES:
        cols = [c.name for c in table.c]
        lines.append(f"-- {table.name}：{len(rows[table.name])} 行")
        head = f"INSERT INTO {q(table.name)} ({', '.join(q(c) for c in cols)}) VALUES"
        lines += [f"{head} ({', '.join(_literal(r.get(c)) for c in cols)});" for r in rows[table.name]]
        lines.append("")
    return "\n".join(lines)


def export_texts(config_dir: Path | None = None) -> dict[str, str]:
    diffs = schema_differences()
    if diffs:
        raise SchemaMismatchError(f"storage/schema.py 与 Alembic 最新迁移不一致：{diffs[:5]}")
    out = {name: schema_sql(d) for d, name in SCHEMA_FILES.items()}
    out[BASE_DATA_FILE] = base_data_sql(config_dir)
    return out


def export_sql(directory: Path, config_dir: Path | None = None) -> list[Path]:
    """写出 db/sql/ 下的 SQL 文件，返回写出的路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in export_texts(config_dir).items():
        path = directory / name
        path.write_bytes(text.encode("utf-8"))      # 固定 LF 换行
        written.append(path)
    return written


def stale_files(directory: Path, config_dir: Path | None = None) -> list[str]:
    """与当前表结构、YAML 不一致（需要重新导出）的文件名。"""
    return [name for name, text in export_texts(config_dir).items()
            if not (directory / name).exists() or (directory / name).read_bytes() != text.encode("utf-8")]
