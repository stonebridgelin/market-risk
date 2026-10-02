"""数据留痕：原始快照缺失的原因；已有来源文件的路径与该文件自身的 SHA-256。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provenance_records") as batch:
        batch.add_column(sa.Column("snapshot_missing_reason", sa.Text()))
        batch.add_column(sa.Column("source_file_path", sa.String(300)))
        batch.add_column(sa.Column("source_file_sha256", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("provenance_records") as batch:
        batch.drop_column("source_file_sha256")
        batch.drop_column("source_file_path")
        batch.drop_column("snapshot_missing_reason")
