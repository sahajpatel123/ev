"""Provider-agnostic voice engines: local + HTTP providers and runtime wiring.

Tests exercise provider logic with injected fakes; tests that touch real
weights/binaries skip cleanly when the dependency or model is absent so
offline CI stays green.
"""

from __future__ import annotations

import base64
import json
import math
import shutil
import types

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport

from app.config import settings
from app.main import app
from app.voice.asr import FasterWhisperTranscriber, OpenAICompatTranscriber, get_transcriber
from app.voice.contracts import SpeechStyle, VoiceError
from app.voice.speaker import (
    ProfileSpeakerVerifier,
    SpeechBrainSpeakerVerifier,
    default_speaker_verifier,
)
from app.voice.tts import OpenAICompatSynthesizer, PiperSynthesizer, get_synthesizer
from app.voice.wake import (
    MultiStageWakeEngine,
    PhraseWakeEngine,
    PorcupineWakeEngine,
    SileroVadWakeEngine,
    default_wake_engine,
)

SAMPLE_A = b"owner-voice-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _speaker_vector(owner: bool) -> list[float]:
    vector = [0.0] * 192
    vector[0] = 1.0
    vector[1] = 1.0 if owner else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


def fake_speechbrain_encoder(sample: dict) -> list[float]:
    raw = base64.b64decode(sample["audio_b64"])
    return _speaker_vector(raw.startswith(b"owner-"))


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


async def test_openai_compat_transcriber_reads_audio_ref(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"clip.wav" in body
        return httpx.Response(200, json={"text": "Read from file"})

    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"fake-wav-bytes")
    client = httpx.AsyncClient(transport=MockTransport(handler))
    transcriber = OpenAICompatTranscriber(
        base_url="https://asr.test/v1",
        model="whisper-1",
        client=client,
    )
    transcript = await transcriber.transcribe(audio_ref=str(audio_file))
    assert transcript.text == "Read from file"
    await client.aclose()


async def test_openai_compat_tts_maps_speech_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
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
    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
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
            "samples": [
                {"audio_b64": b64(SAMPLE_A), "liveness_proof": "live"}
                for _ in range(5)
            ],
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


# --------------------------------------------------------------------------- #
# Speaker verification: SpeechBrain ECAPA-TDNN (mock encoder + skips)
# --------------------------------------------------------------------------- #


async def test_speechbrain_verifier_enrolls_and_verifies_owner() -> None:
    verifier = SpeechBrainSpeakerVerifier(
        sample_encoder=fake_speechbrain_encoder,
        threshold=0.72,
    )
    samples = [
        {"audio_b64": b64(b"owner-voice-sample-" + bytes([index]))}
        for index in range(5)
    ]
    payload = await verifier.enroll(samples, reason="test enrollment")
    assert payload["algorithm"] == "speechbrain-ecapa"
    assert payload["dim"] == 192
    assert payload["sample_count"] == 5
    decision = await verifier.verify(samples[0], enrolled_payload=payload)
    assert decision.verified is True
    assert decision.confidence > 0.99
    assert decision.algorithm == "speechbrain-ecapa"


async def test_speechbrain_verifier_rejects_other_speaker() -> None:
    verifier = SpeechBrainSpeakerVerifier(
        sample_encoder=fake_speechbrain_encoder,
        threshold=0.72,
    )
    samples = [
        {"audio_b64": b64(b"owner-voice-sample-" + bytes([index]))}
        for index in range(5)
    ]
    payload = await verifier.enroll(samples, reason="test enrollment")
    other = {"audio_b64": b64(b"other-speaker-sample-" * 40)}
    decision = await verifier.verify(other, enrolled_payload=payload)
    assert decision.verified is False
    assert decision.reason == "voiceprint mismatch"


async def test_speechbrain_verifier_requires_five_samples() -> None:
    verifier = SpeechBrainSpeakerVerifier(sample_encoder=fake_speechbrain_encoder)
    with pytest.raises(ValueError, match="at least 5"):
        await verifier.enroll([{"audio_b64": b64(b"x")}] * 4)


