"""Near-end barge-in: local confirm, provider cancel, stale output, persist."""

from __future__ import annotations

import asyncio
import base64
import json

from app.voice.live.barge_in import (
    delivered_assistant_text,
    generated_duration_ms,
    interrupt_metadata,
    parse_interrupt_request,
)
from app.voice.live.events import LatencyEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.grok_voice import GrokVoiceBridge, grok_session_update
from app.voice.live.session import LiveSession


class _FakeRealtime:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


async def _wait_until(predicate, *, ticks: int = 200) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


def test_interrupt_response_stays_false_on_openai_session() -> None:
    update = grok_session_update(provider="openai")
    vad = update["session"]["audio"]["input"]["turn_detection"]
    assert vad["interrupt_response"] is False
    assert vad["create_response"] is True


def test_delivered_text_keeps_heard_prefix_not_unheard_tail() -> None:
    generated = "Your appointment is at four, and I also found three emails about it."
    heard = delivered_assistant_text(
        generated, audio_played_ms=2000, generated_duration_ms=8000
    )
    assert heard.startswith("Your appointment is at four")
    assert "three emails" not in heard


def test_delivered_text_empty_without_played_timing() -> None:
    generated = "I also found three emails about the meeting tomorrow."
    assert delivered_assistant_text(generated, audio_played_ms=None, generated_duration_ms=4000) == ""
    assert delivered_assistant_text(generated, audio_played_ms=0, generated_duration_ms=4000) == ""


def test_generated_duration_from_pcm_bytes() -> None:
    # 16 kHz PCM16, 2 seconds
    assert generated_duration_ms(audio_bytes=16_000 * 2 * 2) == 2000


def test_interrupt_metadata_marks_delivery() -> None:
    meta = interrupt_metadata(
        reason="user_barge_in",
        provider_response_id="resp_1",
        audio_played_ms=1200,
        generated_duration_ms=4000,
        generated_text="full generated sentence that was not all heard",
    )
    assert meta["interrupted"] is True
    assert meta["delivery"] == "interrupted"
    assert meta["interruption_reason"] == "user_barge_in"
    assert "full generated" in meta["generated_text"]


def test_parse_interrupt_request_from_client_control() -> None:
    req = parse_interrupt_request(
        {
            "type": "control",
            "action": "barge_in",
            "reason": "user_barge_in",
            "audio_played_ms": 1400,
            "confidence": 0.81,
            "preroll_ms": 320,
        }
    )
    assert req.reason == "user_barge_in"
    assert req.audio_played_ms == 1400
    assert req.preroll_ms == 320
    assert abs((req.confidence or 0) - 0.81) < 1e-6


async def _bridge():
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    return bridge, fake, events


async def test_provider_speech_started_during_playback_still_does_not_cancel() -> None:
    bridge, fake, events = await _bridge()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    assert not any(item.get("type") == "conversation.item.truncate" for item in fake.sent)
    bridge.close()


async def test_client_interrupt_cancels_and_truncates_active_response() -> None:
    bridge, fake, events = await _bridge()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_live"}})
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.created",
                "item": {"id": "item_asst", "role": "assistant"},
            }
        )
    )
    await _wait_until(lambda: bridge._response_id == "resp_live")
    await _wait_until(lambda: bridge._assistant_item_id == "item_asst")
    bridge._reply_text = "Your appointment is at four, and I also found three emails."
    bridge._turn_audio_bytes = 16_000 * 2 * 8
    fake.sent.clear()
    events.clear()
    result = await bridge.interrupt_for_user(
        reason="user_barge_in", audio_played_ms=2000, confidence=0.9, preroll_ms=320
    )
    assert result["latched"] is True
    types = [item.get("type") for item in fake.sent]
    assert "response.cancel" in types
    assert "conversation.item.truncate" in types
    truncate = next(item for item in fake.sent if item.get("type") == "conversation.item.truncate")
    assert truncate["item_id"] == "item_asst"
    assert truncate["audio_end_ms"] == 2000
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert replies
    assert replies[0].interrupted is True
    assert replies[0].interruption_reason == "user_barge_in"
    assert "three emails" not in (replies[0].text or "")
    assert any(isinstance(event, LatencyEvent) for event in events)
    bridge.close()


