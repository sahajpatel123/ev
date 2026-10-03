"""Owner-only connection setup never drives conversations or sends messages."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.digital.adapters.whatsapp import BackgroundWhatsAppBacking
from app.ev.messaging import whatsapp_cdp


async def test_whatsapp_status_returns_metadata_not_private_payload(client, monkeypatch):
    probe = AsyncMock(return_value={"authenticated": True, "background": True,
                                  "diagnosis": "ok", "cookies": "private", "messages": ["private"]})
    monkeypatch.setattr(BackgroundWhatsAppBacking, "status", probe, raising=False)
    response = await client.get("/v1/digital/whatsapp/status")
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "diagnosis": "ok", "background": True}


async def test_whatsapp_connect_never_reveals_workspace(client, monkeypatch):
    start = AsyncMock(return_value=("qr", "cdp_qr"))
    monkeypatch.setattr(whatsapp_cdp, "setup", start)
    monkeypatch.setattr(BackgroundWhatsAppBacking, "status", AsyncMock(return_value={"authenticated": False, "qr_required": True}), raising=False)
    response = await client.post("/v1/digital/whatsapp/connect")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    start.assert_awaited_once_with(include_qr=False)


async def test_link_qr_is_private_uncached_png(client, monkeypatch):
    monkeypatch.setattr(whatsapp_cdp, "link_qr", AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfake-qr"), raising=False)
    response = await client.get("/v1/digital/whatsapp/link-qr")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_link_qr_missing_is_conflict_not_fake_image(client, monkeypatch):
    monkeypatch.setattr(whatsapp_cdp, "link_qr", AsyncMock(return_value=None), raising=False)
    response = await client.get("/v1/digital/whatsapp/link-qr")
    assert response.status_code == 409


@pytest.mark.parametrize("method,path", [("get", "status"), ("post", "connect"), ("get", "link-qr")])
async def test_whatsapp_connection_requires_owner_key(client, method, path, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(BackgroundWhatsAppBacking, "status", probe, raising=False)
    monkeypatch.setattr(whatsapp_cdp, "link_qr", probe, raising=False)
    monkeypatch.setattr(whatsapp_cdp, "setup", probe)
    response = await getattr(client, method)(f"/v1/digital/whatsapp/{path}", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 403
    probe.assert_not_awaited()