def test_default_speaker_verifier_selects_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voiceprint_provider", "hash")
    assert isinstance(default_speaker_verifier(), ProfileSpeakerVerifier)
    monkeypatch.setattr(settings, "voiceprint_provider", "speechbrain")
    assert isinstance(default_speaker_verifier(), SpeechBrainSpeakerVerifier)


def test_speechbrain_real_encoder_skips_without_install() -> None:
    pytest.importorskip("speechbrain")
    verifier = SpeechBrainSpeakerVerifier()
    assert verifier.name == "speechbrain-ecapa"


async def test_api_runtime_wires_config_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voiceprint_provider", "speechbrain")
    from app.api.training import _runtime as training_runtime
    from app.api.voice import _runtime as voice_runtime

    assert voice_runtime(None).verifier.name == "speechbrain-ecapa"  # type: ignore[arg-type]
    assert training_runtime(None).verifier.name == "speechbrain-ecapa"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Wake word: Porcupine + Silero VAD (fake engines + skips)
# --------------------------------------------------------------------------- #


class _FakePorcupine:
    def __init__(self, hit: bool) -> None:
        self.hit = hit
        self.processed = 0

    def process(self, frame: bytes) -> int:
        self.processed += 1
        return 0 if self.hit else -1


async def test_porcupine_wake_engine_detects_evie() -> None:
    engine = PorcupineWakeEngine(
        access_key="test-key",
        model_path="/tmp/evie.ppn",
        porcupine_factory=lambda **kwargs: _FakePorcupine(hit=True),
    )
    result = await engine.detect(
        frames=b"evie" + b"\x00" * 1020,
        sample_rate=16000,
        device_id="d1",
    )
    assert result.triggered is True
    assert result.wake_word == "evie"
    assert result.confidence == 0.98
    assert result.details["engine"] == "porcupine"

    miss_engine = PorcupineWakeEngine(
        access_key="test-key",
        model_path="/tmp/evie.ppn",
        porcupine_factory=lambda **kwargs: _FakePorcupine(hit=False),
    )
    missed = await miss_engine.detect(frames=b"\x00" * 1024)
    assert missed.triggered is False
    assert missed.confidence == 0.0


def test_porcupine_requires_key_and_model() -> None:
    engine = PorcupineWakeEngine(access_key="", model_path=None)
    with pytest.raises(RuntimeError, match="EV_VOICE_WAKE_ACCESS_KEY"):
        engine._create()
    engine = PorcupineWakeEngine(access_key="key", model_path=None)
    with pytest.raises(RuntimeError, match="EV_VOICE_WAKE_MODEL_PATH"):
        engine._create()


def test_porcupine_real_engine_skips_without_library() -> None:
    pytest.importorskip("pvporcupine")
    assert PorcupineWakeEngine.__name__ == "PorcupineWakeEngine"


async def test_silero_vad_gate_accepts_speech_and_rejects_silence() -> None:
    speech_engine = SileroVadWakeEngine(
        PhraseWakeEngine(),
        threshold=0.5,
        probability_fn=lambda pcm: 0.9,
    )
    result = await speech_engine.detect(frames=b"evie " + b"\x00" * 1019)
    assert result.triggered is True
    assert result.details["speech_probability"] == 0.9

    silence_engine = SileroVadWakeEngine(
        PhraseWakeEngine(),
        threshold=0.5,
        probability_fn=lambda pcm: 0.1,
    )
    rejected = await silence_engine.detect(frames=b"evie " + b"\x00" * 1019)
    assert rejected.triggered is False
    assert rejected.details["vad_rejected"] is True


def test_default_wake_engine_falls_back_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_wake_provider", "porcupine")
    monkeypatch.setattr(settings, "voice_wake_access_key", None)
    monkeypatch.setattr(settings, "voice_wake_model_path", None)
    assert isinstance(default_wake_engine(), MultiStageWakeEngine)

    monkeypatch.setattr(settings, "voice_wake_provider", "silero_vad")
    monkeypatch.setattr(settings, "voice_wake_vad_model_path", None)
    assert isinstance(default_wake_engine(), MultiStageWakeEngine)

    monkeypatch.setattr(settings, "voice_wake_provider", "phrase")
    assert isinstance(default_wake_engine(), MultiStageWakeEngine)


