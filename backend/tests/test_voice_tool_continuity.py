"""Tool-call voice continuity: single speech lane + long-tool mic gates.

Regression tests for the "voice breaks/lags every time a tool is called"
report. Normal chat is one continuous provider response; tool turns insert
a 0.3-15s provider silence while EV runs recall/memory/computer tools.
Two failure modes made EVERY tool turn glitch:

1. Duplicate speech: the transcript broker cancelled the active S2S reply
   and spoke a second response (speak_life_record) while the provider's own
   function-call continuation also spoke → overlapping PCM.
2. Gate expiry: short mic gates (4s backend / 3s client) reopened mid-tool
   on long computer/camera round-trips; room noise triggered provider VAD
   and a spurious second response collided with the continuation.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.voice.live import grok_voice as gv
from app.voice.live.session import LiveSession


def _bridge_double(**overrides):
    """Minimal function-capable bridge double for broker gating tests."""
    base = {
        "supports_function_calls": True,
        "_response_active": False,
        "_assistant_open": False,
        "_pending_tools": 0,
        "_tool_boundary_pending": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_provider_owns_turn_when_response_active() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(_response_active=True)
    assert session._provider_owns_live_turn() is True


def test_provider_owns_turn_when_tool_pending() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(_pending_tools=1)
    assert session._provider_owns_live_turn() is True


def test_provider_owns_turn_when_tool_boundary() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(_tool_boundary_pending=True)
    assert session._provider_owns_live_turn() is True


def test_provider_idle_turn_returns_false() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double()
    assert session._provider_owns_live_turn() is False


def test_provider_without_function_calls_never_owns() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(
        supports_function_calls=False, _response_active=True
    )
    assert session._provider_owns_live_turn() is False


def test_no_bridge_never_owns() -> None:
    session = LiveSession(session_id="s1")
    session.grok_voice = None
    assert session._provider_owns_live_turn() is False


async def _never_broker_call(*args, **kwargs):
    raise AssertionError("broker must stand down while provider owns the turn")


@pytest.mark.asyncio
async def test_memory_broker_stands_down_while_tool_in_flight() -> None:
    """Memory transcript broker must not double-speak over a tool turn."""
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(
        _response_active=True, _pending_tools=1, _tool_boundary_pending=True
    )
    session.run_live_tool = _never_broker_call
    # "what did we decide about postgres?" resolves to a memory live action,
    # but the provider already committed to a function call: its continuation
    # owns the single spoken reply.
    handled = await session._maybe_local_intent(
        "what did we decide about postgres?", from_grok=True
    )
    assert handled is False


def test_tool_in_flight_narrower_than_owns_turn() -> None:
    """A merely-speaking response (possible hedge) still allows broker fallback."""
    session = LiveSession(session_id="s1")
    session.grok_voice = _bridge_double(_response_active=True)
    assert session._provider_owns_live_turn() is True
    assert session._provider_tool_in_flight() is False
    session.grok_voice = _bridge_double(_pending_tools=1)
    assert session._provider_tool_in_flight() is True


@pytest.mark.asyncio
async def test_preempt_hedge_no_cancel_while_provider_active() -> None:
    """Partial-transcript preempt must not cancel a function-capable reply."""
    session = LiveSession(session_id="s1")

    async def _fail_cancel() -> None:
        raise AssertionError("provider reply must not be cancelled")

    session.grok_voice = _bridge_double(_response_active=True)
    session.grok_voice.cancel = _fail_cancel  # type: ignore[attr-defined]
    session.run_live_tool = _never_broker_call
    await session._preempt_memory_hedge("what did we decide about postgres?")
    # No exception → no cancel attempted.


def test_tool_gap_gate_constants_cover_long_tools() -> None:
    """Backend mic gate must cover 5-15s computer/camera round-trips."""
    assert gv._TOOL_GAP_GATE_S >= 10.0
    assert gv._TOOL_GAP_CONTINUATION_GATE_S >= 3.0


@pytest.mark.asyncio
async def test_tool_call_sets_long_mic_gate() -> None:
    """Function-call dispatch must hold the mic gate for long tools."""
    events: list = []

    async def _on_event(event) -> None:
        events.append(event)

    async def _on_tool(name, arguments, call_id) -> str:
        return '{"ok": true, "spoken": "done"}'

    bridge = gv.GrokVoiceBridge(
        on_event=_on_event,
        on_tool=_on_tool,
        now_ms=lambda: 0,
        provider="openai",
        api_key="test-key",
        capability_manifest={},
        approved_tool_specs=[],
    )
    # Simulate an acknowledged live tool so validation passes.
    tool_spec = {
        "type": "function",
        "name": "search_memory",
        "description": "search",
        "parameters": {"type": "object", "properties": {}},
    }
    bridge._tool_specs = [tool_spec]
    bridge._upstream_tool_names = ("search_memory",)
    bridge._upstream_session_ready = True
    # Stub the provider send so no socket is needed.
    sent: list = []

    async def _fake_send(payload, timeout_s=2.0) -> bool:
        sent.append(payload)
        return True

    bridge._send = _fake_send  # type: ignore[method-assign]
    before = time.monotonic()
    await bridge._run_tool(
        {"name": "search_memory", "call_id": "call-1", "arguments": "{}"}
    )
    gate = bridge._tool_gap_gate_until - before
    assert gate >= 4.9  # continuation hold covers slow first-chunk delivery
