"""Installable Evie PWA at /evie. Same-origin with the Device Gateway."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

PWA_ROOT = Path(__file__).resolve().parents[2] / "clients" / "pwa"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self' ws: wss: https://api.openai.com; "
        "media-src 'self' blob: mediastream:; "
        "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "microphone=(self), camera=(self), geolocation=()",
    "Cache-Control": "no-store",
}

STATIC_CACHE = {"Cache-Control": "public, max-age=60"}

JS_FILES = (
    "app.js",
    "audio.js",
    "orb.js",
    "presence.js",
    "webrtc.js",
    "mobile-actions.js",
    "feedback.js",
    "pcm-worklet.js",
    "playback-worklet.js",
    "sw.js",
)

router = APIRouter(tags=["evie-pwa"])


def _file(name: str, *, media_type: str | None = None, cache: bool = False) -> FileResponse:
    path = PWA_ROOT / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="PWA asset missing")
    headers = {**SECURITY_HEADERS, **(STATIC_CACHE if cache else {})}
    if name in {"sw.js", "index.html"} or name.endswith(".js"):
        headers["Cache-Control"] = "no-store"
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/evie")
@router.get("/evie/")
async def pwa_index() -> HTMLResponse:
    path = PWA_ROOT / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PWA not installed")
    return HTMLResponse(path.read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


@router.get("/evie/app.js")
async def pwa_js() -> FileResponse:
    return _file("app.js", media_type="application/javascript")


@router.get("/evie/audio.js")
async def pwa_audio() -> FileResponse:
    return _file("audio.js", media_type="application/javascript")


@router.get("/evie/orb.js")
async def pwa_orb() -> FileResponse:
    return _file("orb.js", media_type="application/javascript")


@router.get("/evie/presence.js")
async def pwa_presence() -> FileResponse:
    return _file("presence.js", media_type="application/javascript")


@router.get("/evie/webrtc.js")
async def pwa_webrtc() -> FileResponse:
    return _file("webrtc.js", media_type="application/javascript")


@router.get("/evie/mobile-actions.js")
async def pwa_mobile_actions() -> FileResponse:
    return _file("mobile-actions.js", media_type="application/javascript")


@router.get("/evie/feedback.js")
async def pwa_feedback() -> FileResponse:
    return _file("feedback.js", media_type="application/javascript")


@router.get("/evie/style.css")
async def pwa_css() -> FileResponse:
    return _file("style.css", media_type="text/css")


@router.get("/evie/manifest.webmanifest")
async def pwa_manifest() -> FileResponse:
    return _file("manifest.webmanifest", media_type="application/manifest+json", cache=True)


@router.get("/evie/sw.js")
async def pwa_sw() -> FileResponse:
    return _file("sw.js", media_type="application/javascript")


@router.get("/evie/pcm-worklet.js")
async def pwa_worklet() -> FileResponse:
    return _file("pcm-worklet.js", media_type="application/javascript")


@router.get("/evie/playback-worklet.js")
async def pwa_playback_worklet() -> FileResponse:
    return _file("playback-worklet.js", media_type="application/javascript")


@router.get("/evie/diag-speech.pcm")
async def pwa_diag_pcm() -> FileResponse:
    return _file("diag-speech.pcm", media_type="application/octet-stream")


@router.get("/evie/diag-speech.wav")
async def pwa_diag_wav() -> FileResponse:
    return _file("diag-speech.wav", media_type="audio/wav")


@router.get("/evie/icon.svg")
async def pwa_icon() -> FileResponse:
    return _file("icon.svg", media_type="image/svg+xml", cache=True)


@router.get("/evie/apple-touch-icon.png")
async def pwa_touch() -> FileResponse:
    return _file("apple-touch-icon.png", media_type="image/png", cache=True)


@router.get("/evie/offline")
async def pwa_offline() -> Response:
    return HTMLResponse(
        "<!doctype html><title>Evie</title><p>Home Station unavailable.</p>",
        headers=SECURITY_HEADERS,
    )
