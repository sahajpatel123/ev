"""Personal-helper path: do-this asks hit dispatch; Evie wake is not chat-only."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.contracts import ChatMessage, ChatResult, RequestEnvelope
from app.ev.briefing import infer_write_args, plan_life_tool_calls
from app.ev.interaction import detect_intent, detect_life_action
from app.ev.tool_select import select_tool
from app.ev.tools import dispatch, get_spec, life_success_reply
from app.gateway.service import ModelGateway, tool_specs_from_dicts
from app.gateway.validation import validate_arguments, validate_tool_calls
from app.models import AccessLog
from app.services.tool_loop import planned_calls_for, run_tool_loop
from tests.test_life_agency import _life_spec
from tests.test_voice_lifecycle import enroll_owner, grant_voice_consent


def test_select_tool_treats_remind_send_show_as_actions() -> None:
    remind = select_tool("remind me to drink water")
    send = select_tool("text Mom I'm late")
    show = select_tool("show that on my screen")
    assert remind.selected == "set_reminder"
    assert send.selected == "send_message"
    assert show.selected == "present"
    mixed = select_tool("I'm tired, remind me to drink water")
    assert mixed.selected == "set_reminder"
    evie = select_tool("Evie remind me to call mom")
    assert evie.selected == "set_reminder"


def test_detect_life_action_hits_helper_phrases() -> None:
    assert detect_life_action("remind me to drink water") == "reminder"
    assert detect_life_action("text Mom I'm late") == "send_message"
    assert detect_life_action("send Mom a message") == "send_message"
    assert detect_life_action("I'm exhausted, remind me to drink water") == "reminder"
    assert detect_life_action("what's the weather tomorrow") is None
    assert detect_intent("I'm tired, remind me to drink water") == "life_action"


def test_plan_life_tool_calls_for_show_and_remind() -> None:
    remind = plan_life_tool_calls(
        "remind me to drink water", {"set_reminder", "present"}
    )
    assert any(call.name == "set_reminder" for call in remind)
    show = plan_life_tool_calls("show that on my screen", {"present", "set_reminder"})
    assert any(call.name == "present" for call in show)
    args = infer_write_args("set_reminder", "remind me to drink water")
    assert args is not None
    assert "drink" in args["text"].lower() or "water" in args["text"].lower()


@pytest.mark.asyncio
async def test_dispatch_helper_asks_return_action_outcomes(db_session) -> None:
    """Shipped dispatcher: execution-shaped result, not a chat essay."""

    remind = await dispatch(
        db_session, "set_reminder", {"text": "drink water"}, actor="owner"
    )
    assert remind.ok is True
    assert remind.result is not None
    assert "ok" in remind.result or remind.result.get("degraded") is True or remind.result.get("next_step")
    show = await dispatch(
        db_session,
        "present",
        {"title": "EVIE", "body": "show that on my screen"},
        actor="owner",
    )
    assert show.error is None or show.result is not None or show.ok is False
    # present is an action outcome: ok, denied-with-reason, or confirmed attempt
    assert show.name == "present"
    assert isinstance(show.ok, bool)


@pytest.mark.asyncio
async def test_ears_accepts_evie_and_rejects_non_evie(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    evie = await client.post(
        "/v1/ears/wake",
        json={"device_id": "mac-helper-evie", "consent": True, "text_hint": "hey evie"},
    )
    assert evie.status_code == 200, evie.text
    body = evie.json()
    assert body["accepted"] is True
    assert body["session_id"]
    assert body["listening"] is True or body["state"] in {"verifying", "awake", "follow_up"}

    other = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-helper-none",
            "consent": True,
            "text_hint": "what's the weather tomorrow",
        },
    )
    assert other.status_code == 200, other.text
    skipped = other.json()
    assert skipped["accepted"] is False
    assert skipped["listening"] is False
    assert skipped.get("state") in {None, "idle"} or "wake word" in (
        skipped.get("message") or ""
    ).lower()


def test_planned_set_reminder_does_not_inject_null_when() -> None:
    """Optional when=None must not become 'must be string' on the loop path."""

    spec = get_spec("set_reminder")
    assert spec is not None
    effective, issues = validate_arguments({"text": "drink water"}, spec["parameters"])
    assert issues == []
    assert "when" not in effective
    converted = tool_specs_from_dicts([spec])

    planned = plan_life_tool_calls("remind me to drink water", {"set_reminder"})
    assert planned
    validated = validate_tool_calls(planned, converted, sensitive_allowed=True)
    assert validated
    assert validated[0].status in {"ok", "rectified"}
    args = validated[0].rectified_arguments or validated[0].call.arguments
    assert args.get("text")
    assert args.get("when") is None or "when" not in args


def test_life_success_reply_uses_reminder_not_send_copy() -> None:
    degraded = {
        "ok": False,
        "degraded": True,
        "next_step": "install the reminders integration and grant scope 'reminders:act'",
        "reason": "no reminders bridge is installed",
    }
    reply = life_success_reply(degraded, tool_name="set_reminder")
    assert "sent to the recipient" not in reply.lower()
    assert "reminder" in reply.lower()
    assert "reminders" in reply.lower() or "couldn't" in reply.lower()
    opened = life_success_reply({"opened": True, "surface": "overlay"}, tool_name="present")
    assert "sent to the recipient" not in opened.lower()
    assert "screen" in opened.lower() or "opened" in opened.lower()


class _EssayOnlyProvider:
    """Model describes the action and never calls a tool — the loop must plan."""

    name = "essay-only"
    supports_media = False

    async def chat(self, messages, *, model=None, temperature=0.7):
        return ChatResult(text="Sure, I can help with that.", usage={}, model=model or self.name)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
        return ChatResult(
            text="Sure, I can help with that.",
            tool_calls=[],
            usage={},
            model=model or self.name,
        )

    async def list_models(self):
        return [self.name]


@pytest.mark.asyncio
async def test_mixed_talk_helper_turn_dispatches_set_reminder(db_session) -> None:
    """Tired + remind must run set_reminder on the shipped tool loop, not echo chat."""

    spoken = "I'm tired, remind me to drink water"
    assert detect_life_action(spoken) == "reminder"
    assert select_tool(spoken).selected == "set_reminder"
    planned = planned_calls_for(
        [ChatMessage(role="user", content=spoken)],
        tool_specs_from_dicts([_life_spec("set_reminder")]),
        sensitive_allowed=True,
    )
    assert planned
    assert planned[0].call.name == "set_reminder"
    assert planned[0].status in {"ok", "rectified"}

    gateway = ModelGateway(_EssayOnlyProvider())
    call = await run_tool_loop(
        db_session,
        gateway,
        [ChatMessage(role="user", content=spoken)],
        envelope=RequestEnvelope(request_id="helper-mix", strategy={}),
        tool_specs=tool_specs_from_dicts([_life_spec("set_reminder")]),
        actor="owner",
        allow_sensitive_tools=True,
    )
    logged = (
        await db_session.execute(select(AccessLog).where(AccessLog.action == "tool_call"))
    ).scalars().all()
    assert any(
        row.resource_ids and row.resource_ids[0] == "set_reminder" for row in logged
    ), f"set_reminder never dispatched; logs={[row.resource_ids for row in logged]}"
    text = (call.result.text or "").lower()
    assert "sent to the recipient" not in text
    assert "reminder" in text or "remind" in text or "couldn't" in text