async def test_late_pcm_after_interrupt_is_dropped() -> None:
    bridge, fake, events = await _bridge()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_old"}})
    )
    await _wait_until(lambda: bridge._response_id == "resp_old")
    await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=500)
    events[:] = [event for event in events if not isinstance(event, TtsChunkEvent)]
    pcm = b"\x11\x22" * 2400
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp_old",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )
    )
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, TtsChunkEvent) for event in events)
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.done",
                "response": {"id": "resp_old"},
            }
        )
    )
    await asyncio.sleep(0.05)
    late_replies = [
        event
        for event in events
        if isinstance(event, ReplyEvent) and not event.interrupted
    ]
    assert late_replies == []
    bridge.close()


async def test_mic_forwards_after_confirmed_barge_in_even_if_playback_was_active() -> None:
    bridge, fake, _events = await _bridge()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_play"}})
    )
    await _wait_until(lambda: bridge._response_id == "resp_play")
    bridge.set_playback(True)
    fake.sent.clear()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not any(item.get("type") == "input_audio_buffer.append" for item in fake.sent)
    await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=800)
    fake.sent.clear()
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item.get("type") == "input_audio_buffer.append" for item in fake.sent)
    )
    bridge.close()


async def test_duplicate_interrupt_is_latched() -> None:
    bridge, fake, _events = await _bridge()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_dup"}})
    )
    await _wait_until(lambda: bridge._response_id == "resp_dup")
    bridge._interrupt_in_flight = True
    result = await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=100)
    assert result["latched"] is False
    assert result["duplicate"] is True
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    bridge.close()


async def test_live_session_barge_in_forwards_played_ms() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=session.now,
    )
    await session.grok_voice.start()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_sess"}})
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.created",
                "item": {"id": "item_sess", "role": "assistant"},
            }
        )
    )
    await _wait_until(lambda: session.grok_voice._response_id == "resp_sess")
    await _wait_until(lambda: session.grok_voice._assistant_item_id == "item_sess")
    fake.sent.clear()
    await session.handle_client(
        {
            "type": "control",
            "action": "barge_in",
            "reason": "user_barge_in",
            "audio_played_ms": 900,
            "preroll_ms": 280,
        }
    )
    types = [item.get("type") for item in fake.sent]
    assert "response.cancel" in types
    assert "conversation.item.truncate" in types
    truncate = next(item for item in fake.sent if item["type"] == "conversation.item.truncate")
    assert truncate["audio_end_ms"] == 900
    session.close()

async def test_disconnect_closes_upstream_socket_no_leak() -> None:
    bridge, fake, _events = await _bridge()
    assert fake.closed is False
    await bridge._note_disconnect(ConnectionError("realtime stream closed"))
    assert bridge._ws is None
    assert fake.closed is True
    bridge.close()


async def test_stale_socket_disconnect_closes_only_stale_socket() -> None:
    bridge, fake, _events = await _bridge()
    stale = _FakeRealtime()
    await bridge._note_disconnect(ConnectionError("old socket died"), ws=stale)
    assert stale.closed is True
    # The live socket is untouched by a stale-socket notification.
    assert bridge._ws is fake
    assert fake.closed is False
    bridge.close()


async def test_zero_audio_interrupt_skips_truncate() -> None:
    bridge, fake, _events = await _bridge()
    await fake.incoming.put(
        json.dumps({"type": "response.created", "response": {"id": "resp_quiet"}})
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.created",
                "item": {"id": "item_quiet", "role": "assistant"},
            }
        )
    )
    await _wait_until(lambda: bridge._response_id == "resp_quiet")
    await _wait_until(lambda: bridge._assistant_item_id == "item_quiet")
    fake.sent.clear()
    # Monologue storm shape: response cancelled before any audio existed.
    result = await bridge.interrupt_for_user(
        reason="user_barge_in", audio_played_ms=0, confidence=0.5
    )
    assert result["latched"] is True
    types = [item.get("type") for item in fake.sent]
    assert "response.cancel" in types
    # Truncating a zero-audio item is a provider protocol error, not a no-op.
    assert "conversation.item.truncate" not in types
    bridge.close()


