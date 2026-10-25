"""EV — FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.api import (
    agent as agent_api,
)
from app.api import (
    assistant,
    backup,
    cognitive,
    companion,
    compliance,
    core,
    ears,
    edith,
    ev,
    everywhere,
    fabric,
    file_sandbox,
    filter,
    identity,
    integrations,
    life,
    maintenance,
    ops,
    people,
    routines,
    runtime,
    tools,
    training,
    voice,
    voice_live,
    web,
    world_model,
)
from app.config import settings
from app.db import init_db
from app.device_gateway import api as device_gateway_api
from app.device_gateway import pwa as device_gateway_pwa
from app.device_gateway import release_portal
from app.device_gateway.security import origin_allowed
from app.digital.api import router as digital_ops_router
from app.gateway.openrouter_muse import BrainEgressDenied, BrainUnavailable

LOGGER = logging.getLogger("ev.main")


class GatewayOriginMiddleware:
    """Isolate Device Gateway / PWA from global CORS * + credentials.

    Pure-ASGI (deliberately NOT BaseHTTPMiddleware): the base class buffers
    the entire response body before dispatch returns, which breaks SSE
    streaming — first token waits for the whole pipeline. This passes bodies
    through untouched and only rewrites response-start headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if not (path.startswith("/v1/device-gateway") or path.startswith("/evie")):
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        origin = headers.get("origin")
        host = headers.get("host")
        if origin and not origin_allowed(origin, host):
            body = json.dumps(
                {"detail": "Origin not allowed"}, separators=(",", ":")
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                        (b"x-error-code", b"origin_denied"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        async def send_with_origin(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = [(k, v) for k, v in message.get("headers", [])]
                if origin and origin_allowed(origin, host):
                    raw = [
                        (k, v)
                        for k, v in raw
                        if k.lower()
                        not in (
                            b"access-control-allow-origin",
                            b"access-control-allow-credentials",
                            b"vary",
                        )
                    ]
                    raw += [
                        (b"access-control-allow-origin", origin.encode("latin-1")),
                        (b"access-control-allow-credentials", b"true"),
                        (b"vary", b"Origin"),
                    ]
                else:
                    first_acao: bytes | None = None
                    for k, v in raw:
                        if k.lower() == b"access-control-allow-origin":
                            first_acao = v
                            break
                    if first_acao == b"*":
                        raw = [
                            (k, v)
                            for k, v in raw
                            if k.lower() != b"access-control-allow-origin"
                        ]
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_origin)


class GatewayCorrelationMiddleware:
    """G2 PART 10/13: gateway responses correlate to the running build and
    authorization-bearing responses are never stale-cached as authority.

    Pure-ASGI for the same streaming reason as GatewayOriginMiddleware.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(
            "/v1/device-gateway"
        ):
            await self.app(scope, receive, send)
            return
        from app.runtime_identity import runtime_git_sha

        sha = (runtime_git_sha() or "unknown").encode("latin-1")

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"x-evie-backend-sha", b"cache-control")
                ]
                headers += [
                    (b"x-evie-backend-sha", sha),
                    (b"cache-control", b"no-store"),
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_correlation)


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
    from app.agent.runner import bootstrap as agent_bootstrap

    agent_watch = agent_bootstrap()
    warmup = asyncio.create_task(_warmup_tts())
    from app.ev.timers import timer_watch_loop

    watch = asyncio.create_task(timer_watch_loop(), name="ev-timer-watch")
    from app.device_gateway.digest import phone_digest_watch_loop

    digest_watch = asyncio.create_task(phone_digest_watch_loop(), name="ev-phone-digest")
    desk_watch = None
    if settings.environment != "test":
        from app.ev.laptop_files import laptop_files_allowed

        if laptop_files_allowed():
            from app.ev.desk_scene import steward_watch_loop

            desk_watch = asyncio.create_task(steward_watch_loop(), name="ev-desk-steward")
    yield
    from app.agent.runner import shutdown as agent_shutdown

    agent_shutdown()
    if agent_watch is not None:
        agent_watch.cancel()
    watch.cancel()
    digest_watch.cancel()
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
    # The API is reachable by paired iPhones over Tailscale Serve. Keep the
    # interactive schema off unless the owner explicitly opts in.
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)


@app.exception_handler(BrainUnavailable)
async def _brain_unavailable(_request: Request, exc: BrainUnavailable):
    """Fail closed: missing or rejected brain credentials are not a 500."""

    return JSONResponse(
        status_code=503,
        content={"detail": str(exc) or "Intelligence provider is unavailable"},
        headers={"X-Error-Code": "brain_unavailable"},
    )


@app.exception_handler(BrainEgressDenied)
async def _brain_egress_denied(_request: Request, exc: BrainEgressDenied):
    """Fail closed: denied brain egress is not a 500."""

    return JSONResponse(
        status_code=503,
        content={"detail": str(exc) or "Intelligence provider is unavailable"},
        headers={"X-Error-Code": "brain_egress_denied"},
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
app.include_router(ears.router)  # always-on ears delivery (/v1/ears/wake)
app.include_router(voice_live.router)  # hands-free EVIE loop (/v1/voice/hands-free)
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
app.include_router(file_sandbox.router)  # Evie file sandbox: one jail for Mac + iPhone + brain
app.include_router(web.router)
app.include_router(device_gateway_api.router)


app.add_middleware(GatewayCorrelationMiddleware)
app.include_router(device_gateway_pwa.router)
app.include_router(release_portal.router)  # private tailnet-only install portal
app.include_router(digital_ops_router)
app.include_router(cognitive.router)
app.include_router(fabric.router)  # Evie fabric lanes (WP1, additive)
app.include_router(agent_api.router)  # general local agent runtime (additive)

@app.get("/")
async def root() -> dict:
    return {
        "app": settings.app_name,
        "status": "ok",
        "docs": "/docs",
        "health": "/v1/health",
    }

