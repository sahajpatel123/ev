"""WhatsApp Web autosend behind one spoken approval, in the background.

The owner keeps WhatsApp Web signed in in Chrome. With approval Evie drives
that tab through page JS only: no activation, no new window, no foreground
compose. Without approval she parks the exact prepared message and asks.
"""

from __future__ import annotations

import pytest

from app.ev.messaging import whatsapp_web
from app.ev.messaging.approval import (
    handle_send_approval,
    is_affirmative,
    is_negative,
    latest_pending,
    park_send,
    question_for,
)
from app.ev.messaging.routing import route_channel
from app.ev.messaging.whatsapp_web import score_chat


def test_affirmative_and_negative_are_short_and_unambiguous() -> None:
    for phrase in (
        "yes",
        "yeah",
        "yep",
        "yes please",
        "yes send it",
        "send it",
        "do it",
        "go ahead",
        "okay send the message",
        "sure",
        "sounds good",
        "please send it",
    ):
        assert is_affirmative(phrase), phrase
    for phrase in ("no", "nope", "cancel", "don't send it", "stop", "never mind"):
        assert is_negative(phrase), phrase
    # Not approvals: sentences that merely start with a yes-word.
    for phrase in (
        "yes but wait for the meeting",
        "yes I will call you later",
        "no idea what that was",
        "send me the file",
    ):
        assert not is_affirmative(phrase), phrase
        assert not is_negative(phrase), phrase


def test_web_route_requires_approval_and_never_degrades_to_sms() -> None:
    web = route_channel("whatsapp", helper_available=True, web_available=True)
    assert web.mode == "send"
    assert web.provider == "web"
    assert web.requires_approval is True
    # Without the tab, WhatsApp stays compose-only, still never SMS.
    compose = route_channel("whatsapp", helper_available=True)
    assert compose.mode == "compose"
    assert compose.channel == "whatsapp"
    # Web availability never upgrades another channel or an unknown one.
    assert route_channel("sms", helper_available=True, web_available=True).channel == "sms"
    assert route_channel("pigeon", helper_available=True, web_available=True).mode == "unavailable"


def test_chat_scoring_is_whole_name_and_number_aware() -> None:
    assert score_chat("John", {"name": "John Smith", "gist": ""}) > 0.8
    assert score_chat("John", {"name": "Johnson", "gist": ""}) == 0.0
    assert score_chat("John", {"name": "John Doe", "gist": ""}) > 0.8
    assert score_chat("+1 555 0100", {"name": "Ada", "gist": "+1 555 0100"}) == 1.0
    assert score_chat("+1 555 0100", {"name": "Ada", "gist": "lunch tomorrow"}) == 0.0


@pytest.mark.asyncio
async def test_chat_resolution_is_unique_or_clarify_not_first_hit() -> None:
    from app.ev.messaging.whatsapp_web import resolve

    rows = [
        {"name": "John Smith", "chat_ref": "John Smith"},
        {"name": "John Doe", "chat_ref": "John Doe"},
    ]
    exact = await resolve("John Smith", rows=rows)
    assert exact["status"] == "unique"
    assert exact["display"] == "John Smith"
    ambiguous = await resolve("John", rows=rows)
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidates"] == ["John Smith", "John Doe"]
    missing = await resolve("Nobody Here", rows=rows)
    assert missing["status"] == "none"


@pytest.mark.asyncio
async def test_park_question_names_recipient_and_exact_text(db_session) -> None:
    action = await park_send(
        db_session,
        to="John Smith",
        text="running late",
        display="John Smith",
        actor="voice",
    )
    question = question_for(action)
    assert "John Smith" in question
    assert "running late" in question
    assert "WhatsApp" in question
    found = await latest_pending(db_session)
    assert found is not None and found.id == action.id


async def _fake_web_available(*_args, **_kwargs) -> bool:
    return True


async def _fake_destination(*_args, **_kwargs) -> dict:
    return {"status": "unique", "handle": "John Smith", "phone": "", "email": ""}


