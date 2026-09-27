"""结果标签字段改名：drawdown_from_base（基准日口径）与 peak_to_trough_drawdown（峰谷回撤）

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RENAMES = (
    ("spx_min_close_drawdown", "spx_drawdown_from_base"),
    ("qqq_min_close_drawdown", "qqq_drawdown_from_base"),
    ("spx_max_drawdown", "spx_peak_to_trough_drawdown"),
    ("qqq_max_drawdown", "qqq_peak_to_trough_drawdown"),
)


def upgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        for old, new in RENAMES:
            batch.alter_column(old, new_column_name=new)


def downgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        for old, new in RENAMES:
            batch.alter_column(new, new_column_name=old)
