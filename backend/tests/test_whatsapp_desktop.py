"""Legacy Desktop entry points must obey background-only WhatsApp policy."""

from __future__ import annotations

import pytest

from app.ev.messaging import whatsapp_desktop
from app.ev.messaging.routing import route_channel


def test_only_background_web_is_an_eligible_transport() -> None:
    refused = route_channel("whatsapp", helper_available=True, desktop_available=True)
    assert refused.mode == "unavailable"
    assert refused.provider == "none"
    background = route_channel("whatsapp", helper_available=False, web_available=True)
    assert background.provider == "web"
    assert background.requires_approval is True


async def test_available_delegates_to_headless_workspace(monkeypatch) -> None:
    seen = []

    async def available(*, refresh=False):
        seen.append(refresh)
        return False, "cdp_qr"

    monkeypatch.setattr("app.ev.messaging.whatsapp_cdp.available", available)
    assert await whatsapp_desktop.available(refresh=True) == (False, "cdp_qr")
    assert seen == [True]


async def test_send_preserves_uncertainty_without_accessibility(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Desktop helper must never run")

    async def uncertain(to, text):
        return {"ok": False, "sent": False, "send_attempted": True,
                "retry_safe": False, "error": "send_not_confirmed", "to": to}

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", forbidden)
    monkeypatch.setattr("app.ev.messaging.whatsapp_cdp.send", uncertain)
    outcome = await whatsapp_desktop.send("Ada", "hello")
    assert outcome["sent"] is False
    assert outcome["send_attempted"] is True
    assert outcome["retry_safe"] is False


async def test_send_uses_only_background_transport(monkeypatch) -> None:
    seen = []

    async def verified(to, text):
        seen.append((to, text))
        return {"ok": True, "sent": True, "verified_in_thread": True, "focus_theft": 0,
                "driver": "cdp", "to": to}

    monkeypatch.setattr("app.ev.messaging.whatsapp_cdp.send", verified)
    outcome = await whatsapp_desktop.send("Ada", "hello")
    assert seen == [("Ada", "hello")]
    assert outcome["driver"] == "cdp"
    assert outcome["focus_theft"] == 0


@pytest.mark.parametrize("operation", ["read", "list"])
async def test_reads_use_background_workspace(monkeypatch, operation) -> None:
    async def read(to, *, limit):
        return {"ok": True, "to": to, "messages": [], "complete_history": False}

    async def chats(query, *, limit):
        return {"ok": True, "chats": [], "complete_history": False}

    monkeypatch.setattr("app.ev.messaging.whatsapp_cdp.read_recent", read)
    monkeypatch.setattr("app.ev.messaging.whatsapp_cdp.search_chats", chats)
    outcome = (await whatsapp_desktop.read_recent("Ada", limit=10) if operation == "read"
               else await whatsapp_desktop.list_chats(limit=10))
    assert outcome["ok"] is True
    assert outcome["complete_history"] is False
