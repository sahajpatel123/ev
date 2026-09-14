"""Regression tests for the approved-route contract.

The owner asked Evie to send a WhatsApp message. Evie asked "Should I send …
to X on WhatsApp?", the owner said yes, and the message was never delivered:
by execution time the WhatsApp Web tab was gone, so the send silently moved to
a different transport (the macOS helper) and died on a permission error the
owner never heard about.

These tests pin the two halves of the contract that failure violated:

1. an approval covers a *transport*, not just a channel name — if the approved
   route is gone at execution time the send is refused with an actionable line
   instead of being re-routed onto a different app;
2. the recipient identity resolved at park time is the identity used at send
   time, so the person approved is the person addressed.
"""

from __future__ import annotations

import pytest

from app.ev.confirm import pol_meta
from app.ev.messaging import whatsapp_web
from app.ev.messaging.approval import (
    handle_send_approval,
    latest_pending,
    question_for,
)

VERB_WEB = "https://web.whatsapp.com/"


async def _fake_destination(*_args, **_kwargs) -> dict:
    return {"status": "unique", "handle": "John Smith", "phone": "", "email": ""}


async def _fake_chat_identity(to, **_kwargs) -> dict:
    """Channel-native resolution that carries a stable chat identity."""

    return {
        "status": "unique",
        "display": "John Smith",
        "chat_ref": "919876543210@s.whatsapp.net",
        "peer": {"handle": "John Smith", "phone": "919876543210"},
    }


def _allow_policy(monkeypatch) -> None:
    from app.ev.policy import PolicyDecision

    async def fake_authorize(*_args, **_kwargs):
        return PolicyDecision(
            allowed=True,
            effect="allow",
            reason="ok",
            risk_class="R2",
            confirmation_required=False,
            confirmation_policy="none",
            provider="messaging",
            spoken="",
        )

    monkeypatch.setattr("app.ev.policy.authorize", fake_authorize)
    monkeypatch.setattr("app.ev.policy.provider_connected", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.ev.tools._whatsapp_peer", lambda *_a, **_k: None)


def _forbid_other_transports(monkeypatch) -> list[str]:
    """Any use of the non-approved transports is a failure of the contract."""

    used: list[str] = []

    def boom(*_a, **_k):
        used.append("helper")
        raise AssertionError("the helper transport must never be used for an approved web send")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", boom)
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_compose",
        lambda *a, **k: used.append("foreground_compose") or False,
    )
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_app",
        lambda *a, **k: used.append("foreground_app") or False,
    )
    return used


@pytest.mark.asyncio
async def test_approved_web_send_refuses_when_the_tab_vanishes(db_session, monkeypatch) -> None:
    """Park with the tab open, approve after it is gone: no silent re-route."""

    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    monkeypatch.setattr(whatsapp_web, "resolve", _fake_chat_identity)
    used = _forbid_other_transports(monkeypatch)
    sends: list[tuple[str, str]] = []

    async def fake_web_send(to, text):
        sends.append((to, text))
        return {"ok": True, "sent": True, "verified_in_thread": True, "to": to}

    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    # 1. The tab is open, so the send is parked for approval.
    probes: list[bool] = []

    async def web_available(*_a, **_k) -> bool:
        probes.append(True)
        return True

    monkeypatch.setattr(whatsapp_web, "web_available", web_available)
    parked = await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    assert parked.result is not None
    assert parked.result.get("pending_approval") is True
    assert sends == []

    # 2. The owner walks away, Chrome is closed, and only now does she say yes.
    async def web_gone(*_a, **_k) -> bool:
        return False

    monkeypatch.setattr(whatsapp_web, "web_available", web_gone)

    approved = await handle_send_approval(db_session, "yes", actor="voice")
    assert approved is not None
    assert approved.get("sent") is False
    assert sends == [], "a send must not go out over an unapproved transport"
    assert used == [], "the approved route is web; nothing else may run"
    spoken = str(approved.get("spoken") or "")
    assert "WhatsApp Web" in spoken
    assert "Chrome" in spoken
    assert "didn't send" in spoken


