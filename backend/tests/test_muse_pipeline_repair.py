from __future__ import annotations

import base64
import io
import math
import struct
import wave
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _wav_24khz(seconds: float = 0.2) -> bytes:
    rate = 24_000
    frames = b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(rate * seconds))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_live_tts_container_is_normalized_to_declared_pcm() -> None:
    from app.voice.live.audio import normalize_live_audio

    normalized = await normalize_live_audio(
        _wav_24khz(),
        content_type="audio/wav",
        sample_rate=None,
    )

    assert normalized is not None
    pcm, content_type, sample_rate, duration_ms = normalized
    assert content_type == "audio/pcm"
    assert sample_rate == 16_000
    assert not pcm.startswith(b"RIFF")
    assert len(pcm) % 2 == 0
    assert 180 <= duration_ms <= 220


@pytest.mark.asyncio
async def test_live_session_emits_pcm_not_tts_container() -> None:
    from app.voice.live.events import TtsChunkEvent
    from app.voice.live.session import LiveSession

    live = LiveSession()
    await live.emit(
        TtsChunkEvent(
            at_ms=0,
            index=0,
            text="Hello",
            audio_b64=base64.b64encode(_wav_24khz()).decode("ascii"),
            content_type="audio/wav",
            provider="edge_tts",
        )
    )
    event = live.outbound.get_nowait()

    assert event.content_type == "audio/pcm"
    assert event.sample_rate == 16_000
    assert not base64.b64decode(event.audio_b64).startswith(b"RIFF")
    live.close()


def test_muse_phone_audio_cannot_silently_route_to_openai_webrtc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.device_gateway.webrtc_live import resolve_phone_audio_backend

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "openai_api_key", "legacy-key-must-not-win")

    assert resolve_phone_audio_backend("webrtc_strict") == "pcm_ws"
    assert resolve_phone_audio_backend("webrtc") == "pcm_ws"
    assert resolve_phone_audio_backend("auto") == "pcm_ws"


@pytest.mark.asyncio
async def test_trusted_text_conversation_gets_real_chat_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.device_gateway import pipeline as gateway_pipeline

    async def canonical(*args, **kwargs):
        return {
            "ok": True,
            "conversational": True,
            "reply": None,
            "route": "CONVERSATION",
            "operation": "UNKNOWN",
        }

    thread_id = uuid4()

    async def resolve_thread(*args, **kwargs):
        return SimpleNamespace(id=thread_id)

    async def chat(*args, **kwargs):
        return {
            "result": SimpleNamespace(
                text="I am here.",
                model="muse-spark-1.3-contributor",
            ),
            "conversation_id": str(thread_id),
            "context_tokens": 42,
            "context_depth": "standard",
            "request_id": "chat-request",
            "memory_deltas": [],
            "provenance": [],
        }

    monkeypatch.setattr(gateway_pipeline, "run_trusted_device_turn", canonical)
    monkeypatch.setattr("app.ev.assistant.resolve_live_thread", resolve_thread)
    monkeypatch.setattr("app.api.core.run_chat_pipeline", chat)

    device = SimpleNamespace(id=uuid4(), name="Owner iPhone")
    result = await gateway_pipeline.run_trusted_device_text(
        SimpleNamespace(),
        device=device,
        text="Are you there?",
        idempotency_key="turn-1",
    )

    assert result["reply"] == "I am here."
    assert result["model"] == "muse-spark-1.3-contributor"
    assert result["conversational"] is True


def test_pwa_uses_server_selected_muse_lane_and_keeps_encoded_fallback() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "clients" / "pwa" / "app.js").read_text()
    webrtc = (root / "clients" / "pwa" / "webrtc.js").read_text()
    live_open = (root / "app" / "device_gateway" / "api.py").read_text()
    assert 'media_backend: "webrtc_strict"' in source
    assert "playEncodedFallback(msg, gen)" in source
    # Muse Voice clocks ingress at realtime: worklet quanta must be batched
    # to steady 20 ms frames, never sent per-quantum (slower-than-realtime).
    assert "FRAME_SAMPLES" in source
    assert "TARGET_RATE * 0.02" in source
    # Non-fatal ASR errors must surface as captions, not silent Listening.
    assert 'code.indexOf("asr") === 0' in source
    assert "await rtc.start(opened" in source
    assert "opened.client_generation" in webrtc
    assert "await this.onCamera({" in webrtc
    assert 'type === "response.output_audio.done"' in webrtc
    assert '"client_generation": int(data.client_generation or 0)' in live_open
