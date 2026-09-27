"""参考表：标的登记、休市日历、已裁定日期表（内容由 config/ 下的 YAML 生成，YAML 为源头）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "symbols",
        sa.Column("symbol", sa.String(40), primary_key=True),
        sa.Column("tv_symbol", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.Text),
        sa.Column("category", sa.String(32)),
        sa.Column("usage", sa.String(16), nullable=False),
        sa.Column("unit", sa.String(16)),
        sa.Column("timezone", sa.String(40)),
        sa.Column("calendar", sa.String(16)),
        sa.Column("inception", sa.Date),
        sa.Column("api_source", sa.String(64)),
        sa.Column("tolerance", sa.Numeric(20, 8)),
        sa.Column("crosscheck_note", sa.Text),
        sa.Column("filename_aliases", sa.Text),
        sa.Column("known_values", sa.Text),
    )
    op.create_table(
        "market_holidays",
        sa.Column("market", sa.String(8), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("note", sa.Text),
    )
    op.create_table(
        "data_decisions",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("symbol", sa.String(40), primary_key=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("decided_on", sa.Date, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("data_decisions")
    op.drop_table("market_holidays")
    op.drop_table("symbols")
