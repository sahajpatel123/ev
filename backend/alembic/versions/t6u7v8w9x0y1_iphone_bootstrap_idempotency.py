"""iPhone bootstrap idempotency: stable client_device_id on devices."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "t6u7v8w9x0y1"
down_revision = "s5t6u7v8w9x0"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def _has_index(bind, table: str, name: str) -> bool:
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "devices") and not _has_column(bind, "devices", "client_device_id"):
        op.add_column("devices", sa.Column("client_device_id", sa.String(128), nullable=True))
    if _has_table(bind, "devices") and not _has_index(bind, "devices", "ix_devices_client_device_id"):
        # NULL-friendly on SQLite and PostgreSQL: only non-NULL ids dedupe.
        op.create_index("ix_devices_client_device_id", "devices", ["client_device_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "devices"):
        if _has_index(bind, "devices", "ix_devices_client_device_id"):
            op.drop_index("ix_devices_client_device_id", table_name="devices")
        if _has_column(bind, "devices", "client_device_id"):
            op.drop_column("devices", "client_device_id")
