"""阶段6 逐日历史回测：回测运行、逐日分数、回测结果标签（隔离）、回调事件（隔离）

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

DECIMAL = sa.Numeric(20, 8, asdecimal=True)
DIMS = ("price", "breadth", "vix", "rates", "credit")


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("created_at_utc", sa.String(40)),
        sa.Column("git_commit", sa.String(40)),
        sa.Column("git_dirty", sa.Boolean),
        sa.Column("market_manifest_sha256", sa.String(64)),
        sa.Column("config_sha256", sa.String(64)),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("versions", sa.String(32), nullable=False),
        sa.Column("days", sa.Integer, nullable=False),
        sa.Column("runtime_seconds", DECIMAL),
        sa.Column("holdout_unlocked_at", sa.String(40)),
        sa.Column("is_official", sa.Boolean, nullable=False),
        sa.Column("run_dir", sa.String(300), nullable=False),
    )
    op.create_table(
        "backtest_daily_scores",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("backtest_runs.run_id"), primary_key=True),
        sa.Column("base_date", sa.Date, primary_key=True),
        sa.Column("version", sa.String(16), primary_key=True),
        *[sa.Column(d, sa.Integer) for d in DIMS],
        *[sa.Column(f"{d}_possible", sa.String(16)) for d in DIMS],
        sa.Column("total", sa.Integer),
        sa.Column("total_min", sa.Integer, nullable=False),
        sa.Column("total_max", sa.Integer, nullable=False),
        sa.Column("stage", sa.String(16)),
        sa.Column("pending_dimensions", sa.Text),
        sa.Column("clear_deterioration", sa.String(8)),
        sa.Column("alert", sa.String(8)),
        sa.Column("flags", sa.Text),
    )
    op.create_table(
        "backtest_outcomes",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("backtest_runs.run_id"), primary_key=True),
        sa.Column("base_date", sa.Date, primary_key=True),
        sa.Column("window_start", sa.Date, nullable=False),
        sa.Column("window_end", sa.Date, nullable=False),
        sa.Column("spx_drawdown_from_base", DECIMAL),
        sa.Column("qqq_drawdown_from_base", DECIMAL),
        sa.Column("spx_peak_to_trough_drawdown", DECIMAL),
        sa.Column("qqq_peak_to_trough_drawdown", DECIMAL),
        sa.Column("is_event", sa.Boolean, nullable=False),
        sa.Column("event_date", sa.Date),
        sa.Column("is_near_event", sa.Boolean, nullable=False),
        sa.Column("period", sa.String(16)),
        sa.Column("crosses_period", sa.Boolean, nullable=False),
    )
    op.create_table(
        "pullback_episodes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("backtest_runs.run_id"), nullable=False),
        sa.Column("symbol", sa.String(8), nullable=False),
        sa.Column("level", DECIMAL, nullable=False),
        sa.Column("high_date", sa.Date, nullable=False),
        sa.Column("high_close", DECIMAL, nullable=False),
        sa.Column("low_date", sa.Date),
        sa.Column("low_close", DECIMAL),
        sa.Column("drawdown_pct", DECIMAL),
        sa.Column("trading_days", sa.Integer),
        sa.Column("grade", sa.String(16)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confirm_date", sa.Date),
        sa.Column("recovery_date", sa.Date),
        sa.Column("recovery_note", sa.Text),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("before_start", sa.Boolean, nullable=False),
        sa.Column("crosses_boundary", sa.Boolean, nullable=False),
        sa.Column("counted", sa.Boolean, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pullback_episodes")
    op.drop_table("backtest_outcomes")
    op.drop_table("backtest_daily_scores")
    op.drop_table("backtest_runs")
