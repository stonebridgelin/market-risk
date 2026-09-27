"""初始表结构（阶段4.5 的八张表 + officials）

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DECIMAL = sa.Numeric(20, 8, asdecimal=True)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_key", sa.String(200), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("framework", sa.String(40), nullable=False),
        sa.Column("base_date", sa.Date, nullable=False),
        sa.Column("mode", sa.String(16)),
        sa.Column("created_at_utc", sa.String(40)),
        sa.Column("created_at_local", sa.String(40)),
        sa.Column("git_commit", sa.String(40)),
        sa.Column("git_dirty", sa.Boolean),
        sa.Column("data_source_type", sa.String(16)),
        sa.Column("status", sa.String(16)),
        sa.Column("run_dir", sa.String(300)),
        sa.Column("is_official", sa.Boolean, nullable=False),
        sa.Column("reviewed", sa.Boolean, nullable=False),
        sa.Column("official_set_by", sa.String(32)),
    )
    op.create_table(
        "officials",
        sa.Column("subject", sa.String(16), primary_key=True),
        sa.Column("framework", sa.String(40), primary_key=True),
        sa.Column("base_date", sa.Date, primary_key=True),
        sa.Column("run_key", sa.String(200), sa.ForeignKey("runs.run_key"), nullable=False),
        sa.Column("set_by", sa.String(32)),
        sa.Column("reviewed", sa.Boolean, nullable=False),
        sa.Column("set_at_utc", sa.String(40)),
        sa.Column("reviewed_at_utc", sa.String(40)),
    )
    op.create_table(
        "dimension_scores",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_key", sa.String(200), sa.ForeignKey("runs.run_key"), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("dimension", sa.String(16), nullable=False),
        sa.Column("score", sa.Integer),
        sa.Column("possible_scores", sa.String(32)),
        sa.Column("triggered_conditions", sa.Text),
        sa.Column("pending_reason", sa.Text),
        sa.Column("calculation", sa.Text),
    )
    op.create_table(
        "totals",
        sa.Column("run_key", sa.String(200), sa.ForeignKey("runs.run_key"), primary_key=True),
        sa.Column("version", sa.String(16), primary_key=True),
        sa.Column("total", sa.Integer),
        sa.Column("total_min", sa.Integer, nullable=False),
        sa.Column("total_max", sa.Integer, nullable=False),
        sa.Column("stage", sa.String(16)),
        sa.Column("clear_deterioration", sa.String(8)),
        sa.Column("alert", sa.String(8)),
        sa.Column("review_flags", sa.Text),
        sa.Column("notes", sa.Text),
    )
    op.create_table(
        "metrics",
        sa.Column("run_key", sa.String(200), sa.ForeignKey("runs.run_key"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", DECIMAL),
        sa.Column("text", sa.Text),
    )
    op.create_table(
        "near_threshold",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_key", sa.String(200), sa.ForeignKey("runs.run_key"), nullable=False),
        sa.Column("item", sa.Text, nullable=False),
        sa.Column("value", DECIMAL),
        sa.Column("threshold", DECIMAL),
        sa.Column("gap", DECIMAL),
        sa.Column("unit", sa.String(16)),
    )
    op.create_table(
        "outcomes",
        sa.Column("subject", sa.String(16), primary_key=True),
        sa.Column("base_date", sa.Date, primary_key=True),
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("window_start", sa.Date, nullable=False),
        sa.Column("window_end", sa.Date, nullable=False),
        sa.Column("spx_min_close_drawdown", DECIMAL),
        sa.Column("qqq_min_close_drawdown", DECIMAL),
        sa.Column("is_event", sa.Boolean, nullable=False),
        sa.Column("event_date", sa.Date),
        sa.Column("entered_at", sa.String(40)),
    )
    op.create_table(
        "materials",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("base_date", sa.Date, nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("file_path", sa.String(300), nullable=False),
        sa.Column("source", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("added_at", sa.String(40)),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("base_date", sa.Date),
        sa.Column("reviewer", sa.String(32), nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("content", sa.Text),
        sa.Column("impact", sa.Text),
        sa.Column("run_key", sa.String(200)),
        sa.Column("other_run_key", sa.String(200)),
        sa.Column("created_at", sa.String(40)),
    )


def downgrade() -> None:
    for table in ("reviews", "materials", "outcomes", "near_threshold", "metrics", "totals",
                  "dimension_scores", "officials", "runs"):
        op.drop_table(table)
