"""Audio-scene classification from local PCM WAV bytes.

Two classifiers share one interface:

* ``YamNetSceneClassifier`` — YAMNet (17 MB ONNX, AudioSet 521 classes) mapped
  to EV's five scene classes (speech, meeting, music, noise, silence), loaded
  through the ModelArbiter. Used when a model path is configured.
* the existing pure-Python VAD-feature classifier (``vad_features``) as the
  zero-dependency fallback. Its O(n^2) tone loop is vectorized with numpy when
  numpy is importable.

Only the derived label + confidence are emitted; raw audio never leaves the
device without separate explicit consent.
"""

from __future__ import annotations

import array
import csv
import importlib.util
import math
import wave
from io import BytesIO
from pathlib import Path

SILENCE_RMS = 250.0
TONE_SCORE_THRESHOLD = 0.55
VOICED_FRAME_MS = 20

EV_SCENES = ("speech", "meeting", "music", "noise", "silence")

# Fallback AudioSet indices when no YAMNet label file is available. These are
# the canonical YAMNet class-map positions for Speech (0), Conversation (3),
# Narration (4), Music (132), and Silence (494).
FALLBACK_SPEECH_INDICES = frozenset(range(0, 11))
FALLBACK_MEETING_INDICES = frozenset({3, 4})
FALLBACK_MUSIC_INDICES = frozenset({132})
FALLBACK_SILENCE_INDICES = frozenset({494})


