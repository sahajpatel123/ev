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

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.voice.live import gemini_live as gv
from app.voice.live.events import FinalTranscriptEvent, ReplyEvent
from app.voice.live.session import LiveSession


async def test_canonical_speech_context_and_interrupt(monkeypatch) -> None:
    """The coprocessor mouth speaks only the approved reply, verbatim."""
    monkeypatch.setattr(gv, "_mouth_coprocessor", lambda: True)
    sent = []

    class Socket:
        async def send(self, payload):
            sent.append(json.loads(payload))

    bridge = gv.GeminiLiveBridge(
        on_event=lambda _event: asyncio.sleep(0), provider="gemini",
        api_key="test-key", approved_tool_specs=[],
    )
    bridge._ws = Socket()
    assert await bridge.speak_supplied_text("Hello!") is True
    assert len(sent) == 1
    turn = sent[-1]["clientContent"]
    assert turn["turnComplete"] is True
    text = turn["turns"][0]["parts"][0]["text"]
    assert text.startswith("(system confirmation — speak this to the owner now) Hello!")
    assert text.endswith(gv._MOUTH_SPEAK_INSTRUCTIONS)
    bridge._turn_audio_bytes = 3200
    # Truncation is a no-op on the Live API: interrupting keeps only
    # already-sent content in history, so nothing new goes upstream.
    await bridge._truncate_assistant_item(50)
    assert len(sent) == 1


async def test_short_speech_flush_records_latency_and_next_turn_resets() -> None:
    """Replies shorter than one chunk still report measured first audio."""
    events = []

    async def on_event(event):
        events.append(event)

    bridge = gv.GeminiLiveBridge(
        on_event=on_event, provider="gemini", api_key="test-key",
        approved_tool_specs=[],
    )
    bridge._audio_accepting = True
    bridge._out_pcm.extend(b"\x00\x00" * 100)
    bridge._last_response_create_at = time.monotonic() - 0.05
    bridge._latency_speech_stopped_at = time.monotonic() - 0.1
    bridge.note_kernel_latency(12.5)
    await bridge._flush_audio(force=True)
    snapshot = bridge.voice_health_snapshot()
    assert snapshot["last_response_create_to_first_audio_ms"] >= 50
    assert snapshot["last_speech_stop_to_first_audio_ms"] >= 100
    assert snapshot["last_kernel_ms"] == 12.5
    assert len(events) == 1
    await bridge._handle_upstream({"serverContent": {"inputTranscription": {"text": "hi"}}})
    snapshot = bridge.voice_health_snapshot()
    assert snapshot["last_response_create_to_first_audio_ms"] is None
    assert snapshot["last_speech_stop_to_first_audio_ms"] is None
    assert snapshot["last_kernel_ms"] is None


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
    session.gemini_live = _bridge_double(_response_active=True)
    assert session._provider_owns_live_turn() is True


def test_provider_owns_turn_when_tool_pending() -> None:
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double(_pending_tools=1)
    assert session._provider_owns_live_turn() is True


def test_provider_owns_turn_when_tool_boundary() -> None:
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double(_tool_boundary_pending=True)
    assert session._provider_owns_live_turn() is True


def test_provider_idle_turn_returns_false() -> None:
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double()
    assert session._provider_owns_live_turn() is False


def test_provider_without_function_calls_never_owns() -> None:
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double(
        supports_function_calls=False, _response_active=True
    )
    assert session._provider_owns_live_turn() is False


def test_no_bridge_never_owns() -> None:
    session = LiveSession(session_id="s1")
    session.gemini_live = None
    assert session._provider_owns_live_turn() is False


async def _never_broker_call(*args, **kwargs):
    raise AssertionError("broker must stand down while provider owns the turn")


@pytest.mark.asyncio
async def test_memory_broker_stands_down_while_tool_in_flight() -> None:
    """Memory transcript broker must not double-speak over a tool turn."""
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double(
        _response_active=True, _pending_tools=1, _tool_boundary_pending=True
    )
    session.run_live_tool = _never_broker_call
    # "what did we decide about postgres?" resolves to a memory live action,
    # but the provider already committed to a function call: its continuation
    # owns the single spoken reply.
    handled = await session._maybe_local_intent(
        "what did we decide about postgres?", from_live=True
    )
    assert handled is False


def test_tool_in_flight_narrower_than_owns_turn() -> None:
    """A merely-speaking response (possible hedge) still allows broker fallback."""
    session = LiveSession(session_id="s1")
    session.gemini_live = _bridge_double(_response_active=True)
    assert session._provider_owns_live_turn() is True
    assert session._provider_tool_in_flight() is False
    session.gemini_live = _bridge_double(_pending_tools=1)
    assert session._provider_tool_in_flight() is True


