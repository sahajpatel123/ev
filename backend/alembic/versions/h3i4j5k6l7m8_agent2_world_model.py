"""Agent 2: structured observations, owner objects, camera state, health metadata.

Revision ID: h3i4j5k6l7m8
Revises: g2b3c4d5e6f7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "h3i4j5k6l7m8"
down_revision = "g2b3c4d5e6f7"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names() and column in {
        item["name"] for item in inspector.get_columns(table)
    }


def upgrade() -> None:
    bind = op.get_bind()
    uuid = sa.Uuid()
    json_type = sa.JSON()
    if not _has_table("observations"):
        op.create_table(
            "observations",
            sa.Column("id", uuid, primary_key=True),
            sa.Column("subject", sa.String(256), nullable=False),
            sa.Column("subject_type", sa.String(32), nullable=False),
            sa.Column("object", sa.String(256), nullable=False),
            sa.Column("action", sa.String(128), nullable=False),
            sa.Column("location", sa.String(512), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_device", sa.String(128), nullable=False),
            sa.Column("evidence_ref", sa.String(512), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("uncertainty", sa.String(512), nullable=False),
            sa.Column("consent_state", sa.String(32), nullable=False),
            sa.Column("retention_class", sa.String(64), nullable=False),
            sa.Column("freshness_state", sa.String(16), nullable=False),
            sa.Column("stale_after_seconds", sa.Integer(), nullable=False),
            sa.Column("fact_kind", sa.String(16), nullable=False),
            sa.Column("metadata", json_type, nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not _has_table("owner_objects"):
        op.create_table(
            "owner_objects",
            sa.Column("id", uuid, primary_key=True),
            sa.Column("owner", sa.String(256), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("object_type", sa.String(64), nullable=False),
            sa.Column("enrollment_source", sa.String(128), nullable=False),
            sa.Column("appearance_references", json_type, nullable=False),
            sa.Column("common_locations", json_type, nullable=False),
            sa.Column("last_observed_location", sa.String(512), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_evidence_ref", sa.String(512), nullable=True),
            sa.Column("last_confidence", sa.Float(), nullable=True),
            sa.Column("last_uncertainty", sa.String(512), nullable=True),
            sa.Column("last_freshness_state", sa.String(16), nullable=False),
            sa.Column("possible_matches", json_type, nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not _has_table("camera_states"):
        op.create_table(
            "camera_states",
            sa.Column("id", uuid, primary_key=True),
            sa.Column("device_id", sa.String(128), nullable=False),
            sa.Column("platform", sa.String(32), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("visible", sa.Boolean(), nullable=False),
            sa.Column("permission_state", sa.String(32), nullable=False),
            sa.Column("explicit_request", sa.Boolean(), nullable=False),
            sa.Column("paused_reason", sa.String(256), nullable=True),
            sa.Column("consent_state", sa.String(32), nullable=False),
            sa.Column("raw_frames_persisted", sa.Boolean(), nullable=False),
            sa.Column("last_error", sa.String(512), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("device_id", name="uq_camera_states_device"),
        )
    additions = {
        "permission_state": sa.Column("permission_state", sa.String(32), nullable=True),
        "synced_at": sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        "units": sa.Column("units", json_type, nullable=True),
        "source_metadata": sa.Column("source_metadata", json_type, nullable=True),
        "freshness_state": sa.Column("freshness_state", sa.String(16), nullable=True),
    }
    for name, column in additions.items():
        if _has_table("health_snapshots") and not _has_column("health_snapshots", name):
            op.add_column("health_snapshots", column)


def downgrade() -> None:
    if _has_table("camera_states"):
        op.drop_table("camera_states")
    if _has_table("owner_objects"):
        op.drop_table("owner_objects")
    if _has_table("observations"):
        op.drop_table("observations")
    for name in ("freshness_state", "source_metadata", "units", "synced_at", "permission_state"):
        if _has_table("health_snapshots") and _has_column("health_snapshots", name):
            op.drop_column("health_snapshots", name)
