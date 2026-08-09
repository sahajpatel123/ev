"""Tests for the EV web workbench (static SPA served at /app)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_web_app_served_with_strict_csp(client: AsyncClient) -> None:
    resp = await client.get("/app")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert "no-referrer" in resp.headers["referrer-policy"]
    html = resp.text
    for marker in (
        "ev-hud-card",
        "ev-memory-browser",
        "ev-timeline",
        "capture-form",
        "ask-form",
        "connection-form",
    ):
        assert marker in html


async def test_web_app_has_no_third_party_resources(client: AsyncClient) -> None:
    html = (await client.get("/app")).text
    js = (await client.get("/app/app.js")).text
    assert 'src="http' not in html and 'src="https' not in html
    assert 'href="http' not in html and 'href="https' not in html
    assert "<script src=\"http" not in js


async def test_web_assets_served_and_allowlisted(client: AsyncClient) -> None:
    js = await client.get("/app/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    for endpoint in (
        "/v1/health",
        "/v1/events",
        "/v1/chat",
        "/v1/memories",
        "/v1/audit/",
        "/v1/timeline",
        "/v1/hud/card",
    ):
        assert endpoint in js.text
    assert js.headers["content-security-policy"].startswith("default-src 'self'")

    css = await client.get("/app/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    assert (await client.get("/app/secret.txt")).status_code == 404
    assert (await client.get("/app/../app.py")).status_code == 404


async def test_web_uses_idempotent_capture_keys(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    assert "Idempotency-Key" in js
    assert "crypto.randomUUID()" in js
    assert "ev.offlineQueue" in js
    assert "ev.quarantine" in js
    assert "queueCapture" in js
    assert "syncQueue" in js
    assert "window.EV" in js
    html = (await client.get("/app")).text
    assert 'id="sync-queue"' in html
    assert 'id="queue-status"' in html
