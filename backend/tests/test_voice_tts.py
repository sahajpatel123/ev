"""Real TTS engines: Kokoro/Chatterbox factory, degradation, remote gate, audio."""

from __future__ import annotations

import io
import math
import wave

import httpx
import pytest

from app.config import settings
from app.voice.contracts import SpeechStyle
from app.voice.tts import (
    ChatterboxSynthesizer,
    KokoroSynthesizer,
    MetaSynthesizer,
    OpenAICompatSynthesizer,
    get_synthesizer,
)


class FakeKokoroPipeline:
    def __call__(self, text: str, voice: str, speed: float):
        rate = 24000
        chunk = (
            [
                0.2 * math.sin(2 * math.pi * 440 * index / rate)
                for index in range(int(rate * 0.25))
            ]
        )
        yield ("hello", "h\u025blo\u028a", chunk)


def test_kokoro_factory_selects_real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_tts_provider", "kokoro")
    assert isinstance(get_synthesizer(), KokoroSynthesizer)
    monkeypatch.setattr(settings, "voice_tts_provider", "chatterbox")
    assert isinstance(get_synthesizer(), ChatterboxSynthesizer)
    monkeypatch.setattr(settings, "voice_tts_provider", "meta")
    assert isinstance(get_synthesizer(), MetaSynthesizer)


async def test_kokoro_degrades_without_weights() -> None:
    synthesizer = KokoroSynthesizer(model_path="/nonexistent/kokoro-82m-int8.onnx")
    result = await synthesizer.synthesize("Hello", style=SpeechStyle())
    assert result.degraded is True
    assert result.audio is None
    assert result.audio_ref is None
    assert result.provider == "kokoro-82m"


async def test_kokoro_synthesizes_wav_with_fake_pipeline() -> None:
    synthesizer = KokoroSynthesizer(
        model_path="/models/kokoro.onnx",
        pipeline_factory=lambda path, voices: FakeKokoroPipeline(),
    )
    style = SpeechStyle(urgency=0.8, warmth=0.4, brevity=0.9)
    result = await synthesizer.synthesize("Check the deploy now", style=style)
    assert result.degraded is False
    assert result.content_type == "audio/wav"
    assert result.audio is not None
    assert result.audio.startswith(b"RIFF")
    assert result.duration_ms is not None and result.duration_ms > 100
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1


async def test_chatterbox_degrades_without_runtime() -> None:
    synthesizer = ChatterboxSynthesizer(model_name="chatterbox-nano")
    result = await synthesizer.synthesize("Hello", style=SpeechStyle())
    assert result.degraded is True
    assert result.audio is None
    assert result.provider == "chatterbox-nano"


async def test_meta_double_reports_no_fake_duration() -> None:
    result = await MetaSynthesizer().synthesize("Hello", style=SpeechStyle())
    assert result.ssml is not None
    assert result.duration_ms is None


def test_remote_tts_gate_matches_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_tts_provider", "openai_compat")
    monkeypatch.setattr(settings, "voice_tts_base_url", "https://tts.test/v1")
    monkeypatch.delenv("EV_ALLOW_REMOTE_TTS", raising=False)
    with pytest.raises(RuntimeError, match="EV_ALLOW_REMOTE_TTS"):
        get_synthesizer()
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
    assert get_synthesizer().name == "openai_compat"


async def test_openai_compat_synthesizer_refuses_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EV_ALLOW_REMOTE_TTS", raising=False)
    synthesizer = OpenAICompatSynthesizer(
        base_url="https://tts.test/v1",
        client=httpx.AsyncClient(),
    )
    with pytest.raises(RuntimeError, match="EV_ALLOW_REMOTE_TTS"):
        await synthesizer.synthesize("Hello", style=SpeechStyle())
    await synthesizer._client.aclose()


async def test_openai_compat_synthesizer_degrades_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    synthesizer = OpenAICompatSynthesizer(
        base_url="https://tts.test/v1",
        client=client,
    )
    result = await synthesizer.synthesize("Hello", style=SpeechStyle())
    assert result.degraded is True
    assert result.audio is None
    assert result.audio_ref is None
    assert result.provider == "openai_compat"
    assert "remote-http-503" in result.details["reason"]
    await client.aclose()


async def test_openai_compat_does_not_fabricate_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"fake-mp3", headers={"content-type": "audio/mpeg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    synthesizer = OpenAICompatSynthesizer(
        base_url="https://tts.test/v1",
        model="gpt-4o-mini-tts",
        fmt="mp3",
        client=client,
    )
    result = await synthesizer.synthesize("Hello", style=SpeechStyle())
    assert result.audio == b"fake-mp3"
    assert result.duration_ms is None
    assert result.degraded is False
    await client.aclose()
