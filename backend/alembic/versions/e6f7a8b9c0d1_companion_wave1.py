"""Day-long companion: assistant profile, callouts, calibration cache, gates.

Revision ID: e6f7a8b9c0d1
Revises: d4e5f6a7b8c9
Create Date: 2026-08-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


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

    if not _has_table("assistant_profiles"):
        op.create_table(
            "assistant_profiles",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("owner_id", sa.Uuid(), nullable=True),
            sa.Column("nickname", sa.String(length=64), nullable=False),
            sa.Column("owner_preferred_name", sa.String(length=128), nullable=True),
            sa.Column("greeting_enabled", sa.Boolean(), nullable=False),
            sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dedication_text", sa.String(length=500), nullable=True),
            sa.Column("dedication_blob_id", sa.String(length=256), nullable=True),
            sa.Column("dedication_played_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("live_conversation_id", sa.Uuid(), nullable=True),
            sa.Column("training_wheels_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("training_wheels_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("social_turn_count", sa.Integer(), nullable=False),
            sa.Column("social_nudge_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("isolation_scan_ran_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("isolation_detected", sa.Boolean(), nullable=False),
            sa.Column("quiet_hours_start", sa.String(length=16), nullable=True),
            sa.Column("quiet_hours_end", sa.String(length=16), nullable=True),
            sa.Column("quiet_digest_spoken_on", sa.String(length=16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["owner_identities.id"]),
            sa.ForeignKeyConstraint(["live_conversation_id"], ["conversation_threads.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_assistant_profiles_owner_id"),
            "assistant_profiles",
            ["owner_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_assistant_profiles_live_conversation_id"),
            "assistant_profiles",
            ["live_conversation_id"],
            unique=False,
        )

    if not _has_table("calibration_reports"):
        op.create_table(
            "calibration_reports",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("overall", sa.String(length=16), nullable=False),
            sa.Column("report", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_calibration_reports_generated_at"),
            "calibration_reports",
            ["generated_at"],
            unique=False,
        )
        op.create_index(
            op.f("ix_calibration_reports_overall"),
            "calibration_reports",
            ["overall"],
            unique=False,
        )

    if not _has_table("callouts"):
        op.create_table(
            "callouts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("source_item", sa.String(length=128), nullable=True),
            sa.Column("hud", sa.JSON(), nullable=False),
            sa.Column("spoken", sa.Boolean(), nullable=False),
            sa.Column("emergency", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_callouts_source"), "callouts", ["source"], unique=False)
        op.create_index(
            op.f("ix_callouts_source_item"), "callouts", ["source_item"], unique=False
        )
        op.create_index(op.f("ix_callouts_spoken"), "callouts", ["spoken"], unique=False)
        op.create_index(
            op.f("ix_callouts_created_at"), "callouts", ["created_at"], unique=False
        )

    if not _has_table("feature_gates"):
        op.create_table(
            "feature_gates",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("setup_hint", sa.String(length=256), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_feature_gates_key"), "feature_gates", ["key"], unique=True)
        op.create_index(
            op.f("ix_feature_gates_status"), "feature_gates", ["status"], unique=False
        )


def downgrade() -> None:
    if _has_table("feature_gates"):
        op.drop_table("feature_gates")
    if _has_table("callouts"):
        op.drop_table("callouts")
    if _has_table("calibration_reports"):
        op.drop_table("calibration_reports")
    if _has_table("assistant_profiles"):
        op.drop_table("assistant_profiles")
    if _has_index("voice_sessions", "ix_voice_sessions_conversation_id"):
        op.drop_index(op.f("ix_voice_sessions_conversation_id"), table_name="voice_sessions")
    if _has_column("voice_sessions", "malfunction_spoken_at"):
        op.drop_column("voice_sessions", "malfunction_spoken_at")
    if _has_column("voice_sessions", "greeted_at"):
        op.drop_column("voice_sessions", "greeted_at")
    if _has_column("voice_sessions", "conversation_id"):
        op.drop_column("voice_sessions", "conversation_id")