async def test_monologue_storm_keeps_session_alive_and_leak_free() -> None:
    bridge, fake, _events = await _bridge()
    for round_index in range(12):
        await fake.incoming.put(
            json.dumps(
                {"type": "response.created", "response": {"id": f"resp_{round_index}"}}
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.created",
                    "item": {"id": f"item_{round_index}", "role": "assistant"},
                }
            )
        )
        await _wait_until(lambda b=bridge, r=round_index: b._response_id == f"resp_{r}")
        await _wait_until(lambda b=bridge, r=round_index: b._assistant_item_id == f"item_{r}")
        bridge._reply_text = "Short response the owner talks over."
        bridge._turn_audio_bytes = 16_000 * 2 * 2
        await bridge.interrupt_for_user(
            reason="user_barge_in",
            audio_played_ms=150 + round_index,
            confidence=0.6,
        )
    # The storm never tore down the upstream socket by itself.
    assert bridge._ws is fake
    assert fake.closed is False
    truncates = [
        item
        for item in fake.sent
        if item.get("type") == "conversation.item.truncate"
    ]
    assert truncates, "audio-bearing interrupts must still truncate"
    assert all(item["audio_end_ms"] > 0 for item in truncates)
    bridge.close()
    # close() schedules the socket close; give the loop a tick to land it.
    for _ in range(20):
        if fake.closed:
            break
        await asyncio.sleep(0)
    assert fake.closed is True

async def test_spend_limit_provider_error_routes_to_quota_not_reconnect_loop() -> None:
    from app.voice.live.events import ErrorEvent

    bridge, fake, events = await _bridge()
    await fake.incoming.put(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "usage_limit_reached",
                    "message": (
                        "Your organization has reached its configured enforced "
                        "spend limit. Update your limit at "
                        "https://platform.openai.com/settings/organization/limits."
                    ),
                },
            }
        )
    )
    await _wait_until(lambda: bridge._ws is None)
    assert fake.closed is True
    quota = [e for e in events if isinstance(e, ErrorEvent) and e.code == "realtime_quota"]
    disconnects = [
        e for e in events if isinstance(e, ErrorEvent) and e.code == "realtime_disconnect"
    ]
    assert quota, "spend-limit refusal must speak the truthful quota line"
    assert "spend limit" in quota[0].message
    assert disconnects == [], "spend limit must not masquerade as a transient disconnect"
    assert bridge._reconnect_delay >= 60.0
    bridge.close()

async def test_quota_notified_once_per_episode() -> None:
    from app.voice.live.events import ErrorEvent

    bridge, fake, events = await _bridge()
    # First signal: the provider error event / 1013 close.
    await bridge._note_disconnect(
        ConnectionError(
            "usage_limit_reached Your organization has reached its "
            "configured enforced spend limit."
        )
    )
    # Second signal for the same episode: the follow-on close frame.
    await bridge._note_disconnect(
        ConnectionError("insufficient_quota.organization_spend_limit_exceeded")
    )
    quota = [e for e in events if isinstance(e, ErrorEvent) and e.code == "realtime_quota"]
    assert len(quota) == 1, "one truthful notification per quota episode"
    assert bridge._reconnect_delay >= 60.0
    assert bridge._reconnect_floor >= 60.0
    bridge.close()


async def test_quota_floor_bounds_backoff_despite_unclassified_failures() -> None:
    bridge, _fake, _events = await _bridge()
    # Normal transient path: exponential growth capped at 8s.
    bridge._reconnect_delay = 2.0
    assert bridge._next_reconnect_delay() == 4.0
    bridge._reconnect_delay = 8.0
    assert bridge._next_reconnect_delay() == 8.0
    # Quota episode: the floor holds the cadence even when a refusal arrives
    # without quota markers (the loop's 8s cap must not re-create a storm).
    bridge._reconnect_floor = 60.0
    bridge._reconnect_delay = 2.0
    assert bridge._next_reconnect_delay() == 60.0
    bridge._reconnect_delay = 60.0
    assert bridge._next_reconnect_delay() == 60.0
    bridge.close()

