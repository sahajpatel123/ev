"""Evie OS G1: projects / goals / goal_steps / commitments (core state)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "l7m8n9o0p1q2"
down_revision = "k6l7m8n9o0p1"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "projects"):
        op.create_table(
            "projects",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("actor", sa.String(64), nullable=False, server_default="master"),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
            sa.Column("priority", sa.String(16), nullable=False, server_default="NORMAL"),
            sa.Column("privacy_level", sa.String(32), nullable=False, server_default="normal"),
            sa.Column("source", sa.String(32), nullable=False, server_default="owner"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_projects_title", "projects", ["title"])
        op.create_index("ix_projects_status", "projects", ["status"])
        op.create_index("ix_projects_priority", "projects", ["priority"])
    if not _table_exists(bind, "goals"):
        op.create_table(
            "goals",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("actor", sa.String(64), nullable=False, server_default="master"),
            sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("parent_goal_id", sa.Uuid(), sa.ForeignKey("goals.id"), nullable=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("state", sa.String(16), nullable=False, server_default="ACTIVE"),
            sa.Column("priority", sa.String(16), nullable=False, server_default="NORMAL"),
            sa.Column("success_criteria", sa.Text(), nullable=False, server_default=""),
            sa.Column("progress_note", sa.Text(), nullable=False, server_default=""),
            sa.Column("next_action", sa.Text(), nullable=False, server_default=""),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("privacy_level", sa.String(32), nullable=False, server_default="normal"),
            sa.Column("source", sa.String(32), nullable=False, server_default="owner"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_goals_title", "goals", ["title"])
        op.create_index("ix_goals_state", "goals", ["state"])
        op.create_index("ix_goals_project_id", "goals", ["project_id"])
    if not _table_exists(bind, "goal_steps"):
        op.create_table(
            "goal_steps",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("goals.id"), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_goal_steps_goal_id", "goal_steps", ["goal_id"])
    if not _table_exists(bind, "commitments"):
        op.create_table(
            "commitments",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("actor", sa.String(64), nullable=False, server_default="master"),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("goals.id"), nullable=True),
            sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id"), nullable=True),
            sa.Column(
                "source_event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=True
            ),
            sa.Column("privacy_level", sa.String(32), nullable=False, server_default="normal"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_commitments_status", "commitments", ["status"])
        op.create_index("ix_commitments_due_at", "commitments", ["due_at"])


def downgrade() -> None:
    for name in ("commitments", "goal_steps", "goals", "projects"):
        if _table_exists(op.get_bind(), name):
            op.drop_table(name)
