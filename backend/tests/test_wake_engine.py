"""Wake engine tests: phrase, multi-stage, real-engine text-hint rules, VAD."""

from __future__ import annotations

import array
import json
import wave
from pathlib import Path

import pytest

from app.voice.wake import (
    MultiStageWakeEngine,
    OpenWakeWordEngine,
    PhraseWakeEngine,
    PorcupineWakeEngine,
    SileroVadWakeEngine,
    WhisperPhraseWakeEngine,
    configured_wake_engine,
    default_wake_engine,
    set_default_wake_engine,
)

PCM16 = (
    array.array("h", [0] * 512 + [12000] * 512).tobytes()
    + b"evie"
    + array.array("h", [0] * 512).tobytes()
)


async def test_phrase_engine_matches_text_and_frames() -> None:
    engine = PhraseWakeEngine()
    hit = await engine.detect(text_hint="hey evie")
    assert hit.triggered
    assert hit.confidence == 0.98
    miss = await engine.detect(text_hint="remind me later")
    assert not miss.triggered
    frame_hit = await engine.detect(frames=b"evie")
    assert frame_hit.triggered
    frame_miss = await engine.detect(frames=b"nothing here")
    assert not frame_miss.triggered


async def test_multi_stage_requires_both_stages() -> None:
    engine = MultiStageWakeEngine(PhraseWakeEngine(), PhraseWakeEngine())
    hit = await engine.detect(text_hint="evie")
    assert hit.triggered
    assert hit.stage == "burst"
    assert hit.power_state == "burst"
    miss = MultiStageWakeEngine(PhraseWakeEngine(), PhraseWakeEngine())
    result = await miss.detect(text_hint="evie wake up")
    assert result.triggered


class FakePorcupine:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.processed = []
        self.calls = 0

    def process(self, frame):
        self.processed.append(frame)
        self.calls += 1
        return 0 if self.calls == 1 else -1


async def test_porcupine_never_delegates_to_text_matcher() -> None:
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakePorcupine(**kwargs)

    engine = PorcupineWakeEngine(
        access_key="test-key",
        model_path="/tmp/evie.ppn",
        porcupine_factory=factory,
    )
    # Text hint present: real engine still scans the supplied frames; the
    # string matcher must not decide the result.
    hit = await engine.detect(frames=PCM16, text_hint="not evie at all")
    assert hit.triggered
    assert "text_hint_present" in hit.details
    assert hit.details["engine"] == "porcupine"
    assert len(calls) == 1
    assert calls[0]["keyword_paths"] == ["/tmp/evie.ppn"]

    # Frames that contain "evie" bytes but no porcupine hit stay a miss.
    miss = await engine.detect(frames=b"evie", text_hint="evie")
    assert not miss.triggered
    assert miss.details["keyword_index"] is None


async def test_porcupine_requires_real_audio_even_with_text_hint() -> None:
    engine = PorcupineWakeEngine(
        access_key="test-key",
        model_path="/tmp/evie.ppn",
        porcupine_factory=lambda **kwargs: FakePorcupine(**kwargs),
    )
    with pytest.raises(ValueError, match="requires 'frames'"):
        await engine.detect(text_hint="evie")


async def test_silero_vad_gate_accepts_and_rejects() -> None:
    def high(pcm):
        return 0.99

    def low(pcm):
        return 0.01

    accepted = SileroVadWakeEngine(
        PhraseWakeEngine(),
        threshold=0.5,
        probability_fn=high,
    )
    result = await accepted.detect(frames=PCM16)
    assert result.triggered
    assert result.details["speech_probability"] == 0.99

    rejected = SileroVadWakeEngine(
        PhraseWakeEngine(),
        threshold=0.5,
        probability_fn=low,
    )
    result = await rejected.detect(frames=PCM16)
    assert not result.triggered
    assert result.details["vad_rejected"] is True


class FakeOpenWakeWordModel:
    def __init__(self, scores=None, **kwargs):
        self.scores = scores or {"evie": 0.9}
        self.kwargs = kwargs
        self.predict_calls = 0

    def predict(self, frame):
        self.predict_calls += 1
        return dict(self.scores)


async def test_openwakeword_engine_scores_real_model_output() -> None:
    pytest.importorskip("numpy")
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeOpenWakeWordModel(**kwargs)

    engine = OpenWakeWordEngine(
        model_path="/tmp/evie.onnx",
        threshold=0.5,
        model_factory=factory,
    )
    hit = await engine.detect(frames=PCM16, text_hint="fake hint")
    assert hit.triggered
    assert hit.confidence == pytest.approx(0.9)
    assert hit.details["verifier_enabled"] is False
    assert hit.details["text_hint_present"] is True
    assert calls[0]["wakeword_models"] == ["/tmp/evie.onnx"]

    engine_low = OpenWakeWordEngine(
        model_path="/tmp/evie.onnx",
        threshold=0.95,
        model_factory=factory,
    )
    miss = await engine_low.detect(frames=PCM16)
    assert not miss.triggered


