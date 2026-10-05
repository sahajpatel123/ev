"""Near-end barge-in: local confirm, provider cancel, stale output, persist."""

from __future__ import annotations

import asyncio
import base64
import json
import time

from app.voice.live.barge_in import (
    delivered_assistant_text,
    generated_duration_ms,
    interrupt_metadata,
    parse_interrupt_request,
)
from app.voice.live.events import LatencyEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.gemini_live import GeminiLiveBridge, gemini_live_setup
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


def test_default_setup_uses_automatic_vad_manual_opt_in() -> None:
    auto = gemini_live_setup(provider="gemini")["setup"]
    # Server VAD by default: no realtimeInputConfig override.
    assert "realtimeInputConfig" not in auto
    manual = gemini_live_setup(provider="gemini", manual_vad=True)["setup"]
    detection = manual["realtimeInputConfig"]["automaticActivityDetection"]
    assert detection["disabled"] is True


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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    return bridge, fake, events


def _model_audio_message(pcm: bytes) -> str:
    return json.dumps(
        {
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/pcm;rate=24000",
                                "data": base64.b64encode(pcm).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        }
    )


def _input_transcript_message(text: str) -> str:
    return json.dumps({"serverContent": {"inputTranscription": {"text": text}}})


def _mic_audio_sent(fake: _FakeRealtime) -> bool:
    return any(
        isinstance(item.get("realtimeInput"), dict)
        and "audio" in item["realtimeInput"]
        for item in fake.sent
    )


async def test_owner_speech_during_playback_still_does_not_cancel() -> None:
    bridge, fake, events = await _bridge()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    await fake.incoming.put(_input_transcript_message("hey evie"))
    await asyncio.sleep(0.05)
    # Cancelling is local-only on the Live API: nothing goes upstream, and
    # playback keeps the floor (no pre-playback cancel while rendering).
    assert fake.sent == []
    assert bridge._response_active is True
    bridge.close()


async def test_pre_playback_cancel_skipped_while_tool_pending() -> None:
    """A pending function call owns the floor: no pre-playback cancel."""

    bridge, fake, events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    bridge._pending_tools = 1
    fake.sent.clear()
    await fake.incoming.put(_input_transcript_message("hey evie"))
    await asyncio.sleep(0.05)
    assert bridge._response_active is True
    assert fake.sent == []
    bridge.close()


async def test_client_interrupt_cancels_active_response_locally() -> None:
    """Barge-in stops speech locally; delivered-vs-generated rides the ReplyEvent.

    The Live API keeps only already-sent content in history when generation
    is interrupted, so there is no cancel verb and no item to truncate.
    """

    bridge, fake, events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    bridge._reply_text = "Your appointment is at four, and I also found three emails."
    bridge._turn_audio_bytes = 16_000 * 2 * 8
    fake.sent.clear()
    events.clear()
    result = await bridge.interrupt_for_user(
        reason="user_barge_in", audio_played_ms=2000, confidence=0.9, preroll_ms=320
    )
    assert result["latched"] is True
    assert fake.sent == []
    assert bridge._response_active is False
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert replies
    assert replies[0].interrupted is True
    assert replies[0].interruption_reason == "user_barge_in"
    assert "three emails" not in (replies[0].text or "")
    assert any(isinstance(event, LatencyEvent) for event in events)
    bridge.close()


async def test_late_pcm_after_interrupt_is_dropped() -> None:
    bridge, fake, events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=500)
    events[:] = [event for event in events if not isinstance(event, TtsChunkEvent)]
    pcm = b"\x11\x22" * 2400
    await fake.incoming.put(_model_audio_message(pcm))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, TtsChunkEvent) for event in events)
    await fake.incoming.put(json.dumps({"serverContent": {"turnComplete": True}}))
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
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    bridge.set_playback(True)
    fake.sent.clear()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not _mic_audio_sent(fake)
    await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=800)
    fake.sent.clear()
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(lambda: _mic_audio_sent(fake))
    bridge.close()


async def test_duplicate_interrupt_is_latched() -> None:
    bridge, fake, _events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    bridge._interrupt_in_flight = True
    result = await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=100)
    assert result["latched"] is False
    assert result["duplicate"] is True
    assert len(fake.sent) == 1 and "setup" in fake.sent[0]
    bridge.close()


async def test_live_session_barge_in_forwards_played_ms() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.gemini_live = GeminiLiveBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=session.now,
    )
    await session.gemini_live.start()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: session.gemini_live._response_active)
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
    # Local-only interrupt: nothing new goes upstream, speech state resets.
    assert fake.sent == []
    assert session.gemini_live._response_active is False
    assert session.gemini_live._user_input_open is True
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


async def test_zero_audio_interrupt_sends_nothing() -> None:
    bridge, fake, _events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    fake.sent.clear()
    # Monologue storm shape: response cancelled before any audio existed.
    result = await bridge.interrupt_for_user(
        reason="user_barge_in", audio_played_ms=0, confidence=0.5
    )
    assert result["latched"] is True
    assert fake.sent == []
    bridge.close()


