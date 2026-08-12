"""Merge concurrent agent migration heads (integration, no schema changes)

Revision ID: c0d0e0f0a7b1
Revises: a8b2c3d4e5f60718, b19f0a17c001, b7d9c2e1f4a3
Create Date: 2026-08-11 18:55:00.000000

This no-op merge linearizes the three heads left by concurrently running
agents (8 SYNAPSE, 14 PULSE notifications, 19 VAULT) so ``alembic upgrade
head`` resolves to a single revision. No other migration was modified.
"""

from __future__ import annotations

from alembic import op

revision = "c0d0e0f0a7b1"
down_revision = ("a8b2c3d4e5f60718", "b19f0a17c001", "b7d9c2e1f4a3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
