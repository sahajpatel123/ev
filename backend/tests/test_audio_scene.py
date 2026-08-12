"""Tests for the local VAD/feature audio-scene classifier and collector wiring."""

from __future__ import annotations

import math
import random
import struct
import wave
from io import BytesIO

import pytest

from app.audio.scene import (
    YamNetSceneClassifier,
    _tone_score_numpy,
    _tone_score_pure,
    classify_wav,
)
from clients.collectors.audio import audio_scene


def _wav_bytes_width(samples: list[int], width: int, rate: int = 16000) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        if width == 1:
            frames = bytes(
                max(0, min(255, int((s / 32768) * 127 + 128))) for s in samples
            )
        elif width == 2:
            frames = b"".join(struct.pack("<h", max(-32768, min(32767, s))) for s in samples)
        else:
            frames = b"".join(
                int(max(-8388608, min(8388607, s))).to_bytes(3, "little", signed=True)
                for s in samples
            )
        wav.writeframes(frames)
    return buffer.getvalue()


def _wav_bytes(samples: list[int], rate: int = 16000) -> bytes:
    return _wav_bytes_width(samples, 2, rate)


def _silence(duration_s: float = 1.0, rate: int = 16000) -> bytes:
    return _wav_bytes([0] * int(rate * duration_s), rate)


def _tone(freq: float = 440.0, duration_s: float = 1.0, rate: int = 16000) -> bytes:
    samples = [
        int(12000 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(int(rate * duration_s))
    ]
    return _wav_bytes(samples, rate)


def _noise(duration_s: float = 1.0, rate: int = 16000, seed: int = 7) -> bytes:
    rng = random.Random(seed)
    return _wav_bytes([rng.randint(-8000, 8000) for _ in range(int(rate * duration_s))], rate)


def _speech_like(duration_s: float = 2.0, rate: int = 16000) -> bytes:
    rng = random.Random(11)
    samples: list[int] = []
    block = int(rate * 0.2)
    while len(samples) < int(rate * duration_s):
        if len(samples) // block % 2 == 0:
            samples.extend(rng.randint(-6000, 6000) for _ in range(block))
        else:
            samples.extend(0 for _ in range(block))
    return _wav_bytes(samples[: int(rate * duration_s)], rate)


def test_classifies_silence_tone_noise_and_speech_like() -> None:
    silence = classify_wav(_silence())
    assert silence["scene"] == "silence"
    assert silence["confidence"] >= 0.8
    assert silence["in_call"] is False

    tone = classify_wav(_tone())
    assert tone["scene"] == "music"
    assert tone["confidence"] >= 0.6
    assert tone["in_call"] is False

    noise = classify_wav(_noise())
    assert noise["scene"] == "noise"

    speech = classify_wav(_speech_like())
    assert speech["scene"] == "speech"
    assert speech["voiced_ratio"] >= 0.35


def test_unsupported_audio_returns_unknown_without_raw() -> None:
    result = classify_wav(b"not a wav file")
    assert result["scene"] == "unknown"
    assert result["error"] == "unsupported_audio"
    assert "data" not in result


def test_classifier_accepts_8bit_and_24bit_pcm() -> None:
    tone_samples = [
        int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
        for i in range(16000)
    ]
    assert classify_wav(_wav_bytes_width(tone_samples, 1))["scene"] == "music"
    assert classify_wav(_wav_bytes_width([s * 256 for s in tone_samples], 3))["scene"] == "music"
    assert classify_wav(_wav_bytes_width([0] * 16000, 1))["scene"] == "silence"


def test_collector_classifies_local_sample_and_never_ships_raw(
    tmp_path,
    monkeypatch,
) -> None:
    sample = tmp_path / "tone.wav"
    sample.write_bytes(_tone())
    monkeypatch.setenv("EV_AUDIO_SAMPLE_FILE", str(sample))
    monkeypatch.delenv("EV_AUDIO_SCENE", raising=False)
    monkeypatch.delenv("EV_IN_CALL", raising=False)

    payload = audio_scene()
    assert payload is not None
    assert payload["scene"] == "music"
    assert payload["classifier"] == "vad_features"
    assert "data" not in payload
    assert "wav" not in payload


def test_classify_wav_degrades_without_yamnet() -> None:
    result = classify_wav(_silence())
    assert result["classifier"] == "vad_features"
    assert result["degraded"] is True


class _FakeInput:
    name = "input_1"


class _FakeYamNetSession:
    def __init__(self, scores):
        self.scores = scores

    def get_inputs(self):
        return [_FakeInput()]

    def run(self, outputs, feed):
        assert feed["input_1"].shape[0] >= 1
        return [self.scores]


def test_yamnet_maps_ev_scenes() -> None:
    np = pytest.importorskip("numpy")
    speech = np.zeros((1, 521), dtype=np.float32)
    speech[0, 0] = 0.9
    classifier = YamNetSceneClassifier(session_factory=lambda: _FakeYamNetSession(speech))
    result = classifier.classify(_speech_like())
    assert result["scene"] == "speech"
    assert result["classifier"] == "yamnet"
    assert result["in_call"] is True

    music = np.zeros((1, 521), dtype=np.float32)
    music[0, 132] = 0.85
    classifier = YamNetSceneClassifier(session_factory=lambda: _FakeYamNetSession(music))
    assert classifier.classify(_tone())["scene"] == "music"

    silence = np.zeros((1, 521), dtype=np.float32)
    silence[0, :] = 0.01
    classifier = YamNetSceneClassifier(session_factory=lambda: _FakeYamNetSession(silence))
    assert classifier.classify(_silence())["scene"] == "silence"


def test_yamnet_labels_csv_drives_meeting_and_custom_music(tmp_path) -> None:
    np = pytest.importorskip("numpy")
    labels = tmp_path / "yamnet_class_map.csv"
    labels.write_text(
        "0,/m/09x0r,Speech\n3,/m/01k8sn,Conversation\n132,/m/04rlf,Music\n"
        "494,/m/0dgw9r,Silence\n400,/m/0l14j_,Piano\n",
        encoding="utf-8",
    )
    conversation = np.zeros((1, 521), dtype=np.float32)
    conversation[0, 3] = 0.95
    classifier = YamNetSceneClassifier(
        labels_path=str(labels),
        session_factory=lambda: _FakeYamNetSession(conversation),
    )
    assert classifier.classify(_speech_like())["scene"] == "meeting"

    piano = np.zeros((1, 521), dtype=np.float32)
    piano[0, 400] = 0.8
    classifier = YamNetSceneClassifier(
        labels_path=str(labels),
        session_factory=lambda: _FakeYamNetSession(piano),
    )
    assert classifier.classify(_tone())["scene"] == "music"
    assert classifier._label_name(400) == "piano"


def test_tone_score_numpy_matches_pure_loop() -> None:
    pytest.importorskip("numpy")
    samples = [
        int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
        for i in range(16000)
    ]
    segment = samples[len(samples) // 4 : len(samples) // 2]
    mean = sum(segment) / len(segment)
    centered = [s - mean for s in segment]
    energy = sum(s * s for s in centered)
    pure = _tone_score_pure(centered, energy, 16000)
    vectorized = _tone_score_numpy(centered, energy, 16000)
    assert vectorized == pytest.approx(pure, abs=1e-6)
