"""Device Gateway tables + Device role/memory_scope columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "k6l7m8n9o0p1"
down_revision = "j5k6l7m8n9o0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {col["name"] for col in inspector.get_columns("devices")}
    if "role" not in cols:
        op.add_column("devices", sa.Column("role", sa.String(32), nullable=True))
        op.create_index("ix_devices_role", "devices", ["role"])
    if "memory_scope" not in cols:
        op.add_column("devices", sa.Column("memory_scope", sa.String(16), nullable=True))
        op.create_index("ix_devices_memory_scope", "devices", ["memory_scope"])
    if "client_version" not in cols:
        op.add_column("devices", sa.Column("client_version", sa.String(64), nullable=True))
    if "protocol_version" not in cols:
        op.add_column("devices", sa.Column("protocol_version", sa.String(16), nullable=True))

    tables = set(inspector.get_table_names())
    if "device_pairing_tokens" not in tables:
        op.create_table(
            "device_pairing_tokens",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="companion"),
            sa.Column("display_name", sa.String(128), nullable=False, server_default="Evie phone"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_device_pairing_tokens_token_hash", "device_pairing_tokens", ["token_hash"], unique=True)
        op.create_index("ix_device_pairing_tokens_expires_at", "device_pairing_tokens", ["expires_at"])
        op.create_index("ix_device_pairing_tokens_device_id", "device_pairing_tokens", ["device_id"])
    if "conversation_leases" not in tables:
        op.create_table(
            "conversation_leases",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("owner_key", sa.String(64), nullable=False, server_default="owner"),
            sa.Column("lease_id", sa.String(64), nullable=False),
            sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("instance_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("method", sa.String(32), nullable=False, server_default="manual"),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_conversation_leases_owner_key", "conversation_leases", ["owner_key"], unique=True)
        op.create_index("ix_conversation_leases_lease_id", "conversation_leases", ["lease_id"])
        op.create_index("ix_conversation_leases_device_id", "conversation_leases", ["device_id"])
        op.create_index("ix_conversation_leases_expires_at", "conversation_leases", ["expires_at"])
    if "active_conversation_states" not in tables:
        op.create_table(
            "active_conversation_states",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("owner_key", sa.String(64), nullable=False, server_default="owner"),
            sa.Column("active_device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=True),
            sa.Column("topic", sa.String(240), nullable=True),
            sa.Column("turns", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_active_conversation_states_owner_key",
            "active_conversation_states",
            ["owner_key"],
            unique=True,
        )
        op.create_index("ix_active_conversation_states_expires_at", "active_conversation_states", ["expires_at"])
    if "sandbox_facts" not in tables:
        op.create_table(
            "sandbox_facts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("namespace", sa.String(64), nullable=False, server_default="cross_platform_test"),
            sa.Column("fact_key", sa.String(160), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("source_device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("namespace", "fact_key", name="uq_sandbox_facts_ns_key"),
        )
        op.create_index("ix_sandbox_facts_namespace", "sandbox_facts", ["namespace"])
        op.create_index("ix_sandbox_facts_fact_key", "sandbox_facts", ["fact_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name in (
        "sandbox_facts",
        "active_conversation_states",
        "conversation_leases",
        "device_pairing_tokens",
    ):
        if name in tables:
            op.drop_table(name)
