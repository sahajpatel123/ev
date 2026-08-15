"""Execute-then-word turn: working-on context and real action dispatch."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.api import core as core_api
from app.config import settings
from app.contracts import ChatProvider, ChatResult
from app.ev.briefing import extract_reminder_when, infer_write_args
from app.ev.turn import (
    ActionReceipt,
    confirmed_reply,
    execute_requested_actions,
    snapshot_working_on,
)
from app.integrations import service as integrations
from app.models import Alert, OwnerTimer
from tests.test_life_agency import _add_bridge


def test_snapshot_working_on_always_names_this_request() -> None:
    block = snapshot_working_on(
        "what's the weather in Surat?",
        user_state=SimpleNamespace(
            current_task="wrist unit",
            active_project="EV visor",
            active_goal=None,
            activity=None,
            recent_topics=["HUD"],
        ),
        conv_state=SimpleNamespace(
            focus="folio redesign",
            pending_questions=["which layout?"],
            working_context={"last_user_message": "older topic"},
        ),
        continuation=False,
    )
    assert "WORKING ON" in block
    assert "what's the weather in Surat?" in block
    assert "wrist unit" in block
    assert "EV visor" in block
    assert "self-contained" in block


def test_confirmed_reply_replaces_promised_action() -> None:
    receipts = [
        ActionReceipt(
            name="send_message",
            ok=True,
            result={
                "ok": True,
                "delivery": {
                    "confirmed": True,
                    "evidence": {
                        "recipient": "Mom",
                        "channel": "Messages",
                        "sent_at": "2026-08-15T10:00:00Z",
                    },
                },
            },
        )
    ]
    reply = confirmed_reply("I'll send a text to Mom saying I'm late.", receipts)
    assert "i'll send a text" not in reply.lower()
    assert "mom" in reply.lower()
    assert confirmed_reply("Sent to Mom via Messages.", receipts) == "Sent to Mom via Messages."


def test_extract_reminder_when() -> None:
    assert extract_reminder_when("remind me in 10 minutes to drink water") == "in 10 minutes"
    assert extract_reminder_when("remind me at 5pm to call mom") == "5pm"
    assert extract_reminder_when("remind me to drink water") is None
    args = infer_write_args("set_reminder", "remind me in 20 minutes to stretch")
    assert args is not None
    assert args["when"] == "in 20 minutes"


async def test_execute_requested_actions_sends_text(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="contacts", adapter="contacts", scopes=["contacts:read"])
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])
    calls: list[tuple[str, dict]] = []

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        calls.append((action, dict(args)))
        if action == "contacts.resolve":
            return SimpleNamespace(
                result={"ok": True, "contact": {"name": "Mom", "phone": "+15551234567"}}
            )
        if action == "messaging.send":
            return SimpleNamespace(
                result={
                    "ok": True,
                    "delivery": {
                        "confirmed": True,
                        "evidence": {
                            "recipient": args.get("to") or "Mom",
                            "channel": "Messages",
                            "sent_at": "2026-08-15T10:00:00Z",
                        },
                    },
                }
            )
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)
    receipts = await execute_requested_actions(
        db_session,
        "text Mom I'm late",
        actor="owner",
        allow_sensitive=True,
        request_id="turn-send",
    )
    names = [item.name for item in receipts]
    assert "send_message" in names
    send_calls = [args for action, args in calls if action == "messaging.send"]
    assert send_calls
    assert send_calls[0].get("to") == "Mom"
    assert "late" in str(send_calls[0].get("text") or "").lower()


async def test_execute_requested_actions_stores_reminder(db_session) -> None:
    receipts = await execute_requested_actions(
        db_session,
        "remind me to drink water",
        actor="owner",
        allow_sensitive=True,
        request_id="turn-remind",
    )
    assert any(item.name == "set_reminder" and item.ok for item in receipts)
    alerts = (
        await db_session.execute(select(Alert).where(Alert.kind == "reminder"))
    ).scalars().all()
    assert alerts
    assert "water" in (alerts[0].body or "").lower()


async def test_execute_requested_actions_starts_timed_reminder(db_session) -> None:
    receipts = await execute_requested_actions(
        db_session,
        "remind me in 10 minutes to stretch",
        actor="owner",
        allow_sensitive=True,
        request_id="turn-timer",
    )
    assert any(item.name == "set_reminder" and item.ok for item in receipts)
    timers = (await db_session.execute(select(OwnerTimer))).scalars().all()
    assert timers
    assert "stretch" in str(timers[0].payload.get("text") or "").lower()


async def test_chat_prompt_includes_working_on_and_actions(client, monkeypatch) -> None:
    captured: dict[str, str] = {}

    class CaptureProvider(ChatProvider):
        name = "capture"
        supports_media = False
        supports_tools = False

        async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
            captured["system"] = next(m.content for m in messages if m.role == "system")
            captured["user"] = next(
                (m.content for m in reversed(messages) if m.role == "user"),
                "",
            )
            return ChatResult(text="Noted.", usage={}, model=model or self.name)

        async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
            return await self.chat(messages, model=model, temperature=temperature)

        async def list_models(self) -> list[str]:
            return [self.name]

    monkeypatch.setattr(core_api, "get_chat_provider", lambda: CaptureProvider())
    resp = await client.post("/v1/chat", json={"message": "what's 2 plus 2?"})
    assert resp.status_code == 200, resp.text
    system = captured["system"]
    assert "WORKING ON" in system
    assert "2 plus 2" in system or "2 plus 2" in captured["user"]
    assert "ACTIONS THIS TURN" in system
    assert "already executed" in system.lower() or "none requested" in system.lower()


async def test_chat_text_mom_confirms_after_opencode_prose(
    client, db_session, monkeypatch
) -> None:
    from tests.test_command_loop import OpenCodeShapedProvider

    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])
    calls: list[tuple[str, dict]] = []

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        calls.append((action, dict(args)))
        return SimpleNamespace(
            result={
                "ok": True,
                "delivery": {
                    "confirmed": True,
                    "evidence": {
                        "recipient": "Mom",
                        "channel": "Messages",
                        "sent_at": "2026-08-15T10:00:00Z",
                    },
                },
            }
        )

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: OpenCodeShapedProvider())
    resp = await client.post("/v1/chat", json={"message": "text Mom I'm late"})
    assert resp.status_code == 200, resp.text
    send_calls = [args for action, args in calls if action == "messaging.send"]
    assert send_calls
    reply = resp.json()["reply"].lower()
    assert "i'll send a text" not in reply
    assert "sent" in reply or "mom" in reply
