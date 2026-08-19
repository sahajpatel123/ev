import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="ev-tests-")
# Never inherit the owner's live DATABASE_URL. pytest drop_all would wipe
# the running Postgres. Opt in with EV_TEST_USE_LIVE_DB=1 only.
if os.environ.get("EV_TEST_USE_LIVE_DB") != "1":
    os.environ["EV_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["EV_MASTER_KEY"] = "test-key"
os.environ["EV_API_KEY"] = "test-key"
os.environ.setdefault("EV_PROCESSING_MODE", "sync")
if os.environ.get("EV_TEST_USE_LIVE_CHAT") != "1":
    os.environ["EV_CHAT_PROVIDER"] = "mock"
    os.environ["EV_XAI_API_KEY"] = ""
    os.environ["EV_OPENAI_API_KEY"] = ""
    os.environ.setdefault("EV_VOICE_LIVE_BRAIN", "pipeline")
os.environ.setdefault("EV_EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("EV_EMBEDDING_DIM", "64")
os.environ["EV_VOICEPRINT_PROVIDER"] = "hash"
os.environ["EV_VOICEPRINT_MODEL_DIR"] = f"{_TMP}/no-campp"
os.environ["EV_ML_MODEL_DIR"] = f"{_TMP}/models"
os.environ["EV_MODEL_DIR"] = f"{_TMP}/models"
os.environ["EV_VOICE_TTS_PROVIDER"] = "meta"
os.environ["EV_VOICE_ASR_PROVIDER"] = "echo"
os.environ["EV_VOICE_WAKE_PROVIDER"] = "phrase"
os.environ["EV_SEARCH_PROVIDER"] = "none"
os.environ["EV_OPENCODE_TOOL_EMULATION"] = "false"
os.environ["EV_EARS_CONSENT"] = "false"
os.environ.pop("EV_EARS_API_URL", None)
os.environ.pop("EV_EARS_API_KEY", None)
os.environ.setdefault("EV_MASTER_KEY", "test-key")
os.environ.setdefault("EV_VAULT_KEY", "test-vault-key-0123456789abcdef")
os.environ.setdefault("EV_STORAGE_ROOT", f"{_TMP}/storage")
os.environ.setdefault("EV_ACCESS_LOG_ENABLED", "true")
os.environ.setdefault("EV_QUIET_HOURS_START", "23:59")
os.environ.setdefault("EV_QUIET_HOURS_END", "00:00")

from collections.abc import AsyncIterator, Iterator

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


@pytest.fixture(autouse=True)
def reset_mutable_settings() -> Iterator[None]:
    """Restore runtime-mutated ``settings`` between tests.

    ``set_quiet_hours`` (and the quiet-hours API) assign ``settings.quiet_hours_*``
    directly, so one test that says "quiet until 8" leaves every later voice
    test in permanent quiet hours. Restore the conftest defaults after each
    test so the mutation is test-local.
    """

    yield
    from app.config import settings

    settings.quiet_hours_start = "23:59"
    settings.quiet_hours_end = "00:00"


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