async def test_openwakeword_engine_wires_verifier() -> None:
    pytest.importorskip("numpy")
    def factory(**kwargs):
        assert kwargs["custom_verifier_models"] == {"evie": "/tmp/verifier.pkl"}
        return FakeOpenWakeWordModel(scores={"evie": 0.7}, **kwargs)

    engine = OpenWakeWordEngine(
        model_path="/tmp/evie.onnx",
        verifier_path="/tmp/verifier.pkl",
        threshold=0.5,
        model_factory=factory,
    )
    result = await engine.detect(frames=PCM16)
    assert result.triggered
    assert result.details["verifier_enabled"] is True


async def test_openwakeword_engine_ignores_text_hint_without_audio() -> None:
    engine = OpenWakeWordEngine(
        model_path="/tmp/evie.onnx",
        model_factory=lambda **kwargs: FakeOpenWakeWordModel(**kwargs),
    )
    with pytest.raises(ValueError, match="requires 'frames'"):
        await engine.detect(text_hint="evie")


async def test_default_wake_engine_providers(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "voice_wake_provider", "phrase")
    assert default_wake_engine().name == "multi-stage"

    monkeypatch.setattr(settings, "voice_wake_provider", "openwakeword")
    monkeypatch.setattr(settings, "voice_wake_openwakeword_model_path", None)
    assert default_wake_engine().name == "multi-stage"

    monkeypatch.setattr(settings, "voice_wake_openwakeword_model_path", "/tmp/evie.onnx")
    engine = default_wake_engine()
    assert engine.name == "openwakeword"
    assert engine.threshold == pytest.approx(0.5)


def test_configured_wake_engine_uses_audio_fallback_without_exported_head(monkeypatch) -> None:
    import app.voice.wake as wake_module
    from app.config import settings

    set_default_wake_engine(None)
    monkeypatch.setattr(wake_module, "_whisper_phrase_wake", None)
    monkeypatch.setattr(settings, "voice_wake_provider", "openwakeword")
    monkeypatch.setattr(settings, "voice_wake_openwakeword_model_path", None)
    monkeypatch.setattr(settings, "voice_asr_provider", "faster_whisper")
    assert isinstance(configured_wake_engine(), WhisperPhraseWakeEngine)


def test_configured_wake_engine_hears_speech_when_asr_is_echo(monkeypatch) -> None:
    """Stock config is echo ASR + phrase wake. Byte-search cannot hear EVIE."""

    import app.voice.wake as wake_module
    from app.config import settings

    set_default_wake_engine(None)
    monkeypatch.setattr(wake_module, "_whisper_phrase_wake", None)
    monkeypatch.setattr(settings, "voice_wake_provider", "phrase")
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    assert isinstance(configured_wake_engine(), WhisperPhraseWakeEngine)


