"""Owner model store: versioned traits/values/thinking-style + state snapshots.

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "u7v8w9x0y1z2"
down_revision = "t6u7v8w9x0y1"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return name in sa.inspect(bind).get_table_names()


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    return index_name in {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def _owner_row_columns(table: str) -> list:
    json_col = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("payload", json_col, nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("privacy_level", sa.String(length=32), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_group", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("reason_for_change", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["supersedes_id"], [f"{table}.id"]),
        sa.ForeignKeyConstraint(["superseded_by_id"], [f"{table}.id"]),
    ]


def _create_owner_row_table(table: str) -> None:
    if _has_table(table):
        return
    op.create_table(table, *_owner_row_columns(table))
    for column in (
        "owner_id",
        "importance",
        "source_type",
        "privacy_level",
        "event_time",
        "version_group",
        "version",
        "supersedes_id",
        "is_current",
        "fingerprint",
    ):
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)


def upgrade() -> None:
    _create_owner_row_table("owner_traits")
    _create_owner_row_table("owner_values")
    _create_owner_row_table("owner_thinking_style")

    if not _has_table("owner_model_events"):
        op.create_table(
            "owner_model_events",
            sa.Column("row_kind", sa.String(length=16), nullable=False),
            sa.Column("row_id", sa.Uuid(), nullable=False),
            sa.Column("event_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("row_kind", "row_id", "event_id"),
        )

    if not _has_table("owner_state_snapshots"):
        json_col = sa.JSON().with_variant(
            postgresql.JSONB(astext_type=Text()), "postgresql"
        )
        op.create_table(
            "owner_state_snapshots",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("owner_id", sa.Uuid(), nullable=True),
            sa.Column("state_kind", sa.String(length=16), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=False),
            sa.Column("details", json_col, nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_type", sa.String(length=16), nullable=False),
            sa.Column("privacy_level", sa.String(length=32), nullable=False),
            sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("owner_id", "state_kind", "source_type", "privacy_level", "expires_at"):
            op.create_index(
                op.f(f"ix_owner_state_snapshots_{column}"),
                "owner_state_snapshots",
                [column],
                unique=False,
            )


def downgrade() -> None:
    if _has_table("owner_state_snapshots"):
        for column in ("owner_id", "state_kind", "source_type", "privacy_level", "expires_at"):
            name = f"ix_owner_state_snapshots_{column}"
            if _has_index("owner_state_snapshots", name):
                op.drop_index(name, table_name="owner_state_snapshots")
        op.drop_table("owner_state_snapshots")
    if _has_table("owner_model_events"):
        op.drop_table("owner_model_events")
    for table in ("owner_thinking_style", "owner_values", "owner_traits"):
        if not _has_table(table):
            continue
        for column in (
            "owner_id",
            "importance",
            "source_type",
            "privacy_level",
            "event_time",
            "version_group",
            "version",
            "supersedes_id",
            "is_current",
            "fingerprint",
        ):
            name = f"ix_{table}_{column}"
            if _has_index(table, name):
                op.drop_index(name, table_name=table)
        op.drop_table(table)
