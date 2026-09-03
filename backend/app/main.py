"""EV — FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import (
    assistant,
    everywhere,
    life,
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
    world_model,
)
from app.config import settings
from app.db import init_db
from app.device_gateway import api as device_gateway_api
from app.device_gateway import pwa as device_gateway_pwa
from app.device_gateway import release_portal
from app.device_gateway.security import origin_allowed

LOGGER = logging.getLogger("ev.main")


class GatewayOriginMiddleware(BaseHTTPMiddleware):
    """Isolate Device Gateway / PWA from global CORS * + credentials."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not (path.startswith("/v1/device-gateway") or path.startswith("/evie")):
            return await call_next(request)
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and not origin_allowed(origin, host):
            return JSONResponse(
                {"detail": "Origin not allowed"},
                status_code=403,
                headers={"X-Error-Code": "origin_denied"},
            )
        response = await call_next(request)
        if origin and origin_allowed(origin, host):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        elif response.headers.get("access-control-allow-origin") == "*":
            del response.headers["access-control-allow-origin"]
        return response


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
    stop = None
    power_watch = None
    if settings.environment != "test":
        from app.device_gateway.power import start_if_needed
        from app.device_gateway.power import stop as stop_assertion

        start_if_needed(
            mode_on=bool(settings.home_station_mode),
            keep_ac=bool(settings.home_station_keep_awake_on_ac),
            keep_battery=bool(settings.home_station_keep_awake_on_battery),
        )
        stop = stop_assertion

        async def _power_watch() -> None:
            while True:
                await asyncio.sleep(60)
                start_if_needed(
                    mode_on=bool(settings.home_station_mode),
                    keep_ac=bool(settings.home_station_keep_awake_on_ac),
                    keep_battery=bool(settings.home_station_keep_awake_on_battery),
                )

        power_watch = asyncio.create_task(_power_watch(), name="ev-home-station-power")
    warmup = asyncio.create_task(_warmup_tts())
    from app.ev.timers import timer_watch_loop

    watch = asyncio.create_task(timer_watch_loop(), name="ev-timer-watch")
    desk_watch = None
    if settings.environment != "test":
        from app.ev.laptop_files import laptop_files_allowed

        if laptop_files_allowed():
            from app.ev.desk_scene import steward_watch_loop

            desk_watch = asyncio.create_task(steward_watch_loop(), name="ev-desk-steward")
    yield
    watch.cancel()
    warmup.cancel()
    if desk_watch is not None:
        desk_watch.cancel()
    if power_watch is not None:
        power_watch.cancel()
    if stop is not None:
        stop()


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
app.add_middleware(GatewayOriginMiddleware)

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
app.include_router(life.router)
app.include_router(everywhere.router)  # G2 Evie Everywhere: one Evie, many devices
app.include_router(world_model.router)  # AGENT 2 personal world model
app.include_router(filter.router)
app.include_router(integrations.router)
app.include_router(maintenance.router)
app.include_router(ops.router)
app.include_router(compliance.router)
app.include_router(tools.router)
app.include_router(web.router)
app.include_router(device_gateway_api.router)


@app.middleware("http")
async def _gateway_correlation_and_cache_policy(request, call_next):
    """G2 PART 10/13: gateway responses correlate to the running build and
    authorization-bearing responses are never stale-cached as authority."""
    if request.url.path.startswith("/v1/device-gateway"):
        from app.runtime_identity import runtime_git_sha

        response = await call_next(request)
        response.headers["X-Evie-Backend-Sha"] = runtime_git_sha() or "unknown"
        response.headers["Cache-Control"] = "no-store"
        return response
    return await call_next(request)
app.include_router(device_gateway_pwa.router)
app.include_router(release_portal.router)  # private tailnet-only install portal


@app.get("/")
async def root() -> dict:
    return {
        "app": settings.app_name,
        "status": "ok",
        "docs": "/docs",
        "health": "/v1/health",
    }
