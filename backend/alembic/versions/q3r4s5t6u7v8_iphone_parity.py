"""iPhone parity: endpoint profile, lease identity, receipts, inbox, offline queue."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "q3r4s5t6u7v8"
down_revision = "p2q3r4s5t6u7"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    json_type = JSONB() if bind.dialect.name == "postgresql" else sa.JSON()
    if _has_table(bind, "devices") and not _has_column(bind, "devices", "endpoint_profile"):
        op.add_column("devices", sa.Column("endpoint_profile", json_type, nullable=True))
    if _has_table(bind, "conversation_leases"):
        if not _has_column(bind, "conversation_leases", "session_id"):
            op.add_column("conversation_leases", sa.Column("session_id", sa.String(64), nullable=True))
        if not _has_column(bind, "conversation_leases", "client_generation"):
            op.add_column(
                "conversation_leases",
                sa.Column("client_generation", sa.Integer(), nullable=False, server_default="0"),
            )
    if not _has_table(bind, "phone_turn_receipts"):
        op.create_table(
            "phone_turn_receipts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False, server_default="final_transcript"),
            sa.Column("transcript", sa.Text(), nullable=False, server_default=""),
            sa.Column("session_id", sa.String(64), nullable=True),
            sa.Column("lease_id", sa.String(64), nullable=True),
            sa.Column("provider_item_id", sa.String(128), nullable=True),
            sa.Column("provider_response_id", sa.String(128), nullable=True),
            sa.Column("action_calls", json_type, nullable=True),
            sa.Column("evidence", json_type, nullable=True),
            sa.Column("durable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("life_mutation", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trusted_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_phone_turn_receipts_device_id", "phone_turn_receipts", ["device_id"])
        op.create_index("ix_phone_turn_receipts_idempotency_key", "phone_turn_receipts", ["idempotency_key"], unique=True)
    if not _has_table(bind, "phone_action_records"):
        op.create_table(
            "phone_action_records",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("action_id", sa.String(80), nullable=False),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("operation", sa.String(64), nullable=False, server_default=""),
            sa.Column("state", sa.String(32), nullable=False, server_default="created"),
            sa.Column("result", sa.String(64), nullable=True),
            sa.Column("executed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("payload", json_type, nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_phone_action_records_action_id", "phone_action_records", ["action_id"], unique=True)
    if not _has_table(bind, "device_inbox_items"):
        op.create_table(
            "device_inbox_items",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False, server_default="notice"),
            sa.Column("title", sa.String(160), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("payload", json_type, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_device_inbox_items_device_id", "device_inbox_items", ["device_id"])
    if not _has_table(bind, "offline_queue_items"):
        op.create_table(
            "offline_queue_items",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False, server_default="request"),
            sa.Column("payload", json_type, nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("device_id", "idempotency_key", name="uq_offline_queue_device_key"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "offline_queue_items",
        "device_inbox_items",
        "phone_action_records",
        "phone_turn_receipts",
    ):
        if _has_table(bind, table):
            op.drop_table(table)
    if _has_table(bind, "conversation_leases"):
        if _has_column(bind, "conversation_leases", "client_generation"):
            op.drop_column("conversation_leases", "client_generation")
        if _has_column(bind, "conversation_leases", "session_id"):
            op.drop_column("conversation_leases", "session_id")
    if _has_table(bind, "devices") and _has_column(bind, "devices", "endpoint_profile"):
        op.drop_column("devices", "endpoint_profile")
