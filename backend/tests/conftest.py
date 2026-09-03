import os
import sys
import tempfile

# P0 CLOSURE GUARD (PART 8): test runs must never mutate production owner
# state. Live-DB opt-in is refused against a production environment unless
# an operator explicitly allows it for that single invocation.
if (
    os.environ.get("EV_TEST_USE_LIVE_DB") == "1"
    and os.environ.get("EV_ENV", "").strip().lower() == "production"
    and os.environ.get("EV_ALLOW_PROD_TESTS", "") != "1"
):
    sys.exit(
        "REFUSING TO RUN TESTS AGAINST PRODUCTION. "
        "Unset EV_TEST_USE_LIVE_DB (isolated DB) or set EV_ALLOW_PROD_TESTS=1 "
        "for this one invocation."
    )

_TMP = tempfile.mkdtemp(prefix="ev-tests-")
# Never inherit the owner's live DATABASE_URL. pytest drop_all would wipe
# the running Postgres. Opt in with EV_TEST_USE_LIVE_DB=1 only.
if os.environ.get("EV_TEST_USE_LIVE_DB") != "1":
    os.environ["EV_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["EV_MASTER_KEY"] = "test-key"
os.environ["EV_API_KEY"] = "test-key"
# Force sync: inherited EV_PROCESSING_MODE=queue would enqueue onto owner Redis.
os.environ["EV_PROCESSING_MODE"] = "sync"
if os.environ.get("EV_TEST_USE_LIVE_CHAT") != "1":
    # Overwrite inherited shell/env-file provider keys. setdefault leaks the
    # owner's EV_VOICE_LIVE_BRAIN=openai and a live DeepSeek key into unit tests.
    os.environ["EV_CHAT_PROVIDER"] = "mock"
    os.environ["EV_XAI_API_KEY"] = ""
    os.environ["EV_OPENAI_API_KEY"] = ""
    os.environ["EV_DEEPSEEK_API_KEY"] = ""
    os.environ["EV_OPENCODE_API_KEY"] = ""
    os.environ["EV_VOICE_LIVE_BRAIN"] = "pipeline"
    os.environ["EV_BRAVE_SEARCH_API_KEY"] = ""
# Health/queue probes ping Redis; default redis://localhost:6379/0 is the
# owner instance. Port 9 refuses immediately. Opt in with EV_TEST_USE_LIVE_REDIS=1.
if os.environ.get("EV_TEST_USE_LIVE_REDIS") != "1":
    os.environ["EV_REDIS_URL"] = "redis://127.0.0.1:9/15"
# Blank the helper and disable osascript so tests cannot drive the real Mac.
# Opt in with EV_TEST_USE_LIVE_MAC=1.
if os.environ.get("EV_TEST_USE_LIVE_MAC") != "1":
    os.environ["EV_LIFE_HELPER_PATH"] = ""
    os.environ["EV_NOTIFY_MACOS_HELPER_PATH"] = ""
    os.environ["EV_NOTIFY_MACOS_ALLOW_OSASCRIPT"] = "false"
    os.environ["EV_NOTIFY_BACKEND"] = "console"
    os.environ["EV_MESSAGING_PROVIDER"] = "local"
os.environ.setdefault("EV_EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("EV_EMBEDDING_DIM", "64")
# The suite authenticates with these literal values, so they are set rather
# than defaulted: a developer or CI runner with EV_MASTER_KEY exported for a
# real install must not turn every request into a 401.
os.environ["EV_MASTER_KEY"] = "test-key"
os.environ["EV_VAULT_KEY"] = "test-vault-key-0123456789abcdef"
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
os.environ.setdefault("EV_MEMORY_DIR", f"{_TMP}/ev-memory")
os.environ.setdefault("EV_CODE_WORKSPACE", f"{_TMP}/code-workspace")
os.environ.setdefault("EV_MEMORY_GATE", "off")
os.environ.setdefault("EV_MEMORY_PREFETCH", "off")
os.environ.setdefault("EV_ENVIRONMENT", "test")
os.environ.setdefault("EV_ACCESS_LOG_ENABLED", "true")
os.environ.setdefault("EV_QUIET_HOURS_START", "23:59")
os.environ.setdefault("EV_QUIET_HOURS_END", "00:00")
# Voice-control plan: tests stay on the byte-identical supervised surface.
# The owner's runtime .env may set EV_VOICE_LIVE_MODE=shadow; that must not
# leak into unit tests (create_response / tool catalog assertions).
os.environ["EV_VOICE_LIVE_MODE"] = "supervised"

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def offline_voice_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the voice providers to the offline doubles CI resolves `auto` to.

    `auto` picks the real Vosk/Piper engines whenever a developer has the
    speech models installed, which would otherwise make the outcome of every
    voice test depend on the machine it runs on. Tests that want a specific
    engine (including `auto`) set the provider themselves.
    """

    monkeypatch.setattr(settings, "voice_wake_provider", "phrase")
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    monkeypatch.setattr(settings, "voice_tts_provider", "meta")


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

    from app.gateway.reliability import CIRCUIT_BREAKERS

    CIRCUIT_BREAKERS.reset()
    from app.config import settings

    def _restore_flags() -> None:
        settings.quiet_hours_start = "23:59"
        settings.quiet_hours_end = "00:00"
        settings.memory_gate = os.environ.get("EV_MEMORY_GATE", "off")
        settings.memory_dir = os.environ.get("EV_MEMORY_DIR")
        settings.memory_curator_enabled = True
        settings.memory_prefetch = os.environ.get("EV_MEMORY_PREFETCH", "off")
        settings.cross_platform_production_memory = False
        # F2/F3/F4/F5: secrets overlay must not leak into tests. Set BEFORE
        # the test too — pytest yield-teardown is too late for the first case.
        settings.computer_executor_v2 = "off"
        settings.capability_router_v2 = "off"
        settings.model_surface_v2 = "legacy"
        settings.memory_scoring_v2 = "off"
        settings.prospective_context_v1 = "off"
        settings.code_enabled = True
        settings.code_workspace = os.environ.get("EV_CODE_WORKSPACE")
        settings.code_projects = ""
        settings.code_projects_root = ""
        settings.voice_live_mode = "supervised"

    _restore_flags()
    from app.voice.live.layer import reset_live_registry

    reset_live_registry()
    yield
    from app.memory.bootstrap import reset_bootstrap_cache
    from app.memory.prefetch import reset_prefetch
    from app.voice.live.layer import reset_live_registry

    _restore_flags()
    reset_live_registry()
    reset_bootstrap_cache()
    reset_prefetch()


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
    from app.memory.life_archive.locate import reset_people_cache

    reset_people_cache()
    async with SessionLocal() as session:
        yield session
