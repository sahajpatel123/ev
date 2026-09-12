"""Bridge resilience: a stale path, an extra argument, or a small budget.

Three failures found by running real phone turns against the live system, each
of which turned a working capability into a dead end:

1. An integration row keeps its own ``helper_path``. When the Mac app is
   rebuilt or reinstalled that path goes stale, and every bridge that read it
   died with "EVLifeHelper is not an executable file" — while a perfectly good
   binary sat at the configured default.
2. The mind sometimes adds one argument it inferred from the owner's words
   (a ``query`` on a tool that only declares ``limit``). Strict validation
   answered "That request has invalid arguments" and the request was lost.
3. A phone turn was treated as a compact conversational turn, so it got four
   tool rounds and 25 seconds. Reaching Core or Home Station and still speaking
   the result needs more, and exhausting the budget surfaced the model's
   mid-work narration ("pulling your latest email now") instead of the answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.models import Device


def _phone() -> Device:
    return Device(
        name="Test iPhone",
        token_hash="c" * 64,
        role="primary_companion",
        platform="ios",
        device_type="phone",
        memory_scope="owner",
    )


def _executable(path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


# --------------------------------------------------------------------------
# 1. A stale helper path must not take a bridge down
# --------------------------------------------------------------------------


def test_stale_configured_helper_path_falls_back_to_the_default(monkeypatch, tmp_path) -> None:
    from app.integrations import life_helper

    good = _executable(tmp_path / "EVLifeHelper")
    monkeypatch.setattr(life_helper.settings, "life_helper_path", good)

    stale = str(tmp_path / "macos" / "build" / "EV.app" / "Contents" / "MacOS" / "EVLifeHelper")
    assert life_helper.resolve_helper_path(stale) == good


def test_a_usable_configured_path_is_preferred(monkeypatch, tmp_path) -> None:
    from app.integrations import life_helper

    good = _executable(tmp_path / "EVLifeHelper")
    monkeypatch.setattr(life_helper.settings, "life_helper_path", good)
    assert life_helper.resolve_helper_path(good) == good


def test_environment_path_is_used_when_settings_are_empty(monkeypatch, tmp_path) -> None:
    from app.integrations import life_helper

    good = _executable(tmp_path / "EVLifeHelper")
    monkeypatch.setattr(life_helper.settings, "life_helper_path", "")
    monkeypatch.setenv("EV_LIFE_HELPER_PATH", good)
    assert life_helper.resolve_helper_path(None) == good


def test_missing_helper_reports_every_path_it_tried(monkeypatch, tmp_path) -> None:
    from app.integrations import life_helper

    monkeypatch.setattr(life_helper.settings, "life_helper_path", str(tmp_path / "nope"))
    monkeypatch.delenv("EV_LIFE_HELPER_PATH", raising=False)
    with pytest.raises(life_helper.LifeHelperUnavailableError) as excinfo:
        life_helper.resolve_helper_path(str(tmp_path / "also-gone"))
    message = str(excinfo.value)
    # Actionable, not just "not an executable file": say what was tried.
    assert "no executable EVLifeHelper found" in message
    assert "also-gone" in message
    assert "nope" in message


def test_nothing_configured_at_all_is_still_an_honest_error(monkeypatch, tmp_path) -> None:
    from app.integrations import life_helper

    monkeypatch.setattr(life_helper.settings, "life_helper_path", "")
    monkeypatch.delenv("EV_LIFE_HELPER_PATH", raising=False)
    with pytest.raises(life_helper.LifeHelperUnavailableError) as excinfo:
        life_helper.resolve_helper_path(None)
    assert "EV_LIFE_HELPER_PATH is not set" in str(excinfo.value)


# --------------------------------------------------------------------------
# 2. An extra argument must not lose the request
# --------------------------------------------------------------------------


def test_declared_argument_names_reports_a_strict_spec() -> None:
    from app.ev.tools import declared_argument_names

    names = declared_argument_names("calendar_read")
    assert names is not None
    assert "limit" in names
    # This is the exact argument that used to fail the whole request.
    assert "query" not in names


async def test_semantic_bridge_drops_arguments_the_tool_does_not_declare(
    monkeypatch, db_session
) -> None:
    import app.ev.tools as tools
    import app.voice.live.layer as layer
    from app.cognitive import executor
    from app.cognitive.session_store import CognitiveSession

    seen: dict[str, object] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["name"] = name
        seen["args"] = dict(arguments)
        return {"ok": True, "spoken": "ok"}

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    # Force the in-process dispatch branch.
    monkeypatch.setattr(layer, "active_lives", lambda: [SimpleNamespace(session_id="live")])

    await executor._run_existing(
        db_session,
        "calendar_read",
        {"limit": 5, "query": "what's on my calendar today"},
        actor="master",
        live_session_id=None,
        cognition=CognitiveSession(session_id="t"),
        kind="life.state",
    )

    assert seen["name"] == "calendar_read"
    assert seen["args"] == {"limit": 5}


async def test_semantic_bridge_keeps_declared_arguments(monkeypatch, db_session) -> None:
    import app.ev.tools as tools
    import app.voice.live.layer as layer
    from app.cognitive import executor
    from app.cognitive.session_store import CognitiveSession

    seen: dict[str, object] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["args"] = dict(arguments)
        return {"ok": True, "spoken": "ok"}

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    monkeypatch.setattr(layer, "active_lives", lambda: [SimpleNamespace(session_id="live")])

    await executor._run_existing(
        db_session,
        "calendar_read",
        {"limit": 7},
        actor="master",
        live_session_id=None,
        cognition=CognitiveSession(session_id="t"),
        kind="life.state",
    )
    assert seen["args"] == {"limit": 7}


# --------------------------------------------------------------------------
# 3. A phone turn gets the work budget
# --------------------------------------------------------------------------


async def test_phone_turn_is_not_capped_at_the_conversation_tool_budget(
    monkeypatch, tmp_path, db_session
) -> None:
    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.device_gateway import cognitive_phone

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    device = _phone()
    db_session.add(device)
    await db_session.flush()

    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(kernel, "should_prefetch_memory", lambda **kwargs: False)
    monkeypatch.setattr(
        cognitive_phone, "execute_phone_tool", AsyncMock(return_value={"ok": True})
    )
    semantic = AsyncMock(return_value={"ok": True, "spoken": "found"})
    monkeypatch.setattr(kernel, "execute_semantic", semantic)

    rounds: list[int] = []

    class Muse:
        def __init__(self) -> None:
            self.step = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.step += 1
            rounds.append(self.step)
            if self.step <= 6:
                return ChatResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id=f"c{self.step}",
                            name="memory.search",
                            arguments={"query": f"q{self.step}"},
                        )
                    ],
                )
            return ChatResult(text="Here is what I found.")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", Muse)
    try:
        result = await kernel.handle_turn(
            # Short and conversational: this is what used to select the compact
            # budget of four rounds.
            transcript="What is the last email I got?",
            device_id=str(device.id),
            live_session_id="phone-session",
            session=db_session,
        )
        # Six tool rounds plus the final answer — impossible under a budget of
        # four, which is what produced the mid-work narration instead of a reply.
        assert result.tool_calls == 6
        assert result.spoken == "Here is what I found."
        assert semantic.await_count == 6
    finally:
        reset_for_tests()