async def _fake_chat(to, **_kwargs) -> dict:
    return {"status": "unique", "display": str(to or "John Smith")}


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
    monkeypatch.setattr(whatsapp_web, "web_available", _fake_web_available)
    monkeypatch.setattr(whatsapp_web, "resolve", _fake_chat)


@pytest.mark.asyncio
async def test_dispatch_parks_whatsapp_web_instead_of_sending(db_session, monkeypatch) -> None:
    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    seen: list[str] = []

    async def no_helper(command, args, helper_path=None):
        seen.append(command)
        raise AssertionError(f"no transport may run before approval: {command}")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", no_helper)

    response = await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    body = response.result or {}
    assert body.get("pending_approval") is True
    assert body.get("requires_approval") is True
    assert body.get("sent") is not True
    assert "John Smith" in str(body.get("spoken") or "")
    assert "running late" in str(body.get("spoken") or "")
    assert seen == []
    pending = await latest_pending(db_session)
    assert pending is not None


@pytest.mark.asyncio
async def test_unknown_chat_is_not_parked_for_approval(db_session, monkeypatch) -> None:
    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)

    async def missing_chat(*_args, **_kwargs) -> dict:
        return {"status": "none", "display": "Nobody Here", "candidates": []}

    monkeypatch.setattr(whatsapp_web, "resolve", missing_chat)
    seen: list[str] = []

    async def no_helper(command, args, helper_path=None):
        seen.append(command)
        raise AssertionError(f"no transport may run before approval: {command}")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", no_helper)

    response = await dispatch(
        db_session,
        "send_message",
        {"to": "Nobody Here", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    body = response.result or {}
    assert body.get("pending_approval") is not True
    assert "Nobody Here" in str(body.get("spoken") or "")
    assert seen == []
    assert await latest_pending(db_session) is None


@pytest.mark.asyncio
async def test_yes_sends_via_web_in_background_and_no_stays_unsent(
    db_session, monkeypatch
) -> None:
    from app.ev.tools import dispatch

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("helper must not run")),
    )
    foreground: list[str] = []
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_compose",
        lambda *a, **k: foreground.append("compose") or False,
    )
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_app",
        lambda *a, **k: foreground.append("app") or False,
    )
    web_sends: list[tuple[str, str]] = []

    async def fake_web_send(to, text):
        web_sends.append((to, text))
        return {
            "ok": True,
            "sent": True,
            "verified_in_thread": True,
            "to": to,
            "focus_theft": 0,
            "spoken": f"Sent WhatsApp to {to}.",
        }

    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    approved = await handle_send_approval(db_session, "yes", actor="voice")
    assert approved is not None
    assert approved.get("sent") is True
    assert web_sends == [("John Smith", "running late")]
    assert foreground == []
    after = await latest_pending(db_session)
    assert after is None

    # A second park + "no" cancels and sends nothing more.
    await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "one more thing", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    denied = await handle_send_approval(db_session, "no", actor="voice")
    assert denied is not None
    assert denied.get("cancelled") is True
    assert web_sends == [("John Smith", "running late")]


@pytest.mark.asyncio
async def test_digital_act_whatsapp_write_parks_instead_of_autosend(
    db_session, monkeypatch
) -> None:
    from app.digital.tools import handle_digital_tool

    async def fake_web_available(*_args, **_kwargs) -> bool:
        return True

    async def fake_resolve(to, **_kwargs) -> dict:
        return {"status": "unique", "display": "John Smith"}

    monkeypatch.setattr(whatsapp_web, "web_available", fake_web_available)
    monkeypatch.setattr(whatsapp_web, "resolve", fake_resolve)

    result = await handle_digital_tool(
        db_session,
        "digital_act",
        {
            "service": "whatsapp",
            "operation": "send",
            "confirmed": True,
            "args": {"chat_ref": "John Smith", "text": "running late"},
        },
        actor="master",
    )
    assert result is not None
    assert result.get("pending_approval") is True
    assert result.get("error") == "confirmation_required"
    assert "John Smith" in str(result.get("spoken") or "")
    assert await latest_pending(db_session) is not None


