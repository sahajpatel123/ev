"""G2 session authority: devices.auth_revision generation counter.

Bumped on every trust transition (PAIRED_SANDBOX -> TRUSTED_OWNER_DEVICE,
TRUSTED -> REVOKED, un-revoke). Sessions established under an older
generation must rebind; the live transport tick loop compares and closes
stale-authority sockets (bounded invalidation both directions).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "n0p1q2r3s4t5"
down_revision = "m8n9o0p1q2r3"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "devices" in set(sa.inspect(bind).get_table_names()):
        if "auth_revision" not in _columns(bind, "devices"):
            op.add_column(
                "devices",
                sa.Column(
                    "auth_revision", sa.Integer(), nullable=False, server_default="1"
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "devices" in set(sa.inspect(bind).get_table_names()):
        if "auth_revision" in _columns(bind, "devices"):
            op.drop_column("devices", "auth_revision")
