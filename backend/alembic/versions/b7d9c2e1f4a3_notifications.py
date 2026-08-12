"""AGENT 14 PULSE: notification delivery ledger

Revision ID: b7d9c2e1f4a3
Revises: a7c0d0c0d7a1
Create Date: 2026-08-11 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "b7d9c2e1f4a3"
down_revision = "a7c0d0c0d7a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("backend", sa.String(length=32), nullable=True),
        sa.Column("backend_ref", sa.String(length=256), nullable=True),
        sa.Column("alert_id", sa.Uuid(), nullable=True),
        sa.Column("action_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["action_id"], ["approved_actions.id"]),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notifications_action_id"),
        "notifications",
        ["action_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_alert_id"),
        "notifications",
        ["alert_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_fingerprint"),
        "notifications",
        ["fingerprint"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_kind"),
        "notifications",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_priority"),
        "notifications",
        ["priority"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_queued_at"),
        "notifications",
        ["queued_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_status"),
        "notifications",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notifications_tier"),
        "notifications",
        ["tier"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_tier"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_queued_at"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_priority"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_kind"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_fingerprint"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_alert_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_action_id"), table_name="notifications")
    op.drop_table("notifications")
