"""AGENT 14 PULSE (WAVE LIFE): device registry + push tokens + job lifecycle

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-08-13 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    # Idempotent guards: the always-on API's init_db(create_all) can race
    # ahead of Alembic on a live deployment, so columns/indexes may already
    # exist. Fresh SQLite/Postgres still get the full schema.
    if not _has_column("devices", "device_type"):
        op.add_column("devices", sa.Column("device_type", sa.String(length=32), nullable=True))
    if not _has_column("devices", "platform"):
        op.add_column("devices", sa.Column("platform", sa.String(length=32), nullable=True))
    if not _has_column("devices", "paired_at"):
        op.add_column("devices", sa.Column("paired_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("devices", "push_token"):
        op.add_column("devices", sa.Column("push_token", sa.Text(), nullable=True))
    if not _has_column("devices", "push_token_updated_at"):
        op.add_column(
            "devices",
            sa.Column("push_token_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("devices", "push_bundle_id"):
        op.add_column("devices", sa.Column("push_bundle_id", sa.String(length=256), nullable=True))
    if not _has_index("devices", "ix_devices_device_type"):
        op.create_index(
            op.f("ix_devices_device_type"),
            "devices",
            ["device_type"],
            unique=False,
        )

    if not _has_column("notifications", "device_id"):
        op.add_column("notifications", sa.Column("device_id", sa.Uuid(), nullable=True))
    if not _has_column("notifications", "attention_kind"):
        op.add_column(
            "notifications",
            sa.Column(
                "attention_kind",
                sa.String(length=24),
                nullable=False,
                server_default="incoming",
            ),
        )
    if not _has_index("notifications", "ix_notifications_attention_kind"):
        op.create_index(
            op.f("ix_notifications_attention_kind"),
            "notifications",
            ["attention_kind"],
            unique=False,
        )
    if not _has_index("notifications", "ix_notifications_device_id"):
        op.create_index(
            op.f("ix_notifications_device_id"),
            "notifications",
            ["device_id"],
            unique=False,
        )

    if not _has_column("life_outbound_actions", "lifecycle"):
        op.add_column(
            "life_outbound_actions",
            sa.Column(
                "lifecycle",
                sa.String(length=16),
                nullable=False,
                server_default="queued",
            ),
        )
    if not _has_column("life_outbound_actions", "dispatched_at"):
        op.add_column(
            "life_outbound_actions",
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("life_outbound_actions", "acknowledged_at"):
        op.add_column(
            "life_outbound_actions",
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_index("life_outbound_actions", "ix_life_outbound_actions_lifecycle"):
        op.create_index(
            op.f("ix_life_outbound_actions_lifecycle"),
            "life_outbound_actions",
            ["lifecycle"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_life_outbound_actions_lifecycle"),
        table_name="life_outbound_actions",
    )
    op.drop_column("life_outbound_actions", "acknowledged_at")
    op.drop_column("life_outbound_actions", "dispatched_at")
    op.drop_column("life_outbound_actions", "lifecycle")

    op.drop_index(op.f("ix_notifications_device_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_attention_kind"), table_name="notifications")
    op.drop_column("notifications", "attention_kind")
    op.drop_column("notifications", "device_id")

    op.drop_index(op.f("ix_devices_device_type"), table_name="devices")
    op.drop_column("devices", "push_bundle_id")
    op.drop_column("devices", "push_token_updated_at")
    op.drop_column("devices", "push_token")
    op.drop_column("devices", "paired_at")
    op.drop_column("devices", "platform")
    op.drop_column("devices", "device_type")
