"""EV — FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    backup,
    companion,
    compliance,
    core,
    edith,
    ev,
    filter,
    identity,
    integrations,
    maintenance,
    ops,
    routines,
    runtime,
    tools,
    training,
    voice,
    web,
)
from app.config import settings
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


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
app.include_router(backup.router)
app.include_router(ev.router)
app.include_router(companion.router)
app.include_router(edith.router)
app.include_router(identity.router)
app.include_router(routines.router)
app.include_router(runtime.router)
app.include_router(training.router)
app.include_router(voice.router)
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
