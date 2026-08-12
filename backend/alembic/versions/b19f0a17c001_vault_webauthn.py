"""AGENT 19 VAULT: WebAuthn challenge and passkey auth-material tables.

Revision ID: b19f0a17c001
Revises: a7c0d0c0d7a1
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b19f0a17c001"
down_revision = "a7c0d0c0d7a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_passkey_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("rp_id", sa.String(length=256), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_identities.id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_passkey_challenges_purpose"),
        "identity_passkey_challenges",
        ["purpose"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_passkey_challenges_challenge_hash"),
        "identity_passkey_challenges",
        ["challenge_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_identity_passkey_challenges_owner_id"),
        "identity_passkey_challenges",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_passkey_challenges_device_id"),
        "identity_passkey_challenges",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_passkey_challenges_expires_at"),
        "identity_passkey_challenges",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "identity_passkey_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("passkey_id", sa.Uuid(), nullable=False),
        sa.Column("public_key_cose", sa.JSON(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("aaguid", sa.String(length=32), nullable=True),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("attestation_format", sa.String(length=32), nullable=True),
        sa.Column("attestation_verified", sa.Boolean(), nullable=False),
        sa.Column("attestation_level", sa.String(length=32), nullable=True),
        sa.Column("rp_id", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["passkey_id"], ["identity_passkeys.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_passkey_materials_passkey_id"),
        "identity_passkey_materials",
        ["passkey_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_identity_passkey_materials_passkey_id"),
        table_name="identity_passkey_materials",
    )
    op.drop_table("identity_passkey_materials")
    op.drop_index(
        op.f("ix_identity_passkey_challenges_expires_at"),
        table_name="identity_passkey_challenges",
    )
    op.drop_index(
        op.f("ix_identity_passkey_challenges_device_id"),
        table_name="identity_passkey_challenges",
    )
    op.drop_index(
        op.f("ix_identity_passkey_challenges_owner_id"),
        table_name="identity_passkey_challenges",
    )
    op.drop_index(
        op.f("ix_identity_passkey_challenges_challenge_hash"),
        table_name="identity_passkey_challenges",
    )
    op.drop_index(
        op.f("ix_identity_passkey_challenges_purpose"),
        table_name="identity_passkey_challenges",
    )
    op.drop_table("identity_passkey_challenges")
