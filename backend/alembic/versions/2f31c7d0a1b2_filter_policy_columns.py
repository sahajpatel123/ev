"""add runtime filter policy columns to filter_recalibrations

Revision ID: 2f31c7d0a1b2
Revises: 616806ad94f0
Create Date: 2026-08-09 20:10:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "2f31c7d0a1b2"
down_revision = "616806ad94f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "filter_recalibrations",
        sa.Column(
            "policy",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "filter_recalibrations",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "filter_recalibrations",
        sa.Column("applied_by", sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f("ix_filter_recalibrations_applied_at"),
        "filter_recalibrations",
        ["applied_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_filter_recalibrations_applied_at"),
        table_name="filter_recalibrations",
    )
    op.drop_column("filter_recalibrations", "applied_by")
    op.drop_column("filter_recalibrations", "applied_at")
    op.drop_column("filter_recalibrations", "policy")
