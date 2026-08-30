"""Metadata-only health coverage for the production realtime bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.voice.live.grok_voice import GrokVoiceBridge
from app.voice.live.layer import register_live, reset_live_registry, unregister_live


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


def test_voice_health_tracks_audio_boundaries_without_content() -> None:
    async def run() -> None:
        events: list = []
        bridge = GrokVoiceBridge(
            on_event=lambda event: events.append(event) or asyncio.sleep(0),
            api_key="test",
            provider="openai",
        )
        bridge._ws = FakeWS()
        bridge._input_audio_task = asyncio.create_task(bridge._input_audio_loop())

        await bridge.append_pcm(b"\x01\x00" * 800)
        for _ in range(20):
            if bridge.voice_health_snapshot()["mic_frames_forwarded"]:
                break
            await asyncio.sleep(0)

        await bridge._handle_upstream(
            {"type": "session.updated", "session": {"tools": []}}
        )
        await bridge._handle_upstream({"type": "input_audio_buffer.speech_started"})
        await bridge._handle_upstream({"type": "input_audio_buffer.speech_stopped"})
        await bridge._handle_upstream(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello",
            }
        )
        bridge.note_owner_turn(turn_id="turn-1")
        bridge.note_turn_gate(turn_id="turn-1")
        bridge.note_turn_result(ok=True)
        await bridge._send({"type": "response.create"})

        health = bridge.voice_health_snapshot()
        assert health["client_socket_connected"] is True
        assert health["realtime_session_accepted"] is True
        assert health["mic_frames_received"] == 1
        assert health["mic_frames_forwarded"] == 1
        assert health["mic_bytes_forwarded"] > 0
        assert health["speech_started"] == 1
        assert health["speech_stopped"] == 1
        assert health["transcription_completed"] == 1
        assert health["final_transcript_emitted"] == 1
        assert health["turn_gate_invoked"] == 1
        assert health["turn_gate_ok"] == 1
        assert health["response_create_sent"] == 1
        assert "hello" not in str(health)
        bridge.close()

    asyncio.run(run())


def test_stale_reconnect_cleanup_cannot_remove_new_live_session() -> None:
    reset_live_registry()
    older = SimpleNamespace(session_id="same-session", device_id="same-device")
    newer = SimpleNamespace(session_id="same-session", device_id="same-device")
    try:
        register_live(older)
        register_live(newer)
        unregister_live(older)

        from app.voice.live.layer import active_lives

        assert active_lives() == [newer]
        unregister_live(newer)
        assert active_lives() == []
    finally:
        reset_live_registry()
