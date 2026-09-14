"""Digital Operations V1 — alembic (additive)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "s5t6u7v8w9x0"
down_revision = "r4s5t6u7v8w9"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    json_type = JSONB() if bind.dialect.name == "postgresql" else sa.JSON()
    tz = sa.DateTime(timezone=True)
    if not _has_table(bind, "digital_person_identities"):
        op.create_table(
            "digital_person_identities",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("entity_id", sa.Uuid(), nullable=True),
            sa.Column("channel", sa.String(24), nullable=False),
            sa.Column("identifier", sa.String(256), nullable=False),
            sa.Column("display_name", sa.String(256), nullable=True),
            sa.Column("service_id", sa.String(256), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("last_used_at", tz, nullable=True),
            sa.Column("extra", json_type, nullable=False, server_default="{}"),
            sa.Column("created_at", tz, nullable=False),
            sa.UniqueConstraint("channel", "identifier", name="uq_digital_person_channel_id"),
        )
    if not _has_table(bind, "digital_comm_refs"):
        op.create_table(
            "digital_comm_refs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("channel", sa.String(24), nullable=False),
            sa.Column("service", sa.String(32), nullable=False),
            sa.Column("external_id", sa.String(256), nullable=False),
            sa.Column("thread_id", sa.String(256), nullable=True),
            sa.Column("participants", json_type, nullable=False, server_default="[]"),
            sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
            sa.Column("occurred_at", tz, nullable=True),
            sa.Column("retrieved_at", tz, nullable=False),
            sa.Column("origin", sa.String(32), nullable=False, server_default="EXTERNAL_CONTENT"),
            sa.Column("extra", json_type, nullable=False, server_default="{}"),
        )
    if not _has_table(bind, "digital_waiting"):
        op.create_table(
            "digital_waiting",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("direction", sa.String(24), nullable=False),
            sa.Column("person", sa.String(256), nullable=False),
            sa.Column("what", sa.Text(), nullable=False, server_default=""),
            sa.Column("channel", sa.String(24), nullable=True),
            sa.Column("when_due", sa.String(64), nullable=True),
            sa.Column("source_ref", json_type, nullable=False, server_default="{}"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("state", sa.String(16), nullable=False, server_default="open"),
            sa.Column("contract_id", sa.Uuid(), nullable=True),
            sa.Column("evidence", json_type, nullable=False, server_default="[]"),
            sa.Column("created_at", tz, nullable=False),
            sa.Column("resolved_at", tz, nullable=True),
        )
    if not _has_table(bind, "digital_artifacts"):
        op.create_table(
            "digital_artifacts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("original_name", sa.String(256), nullable=False),
            sa.Column("mime", sa.String(128), nullable=False, server_default="application/octet-stream"),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("local_path", sa.Text(), nullable=False),
            sa.Column("provenance", json_type, nullable=False, server_default="{}"),
            sa.Column("quarantined", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", tz, nullable=False),
        )
    if not _has_table(bind, "digital_drafts"):
        op.create_table(
            "digital_drafts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("contract_id", sa.Uuid(), nullable=True),
            sa.Column("channel", sa.String(24), nullable=False),
            sa.Column("service", sa.String(32), nullable=False),
            sa.Column("to_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("attachments", json_type, nullable=False, server_default="[]"),
            sa.Column("state", sa.String(24), nullable=False, server_default="PREPARED"),
            sa.Column("idempotency_key", sa.String(64), nullable=True),
            sa.Column("extra", json_type, nullable=False, server_default="{}"),
            sa.Column("created_at", tz, nullable=False),
            sa.Column("updated_at", tz, nullable=False),
        )
    if not _has_table(bind, "digital_watches"):
        op.create_table(
            "digital_watches",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("contract_id", sa.Uuid(), nullable=True),
            sa.Column("channel", sa.String(24), nullable=False),
            sa.Column("cond_class", sa.String(24), nullable=False),
            sa.Column("query", json_type, nullable=False, server_default="{}"),
            sa.Column("last_cursor", sa.String(128), nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_at", tz, nullable=False),
        )
    if not _has_table(bind, "digital_op_audit"):
        op.create_table(
            "digital_op_audit",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("goal_id", sa.String(64), nullable=True),
            sa.Column("service", sa.String(32), nullable=False),
            sa.Column("operation", sa.String(64), nullable=False),
            sa.Column("target", sa.String(256), nullable=True),
            sa.Column("risk", sa.String(8), nullable=True),
            sa.Column("approval_id", sa.String(64), nullable=True),
            sa.Column("result", sa.String(32), nullable=False),
            sa.Column("verification", json_type, nullable=False, server_default="{}"),
            sa.Column("created_at", tz, nullable=False),
        )
    if not _has_table(bind, "digital_channel_prefs"):
        op.create_table(
            "digital_channel_prefs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("person_key", sa.String(256), nullable=False),
            sa.Column("channel", sa.String(24), nullable=False),
            sa.Column("uses", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_at", tz, nullable=False),
            sa.UniqueConstraint("person_key", name="uq_digital_channel_pref_person"),
        )


def downgrade() -> None:
    for table in (
        "digital_channel_prefs",
        "digital_op_audit",
        "digital_watches",
        "digital_drafts",
        "digital_artifacts",
        "digital_waiting",
        "digital_comm_refs",
        "digital_person_identities",
    ):
        if _has_table(op.get_bind(), table):
            op.drop_table(table)