@pytest.mark.asyncio
async def test_park_ignores_apple_contacts_for_whatsapp(db_session, monkeypatch) -> None:
    """Apple Contacts ambiguity/absence must not gate a WhatsApp send.

    The person is proven by the WhatsApp chat list; Contacts is not the
    address book of WhatsApp.
    """

    from app.ev.tools import dispatch
    from app.integrations.life_helper import AmbiguousRecipientError

    _allow_policy(monkeypatch)

    async def contacts_ambiguous(*_args, **_kwargs):
        raise AmbiguousRecipientError(
            "X Y", ["X Y One · +1", "X Y Two · +2"]
        )

    monkeypatch.setattr("app.ev.tools._resolve_send_destination", contacts_ambiguous)
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("helper must not run")),
    )

    response = await dispatch(
        db_session,
        "send_message",
        {"to": "X Y", "text": "hello there", "channel": "whatsapp"},
        actor="voice",
        allow_sensitive=True,
    )
    body = response.result or {}
    assert body.get("pending_approval") is True
    assert body.get("requires_approval") is True
    assert "X Y" in str(body.get("spoken") or "")
    pending = await latest_pending(db_session)
    assert pending is not None


@pytest.mark.asyncio
async def test_adapter_sends_whatsapp_recipient_absent_from_contacts(
    monkeypatch,
) -> None:
    """The integrations path must not require Apple Contacts for WhatsApp."""

    import app.integrations.adapters as adapters
    from app.integrations.adapters import BUILTIN_ADAPTERS, MessagingAdapter

    adapter = next(
        item for item in BUILTIN_ADAPTERS if isinstance(item, MessagingAdapter)
    )

    async def native_unique(channel, to):
        assert channel == "whatsapp"
        return {"status": "unique", "id": "chat-x", "display": "X Y", "phone": "", "source": "whatsapp_web"}

    async def not_in_contacts(*_args, **_kwargs):
        return None

    async def fake_web_send(to, text):
        return {
            "ok": True,
            "sent": True,
            "verified_in_thread": True,
            "to": to,
            "focus_theft": 0,
            "spoken": f"Sent WhatsApp to {to}.",
        }

    monkeypatch.setattr("app.ev.messaging.native.resolve_native_contact", native_unique)
    monkeypatch.setattr(adapters, "_resolve_life_contact", not_in_contacts)
    monkeypatch.setattr(whatsapp_web, "web_available", _fake_web_available)
    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    result = await adapter._macos_life_act(
        "messaging.send",
        {"to": "X Y", "text": "hello there", "channel": "whatsapp"},
        ["messaging:act"],
        {"provider": "macos_life", "contact_allowlist": "all", "life_confirm_unknown": True},
    )
    assert result.get("ok") is True
    assert result.get("mode") == "whatsapp_web"
    assert result.get("sent") is True
    assert result.get("policy", {}).get("allowed") is True


@pytest.mark.asyncio
async def test_desktop_only_chat_sends_by_phone_in_background(monkeypatch) -> None:
    """A chat present in WhatsApp Desktop but not rendered by the tab is
    still addressable by its phone number, with no foreground window."""

    class FakeBacking:
        def __init__(self) -> None:
            self.opened: list[str] = []

        async def open_chat(self, chat_ref: str) -> dict:
            self.opened.append(chat_ref)
            return {"chat_ref": chat_ref}

        async def send(self, chat_ref: str, text: str) -> dict:
            return {
                "sent": True,
                "verified_in_thread": True,
                "chat_ref": chat_ref,
                "focus_theft": 0,
            }

        async def read_recent(self, chat_ref: str, *, limit: int) -> list:
            return []

    monkeypatch.setattr(
        "app.digital.adapters.whatsapp.ComputerWhatsAppBacking", FakeBacking
    )
    monkeypatch.setattr(whatsapp_web, "web_available", _fake_web_available)

    async def desktop_only(to, **_kwargs):
        return {
            "status": "desktop_only",
            "display": "X Y",
            "peer": {"phone": "+15551234567"},
        }

    monkeypatch.setattr(whatsapp_web, "resolve", desktop_only)

    result = await whatsapp_web.send("X Y", "hello there")
    assert result["ok"] is True
    assert result["sent"] is True
    assert result.get("focus_theft", 0) == 0


