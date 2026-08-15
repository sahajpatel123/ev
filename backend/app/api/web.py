"""EV web workbench: self-hosted static SPA served with a strict CSP."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Device, OwnerIdentity
from app.utils.text import sha256_hex, utcnow

WEB_ROOT = Path(__file__).resolve().parents[2] / "clients" / "web"
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1"}
_LOOPBACK_HOST_NAMES = {"127.0.0.1", "localhost", "::1"}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

router = APIRouter(prefix="/app", tags=["web"])


@router.get("/bootstrap")
async def bootstrap(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Loopback-only workbench bootstrap (AGENT 17 WORKBENCH).

    Mints a short-lived *device token* for the local workbench — never the
    master key. The API already accepts device tokens as Bearer credentials,
    so no auth engine change is required. Every successful bootstrap rotates
    the token for the ``workbench-local`` device; older tokens stop working.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        raise HTTPException(status_code=403, detail="Loopback clients only")
    host_header = (request.headers.get("host") or "").split(":", 1)[0].strip("[]").lower()
    if host_header not in _LOOPBACK_HOST_NAMES:
        raise HTTPException(status_code=403, detail="Loopback clients only")

    token = secrets.token_urlsafe(32)
    active = (
        await session.execute(
            select(Device).where(
                Device.name == "workbench-local",
                Device.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    owner = (
        await session.execute(
            select(OwnerIdentity)
            .order_by(OwnerIdentity.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if active is None:
        active = Device(
            name="workbench-local",
            token_hash=sha256_hex(token),
            capabilities=[
                "web",
                "workbench",
                "chat",
                "capture",
                "memory",
                "voice",
                "people",
                "integrations",
                "routines",
            ],
            trust_level="owner" if owner else "device",
            owner_id=owner.id if owner else None,
            device_type="desktop",
            platform="web",
            paired_at=utcnow() if owner else None,
        )
        session.add(active)
    else:
        # Rotate the plaintext token: the hash changes, so any previously
        # issued bootstrap token for this device is invalidated.
        active.token_hash = sha256_hex(token)
        active.trust_level = "owner" if owner else "device"
        if owner:
            active.owner_id = owner.id
            active.paired_at = utcnow()
        active.last_seen_at = utcnow()
    await session.commit()

    return {
        "authenticated": True,
        "mode": "loopback",
        "label": "connected (this Mac)",
        "token": token,
        "device_id": str(active.id),
        "device_name": active.name,
        "trust_level": active.trust_level,
        "note": (
            "One-time workbench device token; never the master key. "
            "Each bootstrap rotates the previous token."
        ),
    }


def _html(name: str) -> HTMLResponse:
    path = WEB_ROOT / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Web client not installed")
    return HTMLResponse(path.read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def web_presence() -> HTMLResponse:
    """Default surface: EVIE lives in the menu bar, not this page."""
    return _html("presence.html")


@router.get("/ops", response_class=HTMLResponse)
async def web_ops() -> HTMLResponse:
    """Operator console — not the product UI."""
    return _html("index.html")


@router.get("/app.js", response_class=FileResponse)
async def web_app_js() -> FileResponse:
    return FileResponse(WEB_ROOT / "app.js", headers=SECURITY_HEADERS)


@router.get("/style.css", response_class=FileResponse)
async def web_style_css() -> FileResponse:
    return FileResponse(WEB_ROOT / "style.css", headers=SECURITY_HEADERS)


@router.get("/presence.css", response_class=FileResponse)
async def web_presence_css() -> FileResponse:
    return FileResponse(WEB_ROOT / "presence.css", headers=SECURITY_HEADERS)


@router.get("/gallery", response_class=HTMLResponse)
async def web_gallery() -> HTMLResponse:
    """Owner review of current HUD window types. Not a permanent skin."""
    return _html("gallery.html")


@router.get("/lookout", response_class=HTMLResponse)
async def web_lookout() -> HTMLResponse:
    """Independent HUD window — not the workbench."""
    return _html("lookout.html")


@router.get("/stage", response_class=HTMLResponse)
async def web_stage() -> HTMLResponse:
    """Visor stage that can host or pop out multiple lookouts."""
    return _html("stage.html")


@router.get("/lookout.css", response_class=FileResponse)
async def web_lookout_css() -> FileResponse:
    return FileResponse(WEB_ROOT / "lookout.css", headers=SECURITY_HEADERS)


@router.get("/lookout.js", response_class=FileResponse)
async def web_lookout_js() -> FileResponse:
    return FileResponse(WEB_ROOT / "lookout.js", headers=SECURITY_HEADERS)


@router.get("/indoor", response_class=HTMLResponse)
async def web_indoor() -> HTMLResponse:
    return _html("indoor.html")


@router.get("/indoor.js", response_class=FileResponse)
async def web_indoor_js() -> FileResponse:
    return FileResponse(WEB_ROOT / "indoor.js", headers=SECURITY_HEADERS)


@router.get("/indoor.css", response_class=FileResponse)
async def web_indoor_css() -> FileResponse:
    return FileResponse(WEB_ROOT / "indoor.css", headers=SECURITY_HEADERS)
