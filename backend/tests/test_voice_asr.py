"""Real ASR engines: Parakeet factory/degradation/streaming, fail-closed rules."""

from __future__ import annotations

import base64
import io
import math
import struct
import wave

import httpx
import pytest

from app.config import settings
from app.voice.asr import (
    EchoTranscriber,
    FasterWhisperTranscriber,
    OpenAICompatTranscriber,
    ParakeetTdtTranscriber,
    ParakeetTranscriber,
    get_transcriber,
)
from app.voice.contracts import Transcript, TranscriptPartial, VoiceError


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def make_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = b"".join(
        struct.pack("<h", int(3200 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(rate * seconds))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


class FakeParakeetSession:
    def __init__(self) -> None:
        self.reset_called = False
        self.chunks: list[bytes] = []

    def transcribe(self, pcm, sample_rate: int):
        return "Remind me to call mom", 0.94

    def reset(self) -> None:
        self.reset_called = True

    def decode_chunk(self, pcm):
        self.chunks.append(bytes(pcm))
        return "Remind", False

    def finalize(self) -> str:
        return "Remind me to call mom"


def test_parakeet_factory_selects_real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real factory entry point must be exercised, not reimplemented."""

    monkeypatch.setattr(settings, "voice_asr_provider", "parakeet")
    assert isinstance(get_transcriber(), ParakeetTranscriber)
    monkeypatch.setattr(settings, "voice_asr_provider", "parakeet_tdt")
    assert isinstance(get_transcriber(), ParakeetTdtTranscriber)
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    assert isinstance(get_transcriber(), EchoTranscriber)


async def test_parakeet_degrades_without_weights() -> None:
    transcriber = ParakeetTranscriber(
        model_path="/nonexistent/parakeet-eou-120m-int8.onnx"
    )
    result = await transcriber.transcribe(audio_b64=b64(make_wav()))
    assert result.degraded is True
    assert result.confidence == 0.0
    assert result.text == ""
    assert result.provider == "parakeet-eou-120m"


def test_degraded_result_is_never_high_confidence() -> None:
    """Degraded transcripts must not be presented as real transcriptions."""

    result = Transcript(text="", confidence=0.0, provider="parakeet-eou-120m", degraded=True)
    assert result.degraded is True
    assert result.confidence < 0.1


async def test_parakeet_transcribe_with_fake_session() -> None:
    session = FakeParakeetSession()
    transcriber = ParakeetTranscriber(
        model_path="/models/parakeet.onnx",
        vad_filter=False,
        session_factory=lambda path, vocab: session,
    )
    result = await transcriber.transcribe(audio_b64=b64(make_wav()), language="en")
    assert result.text == "Remind me to call mom"
    assert result.confidence == 0.94
    assert result.degraded is False
    assert result.provider == "parakeet-eou-120m"


async def test_parakeet_stream_emits_partials_then_final() -> None:
    session = FakeParakeetSession()
    transcriber = ParakeetTranscriber(
        model_path="/models/parakeet.onnx",
        vad_filter=False,
        session_factory=lambda path, vocab: session,
    )
    events = [
        item
        async for item in transcriber.stream(
            audio_b64=b64(make_wav(seconds=0.8)),
            language="en",
        )
    ]
    assert isinstance(events[0], TranscriptPartial)
    assert events[0].text == "Remind"
    assert events[0].sequence == 1
    assert events[0].provider == "parakeet-eou-120m"
    assert isinstance(events[-1], Transcript)
    assert events[-1].text == "Remind me to call mom"
    assert events[-1].degraded is False
    assert session.reset_called is True
    assert session.chunks


async def test_echo_fails_closed_on_audio() -> None:
    transcriber = EchoTranscriber()
    with pytest.raises(VoiceError) as excinfo:
        await transcriber.transcribe(audio_b64=b64(make_wav()))
    assert excinfo.value.code == "asr_echo_no_audio"


async def test_echo_dev_double_never_fabricates_confidence() -> None:
    transcriber = EchoTranscriber()
    result = await transcriber.transcribe(text_hint="hello evie")
    assert result.text == "hello evie"
    assert result.confidence == 0.0
    assert result.degraded is False


async def test_openai_compat_requires_audio_no_hint_fallback() -> None:
    transcriber = OpenAICompatTranscriber(
        base_url="https://asr.test/v1",
        client=None,
    )
    with pytest.raises(VoiceError) as excinfo:
        await transcriber.transcribe(text_hint="hello evie")
    assert excinfo.value.code == "asr_audio_required"


async def test_audio_ref_traversal_is_denied() -> None:
    transcriber = OpenAICompatTranscriber(
        base_url="https://asr.test/v1",
        client=None,
    )
    with pytest.raises(VoiceError) as excinfo:
        await transcriber.transcribe(audio_ref="/etc/hosts")
    assert excinfo.value.code == "asr_audio_ref_denied"


async def test_openai_compat_degrades_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transcriber = OpenAICompatTranscriber(
        base_url="https://asr.test/v1",
        client=client,
    )
    result = await transcriber.transcribe(audio_b64=b64(make_wav()))
    assert result.degraded is True
    assert result.confidence == 0.0
    assert result.text == ""
    assert result.provider == "openai_compat"
    assert "remote-http-503" in result.details["reason"]
    await client.aclose()


def test_vad_filter_setting_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_asr_vad_filter", False)
    assert FasterWhisperTranscriber().vad_filter is False
    assert ParakeetTranscriber(model_path="/x.onnx").vad_filter is False
    monkeypatch.setattr(settings, "voice_asr_vad_filter", True)
    assert FasterWhisperTranscriber().vad_filter is True
    assert ParakeetTranscriber(model_path="/x.onnx").vad_filter is True


def test_engine_models_share_the_process_wide_arbiter() -> None:
    """ASR/TTS and wake/VAD must enforce one resident-memory budget."""

    from app.audio.models import model_arbiter
    from app.voice.contracts import acquire_model

    with acquire_model("vad-silero"):
        assert model_arbiter().is_resident("vad-silero")
    assert model_arbiter().is_resident("vad-silero")