@pytest.mark.asyncio
async def test_parked_send_stores_the_chat_identity_and_the_approved_route(
    db_session, monkeypatch
) -> None:
    """The ticket carries the transport and the identity it was asked about."""

    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    monkeypatch.setattr(whatsapp_web, "resolve", _fake_chat_identity)

    async def web_available(*_a, **_k) -> bool:
        return True

    monkeypatch.setattr(whatsapp_web, "web_available", web_available)

    await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    pending = await latest_pending(db_session)
    assert pending is not None
    meta = pol_meta(pending.payload)
    assert meta.get("route") is not None, "the approved transport must be recorded"
    assert meta["route"]["provider"] == "web"
    assert meta["route"]["channel"] == "whatsapp"
    # The stable chat identity, not the display name, is what execution reuses.
    assert meta.get("address") == "919876543210"
    assert meta.get("address") != meta.get("display")
    assert meta.get("binding_fingerprint")


@pytest.mark.asyncio
async def test_tampering_with_the_approved_transport_is_refused(db_session, monkeypatch) -> None:
    """Swapping the transport on a stored ticket must not resume."""

    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    monkeypatch.setattr(whatsapp_web, "resolve", _fake_chat_identity)

    async def web_available(*_a, **_k) -> bool:
        return True

    monkeypatch.setattr(whatsapp_web, "web_available", web_available)
    sends: list[tuple[str, str]] = []

    async def fake_web_send(to, text):
        sends.append((to, text))
        return {"ok": True, "sent": True, "verified_in_thread": True, "to": to}

    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    pending = await latest_pending(db_session)
    assert pending is not None

    payload = dict(pending.payload)
    meta = pol_meta(payload)
    meta["route"] = {
        "channel": "messages",
        "provider": "macos_life",
        "mode": "send",
        "address": "phone",
    }
    payload["_pol"] = meta
    pending.payload = payload

    approved = await handle_send_approval(db_session, "yes", actor="voice")
    assert approved is not None
    assert approved.get("sent") is False
    assert sends == []


def test_question_names_the_recipient_and_the_exact_body(db_session) -> None:
    """The spoken question must identify who and what the owner is approving."""

    import asyncio

    from app.ev.messaging.approval import park_send

    async def _park():
        return await park_send(
            db_session,
            to="John Smith",
            text="running late",
            display="John Smith",
            actor="voice",
        )

    action = asyncio.get_event_loop().run_until_complete(_park())
    question = question_for(action)
    assert "John Smith" in question
    assert "running late" in question
    assert "WhatsApp" in question


def test_route_binding_round_trips_and_reports_conflicts() -> None:
    """A stored binding must survive storage and detect a changed route."""

    from app.ev.messaging.routing import RouteBinding, route_channel

    approved = RouteBinding.of(
        route_channel("whatsapp", helper_available=False, web_available=True)
    )
    restored = RouteBinding.from_payload(approved.as_payload())
    assert restored is not None
    assert restored.satisfies(
        route_channel("whatsapp", helper_available=True, web_available=True)
    ) is None
    # The same channel routed through the macOS helper is a different transport.
    assert restored.satisfies(
        route_channel("whatsapp", helper_available=True, web_available=False)
    ) == "provider"
    # A different channel is a different channel.
    assert restored.satisfies(
        route_channel("sms", helper_available=True, web_available=False)
    ) == "channel"
    assert RouteBinding.from_payload(None) is None
    assert RouteBinding.from_payload({"channel": "whatsapp"}) is None


def test_unavailable_route_wording_names_the_fix() -> None:
    from app.ev.messaging.routing import (
        RouteBinding,
        route_channel,
        route_unavailable_spoken,
    )

    binding = RouteBinding.of(
        route_channel("whatsapp", helper_available=False, web_available=True)
    )
    routing = route_channel("whatsapp", helper_available=True, web_available=False)
    spoken = route_unavailable_spoken(binding, routing)
    assert "WhatsApp Web" in spoken
    assert "Chrome" in spoken
