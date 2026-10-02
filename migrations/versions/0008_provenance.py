"""数据留痕第一步：录入留痕记录、确认记录，以及预留的信号输入关联表。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

DECIMAL = sa.Numeric(20, 8)


def upgrade() -> None:
    op.create_table(
        "provenance_records",
        sa.Column("record_id", sa.String(16), primary_key=True),
        sa.Column("indicator", sa.String(40), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("raw_value", sa.String(40), nullable=False),
        sa.Column("raw_unit", sa.String(16), nullable=False),
        sa.Column("raw_basis", sa.Text(), nullable=False),
        sa.Column("raw_precision", sa.Integer(), nullable=False),
        sa.Column("normalized_value", DECIMAL, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("acquisition_method", sa.String(16), nullable=False),
        sa.Column("first_obtained_at_et", sa.String(40)),
        sa.Column("entered_at_utc", sa.String(40), nullable=False),
        sa.Column("entered_by", sa.String(64), nullable=False),
        sa.Column("is_late", sa.Boolean()),
        sa.Column("source_published_at", sa.String(40)),
        sa.Column("snapshot_path", sa.String(300)),
        sa.Column("snapshot_sha256", sa.String(64)),
        sa.Column("data_version", sa.String(64)),
        sa.Column("code_version", sa.String(40), nullable=False),
        sa.Column("code_dirty", sa.Boolean()),
        sa.Column("revises_record_id", sa.String(16), sa.ForeignKey("provenance_records.record_id")),
        sa.Column("revision_kind", sa.String(16)),
        sa.Column("correction_original_value", sa.String(40)),
        sa.Column("correction_corrected_value", sa.String(40)),
        sa.Column("correction_evidence", sa.Text()),
        sa.Column("historical_backfill", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "provenance_confirmations",
        sa.Column("record_id", sa.String(16), sa.ForeignKey("provenance_records.record_id"), primary_key=True),
        sa.Column("confirmed_by", sa.String(64), nullable=False),
        sa.Column("confirmed_at_utc", sa.String(40), nullable=False),
        sa.Column("self_confirmed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "signal_input_links",
        sa.Column("signal_key", sa.String(200), primary_key=True),
        sa.Column("record_id", sa.String(16), sa.ForeignKey("provenance_records.record_id"), primary_key=True),
        sa.Column("note", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("signal_input_links")
    op.drop_table("provenance_confirmations")
    op.drop_table("provenance_records")
