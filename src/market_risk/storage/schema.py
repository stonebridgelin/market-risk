"""数据库表结构（SQLAlchemy Core，docs/STORAGE.md 第4节）。

约定：只用通用类型（不用任何数据库的专有语法）；小数一律 Numeric，与程序的 Decimal 精确计算一致；
表结构变更用 Alembic 迁移（migrations/），本文件描述迁移后的最新结构。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

metadata = MetaData()
DECIMAL = Numeric(20, 8, asdecimal=True)

runs = Table(
    "runs", metadata,
    Column("run_key", String(200), primary_key=True),       # 对象/框架/基准日/run_id
    Column("run_id", String(64), nullable=False),
    Column("subject", String(16), nullable=False),
    Column("framework", String(40), nullable=False),
    Column("base_date", Date, nullable=False),
    Column("mode", String(16)),
    Column("created_at_utc", String(40)),
    Column("created_at_local", String(40)),
    Column("git_commit", String(40)),
    Column("git_dirty", Boolean),
    Column("data_source_type", String(16)),
    Column("status", String(16)),
    Column("run_dir", String(300)),
    Column("is_official", Boolean, nullable=False),
    Column("reviewed", Boolean, nullable=False),
    Column("official_set_by", String(32)),
)

# 同一对象、框架、基准日只有一个正式记录：由复合主键保证（通用写法）
officials = Table(
    "officials", metadata,
    Column("subject", String(16), primary_key=True),
    Column("framework", String(40), primary_key=True),
    Column("base_date", Date, primary_key=True),
    Column("run_key", String(200), ForeignKey("runs.run_key"), nullable=False),
    Column("set_by", String(32)),
    Column("reviewed", Boolean, nullable=False),
    Column("set_at_utc", String(40)),
    Column("reviewed_at_utc", String(40)),
)

dimension_scores = Table(
    "dimension_scores", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_key", String(200), ForeignKey("runs.run_key"), nullable=False),
    Column("version", String(16), nullable=False),
    Column("dimension", String(16), nullable=False),
    Column("score", Integer),
    Column("possible_scores", String(32)),
    Column("triggered_conditions", Text),
    Column("pending_reason", Text),
    Column("calculation", Text),
)

totals = Table(
    "totals", metadata,
    Column("run_key", String(200), ForeignKey("runs.run_key"), primary_key=True),
    Column("version", String(16), primary_key=True),
    Column("total", Integer),
    Column("total_min", Integer, nullable=False),
    Column("total_max", Integer, nullable=False),
    Column("stage", String(16)),
    Column("clear_deterioration", String(8)),
    Column("alert", String(8)),
    Column("review_flags", Text),
    Column("notes", Text),
)

metrics = Table(
    "metrics", metadata,
    Column("run_key", String(200), ForeignKey("runs.run_key"), primary_key=True),
    Column("key", String(64), primary_key=True),
    Column("value", DECIMAL),
    Column("text", Text),
)

near_threshold = Table(
    "near_threshold", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_key", String(200), ForeignKey("runs.run_key"), nullable=False),
    Column("item", Text, nullable=False),
    Column("value", DECIMAL),
    Column("threshold", DECIMAL),
    Column("gap", DECIMAL),
    Column("unit", String(16)),
)

outcomes = Table(
    "outcomes", metadata,
    Column("subject", String(16), primary_key=True),
    Column("base_date", Date, primary_key=True),
    Column("source", String(16), primary_key=True),
    Column("window_start", Date, nullable=False),
    Column("window_end", Date, nullable=False),
    Column("spx_min_close_drawdown", DECIMAL),
    Column("qqq_min_close_drawdown", DECIMAL),
    Column("is_event", Boolean, nullable=False),
    Column("event_date", Date),
    Column("entered_at", String(40)),
    # 迁移 0002：辅助字段（仅作参考，不改变 is_event）
    Column("spx_max_drawdown", DECIMAL),
    Column("qqq_max_drawdown", DECIMAL),
    Column("near_event", Boolean),
)

materials = Table(
    "materials", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject", String(16), nullable=False),
    Column("base_date", Date, nullable=False),
    Column("type", String(32), nullable=False),
    Column("file_path", String(300), nullable=False),
    Column("source", Text),
    Column("note", Text),
    Column("added_at", String(40)),
)

reviews = Table(
    "reviews", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject", String(16), nullable=False),
    Column("base_date", Date),
    Column("reviewer", String(32), nullable=False),
    Column("category", Text, nullable=False),
    Column("content", Text),
    Column("impact", Text),
    Column("run_key", String(200)),
    Column("other_run_key", String(200)),
    Column("created_at", String(40)),
)

TABLES = (runs, officials, dimension_scores, totals, metrics, near_threshold, outcomes, materials, reviews)
