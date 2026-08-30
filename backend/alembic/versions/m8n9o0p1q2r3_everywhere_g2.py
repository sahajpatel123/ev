"""Evie OS G2 (Evie Everywhere): additive sync-cursor + revision columns.

- projects.version / goals.version: optimistic-lock revisions (default 0).
- devices.sync_cursor_at / devices.sync_cursor_id: per-device resume cursor
  over the canonical event history.

No destructive change; existing rows keep working (version defaults to 0,
cursor columns nullable).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "m8n9o0p1q2r3"
down_revision = "l7m8n9o0p1q2"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "projects" in tables and "version" not in _columns(bind, "projects"):
        op.add_column(
            "projects",
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        )
    if "goals" in tables and "version" not in _columns(bind, "goals"):
        op.add_column(
            "goals",
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        )
    if "devices" in tables:
        device_cols = _columns(bind, "devices")
        if "sync_cursor_at" not in device_cols:
            op.add_column(
                "devices",
                sa.Column("sync_cursor_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "sync_cursor_id" not in device_cols:
            op.add_column(
                "devices",
                sa.Column("sync_cursor_id", sa.Uuid(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "projects" in tables and "version" in _columns(bind, "projects"):
        op.drop_column("projects", "version")
    if "goals" in tables and "version" in _columns(bind, "goals"):
        op.drop_column("goals", "version")
    if "devices" in tables:
        cols = _columns(bind, "devices")
        if "sync_cursor_id" in cols:
            op.drop_column("devices", "sync_cursor_id")
        if "sync_cursor_at" in cols:
            op.drop_column("devices", "sync_cursor_at")
