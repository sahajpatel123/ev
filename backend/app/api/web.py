"""EV web workbench: self-hosted static SPA served with a strict CSP."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

WEB_ROOT = Path(__file__).resolve().parents[2] / "clients" / "web"
_ALLOWED_ASSETS = {"app.js", "style.css", "pcm-worklet.js"}

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


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def web_app() -> HTMLResponse:
    path = WEB_ROOT / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Web client not installed")
    return HTMLResponse(path.read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


@router.get("/{asset}", response_class=FileResponse)
async def web_asset(asset: str) -> FileResponse:
    if asset not in _ALLOWED_ASSETS:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(WEB_ROOT / asset, headers=SECURITY_HEADERS)