def _write_test_wav(path: Path, *, wake: bool, seconds: float = 0.5) -> None:
    payload = (b"evie" if wake else b"xxxx") + b"\x00\x00" * int(16000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(payload)


def test_wake_eval_sweep_and_distance_parsing() -> None:
    from app.audio.wake_eval import parse_distance, sweep_thresholds

    assert parse_distance(Path("evie-003-3m.wav")) == "3m"
    assert parse_distance(Path("evie-001-close.wav")) == "close"
    assert parse_distance(Path("clip.wav")) == "unspecified"

    ambient = [0.1] * 100
    clips = [0.98] * 30
    threshold, curve = sweep_thresholds(ambient, clips, hours_audio=12.0)
    assert threshold == 0.5
    entry = next(e for e in curve if e["threshold"] == threshold)
    assert entry["false_accepts_per_12h"] == 0.0
    assert entry["recall"] == 1.0


def test_wake_eval_skips_without_data(capsys) -> None:
    from app.audio import wake_eval

    assert wake_eval.main(["--dry-run"]) == 0
    assert "no eval artifact at" in capsys.readouterr().out


def test_wake_eval_writes_canonical_artifact_with_test_double(tmp_path) -> None:
    from app.audio import wake_eval

    clips = tmp_path / "clips"
    clips.mkdir()
    for index in range(1, 5):
        _write_test_wav(clips / f"evie-{index:03d}-close.wav", wake=True)
    _write_test_wav(clips / "evie-005-3m.wav", wake=True)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    _write_test_wav(ambient / "ambient-001.wav", wake=False, seconds=2.0)
    _write_test_wav(ambient / "ambient-002.wav", wake=False, seconds=2.0)
    report = tmp_path / "wake_reliability.json"

    assert (
        wake_eval.main(
            [
                "--held-out-dir",
                str(clips),
                "--ambient",
                str(ambient),
                "--test-double",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["schema"] == "ev.wake.eval.v1"
    assert data["producer"] == "ev-eval"
    assert data["provider"] == "test_double"
    assert data["degraded"] is True
    assert data["false_accepts_per_12h"] == 0.0
    assert data["recall"] == 1.0
    assert data["threshold"] == 0.5
    assert "threshold_curve" in data
    assert data["distance_breakdown"]["3m"]["recall"] == 1.0
    assert data["distance_breakdown"]["close"]["recall"] == 1.0
    assert data["replay_speed_x"] > 0


@pytest.mark.asyncio
async def test_whisper_phrase_detects_spoken_evie_from_transcriber() -> None:
    """Shipped WhisperPhraseWakeEngine.detect() on PCM, no text_hint."""

    from app.voice.contracts import Transcript
    from app.voice.wake import WhisperPhraseWakeEngine

    class _FakeAsr:
        async def transcribe(self, **kwargs):
            assert kwargs.get("audio_b64") or kwargs.get("audio_ref")
            return Transcript(text="hey evie", confidence=0.8, provider="faster_whisper")

    frames = b"\x00\x10" * 800
    hit = await WhisperPhraseWakeEngine(transcriber=_FakeAsr()).detect(
        frames=frames, sample_rate=16000
    )
    assert hit.triggered is True
    assert hit.details.get("transcript") == "hey evie"

    class _MissAsr:
        async def transcribe(self, **kwargs):
            return Transcript(
                text="what's the weather", confidence=0.8, provider="faster_whisper"
            )

    miss = await WhisperPhraseWakeEngine(transcriber=_MissAsr()).detect(
        frames=frames, sample_rate=16000
    )
    assert miss.triggered is False

    class _DegradedAsr:
        async def transcribe(self, **kwargs):
            from app.voice.contracts import Transcript

            return Transcript(
                text="",
                confidence=0.0,
                provider="faster_whisper",
                degraded=True,
                details={"reason": "weights missing"},
            )

    degraded = await WhisperPhraseWakeEngine(transcriber=_DegradedAsr()).detect(
        frames=frames, sample_rate=16000
    )
    assert degraded.triggered is False
    assert degraded.details["degraded"] is True
    assert "weights missing" in degraded.details["error"]

    class _EveryAsr:
        async def transcribe(self, **kwargs):
            return Transcript(text="every", confidence=0.7, provider="faster_whisper")

    alias = await WhisperPhraseWakeEngine(transcriber=_EveryAsr()).detect(
        frames=frames, sample_rate=16000
    )
    assert alias.triggered is True

    class _HallucinationAsr:
        def __init__(self, text: str, no_speech_prob: float | None = None) -> None:
            self.text = text
            self.no_speech_prob = no_speech_prob

        async def transcribe(self, **kwargs):
            details = {}
            if self.no_speech_prob is not None:
                details["no_speech_prob"] = self.no_speech_prob
            return Transcript(
                text=self.text,
                confidence=0.7,
                provider="faster_whisper",
                details=details,
            )

    # Whisper can hallucinate the wake word out of pure silence. Weak aliases
    # ("eve"/"evil") and the classic hallucination words must not wake when the
    # model reports no speech (no_speech_prob > Whisper's own 0.6 gate).
    for bogus, nsp in (("eve", 0.85), ("ivy", 0.9), ("avi", 0.9), ("a bee", 0.9)):
        miss_alias = await WhisperPhraseWakeEngine(
            transcriber=_HallucinationAsr(bogus, no_speech_prob=nsp)
        ).detect(frames=frames, sample_rate=16000)
        assert miss_alias.triggered is False, bogus

    # A weak alias with no reliability signal at all stays non-triggering:
    # unknown-source transcripts must not false-wake the system.
    unknown = await WhisperPhraseWakeEngine(
        transcriber=_HallucinationAsr("eve")
    ).detect(frames=frames, sample_rate=16000)
    assert unknown.triggered is False

    # Real speech evidence unlocks the weak aliases: faster-whisper base
    # transcribes the owner's actual spoken "EVIE" as "Eve"/"evil" (measured
    # on the voice-sample clips, no_speech_prob ~0.2-0.4).
    for real in ("Eve", "Hey Eve", "Okay Eve", "evil", "every", "evie here", "Hey EVIE here"):
        hit_alias = await WhisperPhraseWakeEngine(
            transcriber=_HallucinationAsr(real, no_speech_prob=0.3)
        ).detect(frames=frames, sample_rate=16000)
        assert hit_alias.triggered is True, real

    class _LongEvery:
        async def transcribe(self, **kwargs):
            return Transcript(
                text="and almost able to do every type of work",
                confidence=0.8,
                provider="faster_whisper",
                details={"no_speech_prob": 0.2},
            )

    buried = await WhisperPhraseWakeEngine(transcriber=_LongEvery()).detect(
        frames=frames, sample_rate=16000
    )
    assert buried.triggered is False


_WAKE_DATA = Path(__file__).resolve().parents[1] / "data" / "wake"
_EVIE_FIXTURE = _WAKE_DATA / "clips" / "evie-001-close.wav"
_NEG_FIXTURE = _WAKE_DATA / "negatives" / "negative-01.wav"


def _wav_pcm16(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        return wav.readframes(wav.getnframes())


class _FixtureTranscriber:
    """Consumes the WAV the detector actually sends; maps fixture PCM → phrase."""

    def __init__(self) -> None:
        self.positive = _wav_pcm16(_EVIE_FIXTURE)
        self.negative = _wav_pcm16(_NEG_FIXTURE)
        self.seen: list[int] = []

    def _pcm_from_kwargs(self, kwargs: dict) -> bytes:
        import base64
        import io

        audio_b64 = kwargs.get("audio_b64")
        audio_ref = kwargs.get("audio_ref")
        assert audio_b64 or audio_ref, "detect() must pass audio, not a text hint"
        if audio_b64:
            raw = base64.b64decode(audio_b64)
            with wave.open(io.BytesIO(raw), "rb") as wav:
                return wav.readframes(wav.getnframes())
        with wave.open(str(audio_ref), "rb") as wav:
            return wav.readframes(wav.getnframes())

    async def transcribe(self, **kwargs):
        from app.voice.contracts import Transcript

        pcm = self._pcm_from_kwargs(kwargs)
        self.seen.append(len(pcm))
        pos = self.positive
        if pcm[: len(pos)] == pos or pos[: min(len(pos), len(pcm))] == pcm[: min(len(pos), len(pcm))]:
            return Transcript(
                text="EVIE",
                confidence=0.9,
                provider="fixture",
                details={"no_speech_prob": 0.25},
            )
        return Transcript(
            text="room tone",
            confidence=0.2,
            provider="fixture",
            details={"no_speech_prob": 0.85},
        )


@pytest.mark.asyncio
async def test_phrase_engine_does_not_trigger_on_spoken_evie_bytes() -> None:
    """Spoken EVIE WAV must not contain the ASCII bytes the phrase engine hunts."""

    assert _EVIE_FIXTURE.is_file()
    pcm = _wav_pcm16(_EVIE_FIXTURE)
    assert b"evie" not in pcm.lower()
    engine = PhraseWakeEngine()
    miss = await engine.detect(frames=pcm, sample_rate=16000)
    assert miss.triggered is False


@pytest.mark.asyncio
async def test_whisper_phrase_detects_evie_fixture_not_negative() -> None:
    """Shipped WhisperPhraseWakeEngine.detect on the in-repo spoken clips."""

    assert _EVIE_FIXTURE.is_file()
    assert _NEG_FIXTURE.is_file()
    asr = _FixtureTranscriber()
    engine = WhisperPhraseWakeEngine(transcriber=asr)
    await engine.warmup()

    import time

    pos_pcm = _wav_pcm16(_EVIE_FIXTURE)
    t0 = time.perf_counter()
    hit = await engine.detect(frames=pos_pcm, sample_rate=16000, audio_ref=str(_EVIE_FIXTURE))
    first_ms = (time.perf_counter() - t0) * 1000.0
    assert hit.triggered is True
    assert hit.details.get("transcript") == "EVIE"
    assert first_ms < 2500.0, first_ms

    t1 = time.perf_counter()
    again = await engine.detect(frames=pos_pcm, sample_rate=16000)
    second_ms = (time.perf_counter() - t1) * 1000.0
    assert again.triggered is True
    assert second_ms < 2500.0, second_ms

    neg = await engine.detect(
        frames=_wav_pcm16(_NEG_FIXTURE),
        sample_rate=16000,
        audio_ref=str(_NEG_FIXTURE),
    )
    assert neg.triggered is False
    assert asr.seen, "detector must have fed the fixture audio to the transcriber"
