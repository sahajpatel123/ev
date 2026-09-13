"""General in-app item actions: parse, route, and drive one logic for all apps.

The P0 gap: "open John's chat in WhatsApp" was routed to a messaging *read*
and answered "bridge unavailable". These tests pin the generalized system:
normalized intent, router priority, driver order, background behavior, and
honest fallbacks — without opening anything or sending any message.
"""

from __future__ import annotations

import pytest

from app.ev.in_app import InAppIntent, act_in_app, parse_in_app_intent


def test_parse_in_app_intent_normalizes_many_phrasings() -> None:
    expected = InAppIntent(verb="open", app="WhatsApp", item="John", kind="chat", background=False)
    for phrase in (
        "open John's chat in WhatsApp",
        "open chat with John on WhatsApp",
        "open John chat in whatsapp",
        "show me John's chat on WhatsApp",
        "Evie, can you open the chat with John in WhatsApp",
        "open John's chat in WhatsApp and then bring it up",
    ):
        parsed = parse_in_app_intent(phrase)
        assert parsed is not None, phrase
        assert parsed.app == expected.app, phrase
        assert parsed.item == expected.item, phrase
        assert parsed.kind == expected.kind, phrase


def test_parse_in_app_intent_kinds_and_apps() -> None:
    music = parse_in_app_intent("play Bohemian Rhapsody in Music")
    assert music is not None and music.verb == "play"
    assert music.app == "Music" and music.item == "Bohemian Rhapsody"
    assert music.kind == "track"

    playlist = parse_in_app_intent("play my playlist Roadtrip in Music")
    assert playlist is not None and playlist.kind == "playlist" and playlist.item == "Roadtrip"

    note = parse_in_app_intent("show me the note Groceries in Notes")
    assert note is not None and note.app == "Notes" and note.item == "Groceries"
    assert note.kind == "note"

    folder = parse_in_app_intent("open my Downloads folder in Finder")
    assert folder is not None and folder.app == "Finder" and folder.item == "Downloads"
    assert folder.kind == "folder"

    thread = parse_in_app_intent("open the chat with Mansi in Messages")
    assert thread is not None and thread.app == "Messages" and thread.item == "Mansi"

    bare_chat = parse_in_app_intent("open John's chat")
    assert bare_chat is not None and bare_chat.app is None and bare_chat.item == "John"

    background = parse_in_app_intent("open John's chat in WhatsApp in the background")
    assert background is not None and background.background is True


def test_parse_in_app_intent_leaves_other_pipelines_alone() -> None:
    for phrase in (
        "text John I'm late",
        "send a WhatsApp message to John saying hi",
        "reply to John saying ok",
        "open WhatsApp",
        "close WhatsApp",
        "what did John say on WhatsApp",
        "did John message me",
        "any new whatsapp messages",
        "open the file report.pdf",
        "open my Downloads folder",
        "tell me about John",
    ):
        assert parse_in_app_intent(phrase) is None, phrase


def test_router_prefers_open_in_app_for_item_actions() -> None:
    from app.ev.briefing import _prefetch_names, plan_life_tool_calls
    from app.ev.tool_select import resolve_live_action, select_tool

    phrase = "open John's chat in WhatsApp"
    assert select_tool(phrase).selected == "open_in_app"
    live = resolve_live_action(phrase)
    assert live is not None and live[0] == "open_in_app"
    assert live[1]["app"] == "WhatsApp" and live[1]["item"] == "John"
    planned = plan_life_tool_calls(
        phrase, {"open_in_app", "send_message", "resolve_contact"}
    )
    assert [call.name for call in planned] == ["open_in_app"]
    assert planned[0].arguments["item"] == "John"
    assert _prefetch_names(phrase) == []

    # Sends and reads keep their existing routing.
    assert select_tool("send a WhatsApp message to John saying hi").selected == "send_message"
    assert select_tool("what did John say on WhatsApp").selected == "recall_history"
    assert select_tool("open WhatsApp").selected == "open_app"


@pytest.mark.asyncio
async def test_act_in_app_uses_background_web_driver(db_session, monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_web(app_label, item, *, kind=None, background=True):
        calls.append(
            {"app": app_label, "item": item, "kind": kind, "background": background}
        )
        return {
            "ok": True,
            "matched": True,
            "driver": "web_tab",
            "title": item,
            "focused": False,
            "focus_theft": 0,
        }

    monkeypatch.setattr("app.ev.in_app_web.drive_item_in_web", fake_web)
    result = await act_in_app(
        db_session,
        app="WhatsApp",
        item="John",
        kind="chat",
        background=True,
        actor="master",
    )
    assert result["ok"] is True
    assert result["driver"] == "web_tab"
    assert result["executed"] is True and result["verified"] is True
    assert calls == [
        {"app": "WhatsApp", "item": "John", "kind": "chat", "background": True}
    ]
    assert "John" in result["spoken"]


@pytest.mark.asyncio
async def test_act_in_app_guesses_whatsapp_for_bare_chat(db_session, monkeypatch) -> None:
    async def native_unique(channel, to):
        assert channel == "whatsapp" and to == "John"
        return {"status": "unique", "id": "john", "display": "John", "source": "whatsapp_web"}

    async def fake_web(app_label, item, *, kind=None, background=True):
        return {"ok": True, "matched": True, "driver": "web_tab", "focus_theft": 0}

    monkeypatch.setattr(
        "app.ev.messaging.native.resolve_native_contact", native_unique
    )
    monkeypatch.setattr("app.ev.in_app_web.drive_item_in_web", fake_web)
    result = await act_in_app(db_session, app=None, item="John", kind="chat")
    assert result["ok"] is True
    assert result["app"] == "WhatsApp"


@pytest.mark.asyncio
async def test_act_in_app_asks_when_app_is_unknown(db_session, monkeypatch) -> None:
    async def none_native(channel, to):
        return None

    monkeypatch.setattr("app.ev.messaging.native.resolve_native_contact", none_native)
    result = await act_in_app(db_session, app=None, item="Nobody", kind="chat")
    assert result["ok"] is False
    assert result.get("needs_app") is True
    assert "WhatsApp" in result["spoken"] and "Messages" in result["spoken"]


@pytest.mark.asyncio
async def test_dispatch_open_in_app_returns_evidence(db_session, monkeypatch) -> None:
    from app.ev.policy import PolicyDecision
    from app.ev.tools import dispatch

    async def fake_web(app_label, item, *, kind=None, background=True):
        return {
            "ok": True,
            "matched": True,
            "driver": "web_tab",
            "title": item,
            "focused": False,
            "focus_theft": 0,
        }

    async def fake_authorize(*_args, **_kwargs):
        return PolicyDecision(
            allowed=True,
            effect="allow",
            reason="ok",
            risk_class="R1",
            confirmation_required=False,
            confirmation_policy="none",
            provider="macos_life",
            spoken="",
        )

    monkeypatch.setattr("app.ev.in_app_web.drive_item_in_web", fake_web)
    monkeypatch.setattr("app.ev.policy.authorize", fake_authorize)
    response = await dispatch(
        db_session,
        "open_in_app",
        {"app": "WhatsApp", "item": "John", "kind": "chat", "background": True},
        actor="master",
    )
    body = response.result or {}
    assert response.ok is True
    assert body.get("driver") == "web_tab"
    assert body.get("item") == "John"
    assert "John" in str(body.get("spoken") or "")
