"""结果标签辅助字段：最大收盘跌幅、接近事件（SOP 9.3，仅作参考）

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        batch.add_column(sa.Column("spx_max_drawdown", sa.Numeric(20, 8, asdecimal=True)))
        batch.add_column(sa.Column("qqq_max_drawdown", sa.Numeric(20, 8, asdecimal=True)))
        batch.add_column(sa.Column("near_event", sa.Boolean))


def downgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        batch.drop_column("near_event")
        batch.drop_column("qqq_max_drawdown")
        batch.drop_column("spx_max_drawdown")
