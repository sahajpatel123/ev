"""G2 P0: explicit STATE_EPOCH lineage table (see app/ops/state_epoch.py)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "o1p2q3r4s5t6"
down_revision = "n0p1q2r3s4t5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "state_epoch" not in tables:
        op.create_table(
            "state_epoch",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("epoch_id", sa.Uuid(), nullable=False, unique=True, index=True),
            sa.Column("previous_epoch_id", sa.Uuid(), nullable=True),
            sa.Column("reason", sa.String(length=256), nullable=False),
            sa.Column("environment", sa.String(length=32), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "state_epoch" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("state_epoch")
