"""AGENT 7 ROSTER: consented face enrollment, per-sample templates, public-figure cache

Revision ID: a7c0d0c0d7a1
Revises: 2f31c7d0a1b2
Create Date: 2026-08-11 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "a7c0d0c0d7a1"
down_revision = "2f31c7d0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "face_enrollments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("salt", sa.String(length=64), nullable=True),
        sa.Column("privacy_level", sa.String(length=32), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("reason_for_change", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["consent_id"], ["consent_records.id"]),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["face_enrollments.id"]),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["face_enrollments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_face_enrollments_entity_id"),
        "face_enrollments",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_is_current"),
        "face_enrollments",
        ["is_current"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_version"),
        "face_enrollments",
        ["version"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_algorithm"),
        "face_enrollments",
        ["algorithm"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_status"),
        "face_enrollments",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_redacted"),
        "face_enrollments",
        ["redacted"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_enrollments_supersedes_id"),
        "face_enrollments",
        ["supersedes_id"],
        unique=False,
    )

    op.create_table(
        "face_samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("sample_index", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("salt", sa.String(length=64), nullable=True),
        sa.Column("quality", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("attachment_id", sa.Uuid(), nullable=True),
        sa.Column("live_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"]),
        sa.ForeignKeyConstraint(
            ["enrollment_id"], ["face_enrollments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["live_event_id"], ["live_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_face_samples_enrollment_id"),
        "face_samples",
        ["enrollment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_samples_entity_id"),
        "face_samples",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_samples_attachment_id"),
        "face_samples",
        ["attachment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_face_samples_live_event_id"),
        "face_samples",
        ["live_event_id"],
        unique=False,
    )

    op.create_table(
        "public_figure_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("canonical_key", sa.String(length=512), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column(
            "data",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_public_figure_cache_canonical_key"),
        "public_figure_cache",
        ["canonical_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_public_figure_cache_name"),
        "public_figure_cache",
        ["name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_public_figure_cache_entity_id"),
        "public_figure_cache",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_public_figure_cache_fetched_at"),
        "public_figure_cache",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_public_figure_cache_confirmed"),
        "public_figure_cache",
        ["confirmed"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_public_figure_cache_confirmed"),
        table_name="public_figure_cache",
    )
    op.drop_index(
        op.f("ix_public_figure_cache_fetched_at"),
        table_name="public_figure_cache",
    )
    op.drop_index(
        op.f("ix_public_figure_cache_entity_id"),
        table_name="public_figure_cache",
    )
    op.drop_index(
        op.f("ix_public_figure_cache_name"),
        table_name="public_figure_cache",
    )
    op.drop_index(
        op.f("ix_public_figure_cache_canonical_key"),
        table_name="public_figure_cache",
    )
    op.drop_table("public_figure_cache")
    op.drop_index(op.f("ix_face_samples_live_event_id"), table_name="face_samples")
    op.drop_index(op.f("ix_face_samples_attachment_id"), table_name="face_samples")
    op.drop_index(op.f("ix_face_samples_entity_id"), table_name="face_samples")
    op.drop_index(op.f("ix_face_samples_enrollment_id"), table_name="face_samples")
    op.drop_table("face_samples")
    op.drop_index(op.f("ix_face_enrollments_supersedes_id"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_redacted"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_status"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_algorithm"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_version"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_is_current"), table_name="face_enrollments")
    op.drop_index(op.f("ix_face_enrollments_entity_id"), table_name="face_enrollments")
    op.drop_table("face_enrollments")
