"""Presence OS V1: durable intent contracts + conditions (additive only)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "r4s5t6u7v8w9"
down_revision = "q3r4s5t6u7v8"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    json_type = JSONB() if bind.dialect.name == "postgresql" else sa.JSON()
    tz = sa.DateTime(timezone=True)
    if not _has_table(bind, "presence_contracts"):
        op.create_table(
            "presence_contracts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("linked_goal_id", sa.Uuid(), nullable=True),
            sa.Column("origin_device_id", sa.Uuid(), nullable=True),
            sa.Column("objective", sa.Text(), nullable=False, server_default=""),
            sa.Column("normalized_objective", sa.Text(), nullable=False, server_default=""),
            sa.Column("success_criteria", json_type, nullable=False, server_default="{}"),
            sa.Column("constraints", json_type, nullable=False, server_default="{}"),
            sa.Column("deadline_at", tz, nullable=True),
            sa.Column("priority", sa.String(16), nullable=False, server_default="NORMAL"),
            sa.Column("interruption_policy", sa.String(24), nullable=False, server_default="NORMAL"),
            sa.Column("autonomy_policy", sa.String(24), nullable=False, server_default="SAFE_DIGITAL"),
            sa.Column("risk_ceiling", sa.String(8), nullable=False, server_default="R2"),
            sa.Column("entities", json_type, nullable=False, server_default="{}"),
            sa.Column("artifacts", json_type, nullable=False, server_default="{}"),
            sa.Column("target_devices", json_type, nullable=False, server_default="[]"),
            sa.Column("state", sa.String(24), nullable=False, server_default="DRAFT"),
            sa.Column("confidence", sa.String(24), nullable=False, server_default="UNKNOWN"),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("next_condition", json_type, nullable=False, server_default="{}"),
            sa.Column("graph", json_type, nullable=False, server_default="{}"),
            sa.Column("verification", json_type, nullable=False, server_default="{}"),
            sa.Column("evidence", json_type, nullable=False, server_default="{}"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", tz, nullable=False),
            sa.Column("updated_at", tz, nullable=False),
        )
        op.create_index(
            "ix_presence_contracts_state", "presence_contracts", ["state"]
        )
        op.create_index(
            "ix_presence_contracts_updated_at", "presence_contracts", ["updated_at"]
        )
    if not _has_table(bind, "presence_conditions"):
        op.create_table(
            "presence_conditions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("contract_id", sa.Uuid(), nullable=False),
            sa.Column("cond_class", sa.String(24), nullable=False),
            sa.Column("source", sa.String(64), nullable=False, server_default="event"),
            sa.Column("strategy", sa.String(32), nullable=False, server_default="event"),
            sa.Column("frequency_s", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("ttl_s", sa.Integer(), nullable=False, server_default="86400"),
            sa.Column("last_evaluated_at", tz, nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("payload", json_type, nullable=False, server_default="{}"),
            sa.Column("created_at", tz, nullable=False),
        )
        op.create_index(
            "ix_presence_conditions_contract", "presence_conditions", ["contract_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("presence_conditions", "presence_contracts"):
        if _has_table(bind, table):
            op.drop_table(table)
