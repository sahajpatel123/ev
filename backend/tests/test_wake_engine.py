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
    default_wake_engine,
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