def _parse_wav(data: bytes) -> tuple[list[int], int, float] | None:
    try:
        with wave.open(BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        return None
    if width not in (1, 2, 3) or rate <= 0:
        return None
    if width == 1:
        raw = array.array("B")
        raw.frombytes(frames)
        mono = [(int(value) - 128) * 256 for value in raw[0::channels]]
    elif width == 2:
        raw = array.array("h")
        raw.frombytes(frames)
        mono = [int(value) for value in raw[0::channels]]
    else:
        mono = []
        step = 3 * channels
        for i in range(0, len(frames) - step + 1, step):
            mono.append(int.from_bytes(frames[i : i + 3], "little", signed=True) >> 8)
    return mono, rate, len(mono) / rate


def _rms(samples: list[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _zero_crossing_rate(samples: list[int]) -> float:
    if len(samples) < 2:
        return 0.0
    crossings = sum(
        1 for i in range(1, len(samples)) if (samples[i - 1] < 0) != (samples[i] < 0)
    )
    return crossings / (len(samples) - 1)


def _voiced_ratio(samples: list[int], rate: int) -> float:
    """Fraction of 20ms frames whose RMS exceeds the adaptive voice floor."""
    if not samples:
        return 0.0
    frame_size = max(1, int(rate * VOICED_FRAME_MS / 1000))
    overall_rms = _rms(samples)
    floor = max(300.0, overall_rms * 0.3)
    voiced = 0
    total = 0
    for start in range(0, len(samples), frame_size):
        frame = samples[start : start + frame_size]
        total += 1
        if _rms(frame) >= floor:
            voiced += 1
    return voiced / max(1, total)


def _tone_score(samples: list[int], rate: int) -> float:
    """Normalized autocorrelation in the 80-400 Hz lag window (tonality)."""
    if len(samples) < rate // 80:
        return 0.0
    segment = samples[len(samples) // 4 : len(samples) // 2]
    if len(segment) < rate // 80:
        segment = samples
    mean = sum(segment) / len(segment)
    centered = [s - mean for s in segment]
    energy = sum(s * s for s in centered)
    if energy <= 0:
        return 0.0
    try:
        if importlib.util.find_spec("numpy") is not None:
            return _tone_score_numpy(centered, energy, rate)
    except (ImportError, ValueError):
        pass
    return _tone_score_pure(centered, energy, rate)


def _tone_score_numpy(centered: list[float], energy: float, rate: int) -> float:
    """Vectorized autocorrelation over the 80-400 Hz lag window."""

    import numpy as np

    values = np.asarray(centered, dtype=np.float64)
    best = 0.0
    for lag in range(max(2, rate // 400), rate // 80):
        corr = float(np.dot(values[: len(values) - lag], values[lag:]))
        score = abs(corr) / (energy + 1e-9)
        if score > best:
            best = score
    return best


def _tone_score_pure(centered: list[float], energy: float, rate: int) -> float:
    best = 0.0
    for lag in range(max(2, rate // 400), rate // 80):
        corr = sum(centered[i] * centered[i + lag] for i in range(len(centered) - lag))
        score = abs(corr) / (energy + 1e-9)
        if score > best:
            best = score
    return best


def classify_wav_vad_features(data: bytes) -> dict:
    """Pure-Python VAD-feature classification (deterministic double)."""

    parsed = _parse_wav(data)
    if parsed is None:
        return {
            "scene": "unknown",
            "confidence": 0.0,
            "in_call": False,
            "classifier": "vad_features",
            "error": "unsupported_audio",
        }
    samples, rate, duration = parsed
    if not samples or duration <= 0:
        return {
            "scene": "silence",
            "confidence": 0.9,
            "in_call": False,
            "duration_s": 0.0,
            "classifier": "vad_features",
        }
    rms = _rms(samples)
    zcr = _zero_crossing_rate(samples)
    voiced = _voiced_ratio(samples, rate)
    tone = _tone_score(samples, rate)

    if rms < SILENCE_RMS:
        scene, confidence = "silence", 0.85
    elif tone >= TONE_SCORE_THRESHOLD and duration >= 0.5:
        scene, confidence = "music", round(min(0.92, 0.6 + tone * 0.2), 3)
    elif 0.35 <= voiced <= 0.85 and zcr > 0.015:
        scene, confidence = "speech", round(min(0.8, 0.55 + voiced * 0.2), 3)
    elif voiced > 0.85:
        scene, confidence = "noise", 0.7
    else:
        scene, confidence = "noise", 0.6

    return {
        "scene": scene,
        "confidence": confidence,
        "in_call": scene == "speech" and voiced >= 0.5,
        "duration_s": round(duration, 2),
        "voiced_ratio": round(voiced, 3),
        "zcr": round(zcr, 4),
        "rms": int(rms),
        "tone_score": round(tone, 3),
        "classifier": "vad_features",
    }


def _load_labels(labels_path: str | None) -> tuple[dict[int, str], dict[str, set[int]]]:
    """Load a YAMNet class map CSV (index,mid,display_name) into name→indices."""

    groups: dict[str, set[int]] = {
        "speech": set(FALLBACK_SPEECH_INDICES),
        "meeting": set(FALLBACK_MEETING_INDICES),
        "music": set(FALLBACK_MUSIC_INDICES),
        "noise": set(),
        "silence": set(FALLBACK_SILENCE_INDICES),
    }
    names: dict[int, str] = {}
    if not labels_path:
        return names, groups
    try:
        with Path(labels_path).open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if len(row) < 3:
                    continue
                try:
                    index = int(row[0])
                except ValueError:
                    continue
                name = row[2].strip().lower()
                names[index] = name
                if "conversation" in name or "meeting" in name:
                    groups["speech"].add(index)
                    groups["meeting"].add(index)
                elif any(
                    token in name
                    for token in ("speech", "narration", "babbling", "shout", "whisper", "laughter", "sigh")
                ):
                    groups["speech"].add(index)
                elif any(
                    token in name
                    for token in ("music", "song", "instrument", "guitar", "piano", "drum", "singer", "choir")
                ):
                    groups["music"].add(index)
                elif "silence" in name:
                    groups["silence"].add(index)
    except OSError:
        return names, groups
    return names, groups


class YamNetSceneClassifier:
    """YAMNet ONNX → EV scene classes, gated by the ModelArbiter."""

    name = "yamnet"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        labels_path: str | None = None,
        session_factory=None,
        frame_samples: int = 15600,
        sample_rate: int = 16000,
        silence_score_floor: float = 0.20,
    ) -> None:
        self.model_path = model_path
        self.labels_path = labels_path
        self._session_factory = session_factory
        self.frame_samples = frame_samples
        self.sample_rate = sample_rate
        self.silence_score_floor = silence_score_floor
        self._session = None
        self._labels: dict[int, str] = {}
        self._groups: dict[str, set[int]] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return self._session
        self._labels, self._groups = _load_labels(self.labels_path)
        if self._session_factory is not None:
            self._session = self._session_factory()
            self._loaded = True
            return self._session
        if not self.model_path:
            raise RuntimeError(
                "YAMNet model not configured; set EV_EARS_SCENE_MODEL_PATH "
                "or use the vad_features fallback"
            )
        from app.audio.models import acquire_model

        with acquire_model("scene-yamnet"):
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise RuntimeError(
                    "onnxruntime is not installed; install the ml extra "
                    "(Agent 2 dependency request)"
                ) from exc
            self._session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        self._loaded = True
        return self._session

    def classify(self, data: bytes) -> dict:
        """Classify WAV bytes; returns the same dict shape as the fallback."""

        session = self._load()
        parsed = _parse_wav(data)
        if parsed is None:
            return {
                "scene": "unknown",
                "confidence": 0.0,
                "in_call": False,
                "classifier": self.name,
                "error": "unsupported_audio",
            }
        samples, rate, duration = parsed
        if not samples or duration <= 0:
            return {
                "scene": "silence",
                "confidence": 0.9,
                "in_call": False,
                "duration_s": 0.0,
                "classifier": self.name,
            }
        if rate != self.sample_rate:
            return {
                "scene": "unknown",
                "confidence": 0.0,
                "in_call": False,
                "classifier": self.name,
                "error": f"YAMNet requires {self.sample_rate} Hz, got {rate}",
            }
        import numpy as np

        values = np.asarray(samples, dtype=np.float32) / 32768.0
        frames = []
        for start in range(0, len(values) - self.frame_samples + 1, self.frame_samples):
            frames.append(values[start : start + self.frame_samples])
        if not frames:
            frames = [values]
        batch = np.stack(frames).astype(np.float32)
        input_name = session.get_inputs()[0].name
        scores = np.asarray(session.run(None, {input_name: batch})[0], dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != 521:
            return {
                "scene": "unknown",
                "confidence": 0.0,
                "in_call": False,
                "classifier": self.name,
                "error": f"unexpected YAMNet output shape {scores.shape}",
            }
        mean_scores = scores.mean(axis=0)
        top_index = int(mean_scores.argmax())
        top_score = float(mean_scores[top_index])
        scene = "silence" if top_score < self.silence_score_floor else self._map_index(top_index)
        confidence = round(float(mean_scores.max()), 3)
        return {
            "scene": scene,
            "confidence": confidence,
            "in_call": scene in ("speech", "meeting"),
            "duration_s": round(duration, 2),
            "top_class": self._label_name(top_index) or top_index,
            "top_score": round(top_score, 4),
            "classifier": self.name,
        }

    def _map_index(self, index: int) -> str:
        # Meeting is checked before speech so conversation/narration map to
        # the more specific EV class.
        for scene in ("meeting", "speech", "music", "silence", "noise"):
            if index in self._groups.get(scene, set()):
                return scene
        return "noise"

    def _label_name(self, index: int) -> str | None:
        return self._labels.get(index)


_yamnet_singleton: YamNetSceneClassifier | None = None


def default_scene_classifier() -> YamNetSceneClassifier | None:
    """Config-driven YAMNet classifier, or None for the fallback."""

    from app.config import settings

    if not settings.ears_scene_model_path and not settings.ears_scene_labels_path:
        return None
    return YamNetSceneClassifier(
        model_path=settings.ears_scene_model_path,
        labels_path=settings.ears_scene_labels_path,
    )


def classify_wav(data: bytes) -> dict:
    """Classify PCM WAV bytes into a derived scene representation.

    Uses YAMNet when configured and loadable; otherwise degrades to the
    deterministic VAD-feature classifier (``degraded=true`` is reported).
    """

    global _yamnet_singleton
    classifier = default_scene_classifier()
    if classifier is None and _yamnet_singleton is not None:
        classifier = _yamnet_singleton
    if classifier is not None:
        try:
            return classifier.classify(data)
        except (RuntimeError, ImportError, ValueError):
            result = classify_wav_vad_features(data)
            result["degraded"] = True
            result["classifier"] = "vad_features"
            return result
    result = classify_wav_vad_features(data)
    result["degraded"] = True
    return result


def set_scene_classifier(classifier: YamNetSceneClassifier | None) -> None:
    """Override the process-wide classifier (tests / ears process)."""

    global _yamnet_singleton
    _yamnet_singleton = classifier