@pytest.mark.asyncio
async def test_preempt_hedge_no_cancel_while_provider_active() -> None:
    """Partial-transcript preempt must not cancel a function-capable reply."""
    session = LiveSession(session_id="s1")

    async def _fail_cancel() -> None:
        raise AssertionError("provider reply must not be cancelled")

    session.gemini_live = _bridge_double(_response_active=True)
    session.gemini_live.cancel = _fail_cancel  # type: ignore[attr-defined]
    session.run_live_tool = _never_broker_call
    await session._preempt_memory_hedge("what did we decide about postgres?")
    # No exception → no cancel attempted.


def test_tool_gap_gate_constants_cover_long_tools() -> None:
    """Backend mic gate must cover 5-15s computer/camera round-trips."""
    assert gv._TOOL_GAP_GATE_S >= 10.0
    assert gv._TOOL_GAP_CONTINUATION_GATE_S >= 3.0


@pytest.mark.asyncio
async def test_tool_is_reserved_before_worker_gets_event_loop_time() -> None:
    """Back-to-back response.done must already see a pending tool boundary."""

    events: list = []

    async def _on_event(event) -> None:
        events.append(event)

    bridge = gv.GeminiLiveBridge(
        on_event=_on_event,
        now_ms=lambda: 0,
        provider="gemini",
        api_key="test-key",
        capability_manifest={},
        approved_tool_specs=[],
    )
    tool = {
        "name": "search_memory",
        "call_id": "call-race",
        "arguments": "{}",
    }

    # Intentionally do not yield after enqueue. This models the provider
    # placing toolCall and turnComplete in the same read burst, before the
    # sibling worker can start.
    bridge._spawn_tool(tool)
    bridge._spawn_tool(tool)
    assert bridge._pending_tools == 0
    assert bridge._scheduled_tool_calls == {"call-race"}
    assert bridge._tool_boundary_pending is True
    assert bridge._tool_gap_gate_until > time.monotonic()
    assert bridge._tool_queue.qsize() == 1

    bridge._reply_text = "Let me check."
    await bridge._handle_upstream({"serverContent": {"turnComplete": True}})
    assert bridge._pending_tools == 0
    assert bridge._scheduled_tool_calls == {"call-race"}
    assert not any(isinstance(event, ReplyEvent) for event in events)

    bridge._cancel_tool_worker()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_upstream_queue_preserves_control_boundaries_over_audio() -> None:
    """A full provider queue must not evict response/VAD control events."""

    bridge = gv.GeminiLiveBridge(
        on_event=lambda _event: asyncio.sleep(0),
        now_ms=lambda: 0,
        provider="gemini",
        api_key="test-key",
        capability_manifest={},
        approved_tool_specs=[],
    )
    queue = asyncio.Queue(maxsize=2)
    bridge._upstream_events = queue
    queue.put_nowait(
        {
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": "old"}}
                    ]
                }
            }
        }
    )
    queue.put_nowait({"serverContent": {"turnComplete": True}})

    # This models a control event arriving after the queue filled with audio
    # and a boundary. The audio slice may be sacrificed; turnComplete must
    # remain in FIFO order and the new control event must be admitted.
    assert bridge._drop_upstream_audio() is True
    queue.put_nowait({"setupComplete": {}})
    assert [("turnComplete" in item.get("serverContent", {})) for item in queue._queue] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_s2s_final_transcript_publishes_before_slow_tool_route() -> None:
    """Final transcript delivery must not inherit recall/computer latency."""

    release = asyncio.Event()
    session = LiveSession(session_id="s-route")

    async def slow_route(_text: str, *, from_live: bool) -> bool:
        assert from_live is True
        await release.wait()
        return True

    session._maybe_local_intent = slow_route  # type: ignore[method-assign]
    routing = await asyncio.wait_for(
        session.emit(
            FinalTranscriptEvent(
                at_ms=1,
                text="recall the file from yesterday",
                provider="gemini-live",
            )
        ),
        timeout=0.1,
    )
    assert isinstance(routing, asyncio.Task)
    assert [event.type for event in session.outbound._queue] == ["final_transcript"]
    assert not routing.done()

    release.set()
    assert await routing is True


@pytest.mark.asyncio
async def test_tool_call_sets_long_mic_gate() -> None:
    """Function-call dispatch must hold the mic gate for long tools."""
    events: list = []

    async def _on_event(event) -> None:
        events.append(event)

    async def _on_tool(name, arguments, call_id) -> str:
        return '{"ok": true, "spoken": "done"}'

    bridge = gv.GeminiLiveBridge(
        on_event=_on_event,
        on_tool=_on_tool,
        now_ms=lambda: 0,
        provider="gemini",
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
