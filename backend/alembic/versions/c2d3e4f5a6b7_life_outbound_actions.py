"""AGENT 12 CONDUIT (WAVE LIFE): device-proxy outbound action queue

Revision ID: c2d3e4f5a6b7
Revises: c0d0e0f0a7b1
Create Date: 2026-08-12 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "c2d3e4f5a6b7"
down_revision = "c0d0e0f0a7b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "life_outbound_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "args",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_life_outbound_actions_integration_id"),
        "life_outbound_actions",
        ["integration_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_life_outbound_actions_device_id"),
        "life_outbound_actions",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_life_outbound_actions_status"),
        "life_outbound_actions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_life_outbound_actions_action"),
        "life_outbound_actions",
        ["action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_life_outbound_actions_created_at"),
        "life_outbound_actions",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_life_outbound_actions_created_at"), table_name="life_outbound_actions")
    op.drop_index(op.f("ix_life_outbound_actions_action"), table_name="life_outbound_actions")
    op.drop_index(op.f("ix_life_outbound_actions_status"), table_name="life_outbound_actions")
    op.drop_index(op.f("ix_life_outbound_actions_device_id"), table_name="life_outbound_actions")
    op.drop_index(op.f("ix_life_outbound_actions_integration_id"), table_name="life_outbound_actions")
    op.drop_table("life_outbound_actions")
