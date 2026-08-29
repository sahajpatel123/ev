"""Shadow / autonomous live-voice modes (EV VOICE CONTROL PLAN §5–6).

Offline-safe: tests the mode surface filtering, session payload shape, and
the shadow-memory block builder. No websocket and no model ever connects.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.tool_select import LIVE_VOICE_TOOLS, SHADOW_VOICE_TOOLS
from app.memory.history import build_shadow_memory
from app.voice.live.grok_voice import grok_session_update, grok_voice_tools


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


def test_shadow_surface_is_curated() -> None:
    tools = grok_voice_tools(
        _specs("search_memory", "read", "click", "recall_history", "inspect_ui", "app_action"),
        mode="shadow",
    )
    names = {t["name"] for t in tools}
    assert {"read", "click", "recall_history"} <= names
    assert "search_memory" not in names  # replaced by injected history
    assert "inspect_ui" not in names  # replaced by read/see
    assert "app_action" not in names  # replaced by UI verbs
    assert names <= SHADOW_VOICE_TOOLS


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
        db_session, "catalogue the library", k=8, budget_tokens=120
    )
    roomy = await build_shadow_memory(
        db_session, "catalogue the library", k=8, budget_tokens=2000
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
    first = await build_shadow_memory(db_session, "marathon spring", k=5)
    second = await build_shadow_memory(db_session, "marathon spring", k=5)
    assert first == second


def test_shadow_surface_is_stable_subset_of_supervised() -> None:
    assert SHADOW_VOICE_TOOLS <= LIVE_VOICE_TOOLS
    assert "recall_history" in SHADOW_VOICE_TOOLS
    assert "evie_turn" in SHADOW_VOICE_TOOLS
    assert "send_message" in SHADOW_VOICE_TOOLS
    assert "phone_action" in SHADOW_VOICE_TOOLS