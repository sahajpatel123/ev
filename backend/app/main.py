"""EV — FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    assistant,
    backup,
    companion,
    compliance,
    core,
    ears,
    edith,
    ev,
    filter,
    identity,
    integrations,
    maintenance,
    ops,
    people,
    routines,
    runtime,
    tools,
    training,
    voice,
    web,
)
from app.config import settings
from app.db import init_db

LOGGER = logging.getLogger("ev.main")


async def _warmup_tts() -> None:
    """Load Kokoro (or the configured engine) so the first spoken word is not a cold start."""

    try:
        from app.voice.contracts import SpeechStyle
        from app.voice.tts import get_synthesizer

        synthesizer = get_synthesizer()
        await synthesizer.synthesize("Ready.", style=SpeechStyle())
    except Exception as exc:  # noqa: BLE001 - warmup must never block serving
        LOGGER.info("tts warmup skipped: %s", exc)


async def _restore_companion_prefs() -> None:
    try:
        from app.db import SessionLocal
        from app.notify.proactive import restore_quiet_hours

        async with SessionLocal() as session:
            await restore_quiet_hours(session)
    except Exception as exc:  # noqa: BLE001 - prefs must never block serving
        LOGGER.info("companion prefs restore skipped: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await _restore_companion_prefs()
    warmup = asyncio.create_task(_warmup_tts())
    yield
    warmup.cancel()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="EV — persistent personal AI companion (E.V.-inspired).",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(core.router)
app.include_router(assistant.router)
app.include_router(backup.router)
app.include_router(ev.router)
app.include_router(companion.router)
app.include_router(edith.router)
app.include_router(identity.router)
app.include_router(routines.router)
app.include_router(runtime.router)
app.include_router(training.router)
app.include_router(voice.router)
app.include_router(ears.router)
app.include_router(people.router)  # AGENT 7 ROSTER
app.include_router(filter.router)
app.include_router(integrations.router)
app.include_router(maintenance.router)
app.include_router(ops.router)
app.include_router(compliance.router)
app.include_router(tools.router)
app.include_router(web.router)


@app.get("/")
async def root() -> dict:
    return {
        "app": settings.app_name,
        "status": "ok",
        "docs": "/docs",
        "health": "/v1/health",
    }