async def test_monologue_storm_keeps_session_alive_and_leak_free() -> None:
    bridge, fake, events = await _bridge()
    for round_index in range(12):
        await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
        await _wait_until(lambda: bridge._response_active)
        bridge._reply_text = "Short response the owner talks over."
        bridge._turn_audio_bytes = 16_000 * 2 * 2
        await bridge.interrupt_for_user(
            reason="user_barge_in",
            audio_played_ms=150 + round_index,
            confidence=0.6,
        )
        # The server stops the cut generation; only then may the next
        # round's audio be accepted as a fresh generation.
        await fake.incoming.put(json.dumps({"serverContent": {"interrupted": True}}))
        await _wait_until(lambda: bridge._audio_accepting)
    # The storm never tore down the upstream socket by itself.
    assert bridge._ws is fake
    assert fake.closed is False
    # Local-only interrupts: nothing but setup went upstream, and every round
    # produced its interrupted-reply accounting.
    assert len(fake.sent) == 1 and "setup" in fake.sent[0]
    interrupted = [
        event for event in events if isinstance(event, ReplyEvent) and event.interrupted
    ]
    assert len(interrupted) == 12
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
                        "Your project has reached its configured enforced spend "
                        "limit. Raise the limit in the provider console to resume."
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
    assert not _mic_audio_sent(fake), "own-audio echo must not be forwarded to the provider"
    bridge.close()


async def test_mic_reopens_after_quarantine_window() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    fake.sent.clear()
    bridge._last_audio_emit_at = _time.monotonic() - 2.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(lambda: _mic_audio_sent(fake))
    bridge.close()


async def test_quarantine_blocks_even_after_response_done() -> None:
    import time as _time

    bridge, fake, _events = await _bridge()
    fake.sent.clear()
    # turnComplete already arrived but our chunks still sound in the room.
    bridge._response_active = False
    bridge._assistant_open = False
    bridge.set_playback(True)
    bridge._last_audio_emit_at = _time.monotonic()
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert not _mic_audio_sent(fake), "playback-lagging-turn-end echo must not be forwarded"
    bridge.close()

async def test_authoritative_playback_blocks_mic_across_all_queue_depths() -> None:
    """turnComplete + any queued client audio (250ms-2000ms) must NOT open mic.

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
        forwarded = _mic_audio_sent(fake)
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
    assert not _mic_audio_sent(fake), "acoustic tail after playback completion must stay gated"
    await asyncio.sleep(0.65)  # tail (0.5s) expires
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(lambda: _mic_audio_sent(fake))
    bridge.close()


async def test_long_form_diagnostic_is_opt_in_and_per_response() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    # OFF (production): plain client turn, no instructions prefix.
    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    await bridge.send_text("Explain the solar system for ninety seconds.")
    turn = next(item for item in fake.sent if "clientContent" in item)
    text = turn["clientContent"]["turns"][0]["parts"][0]["text"]
    assert text == "Explain the solar system for ninety seconds."
    bridge.close()
    await asyncio.sleep(0)

    # ON (diagnostic): long-form prefix present on the one turn.
    events.clear()
    fake2 = _FakeRealtime()

    async def connect2(url: str, additional_headers=None):
        del url, additional_headers
        return fake2

    bridge2 = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect2,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
        long_form_diagnostic=True,
    )
    await bridge2.start()
    fake2.sent.clear()
    await bridge2.send_text("Explain the solar system for ninety seconds.")
    turn2 = next(item for item in fake2.sent if "clientContent" in item)
    text2 = turn2["clientContent"]["turns"][0]["parts"][0]["text"]
    assert text2.startswith(
        "Give one continuous spoken explanation"
    ), "diagnostic turn must carry the long-form instructions"
    bridge2.close()


async def test_cancel_clears_pending_turn_so_receipt_can_speak() -> None:
    bridge, fake, _events = await _bridge()
    bridge._response_create_pending = True
    bridge._response_create_pending_at = time.monotonic()
    bridge._response_create_pending_key = "default"
    bridge._response_active = True
    fake.sent.clear()
    await bridge.cancel()
    assert bridge._response_create_pending is False
    fake.sent.clear()
    ok = await bridge.speak_life_record("Wish is a birthday film.")
    assert ok is True
    assert any("clientContent" in item for item in fake.sent)
    bridge.close()


async def test_server_interrupted_rearms_audio_for_new_generation() -> None:
    """The server interrupted signal ends the old generation AND re-arms audio.

    Without the re-arm, the acceptance gate closed by a local interrupt
    would deafen every later turn. Late audio *before* the signal is still
    dropped (see test_late_pcm_after_interrupt_is_dropped).
    """

    bridge, fake, events = await _bridge()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    await bridge.interrupt_for_user(reason="user_barge_in", audio_played_ms=100)
    assert bridge._audio_accepting is False
    await fake.incoming.put(json.dumps({"serverContent": {"interrupted": True}}))
    await _wait_until(lambda: bridge._audio_accepting is True)
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 4000))
    await _wait_until(lambda: bridge._response_active)
    await _wait_until(lambda: any(isinstance(event, TtsChunkEvent) for event in events))
    bridge.close()
