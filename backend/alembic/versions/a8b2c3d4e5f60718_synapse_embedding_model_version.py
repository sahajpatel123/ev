"""record embedding model version per vector + Postgres HNSW index

Revision ID: a8b2c3d4e5f60718
Revises: 2f31c7d0a1b2
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8b2c3d4e5f60718"
down_revision = "2f31c7d0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("embedding_model_version", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_memories_embedding_model_version"),
        "memories",
        ["embedding_model_version"],
        unique=False,
    )
    # Pgvector HNSW index (Postgres only). Tuned for ~10K memories on an 8 GB
    # host: m=16, ef_construction=64 keep build cost low while preserving
    # recall; queries can raise hnsw.ef_search for deeper recall.
    # SQLite stores embeddings as JSON and must stay index-free.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
            "ON memories USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_index(
        op.f("ix_memories_embedding_model_version"),
        table_name="memories",
    )
    op.drop_column("memories", "embedding_model_version")
