"""Ensure voice_sessions companion columns exist on already-stamped databases.

Revision ID: e7f8a9b0c1d2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-14 12:00:00.000000

e6f7a8b9c0d1 added conversation_id / greeted_at / malfunction_spoken_at, but a
live Postgres was stamped at that revision without the columns. This revision
is a no-op when they already exist and the repair that runs when they do not.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "e6f7a8b9c0d1"
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
    if not _has_column("voice_sessions", "conversation_id"):
        op.add_column(
            "voice_sessions",
            sa.Column("conversation_id", sa.Uuid(), nullable=True),
        )
    if not _has_column("voice_sessions", "greeted_at"):
        op.add_column(
            "voice_sessions",
            sa.Column("greeted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("voice_sessions", "malfunction_spoken_at"):
        op.add_column(
            "voice_sessions",
            sa.Column("malfunction_spoken_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_index("voice_sessions", "ix_voice_sessions_conversation_id"):
        op.create_index(
            op.f("ix_voice_sessions_conversation_id"),
            "voice_sessions",
            ["conversation_id"],
            unique=False,
        )


def downgrade() -> None:
    return
