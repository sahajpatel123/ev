from collections.abc import AsyncIterator
from contextlib import suppress

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    return create_async_engine(url or settings.database_url, echo=False, future=True)


engine = make_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ensure_schema(connection) -> None:
    """Add model columns/indexes that ``create_all`` will not add to live tables.

    Alembic can be stamped at head while a live Postgres is still missing
    additive columns (``voice_sessions.conversation_id`` was the crash). Every
    process that calls ``init_db`` repairs that drift before serving traffic.
    """

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    dialect = connection.dialect
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue
            if (
                not column.nullable
                and column.default is None
                and column.server_default is None
            ):
                continue
            type_sql = column.type.compile(dialect=dialect)
            connection.execute(
                text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_sql}'
                )
            )
            existing_cols.add(column.name)
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name and index.name not in existing_indexes:
                index.create(connection, checkfirst=True)
    _ensure_memory_os_indexes(connection)


def _ensure_memory_os_indexes(connection) -> None:
    """Postgres FTS/trigram for Memory OS. SQLite tests skip these."""

    if not connection.dialect.name.startswith("postgres"):
        return
    statements = (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        """
        CREATE INDEX IF NOT EXISTS ix_events_content_text_trgm
        ON events USING gin ((content->>'text') gin_trgm_ops)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_events_content_text_fts
        ON events USING gin (to_tsvector('simple', coalesce(content->>'text', '')))
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_memories_text_trgm
        ON memories USING gin (text gin_trgm_ops)
        WHERE is_current AND NOT redacted
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_entities_name_trgm
        ON entities USING gin (name gin_trgm_ops)
        """,
    )
    for stmt in statements:
        with suppress(Exception):
            connection.execute(text(stmt))


async def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        if engine.url.get_backend_name().startswith("postgres"):
            # pgvector columns (embeddings, voiceprints) require the extension;
            # SQLite stores those as JSON lists so this is Postgres-only.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_schema)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
