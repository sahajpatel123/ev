from __future__ import annotations

import pytest

from app.ev.personality import identity_block
from app.ev.protocols import capability_reply, spoken_operator_sheet
from app.voice.live.grok_voice import (
    capability_instructions,
    grok_voice_instructions,
    openai_realtime_instructions,
)


def _manifest() -> dict:
    ready = [
        "get_weather",
        "start_timer",
        "search_memory",
        "present",
        "calibrate",
    ]
    projection = [
        {
            "name": name,
            "availability": "available",
            "model_exposed": True,
            "realtime_eligible": True,
            "executable": True,
        }
        for name in ready
    ]
    return {
        "live_tool_projection": projection,
        "capabilities": [
            *projection,
            {"name": "calendar_read", "availability": "not_connected"},
            {"name": "list_messages", "availability": "not_connected"},
            {
                "name": "place_call",
                "availability": "available",
                "risk_class": "R3",
                "confirmation_required": True,
            },
            {
                "name": "home_act",
                "availability": "available",
                "risk_class": "R3",
                "confirmation_required": True,
            },
        ],
    }


def test_operator_sheet_is_short_partner_speech_and_projection_driven() -> None:
    sheet = spoken_operator_sheet(_manifest())

    assert "I can do now: weather, timers, memory, HUD, diagnostics." in sheet
    assert "Needs a connection: calendar (Google), messages (life helper)." in sheet
    assert "Needs a tap on your phone: calls, home actions." in sheet
    assert "I will not do:" not in sheet
    for function_name in (
        "get_weather",
        "start_timer",
        "search_memory",
        "calendar_read",
        "place_call",
        "home_act",
    ):
        assert function_name not in sheet


def test_operator_sheet_mentions_refusals_only_when_requested() -> None:
    sheet = spoken_operator_sheet(_manifest(), include_refused=True)

    assert "I will not do: Instant Kill, telecom wiretaps" in sheet


def test_empty_live_projection_does_not_widen_from_stale_tools() -> None:
    sheet = spoken_operator_sheet(
        {
            "live_tool_projection": [],
            "tools": [{"type": "function", "name": "get_weather"}],
        }
    )

    assert sheet == "I can do now: nothing is verified yet."


def test_live_instructions_replace_manifest_dump_and_keep_action_guards() -> None:
    manifest = _manifest()
    manifest["capability_error"] = "RuntimeError: registry probe failed"
    instructions = capability_instructions(manifest)

    assert "CURRENT LIVE OPERATOR SHEET" in instructions
    assert "CURRENT LIVE CAPABILITY MANIFEST" not in instructions
    assert "I can do now: weather, timers, memory, HUD, diagnostics." in instructions
    assert "Live capability projection error: RuntimeError: registry probe failed" in instructions
    assert "get_weather" not in instructions
    assert "never claim an action completed" in instructions.lower()


def test_identity_and_live_prompts_do_not_claim_static_capabilities() -> None:
    identity = identity_block("EVIE", "the owner's personal AI")
    grok = grok_voice_instructions(capability_manifest=_manifest())
    openai = openai_realtime_instructions(capability_manifest=_manifest())

    assert "the live operator sheet is the only source" in identity.lower()
    assert "calendar/leave-by" not in identity
    assert "place calls" not in identity
    for prompt in (grok, openai):
        assert "I can do now: weather, timers, memory, HUD, diagnostics." in prompt
        assert "calendar (Google)" not in prompt
        assert "place_call" not in prompt
    for prompt in (grok, openai):
        assert "prefer action over essay" in prompt.lower()
        assert "never invent" in prompt.lower()
        assert "raw function ids" in prompt.lower() or "function ids" in prompt.lower()
        assert "timer" in prompt.lower()


@pytest.mark.asyncio
async def test_real_capability_reply_speaks_only_live_projection_labels(db_session) -> None:
    payload = await capability_reply(
        db_session,
        actor="master",
        realtime_provider="openai",
        session_id="operator-sheet-test",
    )
    reply = payload["reply"]
    projection_names = {
        str(item["name"])
        for item in payload["live_tool_projection"]
        if isinstance(item, dict) and item.get("name")
    }

    from app.ev.protocols import _SPOKEN_CAPABILITY_LABELS

    assert {"get_weather", "start_timer", "calibrate"} <= projection_names
    assert "I can do now: weather" in reply
    assert "timers" in reply
    assert "diagnostics" in reply
    for function_name in projection_names:
        label = _SPOKEN_CAPABILITY_LABELS.get(function_name, "")
        if function_name and function_name in label:
            continue
        assert function_name not in reply
