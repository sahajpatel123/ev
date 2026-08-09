"""Provider-agnostic ASR/TTS: OpenAI-compatible HTTP providers + runtime wiring."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport

from app.config import settings
from app.main import app
from app.voice.asr import OpenAICompatTranscriber, get_transcriber
from app.voice.contracts import SpeechStyle
from app.voice.tts import OpenAICompatSynthesizer, get_synthesizer

SAMPLE_A = b"owner-voice-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_openai_compat_transcriber_posts_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.read()
        assert b"whisper-1" in body
        assert b"voice.wav" in body
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={"text": "Remind me to call mom", "confidence": 0.98},
        )

    client = httpx.AsyncClient(transport=MockTransport(handler))
    transcriber = OpenAICompatTranscriber(
        base_url="https://asr.test/v1",
        api_key="test-key",
        model="whisper-1",
        client=client,
    )
    transcript = await transcriber.transcribe(
        audio_b64=b64(b"fake-wav-bytes"),
        language="en",
    )
    assert transcript.text == "Remind me to call mom"
    assert transcript.confidence == 0.98
    assert transcript.provider == "openai_compat"
    await client.aclose()


async def test_openai_compat_tts_maps_speech_style() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"\x00\xffaudio-bytes",
            headers={"content-type": "audio/mpeg"},
        )

    client = httpx.AsyncClient(transport=MockTransport(handler))
    synthesizer = OpenAICompatSynthesizer(
        base_url="https://tts.test/v1",
        api_key="test-key",
        model="gpt-4o-mini-tts",
        voice="nova",
        fmt="mp3",
        client=client,
    )
    style = SpeechStyle(urgency=0.8, warmth=0.4, brevity=0.9, mode="emergency")
    result = await synthesizer.synthesize("Check the deploy now", style=style)
    assert captured["path"] == "/v1/audio/speech"
    payload = captured["payload"]
    assert payload["model"] == "gpt-4o-mini-tts"
    assert payload["voice"] == "nova"
    assert payload["speed"] == 1.13
    assert "fast and clipped" in payload["instructions"]
    assert "emergency" in payload["instructions"]
    assert result.audio == b"\x00\xffaudio-bytes"
    assert result.content_type == "audio/mp3"
    await client.aclose()


async def test_provider_factories_default_and_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    monkeypatch.setattr(settings, "voice_tts_provider", "meta")
    assert get_transcriber().name == "echo"
    assert get_synthesizer().name == "meta"

    monkeypatch.setattr(settings, "voice_asr_provider", "openai_compat")
    monkeypatch.setattr(settings, "voice_asr_base_url", None)
    with pytest.raises(RuntimeError, match="EV_VOICE_ASR_BASE_URL"):
        get_transcriber()


async def test_voice_utterance_with_http_asr_and_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full wake → verify → listen → reply using HTTP ASR/TTS providers."""
    tts_calls: list[dict] = []

    async def asr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"text": "Open the garage", "confidence": 0.97},
        )

    def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_calls.append(json.loads(request.content))
        return httpx.Response(200, content=b"evie-speaks")

    from app.voice.lifecycle import VoiceRuntime

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            transcriber=OpenAICompatTranscriber(
                base_url="https://asr.test/v1",
                client=httpx.AsyncClient(transport=MockTransport(asr_handler)),
            ),
            synthesizer=OpenAICompatSynthesizer(
                base_url="https://tts.test/v1",
                client=httpx.AsyncClient(transport=MockTransport(tts_handler)),
            ),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)

    resp = await client_post(
        "/v1/training/consent", {"track": "voice_enrollment"}
    )
    assert resp.status_code == 201
    resp = await client_post(
        "/v1/voice/enroll",
        {
            "samples": [{"audio_b64": b64(SAMPLE_A)} for _ in range(5)],
            "reason": "provider test",
        },
    )
    assert resp.status_code == 201
    resp = await client_post(
        "/v1/voice/wake", {"device_id": "mac-provider", "text_hint": "hey evie"}
    )
    assert resp.status_code == 201
    wake = resp.json()
    resp = await client_post(
        "/v1/voice/verify",
        {
            "session_id": wake["session_id"],
            "nonce": wake["challenge_nonce"],
            "phrase": wake["challenge_phrase"],
            "samples": [b64(SAMPLE_A)],
            "liveness_proof": "live",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True

    resp = await client_post(
        "/v1/voice/utterance",
        {"session_id": wake["session_id"], "audio_b64": b64(b"real-speech.wav")},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["transcript"] == "Open the garage"
    assert payload["reply"]
    assert payload["tts"]["provider"] == "openai_compat"
    assert tts_calls
    assert tts_calls[0]["input"] == payload["reply"]


async def client_post(path: str, body: dict) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        return await client.post(path, json=body)
