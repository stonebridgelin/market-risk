"""结果标签字段改名：drawdown_from_base（基准日口径）与 peak_to_trough_drawdown（峰谷回撤）

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
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
# 四列在 0001/0002 中均为 Numeric(20, 8)、可为空、无默认值。MySQL 的 CHANGE COLUMN 需要完整列定义，
# 这里只补充原有列信息，不改变表结构（2026-09-27 为兼容 MySQL 补充）。
EXISTING = {"existing_type": sa.Numeric(20, 8, asdecimal=True), "existing_nullable": True,
            "existing_server_default": None}


def upgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        for old, new in RENAMES:
            batch.alter_column(old, new_column_name=new, **EXISTING)


def downgrade() -> None:
    with op.batch_alter_table("outcomes") as batch:
        for old, new in RENAMES:
            batch.alter_column(new, new_column_name=old, **EXISTING)
