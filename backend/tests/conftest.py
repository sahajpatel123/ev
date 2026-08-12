import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="ev-tests-")
os.environ.setdefault("EV_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/test.db")
os.environ.setdefault("EV_PROCESSING_MODE", "sync")
os.environ.setdefault("EV_CHAT_PROVIDER", "mock")
os.environ.setdefault("EV_EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("EV_EMBEDDING_DIM", "64")
os.environ.setdefault("EV_MASTER_KEY", "test-key")
os.environ.setdefault("EV_VAULT_KEY", "test-vault-key-0123456789abcdef")
os.environ.setdefault("EV_STORAGE_ROOT", f"{_TMP}/storage")
os.environ.setdefault("EV_ACCESS_LOG_ENABLED", "true")
os.environ.setdefault("EV_QUIET_HOURS_START", "23:59")
os.environ.setdefault("EV_QUIET_HOURS_END", "00:00")

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
async def fresh_db() -> AsyncIterator[None]:
    # SQLite + aiosqlite connections are bound to the event loop that created
    # them. Tests run on a fresh loop each time, so dispose before and after so
    # a stale worker thread cannot hold a write lock across tests.
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    ) as c:
        yield c


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        yield session
