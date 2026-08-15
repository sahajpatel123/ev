"""House/lab/devices: timers, delegates, home inventory, indoor graph, prefs.

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-08-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e7f8a9b0c1d2"
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
    if not _has_column("devices", "bootstrapped_at"):
        op.add_column(
            "devices",
            sa.Column("bootstrapped_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("devices", "bootstrapped_spoken_at"):
        op.add_column(
            "devices",
            sa.Column("bootstrapped_spoken_at", sa.DateTime(timezone=True), nullable=True),
        )

    if _has_table("assistant_profiles"):
        if not _has_column("assistant_profiles", "tts_voice"):
            op.add_column(
                "assistant_profiles",
                sa.Column("tts_voice", sa.String(length=64), nullable=True),
            )
        if not _has_column("assistant_profiles", "hud_layout"):
            op.add_column(
                "assistant_profiles",
                sa.Column("hud_layout", sa.JSON(), nullable=True),
            )
        if not _has_column("assistant_profiles", "training_steps"):
            op.add_column(
                "assistant_profiles",
                sa.Column("training_steps", sa.JSON(), nullable=True),
            )
        if not _has_column("assistant_profiles", "volume_percent"):
            op.add_column(
                "assistant_profiles",
                sa.Column("volume_percent", sa.Integer(), nullable=True),
            )

    if not _has_table("timers"):
        op.create_table(
            "timers",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("late", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_timers_fire_at"), "timers", ["fire_at"], unique=False)
        op.create_index(op.f("ix_timers_status"), "timers", ["status"], unique=False)

    if not _has_table("delegates"):
        op.create_table(
            "delegates",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("person_id", sa.Uuid(), nullable=True),
            sa.Column("person_name", sa.String(length=256), nullable=False),
            sa.Column("device_id", sa.Uuid(), nullable=True),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("granted_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["person_id"], ["entities.id"]),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_delegates_person_id"), "delegates", ["person_id"], unique=False)
        op.create_index(op.f("ix_delegates_person_name"), "delegates", ["person_name"], unique=False)
        op.create_index(op.f("ix_delegates_device_id"), "delegates", ["device_id"], unique=False)
        op.create_index(op.f("ix_delegates_not_after"), "delegates", ["not_after"], unique=False)
        op.create_index(op.f("ix_delegates_revoked_at"), "delegates", ["revoked_at"], unique=False)

    if not _has_table("home_entities"):
        op.create_table(
            "home_entities",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("entity_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column("area", sa.String(length=128), nullable=True),
            sa.Column("domain", sa.String(length=32), nullable=False),
            sa.Column("state", sa.String(length=64), nullable=False),
            sa.Column("attributes", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_home_entities_entity_id"), "home_entities", ["entity_id"], unique=True)
        op.create_index(op.f("ix_home_entities_name"), "home_entities", ["name"], unique=False)
        op.create_index(op.f("ix_home_entities_area"), "home_entities", ["area"], unique=False)
        op.create_index(op.f("ix_home_entities_domain"), "home_entities", ["domain"], unique=False)

    if not _has_table("indoor_nodes"):
        op.create_table(
            "indoor_nodes",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.Column("photo_ref", sa.String(length=512), nullable=True),
            sa.Column("x", sa.Float(), nullable=True),
            sa.Column("y", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_indoor_nodes_name"), "indoor_nodes", ["name"], unique=False)

    if not _has_table("indoor_edges"):
        op.create_table(
            "indoor_edges",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("from_node_id", sa.Uuid(), nullable=False),
            sa.Column("to_node_id", sa.Uuid(), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("meters", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["from_node_id"], ["indoor_nodes.id"]),
            sa.ForeignKeyConstraint(["to_node_id"], ["indoor_nodes.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_indoor_edges_from_node_id"), "indoor_edges", ["from_node_id"], unique=False
        )
        op.create_index(
            op.f("ix_indoor_edges_to_node_id"), "indoor_edges", ["to_node_id"], unique=False
        )


def downgrade() -> None:
    if _has_table("indoor_edges"):
        op.drop_table("indoor_edges")
    if _has_table("indoor_nodes"):
        op.drop_table("indoor_nodes")
    if _has_table("home_entities"):
        op.drop_table("home_entities")
    if _has_table("delegates"):
        op.drop_table("delegates")
    if _has_table("timers"):
        op.drop_table("timers")
    if _has_table("assistant_profiles"):
        for column in ("volume_percent", "training_steps", "hud_layout", "tts_voice"):
            if _has_column("assistant_profiles", column):
                op.drop_column("assistant_profiles", column)
    if _has_column("devices", "bootstrapped_spoken_at"):
        op.drop_column("devices", "bootstrapped_spoken_at")
    if _has_column("devices", "bootstrapped_at"):
        op.drop_column("devices", "bootstrapped_at")