async def test_self_echo_quarantine_blocks_mic_near_own_emissions() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    fake.sent.clear()
    # We emitted speech a moment ago (speaker tail / reverb still live).
    bridge._last_audio_emit_at = _time.monotonic()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not any(
        item.get("type") == "input_audio_buffer.append" for item in fake.sent
    ), "own-audio echo must not be forwarded to the provider"
    bridge.close()


async def test_mic_reopens_after_quarantine_window() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    fake.sent.clear()
    bridge._last_audio_emit_at = _time.monotonic() - 2.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item.get("type") == "input_audio_buffer.append" for item in fake.sent)
    )
    bridge.close()


async def test_quarantine_blocks_even_after_response_done() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    fake.sent.clear()
    # response.done already arrived but our chunks still sound in the room.
    bridge._response_active = False
    bridge._assistant_open = False
    bridge.set_playback(True)
    bridge._last_audio_emit_at = _time.monotonic()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not any(
        item.get("type") == "input_audio_buffer.append" for item in fake.sent
    ), "playback-lagging-response-done echo must not be forwarded"
    bridge.close()

async def test_authoritative_playback_blocks_mic_across_all_queue_depths() -> None:
    """response.done + any queued client audio (250ms-2000ms) must NOT open mic.

    The queue depth is simulated by aging our last emission: the client had
    X ms queued after our final send, so at test time the speaker was still
    rendering while our last chunk was already (X + 0.5)s old. The old
    time-based heuristic opened the gate here; only client playback state
    may decide.
    """
    import time as _time

    for queued_ms in (250, 500, 1000, 1500, 2000):
        bridge, fake, _events = await _bridge()
        fake.sent.clear()
        bridge.set_playback(True)  # client: still rendering
        bridge._response_active = False  # response.done already arrived
        bridge._assistant_open = False
        # final backend send happened (queued_ms + 500ms) ago
        bridge._last_audio_emit_at = _time.monotonic() - (queued_ms + 500) / 1000.0
        await bridge.append_pcm(b"\x00\x01" * 800)
        forwarded = any(
            item.get("type") == "input_audio_buffer.append" for item in fake.sent
        )
        assert not forwarded, f"mic opened with {queued_ms}ms still queued at client"
        bridge.close()


async def test_post_playback_tail_gates_then_reopens() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    bridge.set_playback(True)
    bridge._last_audio_emit_at = _time.monotonic() - 2.0  # emissions long done
    bridge.set_playback(False)  # authoritative physical completion
    fake.sent.clear()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not any(
        item.get("type") == "input_audio_buffer.append" for item in fake.sent
    ), "acoustic tail after playback completion must stay gated"
    await asyncio.sleep(0.65)  # tail (0.5s) expires
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item.get("type") == "input_audio_buffer.append" for item in fake.sent)
    )
    bridge.close()


async def test_long_form_diagnostic_is_opt_in_and_per_response() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    # OFF (production): plain response.create, no instructions override.
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    await bridge.send_text("Explain the solar system for ninety seconds.")
    create = next(
        item for item in fake.sent if item.get("type") == "response.create"
    )
    assert "response" not in create, "production sends no per-response instructions"
    bridge.close()
    await asyncio.sleep(0)

    # ON (diagnostic): instructions override present on the one create.
    events.clear()
    fake2 = _FakeRealtime()

    async def connect2(url: str, additional_headers=None):
        del url, additional_headers
        return fake2

    bridge2 = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect2,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
        long_form_diagnostic=True,
    )
    await bridge2.start()
    fake2.sent.clear()
    await bridge2.send_text("Explain the solar system for ninety seconds.")
    create2 = next(
        item for item in fake2.sent if item.get("type") == "response.create"
    )
    assert create2.get("response", {}).get("instructions", "").startswith(
        "Give one continuous spoken explanation"
    ), "diagnostic create must carry the long-form instructions"
    bridge2.close()
