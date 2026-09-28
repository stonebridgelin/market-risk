"""保留数据不齐的回测结果窗口，并允许标签字段为空。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backtest_outcomes") as batch:
        batch.alter_column("is_event", existing_type=sa.Boolean(), existing_nullable=False, nullable=True)
        batch.alter_column("is_near_event", existing_type=sa.Boolean(), existing_nullable=False, nullable=True)
        batch.add_column(sa.Column("data_note", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("backtest_outcomes") as batch:
        batch.drop_column("data_note")
        batch.alter_column("is_near_event", existing_type=sa.Boolean(), existing_nullable=True, nullable=False)
        batch.alter_column("is_event", existing_type=sa.Boolean(), existing_nullable=True, nullable=False)