@pytest.mark.asyncio
async def test_expired_approval_is_not_resumed(db_session) -> None:
    from app.utils.text import utcnow

    action = await park_send(
        db_session,
        to="John Smith",
        text="running late",
        display="John Smith",
        actor="voice",
    )
    meta = dict(action.payload["_pol"])
    meta["expires_at"] = (utcnow().replace(year=2020)).isoformat()
    action.payload = {**action.payload, "_pol": meta}
    await db_session.flush()
    assert await latest_pending(db_session) is None
    assert action.status == "denied"
    assert action.denied_reason == "confirmation_expired"


async def _make_trusted_phone(db_session, name: str = "iPhone"):
    from app.models import Device
    from app.utils.text import utcnow

    device = Device(
        name=name,
        token_hash=f"hash-{name}",
        trust_level="owner",
        device_type="phone",
        platform="ios",
        role="primary_companion",
        capabilities=["text"],
        paired_at=utcnow(),
    )
    db_session.add(device)
    await db_session.flush()
    return device


@pytest.mark.asyncio
async def test_device_turn_parks_send_asks_then_yes_sends_via_existing_tab(
    db_session, monkeypatch
) -> None:
    """iPhone realtime path: send parks + asks, "yes" auto-sends via the tab.

    The approved send must reuse the already-open WhatsApp Web Chrome tab
    (background JS) and never open a new compose window.
    """

    from app.device_gateway.pipeline import run_trusted_device_turn

    _allow_policy(monkeypatch)
    monkeypatch.setattr("app.ev.tools._resolve_send_destination", _fake_destination)
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("helper must not run")),
    )
    foreground: list[str] = []
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_compose",
        lambda *a, **k: foreground.append("compose") or False,
    )
    monkeypatch.setattr(
        "app.ev.tools._open_whatsapp_app",
        lambda *a, **k: foreground.append("app") or False,
    )
    web_sends: list[tuple[str, str]] = []

    async def fake_web_send(to, text):
        web_sends.append((to, text))
        return {
            "ok": True,
            "sent": True,
            "verified_in_thread": True,
            "to": to,
            "focus_theft": 0,
            "spoken": f"Sent WhatsApp to {to}.",
        }

    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    phone = await _make_trusted_phone(db_session)
    await db_session.commit()

    parked = await run_trusted_device_turn(
        db_session,
        device=phone,
        text="Send John Smith a WhatsApp saying running late",
    )
    assert parked.get("operation") == "send_message"
    assert parked.get("pending_approval") is True
    assert "John Smith" in str(parked.get("reply") or "")
    assert "running late" in str(parked.get("reply") or "")
    assert web_sends == []
    assert foreground == []

    approved = await run_trusted_device_turn(
        db_session, device=phone, text="yes"
    )
    assert approved.get("operation") == "send_message"
    assert approved.get("ok") is True
    assert web_sends == [("John Smith", "running late")]
    assert foreground == []
    assert "Sent WhatsApp to John Smith" in str(approved.get("reply") or "")

    # Second park + "no" cancels without sending.
    await run_trusted_device_turn(
        db_session,
        device=phone,
        text="Send John Smith a WhatsApp saying one more thing",
    )
    denied = await run_trusted_device_turn(db_session, device=phone, text="no")
    assert denied.get("ok") is True
    assert web_sends == [("John Smith", "running late")]


@pytest.mark.asyncio
async def test_device_turn_without_send_falls_through_to_existing_pipeline(
    db_session,
) -> None:
    from app.device_gateway.pipeline import run_trusted_device_turn

    phone = await _make_trusted_phone(db_session, name="iPhone Fallthrough")
    await db_session.commit()

    stopped = await run_trusted_device_turn(db_session, device=phone, text="stop")
    assert stopped.get("route") == "STOP"
    assert stopped.get("operation") != "send_message"