# --------------------------------------------------------------------------- #
# ASR: faster-whisper local provider + remote gate
# --------------------------------------------------------------------------- #


class _FakeWhisperModel:
    def __init__(self) -> None:
        self.captured = {}

    def transcribe(self, path, language="en", vad_filter=True):
        self.captured = {
            "path": path,
            "language": language,
            "vad_filter": vad_filter,
        }
        segment = types.SimpleNamespace(text="Remind me to call mom")
        info = types.SimpleNamespace(
            avg_logprob=-0.05,
            duration=2.5,
            language="en",
        )
        return iter([segment]), info


async def test_faster_whisper_transcriber_with_fake_model() -> None:
    fake_model = _FakeWhisperModel()
    transcriber = FasterWhisperTranscriber(
        model="tiny",
        model_factory=lambda name, **kwargs: fake_model,
    )
    transcript = await transcriber.transcribe(
        audio_b64=b64(b"fake-wav-bytes"),
        language="en",
    )
    assert transcript.text == "Remind me to call mom"
    assert transcript.provider == "faster_whisper"
    assert transcript.confidence == round(math.exp(-0.05), 4)
    assert transcript.duration_ms == 2500
    assert fake_model.captured["language"] == "en"
    assert fake_model.captured["vad_filter"] is True


async def test_faster_whisper_requires_audio() -> None:
    transcriber = FasterWhisperTranscriber(
        model="tiny",
        model_factory=lambda name, **kwargs: _FakeWhisperModel(),
    )
    with pytest.raises(VoiceError, match="ASR requires"):
        await transcriber.transcribe()


def test_faster_whisper_real_engine_skips_without_install() -> None:
    pytest.importorskip("faster_whisper")
    assert FasterWhisperTranscriber().name == "faster_whisper"


def test_remote_asr_gate_blocks_without_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_asr_provider", "openai_compat")
    monkeypatch.setattr(settings, "voice_asr_base_url", "https://asr.test/v1")
    monkeypatch.delenv("EV_ALLOW_REMOTE_ASR", raising=False)
    with pytest.raises(RuntimeError, match="EV_ALLOW_REMOTE_ASR"):
        get_transcriber()
    monkeypatch.setenv("EV_ALLOW_REMOTE_ASR", "true")
    assert get_transcriber().name == "openai_compat"


# --------------------------------------------------------------------------- #
# TTS: Piper local provider
# --------------------------------------------------------------------------- #


async def test_piper_synthesizer_maps_style_args() -> None:
    captured: dict = {}

    async def runner(argv: list[str], *, stdin: bytes) -> tuple[int, bytes]:
        captured["argv"] = argv
        captured["stdin"] = stdin
        output = argv[argv.index("--output_file") + 1]
        with open(output, "wb") as audio_file:
            audio_file.write(b"RIFF-fake-wav")
        return 0, b""

    synthesizer = PiperSynthesizer(
        model="en_US-lessac-medium.onnx",
        binary="piper",
        runner=runner,
    )
    style = SpeechStyle(urgency=0.8, warmth=0.4, brevity=0.9, mode="emergency")
    result = await synthesizer.synthesize("Check the deploy now", style=style)
    assert result.provider == "piper"
    assert result.content_type == "audio/wav"
    assert result.audio == b"RIFF-fake-wav"
    assert captured["stdin"] == b"Check the deploy now"
    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "en_US-lessac-medium.onnx"
    assert float(argv[argv.index("--length-scale") + 1]) < 1.0
    assert "--speaker" not in argv


def test_piper_factory_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_tts_provider", "piper")
    monkeypatch.setattr(settings, "voice_tts_model", "")
    with pytest.raises(RuntimeError, match="EV_VOICE_TTS_MODEL"):
        get_synthesizer()


def test_piper_real_engine_skips_without_binary() -> None:
    if shutil.which("piper") is None:
        pytest.skip("piper binary not installed")
    assert PiperSynthesizer().name == "piper"
