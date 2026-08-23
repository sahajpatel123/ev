"""Extend research_jobs. Memory OS: curation outbox + FTS/trigram (Postgres)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "j5k6l7m8n9o0"
down_revision = "i4j5k6l7m8n9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "memory_curation_jobs" not in inspector.get_table_names():
        op.create_table(
            "memory_curation_jobs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("job_key", sa.String(160), nullable=False),
            sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=True),
            sa.Column("kind", sa.String(32), nullable=False, server_default="curate"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("curator_version", sa.String(32), nullable=False, server_default="1"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_event_ids", sa.JSON(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_memory_curation_jobs_job_key", "memory_curation_jobs", ["job_key"], unique=True)
        op.create_index("ix_memory_curation_jobs_status", "memory_curation_jobs", ["status"])
        op.create_index("ix_memory_curation_jobs_priority", "memory_curation_jobs", ["priority"])
        op.create_index("ix_memory_curation_jobs_event_id", "memory_curation_jobs", ["event_id"])
        op.create_index("ix_memory_curation_jobs_available_at", "memory_curation_jobs", ["available_at"])
        op.create_index("ix_memory_curation_jobs_created_at", "memory_curation_jobs", ["created_at"])
    if bind.dialect.name.startswith("postgres"):
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_events_content_text_trgm
            ON events USING gin ((content->>'text') gin_trgm_ops)
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_events_content_text_fts
            ON events USING gin (to_tsvector('simple', coalesce(content->>'text', '')))
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memories_text_trgm
            ON memories USING gin (text gin_trgm_ops)
            WHERE is_current AND NOT redacted
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_events_type_occurred
            ON events (event_type, occurred_at)
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_entities_name_trgm
            ON entities USING gin (name gin_trgm_ops)
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if bind.dialect.name.startswith("postgres"):
        op.execute("DROP INDEX IF EXISTS ix_events_content_text_trgm")
        op.execute("DROP INDEX IF EXISTS ix_events_content_text_fts")
        op.execute("DROP INDEX IF EXISTS ix_memories_text_trgm")
        op.execute("DROP INDEX IF EXISTS ix_events_type_occurred")
        op.execute("DROP INDEX IF EXISTS ix_entities_name_trgm")
    if "memory_curation_jobs" in inspector.get_table_names():
        op.drop_table("memory_curation_jobs")
