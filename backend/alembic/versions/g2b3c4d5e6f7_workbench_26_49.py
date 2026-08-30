"""Workbench 26-49: telemetry, beacons, shares, hud, voice, drafts.

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-15 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "g2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_table("assistant_profiles"):
        if not _has_column("assistant_profiles", "tts_voice_id"):
            op.add_column("assistant_profiles", sa.Column("tts_voice_id", sa.String(64), nullable=True))
        if not _has_column("assistant_profiles", "tts_rate"):
            op.add_column("assistant_profiles", sa.Column("tts_rate", sa.Float(), nullable=True))
        if not _has_column("assistant_profiles", "last_sense_why"):
            op.add_column("assistant_profiles", sa.Column("last_sense_why", sa.Text(), nullable=True))
        if not _has_column("assistant_profiles", "last_sense_source_ids"):
            op.add_column("assistant_profiles", sa.Column("last_sense_source_ids", sa.JSON(), nullable=True))
        if not _has_column("assistant_profiles", "last_sense_callout_id"):
            op.add_column("assistant_profiles", sa.Column("last_sense_callout_id", sa.Uuid(), nullable=True))
        if not _has_column("assistant_profiles", "morning_brief_spoken_on"):
            op.add_column("assistant_profiles", sa.Column("morning_brief_spoken_on", sa.String(16), nullable=True))
    if _has_table("print_jobs"):
        if not _has_column("print_jobs", "vendor_job_id"):
            op.add_column("print_jobs", sa.Column("vendor_job_id", sa.String(128), nullable=True))
        if not _has_column("print_jobs", "adapter"):
            op.add_column("print_jobs", sa.Column("adapter", sa.String(32), nullable=True))
        if not _has_column("print_jobs", "details"):
            op.add_column("print_jobs", sa.Column("details", sa.JSON(), nullable=True))
    if _has_table("devices"):
        if not _has_column("devices", "battery_percent"):
            op.add_column("devices", sa.Column("battery_percent", sa.Float(), nullable=True))
        if not _has_column("devices", "storage_free_bytes"):
            op.add_column("devices", sa.Column("storage_free_bytes", sa.Integer(), nullable=True))
    if _has_table("runtime_heartbeats") and not _has_column("runtime_heartbeats", "storage_free_bytes"):
        op.add_column("runtime_heartbeats", sa.Column("storage_free_bytes", sa.Integer(), nullable=True))

    tables = {
        "location_shares": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("person_id", sa.Uuid(), nullable=True),
            sa.Column("person_name", sa.String(256), nullable=False),
            sa.Column("token_expires", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_lat", sa.Float(), nullable=True),
            sa.Column("last_lon", sa.Float(), nullable=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("owner_family_device", sa.Boolean(), nullable=False),
            sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "beacons": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("last_lat", sa.Float(), nullable=True),
            sa.Column("last_lon", sa.Float(), nullable=True),
            sa.Column("owner_only", sa.Boolean(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "telemetry_sessions": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        ),
        "telemetry_samples": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("session_id", sa.Uuid(), nullable=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("battery", sa.Float(), nullable=True),
            sa.Column("alt", sa.Float(), nullable=True),
            sa.Column("speed", sa.Float(), nullable=True),
            sa.Column("lat", sa.Float(), nullable=True),
            sa.Column("lon", sa.Float(), nullable=True),
            sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("details", sa.JSON(), nullable=True),
        ),
        "mail_drafts": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("mail_id", sa.String(128), nullable=False),
            sa.Column("to_addr", sa.String(256), nullable=True),
            sa.Column("subject", sa.String(512), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("confirm", sa.Boolean(), nullable=False),
            sa.Column("sent", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "hardware_audits": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("command", sa.String(64), nullable=False),
            sa.Column("args", sa.JSON(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("actor", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "public_feeds": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("url", sa.String(1024), nullable=False),
            sa.Column("label", sa.String(256), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_items", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "owner_cameras": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("vault_ref", sa.String(256), nullable=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("clip_attachment_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "hud_pushes": (
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("schema_version", sa.String(32), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("conversation_id", sa.Uuid(), nullable=True),
            sa.Column("prefer_haptic", sa.Boolean(), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
    }
    for name, cols in tables.items():
        if not _has_table(name):
            op.create_table(name, *cols)


def downgrade() -> None:
    for name in (
        "hud_pushes",
        "owner_cameras",
        "public_feeds",
        "hardware_audits",
        "mail_drafts",
        "telemetry_samples",
        "telemetry_sessions",
        "beacons",
        "location_shares",
    ):
        if _has_table(name):
            op.drop_table(name)
