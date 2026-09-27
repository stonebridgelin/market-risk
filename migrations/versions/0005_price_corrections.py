"""裁定表增加人工价格修正值与证据来源；YAML 仍为唯一源头。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_decisions", sa.Column("corrected_value", sa.Numeric(20, 8)))
    op.add_column("data_decisions", sa.Column("evidence_source", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("data_decisions") as batch:
        batch.drop_column("evidence_source")
        batch.drop_column("corrected_value")
