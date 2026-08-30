"""Real TTS engines: Kokoro/Chatterbox factory, degradation, remote gate, audio."""

from __future__ import annotations

import asyncio
import io
import math
import wave

import httpx
import pytest

from app.config import settings
from app.voice.contracts import SpeechStyle
from app.voice.tts import (
    ChatterboxSynthesizer,
    EdgeTTSSynthesizer,
    KokoroSynthesizer,
    MetaSynthesizer,
    OpenAICompatSynthesizer,
    get_synthesizer,
    spoken_fallback_voice,
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


def test_shipped_voice_is_softer_female() -> None:
    synth = KokoroSynthesizer()
    # Local fallback voice: warm British female (movie E.V. / Naomi Watts
    # profile), explicitly not the older robotic-sounding bf_emma.
    assert synth.voice == "bf_alice", synth.voice
    assert not synth.voice.startswith("am_"), synth.voice
    assert synth.voice != "bf_emma"
    say = spoken_fallback_voice()
    assert say
    assert say.lower() != "samantha"


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


class _FakeEdgeCommunicate:
    def __init__(
        self,
        payload: bytes | None = b"fake-mp3",
        *,
        fail: bool = False,
        hang: bool = False,
    ) -> None:
        self._payload = payload
        self._fail = fail
        self._hang = hang

    def stream(self):
        async def _stream():
            if self._hang:
                await asyncio.sleep(30)
                return
            if self._fail:
                raise RuntimeError("network down")
            if self._payload:
                yield {"type": "audio", "data": self._payload}
        return _stream()


def test_edge_tts_factory_returns_single_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    # One voice, always: the edge provider must NOT wrap a cross-engine fallback.
    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    synth = get_synthesizer()
    assert isinstance(synth, EdgeTTSSynthesizer)
    assert synth.voice == "en-GB-SoniaNeural"


async def test_edge_tts_synthesizes_mp3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")

    def factory(text, voice, rate):
        return _FakeEdgeCommunicate(b"mp3-bytes")

    synth = EdgeTTSSynthesizer(voice="en-GB-SoniaNeural", communicate_factory=factory)
    result = await synth.synthesize("Hello", style=SpeechStyle(warmth=0.9))
    assert not result.degraded
    assert result.audio == b"mp3-bytes"
    assert result.content_type == "audio/mpeg"
    assert result.details["voice"] == "en-GB-SoniaNeural"
    assert "rate" in result.details


async def test_edge_tts_degrades_when_remote_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EV_ALLOW_REMOTE_TTS", raising=False)
    synth = EdgeTTSSynthesizer()
    result = await synth.synthesize("Hello", style=SpeechStyle())
    assert result.degraded
    assert result.details["reason"] == "remote_processing_denied"


async def test_edge_tts_degrades_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
    synth = EdgeTTSSynthesizer(
        communicate_factory=lambda *a, **k: _FakeEdgeCommunicate(fail=True),
        retries=0,
    )
    result = await synth.synthesize("Hello", style=SpeechStyle())
    assert result.degraded
    assert "network down" in result.details["reason"]


async def test_edge_tts_retries_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
    calls = {"n": 0}

    def factory(text, voice, rate):
        calls["n"] += 1
        return _FakeEdgeCommunicate(fail=(calls["n"] == 1))

    synth = EdgeTTSSynthesizer(
        voice="en-GB-SoniaNeural", communicate_factory=factory, retries=2, timeout=5.0
    )
    result = await synth.synthesize("Hello", style=SpeechStyle())
    assert not result.degraded
    assert result.audio == b"fake-mp3"
    assert calls["n"] == 2  # one transient failure, then success


async def test_edge_tts_times_out_after_hung_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
    synth = EdgeTTSSynthesizer(
        communicate_factory=lambda *a, **k: _FakeEdgeCommunicate(hang=True),
        retries=0,
        timeout=0.05,
    )
    result = await synth.synthesize("Hello", style=SpeechStyle())
    assert result.degraded
    assert result.details["reason"] == "tts_timeout"
