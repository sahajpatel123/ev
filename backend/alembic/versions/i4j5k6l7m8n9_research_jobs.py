"""Extend research sessions with durable job lifecycle fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i4j5k6l7m8n9"
down_revision = "h3i4j5k6l7m8"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names() and column in {
        item["name"] for item in inspector.get_columns(table)
    }


def upgrade() -> None:
    additions = {
        "goal": sa.Column("goal", sa.Text(), nullable=True),
        "owner": sa.Column("owner", sa.String(128), nullable=True),
        "mode": sa.Column("mode", sa.String(16), nullable=True),
        "allowed_tools": sa.Column("allowed_tools", sa.JSON(), nullable=True),
        "deadline_at": sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        "budget": sa.Column("budget", sa.JSON(), nullable=True),
        "checkpoints": sa.Column("checkpoints", sa.JSON(), nullable=True),
        "progress": sa.Column("progress", sa.JSON(), nullable=True),
        "final_artifacts": sa.Column("final_artifacts", sa.JSON(), nullable=True),
        "citations": sa.Column("citations", sa.JSON(), nullable=True),
        "evidence": sa.Column("evidence", sa.JSON(), nullable=True),
        "cancel_requested": sa.Column("cancel_requested", sa.Boolean(), nullable=True),
        "attempts": sa.Column("attempts", sa.Integer(), nullable=True),
        "last_error": sa.Column("last_error", sa.Text(), nullable=True),
    }
    for name, column in additions.items():
        if not _has_column("research_sessions", name):
            op.add_column("research_sessions", column)
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql(
            "UPDATE research_sessions SET goal=question WHERE goal IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET owner='master' WHERE owner IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET mode='session' WHERE mode IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET allowed_tools='[]' WHERE allowed_tools IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET budget='{}' WHERE budget IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET checkpoints='[]' WHERE checkpoints IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET progress='{}' WHERE progress IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET final_artifacts='[]' WHERE final_artifacts IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET citations='[]' WHERE citations IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET evidence='{}' WHERE evidence IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET cancel_requested=0 WHERE cancel_requested IS NULL"
        )
        bind.exec_driver_sql(
            "UPDATE research_sessions SET attempts=0 WHERE attempts IS NULL"
        )


def downgrade() -> None:
    for name in (
        "last_error",
        "attempts",
        "cancel_requested",
        "evidence",
        "citations",
        "final_artifacts",
        "progress",
        "checkpoints",
        "budget",
        "deadline_at",
        "allowed_tools",
        "mode",
        "owner",
        "goal",
    ):
        if _has_column("research_sessions", name):
            op.drop_column("research_sessions", name)
