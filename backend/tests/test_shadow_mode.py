"""Shadow / autonomous live-voice modes (EV VOICE CONTROL PLAN §5–6).

Offline-safe: tests the mode surface filtering, session payload shape, and
the shadow-memory block builder. No websocket and no model ever connects.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.tool_select import LIVE_VOICE_TOOLS, SHADOW_VOICE_TOOLS
from app.memory.history import build_shadow_memory
from app.voice.live.grok_voice import GrokVoiceBridge, grok_session_update, grok_voice_tools


def _specs(*names: str) -> list[dict]:
    return [
        {
            "name": name,
            "description": f"{name} test spec",
            "parameters": {"type": "object", "properties": {}},
        }
        for name in names
    ]


@pytest.fixture(autouse=True)
def _voice_mode_flag(monkeypatch):
    previous = settings.voice_live_mode
    yield
    monkeypatch.setattr(settings, "voice_live_mode", previous)


def test_supervised_surface_includes_new_tools() -> None:
    tools = grok_voice_tools(
        _specs("search_memory", "read", "click", "recall_history", "inspect_ui")
    )
    names = {t["name"] for t in tools}
    assert {"read", "click", "recall_history", "search_memory", "inspect_ui"} <= names


def test_f4_on_with_shadow_mode_advertises_computer_broker() -> None:
    """F4 + shadow must not intersect to an empty live surface."""
    previous_surface = settings.model_surface_v2
    previous_mode = settings.voice_live_mode
    settings.model_surface_v2 = "on"
    settings.voice_live_mode = "shadow"
    try:
        tools = grok_voice_tools(
            _specs(
                "computer",
                "look",
                "recall",
                "observe_camera",
                "capture_photo",
                "record_video",
                "read",
                "app_action",
                "inspect_ui",
            ),
            mode="shadow",
        )
        names = {t["name"] for t in tools}
        assert {"computer", "look", "recall", "observe_camera", "capture_photo", "record_video"} <= names
        assert "inspect_ui" not in names
        assert "read" not in names
        assert "app_action" not in names
    finally:
        settings.model_surface_v2 = previous_surface
        settings.voice_live_mode = previous_mode


def test_shadow_surface_is_curated() -> None:
    tools = grok_voice_tools(
        _specs("search_memory", "read", "click", "recall_history", "inspect_ui", "app_action"),
        mode="shadow",
    )
    names = {t["name"] for t in tools}
    assert {"read", "click", "recall_history"} <= names
    assert "search_memory" not in names  # replaced by injected history
    assert "inspect_ui" not in names  # replaced by read/see
    assert "app_action" in names
    assert names <= SHADOW_VOICE_TOOLS


def test_shadow_still_honors_unadvertised_search_memory(monkeypatch) -> None:
    """Instructions still tell the model to call search_memory; do not reject it."""
    from app.ev.tools import get_spec

    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    bridge, _ws = _shadow_bridge(monkeypatch)
    bridge._upstream_session_ready = True
    bridge._upstream_tool_names = ("recall_history", "read")
    spec = get_spec("search_memory")
    assert spec is not None
    bridge._tool_specs = [spec]
    effective, error = bridge._validate_function_call(
        "search_memory", {"query": "what did mummy tell me"}
    )
    assert error is None
    assert effective["query"] == "what did mummy tell me"
    rejected, reject_error = bridge._validate_function_call("inspect_ui", {"ref": "x"})
    assert rejected == {}
    assert reject_error


def test_f4_surface_honors_unadvertised_search_memory(monkeypatch) -> None:
    """F4 advertises `recall`; the model is still told to call search_memory."""
    from app.ev.tools import get_spec

    monkeypatch.setattr(settings, "voice_live_mode", "supervised")
    monkeypatch.setattr(settings, "model_surface_v2", "on")
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = _FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
    )
    bridge._shadow_mode = False
    bridge._upstream_session_ready = True
    bridge._upstream_tool_names = ("recall", "computer", "look")
    spec = get_spec("search_memory")
    assert spec is not None
    bridge._tool_specs = [spec, get_spec("recall")]
    effective, error = bridge._validate_function_call(
        "search_memory", {"query": "what did mummy tell me"}
    )
    assert error is None
    assert effective["query"] == "what did mummy tell me"


def test_autonomous_surface_is_empty() -> None:
    assert grok_voice_tools(_specs("read", "search_memory"), mode="autonomous") == []
    assert grok_voice_tools(None, mode="autonomous") == []


def test_openai_session_update_autonomous_has_no_tools() -> None:
    update = grok_session_update(
        provider="openai",
        function_tools=_specs("read", "search_memory", "send_message"),
    )
    session = update["session"]
    # Read the mode from the flag the fixture set via monkeypatch.
    if settings.voice_live_mode == "autonomous":
        assert session["tools"] == []
        assert session["tool_choice"] == "none"


def test_openai_session_update_shadow_surface(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    update = grok_session_update(
        provider="openai",
        function_tools=_specs("read", "click", "search_memory", "inspect_ui", "recall_history"),
    )
    names = {t["name"] for t in update["session"]["tools"]}
    assert "read" in names and "recall_history" in names
    assert "search_memory" not in names
    assert "inspect_ui" not in names
    assert update["session"]["tool_choice"] == "auto"
    assert update["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
    assert update["session"]["audio"]["input"]["turn_detection"]["type"] == "server_vad"


def test_openai_session_update_autonomous(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "autonomous")
    update = grok_session_update(
        provider="openai",
        function_tools=_specs("read", "send_message", "calendar_add"),
    )
    assert update["session"]["tools"] == []
    assert update["session"]["tool_choice"] == "none"


def test_xai_session_update_autonomous_no_web_search(monkeypatch) -> None:
    """Autonomous must not sneak provider-side web_search (or anything) in."""
    monkeypatch.setattr(settings, "voice_live_mode", "autonomous")
    update = grok_session_update(
        provider="xai",
        function_tools=_specs("read"),
    )
    assert update["session"]["tools"] == []
    assert update["session"]["tool_choice"] == "none"


async def test_shadow_block_empty_without_memory(db_session: AsyncSession) -> None:
    block = await build_shadow_memory(db_session, "anything at all", k=5)
    assert block == ""


async def test_shadow_block_builds_from_memory(db_session: AsyncSession, client) -> None:
    response = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "I decided to switch the project to SQLite for local testing",
        },
    )
    assert response.status_code in {200, 201}
    block = await build_shadow_memory(db_session, "why did I pick SQLite", k=5)
    assert block.startswith("SHADOW MEMORY")
    assert "SQLite" in block
    assert "memory" in block and "- [" in block


async def test_shadow_block_respects_budget(db_session: AsyncSession, client) -> None:
    for index in range(8):
        response = await client.post(
            "/v1/events",
            json={
                "source": "test",
                "event_type": "note",
                "text": (
                    f"I decided to catalogue every book in the library shelf {index} "
                    "including the old reference volumes and the paperbacks"
                ),
            },
        )
        assert response.status_code in {200, 201}
    tiny = await build_shadow_memory(
        db_session, "what did I decide about the library catalogue", k=8, budget_tokens=120
    )
    roomy = await build_shadow_memory(
        db_session, "what did I decide about the library catalogue", k=8, budget_tokens=2000
    )
    assert tiny  # at least one chunk
    assert tiny.count("\n- [") <= roomy.count("\n- [")


async def test_shadow_block_deterministic(db_session: AsyncSession, client) -> None:
    response = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "I decided to run the marathon next spring",
        },
    )
    assert response.status_code in {200, 201}
    first = await build_shadow_memory(db_session, "why did I decide to run the marathon", k=5)
    second = await build_shadow_memory(db_session, "why did I decide to run the marathon", k=5)
    assert first == second


def test_shadow_surface_is_stable_subset_of_supervised() -> None:
    assert SHADOW_VOICE_TOOLS <= LIVE_VOICE_TOOLS
    assert "recall_history" in SHADOW_VOICE_TOOLS
    assert "evie_turn" in SHADOW_VOICE_TOOLS
    assert "send_message" in SHADOW_VOICE_TOOLS
    assert "phone_action" in SHADOW_VOICE_TOOLS


def test_supervised_openai_keeps_auto_create(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "supervised")
    vad = grok_session_update(provider="openai")["session"]["audio"]["input"]["turn_detection"]
    assert vad["create_response"] is True
    assert vad["interrupt_response"] is False


def test_shadow_instructions_name_ui_verbs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    text = grok_session_update(provider="openai")["session"]["instructions"]
    assert "call read, see, click" in text
    assert "Do not call inspect_ui" in text


def test_supervised_instructions_keep_computer_primitives(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "supervised")
    text = grok_session_update(provider="openai")["session"]["instructions"]
    assert "inspect_ui, ui_action, screen_look, app_action" in text
    assert "Do not call inspect_ui" not in text


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _shadow_bridge(monkeypatch) -> tuple[GrokVoiceBridge, _FakeWS]:
    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = _FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
    )
    bridge._ws = ws
    bridge._shadow_mode = True
    bridge._shadow_base_instructions = "You are Evie."
    return bridge, ws


@pytest.mark.asyncio
async def test_shadow_spoken_turn_creates_response_with_memory(monkeypatch) -> None:
    bridge, ws = _shadow_bridge(monkeypatch)

    async def fake_block(text: str) -> str:
        return "SHADOW MEMORY:\n- [0.91] I picked Postgres for the local store"

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    # Not an owner-history / keep question — those go to the transcript broker.
    await bridge._emit_user_transcript("tell me about the local store", final=True)
    creates = [m for m in ws.sent if m.get("type") == "response.create"]
    assert len(creates) == 1
    instructions = creates[0]["response"]["instructions"]
    assert "SHADOW MEMORY" in instructions
    assert "Postgres" in instructions
    assert "You are Evie." in instructions


@pytest.mark.asyncio
async def test_shadow_spoken_turn_defers_owner_history_to_broker(monkeypatch) -> None:
    bridge, ws = _shadow_bridge(monkeypatch)

    async def fake_block(_text: str) -> str:
        return "SHADOW MEMORY:\n- [0.9] should not be spoken"

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    await bridge._emit_user_transcript("did you remember the book", final=True)
    await bridge._emit_user_transcript("What did I prefer before?", final=True)
    await bridge._emit_user_transcript("memorize this book", final=True)
    creates = [m for m in ws.sent if m.get("type") == "response.create"]
    assert creates == []


@pytest.mark.asyncio
async def test_shadow_spoken_turn_still_answers_without_memory(monkeypatch) -> None:
    bridge, ws = _shadow_bridge(monkeypatch)

    async def fake_block(_text: str) -> str | None:
        return None

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    await bridge._emit_user_transcript("hello there", final=True)
    creates = [m for m in ws.sent if m.get("type") == "response.create"]
    assert len(creates) == 1
    assert "response" not in creates[0]


@pytest.mark.asyncio
async def test_shadow_spoken_turn_is_idempotent_per_turn(monkeypatch) -> None:
    bridge, ws = _shadow_bridge(monkeypatch)

    async def fake_block(_text: str) -> str:
        return "SHADOW MEMORY:\n- [0.8] note"

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    await bridge._emit_user_transcript("tell me about the local store", final=True)
    # Duplicate text within 8s is ignored by the transcript gate.
    await bridge._emit_user_transcript("tell me about the local store", final=True)
    creates = [m for m in ws.sent if m.get("type") == "response.create"]
    assert len(creates) == 1


@pytest.mark.asyncio
async def test_supervised_spoken_turn_does_not_own_response_create(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_mode", "supervised")
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = _FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
    )
    bridge._ws = ws
    bridge._shadow_mode = False
    await bridge._emit_user_transcript("why did I pick Postgres", final=True)
    assert not any(m.get("type") == "response.create" for m in ws.sent)