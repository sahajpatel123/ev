"""Owner speaker verification.

Production engines:

* :class:`CamppSpeakerVerifier` — CAM++ (7.2M params, 0.65% EER on
  VoxCeleb1-O per ModelScope/3D-Speaker) exported to ONNX, 192-dim, 16 kHz.
  This is the recommended engine: smaller than ECAPA-TDNN and more accurate.
* :class:`SpeechBrainSpeakerVerifier` — ECAPA-TDNN via SpeechBrain
  (``spkrec-ecapa-voxceleb``), kept as an alternative.
* :class:`HttpSpeakerVerifier` — remote encoder service, gated by regional
  remote-processing policy. This makes the ``http`` provider option real
  instead of silently falling back to the hash double.

The deterministic hash test double (:class:`HashTestDoubleSpeakerVerifier`,
formerly ``ProfileSpeakerVerifier``) is NOT a security control. It can only be
selected while pytest is running; any production config that resolves to it
refuses to start the voice path.

Voiceprints stay 192-dim and are encrypted with the existing Fernet/scrypt
layer (:mod:`app.voice.security`); only the encoder changes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import io
import math
import os
import sys
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.voice.contracts import SpeakerDecision, SpeakerVerifier
from app.voice.wake import normalize


def is_test_runtime() -> bool:
    """True while pytest is executing (the only allowed hash-double context)."""
    return os.environ.get("PYTEST_CURRENT_TEST") is not None


def _token_embedding(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-token embedding with signed hash buckets."""
    vector = [0.0] * dim
    for token in normalize(text).split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    return _normalize_vector(vector)


def _audio_embedding(sample: bytes, dim: int) -> list[float]:
    """Deterministic byte-level embedding used by the test double only."""
    vector = [0.0] * dim
    for offset in range(0, len(sample), 128):
        chunk = sample[offset : offset + 128]
        digest = hashlib.sha256(chunk).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0
    return _normalize_vector(vector)


def _normalize_vector(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _flatten_numeric(values: Any) -> list[float]:
    """Flatten an ONNX output (numpy array, list, or scalar) into floats."""
    if hasattr(values, "reshape"):  # numpy array
        return [float(value) for value in values.reshape(-1)]
    if isinstance(values, (list, tuple)):
        return [float(value) for value in values]
    return [float(values)]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Embedding dimension mismatch")
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


# --------------------------------------------------------------------------- #
# Audio sample handling (server-side, allowlisted)
# --------------------------------------------------------------------------- #


def _audio_allowed_roots() -> list[Path]:
    """Explicit allowlist for ``audio_ref`` paths (``EV_VOICE_AUDIO_ALLOWED_DIRS``).

    Empty by default: client-supplied filesystem paths are refused. Only
    directories named here (colon-separated) may be read, and every candidate
    is resolved and containment-checked so ``..`` and symlink escapes fail.
    """

    raw = os.environ.get("EV_VOICE_AUDIO_ALLOWED_DIRS", "")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        root = Path(part).expanduser().resolve()
        if root.is_dir():
            roots.append(root)
    return roots


def _resolve_audio_ref(audio_ref: str) -> bytes:
    if audio_ref.startswith(("http://", "https://")):
        raise ValueError("remote audio_ref is not allowed for speaker verification")
    candidate = Path(audio_ref).expanduser().resolve()
    roots = _audio_allowed_roots()
    if not roots:
        raise ValueError(
            "audio_ref is disabled: set EV_VOICE_AUDIO_ALLOWED_DIRS to an explicit "
            "allowlist of directories"
        )
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError("audio_ref is outside the configured allowlist")
    if not candidate.is_file():
        raise ValueError("audio_ref must be a readable local audio file")
    return candidate.read_bytes()


def sample_audio_bytes(sample: dict) -> bytes:
    """Return raw audio bytes from a verification sample, never a client path."""
    audio_b64 = sample.get("audio_b64")
    if audio_b64:
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise ValueError("audio_b64 must be valid base64") from exc
        if not raw:
            raise ValueError("audio_b64 must not be empty")
        return raw
    audio_ref = sample.get("audio_ref")
    if audio_ref:
        return _resolve_audio_ref(str(audio_ref))
    raise ValueError("Verification sample needs 'audio_b64' or a whitelisted 'audio_ref'")


def _iter_wavs(directory: Path) -> list[Path]:
    """All WAV files under a directory (VoxCeleb-style nested speaker folders)."""
    return sorted(path for path in directory.rglob("*.wav") if path.is_file())


def _resample(values: list[float], src_rate: int, dst_rate: int) -> list[float]:
    if src_rate <= 0 or dst_rate <= 0 or not values:
        return values
    if src_rate == dst_rate:
        return values
    ratio = dst_rate / src_rate
    out_len = max(1, int(len(values) * ratio))
    out: list[float] = []
    for index in range(out_len):
        position = index / ratio
        low = int(position)
        high = min(low + 1, len(values) - 1)
        fraction = position - low
        out.append(values[low] * (1.0 - fraction) + values[high] * fraction)
    return out


def decode_waveform(raw: bytes) -> tuple[list[float], int]:
    """Decode RIFF/WAVE audio to a mono float32 waveform at 16 kHz.

    The CAM++ ONNX engine and the audio liveness model both consume the same
    server-side decode so there is exactly one audio interpretation.
    """

    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError("audio must be a RIFF/WAVE file") from exc
    if width not in (1, 2):
        raise ValueError("only 8-bit or 16-bit PCM WAV is supported")
    if width == 1:
        values = [(byte - 128) / 128.0 for byte in frames]
    else:
        import array

        samples = array.array("h", frames)
        if sys.byteorder == "big":
            samples.byteswap()
        values = [sample / 32768.0 for sample in samples]
    if channels > 1:
        values = [
            sum(values[index : index + channels]) / channels
            for index in range(0, len(values), channels)
        ]
    return _resample(values, rate, 16000), 16000


def _embedding_for_test(sample: dict, dim: int) -> list[float]:
    """Deterministic embedding for the test double / degraded fallback."""
    features = sample.get("features")
    if features:
        if len(features) != dim:
            raise ValueError(f"Expected {dim} features, got {len(features)}")
        return _normalize_vector(features)
    audio_b64 = sample.get("audio_b64")
    if audio_b64:
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise ValueError("audio_b64 must be valid base64") from exc
        if not raw:
            raise ValueError("audio_b64 must not be empty")
        return _audio_embedding(raw, dim)
    audio_ref = sample.get("audio_ref")
    if audio_ref:
        return _audio_embedding(_resolve_audio_ref(str(audio_ref)), dim)
    text = sample.get("text")
    if not text:
        raise ValueError("Verification sample needs 'text', 'features', or 'audio_b64'")
    return _token_embedding(text, dim)


# --------------------------------------------------------------------------- #
# Threshold calibration
# --------------------------------------------------------------------------- #


def calibrate_operating_point(
    owner_scores: Sequence[float],
    impostor_scores: Sequence[float],
) -> dict:
    """Calibrate a cosine-similarity operating point from scored trials.

    Returns the EER, the EER threshold, the highest threshold that still
    achieves **zero false accepts** (FAR = 0), TAR at that threshold, and the
    ROC curve as ``[fpr, tpr, threshold]`` rows. Scores are interpreted as
    "accept when score >= threshold".
    """

    owners = sorted(float(score) for score in owner_scores)
    impostors = sorted(float(score) for score in impostor_scores)
    if not owners or not impostors:
        raise ValueError("calibration needs at least one owner and one impostor score")
    candidates = sorted({*owners, *impostors})

    roc: list[list[float]] = []
    best_eer = (1.0, candidates[0])
    far0_boundary: float | None = None
    for threshold in candidates:
        far = sum(1.0 for score in impostors if score >= threshold) / len(impostors)
        tar = sum(1.0 for score in owners if score >= threshold) / len(owners)
        roc.append([round(far, 4), round(tar, 4), round(threshold, 6)])
        if far == 0 and far0_boundary is None:
            far0_boundary = threshold
        frr = 1.0 - tar
        if abs(far - frr) < best_eer[0]:
            best_eer = (abs(far - frr), threshold)

    if far0_boundary is None:
        far0_boundary = impostors[-1]
    above = [candidate for candidate in candidates if candidate > far0_boundary]
    far0_threshold = (
        (far0_boundary + min(above)) / 2.0 if above else far0_boundary
    )
    tar_at_shipped = sum(
        1.0 for score in owners if score >= far0_threshold
    ) / len(owners)
    return {
        "eer": round(best_eer[0], 4),
        "eer_threshold": round(best_eer[1], 6),
        "threshold": round(far0_threshold, 6),
        "tar_at_far0": round(tar_at_shipped, 4),
        "owner_count": len(owners),
        "impostor_count": len(impostors),
        "roc": roc,
    }


# --------------------------------------------------------------------------- #
# Test double (not a security control)
# --------------------------------------------------------------------------- #


class HashTestDoubleSpeakerVerifier:
    """Dev/test verifier: text/features → deterministic hash-bucket embedding.

    This is a **test double**, not a security control: it fingerprints byte
    patterns, not vocal identity. It is only selectable while pytest is
    running; production configs that would resolve to it refuse to start.
    """

    name = "profile-v1"
    embedding_dim = 192

    def __init__(self, dim: int = 192, threshold: float | None = None) -> None:
        self.embedding_dim = dim
        self.threshold = settings.voiceprint_threshold if threshold is None else threshold

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        if len(samples) < 5:
            raise ValueError(f"Enrollment needs at least 5 samples, got {len(samples)}")
        embeddings = [_embedding_for_test(sample, self.embedding_dim) for sample in samples]
        mean = [
            sum(values[index] for values in embeddings) / len(embeddings)
            for index in range(self.embedding_dim)
        ]
        return {
            "algorithm": self.name,
            "embedding": _normalize_vector(mean),
            "dim": self.embedding_dim,
            "threshold": self.threshold,
            "sample_count": len(samples),
            "degraded": True,
        }

    async def verify(
        self,
        sample: dict,
        *,
        enrolled_payload: dict,
        threshold: float | None = None,
    ) -> SpeakerDecision:
        enrolled = enrolled_payload.get("embedding")
        if not enrolled:
            return SpeakerDecision(
                verified=False,
                confidence=0.0,
                threshold=threshold if threshold is not None else self.threshold,
                algorithm=self.name,
                reason="no enrolled voiceprint",
            )
        embedding = _embedding_for_test(sample, self.embedding_dim)
        similarity = cosine_similarity(embedding, enrolled)
        threshold = (
            threshold
            if threshold is not None
            else enrolled_payload.get("threshold", self.threshold)
        )
        return SpeakerDecision(
            verified=similarity >= threshold,
            confidence=round(similarity, 4),
            threshold=threshold,
            algorithm=self.name,
            speaker_id="owner",
            reason="voiceprint match" if similarity >= threshold else "voiceprint mismatch",
        )


# Backwards-compatible alias: the old name described the class as a production
# profile; it is a test double and new code should use the honest name.
ProfileSpeakerVerifier = HashTestDoubleSpeakerVerifier


# --------------------------------------------------------------------------- #
# SpeechBrain ECAPA-TDNN (alternative production engine)
# --------------------------------------------------------------------------- #


class SpeechBrainSpeakerVerifier:
    """Production speaker verification with SpeechBrain ECAPA-TDNN.

    Enrolls with >=5 samples and verifies with cosine similarity against the
    mean enrollment embedding. SpeechBrain/torch imports are lazy so offline
    CI stays green; pass a ``sample_encoder`` callable to unit-test the
    enrollment/verification logic with a fake encoder.
    """

    name = "speechbrain-ecapa"
    embedding_dim = 192

    def __init__(
        self,
        *,
        source: str | None = None,
        savedir: str | None = None,
        dim: int | None = None,
        threshold: float | None = None,
        sample_encoder: Callable[[dict], Any] | None = None,
    ) -> None:
        self.source = source or settings.voiceprint_model or "speechbrain/spkrec-ecapa-voxceleb"
        self.savedir = savedir or settings.voiceprint_model_dir
        self.embedding_dim = dim or settings.voiceprint_dim
        self.threshold = (
            settings.voiceprint_threshold if threshold is None else threshold
        )
        self._sample_encoder_fn = sample_encoder
        self._encoder = None

    def _load_encoder(self):
        if self._encoder is None:
            try:
                from speechbrain.inference.speaker import EncoderClassifier
            except ImportError as exc:
                raise RuntimeError(
                    "SpeechBrain is not installed; run: uv pip install speechbrain"
                ) from exc
            self._encoder = EncoderClassifier.from_hparams(
                source=self.source, savedir=self.savedir
            )
        return self._encoder

    @staticmethod
    def _waveform(raw: bytes):
        """Decode audio bytes to a mono 16 kHz float waveform (torch tensor)."""
        try:
            import torch
            import torchaudio
        except ImportError:
            torch = None
            torchaudio = None
        if torchaudio is not None:
            waveform, sample_rate = torchaudio.load(io.BytesIO(raw))
            if sample_rate != 16000:
                waveform = torchaudio.functional.resample(waveform, sample_rate, 16000)
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)
            return waveform.squeeze(0)
        if torch is None:
            raise RuntimeError(
                "SpeechBrain audio decoding requires torch; install speechbrain"
            )
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if width != 2:
            raise ValueError("Only 16-bit PCM WAV is supported without torchaudio")
        if sample_rate != 16000:
            raise ValueError("WAV must be 16 kHz without torchaudio")
        import array

        samples = array.array("h", frames)
        if sys.byteorder == "big":
            samples.byteswap()
        if channels > 1:
            samples = array.array(
                "h",
                (
                    sum(samples[index : index + channels]) // channels
                    for index in range(0, len(samples), channels)
                ),
            )
        return torch.tensor(samples, dtype=torch.float32) / 32768.0

    def _encode_sync(self, sample: dict) -> list[float]:
        raw = sample_audio_bytes(sample)
        waveform = self._waveform(raw)
        encoder = self._load_encoder()
        embedding = encoder.encode_batch(waveform.unsqueeze(0))
        return _normalize_vector(embedding.squeeze(0).detach().cpu().tolist())

    async def _sample_embedding(self, sample: dict) -> tuple[list[float], bool]:
        if self._sample_encoder_fn is not None:
            result = self._sample_encoder_fn(sample)
            if inspect.isawaitable(result):
                result = await result
            return _normalize_vector(list(result)), False
        try:
            return await asyncio.to_thread(self._encode_sync, sample), False
        except (ImportError, RuntimeError) as exc:
            if not is_test_runtime():
                raise RuntimeError(
                    "SpeechBrain encoder unavailable; refusing the voice path"
                ) from exc
            return _embedding_for_test(sample, self.embedding_dim), True
        except ValueError:
            if not is_test_runtime():
                raise
            return _embedding_for_test(sample, self.embedding_dim), True

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        if len(samples) < 5:
            raise ValueError(f"Enrollment needs at least 5 samples, got {len(samples)}")
        embeddings, flags = [], []
        for sample in samples:
            embedding, degraded = await self._sample_embedding(sample)
            embeddings.append(embedding)
            flags.append(degraded)
        mean = [
            sum(embedding[index] for embedding in embeddings) / len(embeddings)
            for index in range(self.embedding_dim)
        ]
        return {
            "algorithm": self.name,
            "embedding": _normalize_vector(mean),
            "dim": self.embedding_dim,
            "threshold": self.threshold,
            "sample_count": len(samples),
            "model": self.source,
            "degraded": any(flags),
        }

    async def verify(
        self,
        sample: dict,
        *,
        enrolled_payload: dict,
        threshold: float | None = None,
    ) -> SpeakerDecision:
        enrolled = enrolled_payload.get("embedding")
        if not enrolled:
            return SpeakerDecision(
                verified=False,
                confidence=0.0,
                threshold=threshold if threshold is not None else self.threshold,
                algorithm=self.name,
                reason="no enrolled voiceprint",
            )
        embedding, _degraded = await self._sample_embedding(sample)
        similarity = cosine_similarity(embedding, enrolled)
        threshold = (
            threshold
            if threshold is not None
            else enrolled_payload.get("threshold", self.threshold)
        )
        return SpeakerDecision(
            verified=similarity >= threshold,
            confidence=round(similarity, 4),
            threshold=threshold,
            algorithm=self.name,
            speaker_id="owner",
            reason="voiceprint match" if similarity >= threshold else "voiceprint mismatch",
        )


# --------------------------------------------------------------------------- #
# CAM++ ONNX (recommended production engine)
# --------------------------------------------------------------------------- #


def _model_cache_candidates() -> list[Path]:
    """Candidate paths in the arbiter model cache for the speaker model.

    The arbiter is used (never bypassed) so the 28 MB always-resident speaker
    slot is accounted for even when the file itself is still missing. Falls
    back from the CAM++ entry to the existing ``speaker-ecapa`` slot so the
    current registry roster keeps working until Agent 2 renames it.
    """

    from app.ml.arbiter import ModelArbiter
    from app.ml.registry import ModelRegistry, builtin_models
    from app.ml.settings import get_ml_settings
    from app.ml.store import target_path

    registry = ModelRegistry()
    for spec in builtin_models():
        try:
            registry.register(spec)
        except Exception:
            continue
    arbiter = ModelArbiter(registry)
    candidates: list[Path] = []
    for name in ("speaker-campp", "speaker-ecapa"):
        try:
            with arbiter.acquire(name, release_on_exit=True):
                candidates.append(target_path(get_ml_settings(), registry.get(name)))
        except Exception:
            continue
    return candidates


class CamppSpeakerVerifier:
    """CAM++ speaker encoder (ONNX, 192-dim, 16 kHz).

    CAM++ (7.2M params) reaches 0.65% EER on VoxCeleb1-O — better than
    ECAPA-TDNN (~0.86–1.45%) and ERes2Net-base (0.84%) — and the ONNX export is
    about 28 MB, fitting the locked 28 MB always-resident speaker slot.

    When weights or onnxruntime are absent the engine refuses to run outside
    pytest; inside pytest it degrades to the deterministic test double with
    ``degraded=True`` so offline CI stays green without pretending to be real.
    """

    name = "campp"
    embedding_dim = 192

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        dim: int | None = None,
        threshold: float | None = None,
        onnx_session_factory: Callable[[Path], Any] | None = None,
        require_available: bool | None = None,
    ) -> None:
        self.embedding_dim = dim or settings.voiceprint_dim
        self.threshold = (
            settings.voiceprint_threshold if threshold is None else threshold
        )
        self._factory = onnx_session_factory
        self._session = None
        self._model_path = Path(model_path) if model_path else None
        self.require_available = (
            not is_test_runtime() if require_available is None else require_available
        )
        if self.require_available and not self._resolve_model_path():
            raise RuntimeError(
                "CAM++ ONNX model is not available; set EV_VOICEPRINT_MODEL_DIR to a "
                "directory containing the exported .onnx (see docs/VOICE_SECURITY.md)"
            )

    def _resolve_model_path(self) -> Path | None:
        if self._model_path is not None:
            candidate = self._model_path.expanduser().resolve()
            if candidate.is_file():
                return candidate
            if candidate.is_dir():
                for name in ("campp.onnx", "model.onnx", "speaker.onnx"):
                    found = candidate / name
                    if found.is_file():
                        return found
                onnx_files = sorted(candidate.glob("*.onnx"))
                if onnx_files:
                    return onnx_files[0]
            return None
        configured = settings.voiceprint_model_dir
        if configured:
            candidate = Path(configured).expanduser().resolve()
            if candidate.is_file():
                return candidate
            if candidate.is_dir():
                onnx_files = sorted(candidate.glob("*.onnx"))
                if onnx_files:
                    return onnx_files[0]
        for candidate in _model_cache_candidates():
            if candidate.is_file():
                return candidate
        return None

    def _load_session(self):
        if self._session is not None:
            return self._session
        if self._factory is None:
            try:
                import onnxruntime
            except ImportError as exc:
                raise RuntimeError(
                    "onnxruntime is required for the CAM++ engine (install the ml extra)"
                ) from exc
        path = self._resolve_model_path()
        if path is None:
            raise RuntimeError("CAM++ ONNX model file is not available")
        if self._factory is not None:
            self._session = self._factory(path)
        else:
            self._session = onnxruntime.InferenceSession(
                str(path), providers=["CPUExecutionProvider"]
            )
        return self._session

    def _embed_onnx(self, session, waveform: list[float]) -> list[float]:
        audio = [list(waveform)]
        inputs: dict[str, Any] = {inp.name: audio for inp in session.get_inputs()}
        for inp in session.get_inputs():
            if "length" in inp.name.lower():
                inputs[inp.name] = [len(waveform)]
        outputs = session.run(None, inputs)
        embedding = _flatten_numeric(outputs[0])
        vector = embedding[: self.embedding_dim]
        if len(vector) < self.embedding_dim:
            vector += [0.0] * (self.embedding_dim - len(vector))
        return _normalize_vector(vector)

    def _encode_sync(self, sample: dict) -> list[float]:
        raw = sample_audio_bytes(sample)
        waveform, _rate = decode_waveform(raw)
        if not waveform:
            raise ValueError("audio contains no samples")
        return self._embed_onnx(self._load_session(), waveform)

    async def _sample_embedding(self, sample: dict) -> tuple[list[float], bool]:
        try:
            return await asyncio.to_thread(self._encode_sync, sample), False
        except (ImportError, RuntimeError) as exc:
            if not is_test_runtime():
                raise RuntimeError(
                    "CAM++ encoder unavailable; refusing the voice path"
                ) from exc
            return _embedding_for_test(sample, self.embedding_dim), True
        except ValueError:
            if not is_test_runtime():
                raise
            return _embedding_for_test(sample, self.embedding_dim), True

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        if len(samples) < 5:
            raise ValueError(f"Enrollment needs at least 5 samples, got {len(samples)}")
        embeddings, flags = [], []
        for sample in samples:
            embedding, degraded = await self._sample_embedding(sample)
            embeddings.append(embedding)
            flags.append(degraded)
        mean = [
            sum(embedding[index] for embedding in embeddings) / len(embeddings)
            for index in range(self.embedding_dim)
        ]
        path = self._resolve_model_path()
        return {
            "algorithm": self.name,
            "embedding": _normalize_vector(mean),
            "dim": self.embedding_dim,
            "threshold": self.threshold,
            "sample_count": len(samples),
            "model": str(path) if path else "campp-onnx",
            "degraded": any(flags),
        }

    async def verify(
        self,
        sample: dict,
        *,
        enrolled_payload: dict,
        threshold: float | None = None,
    ) -> SpeakerDecision:
        enrolled = enrolled_payload.get("embedding")
        if not enrolled:
            return SpeakerDecision(
                verified=False,
                confidence=0.0,
                threshold=threshold if threshold is not None else self.threshold,
                algorithm=self.name,
                reason="no enrolled voiceprint",
            )
        embedding, _degraded = await self._sample_embedding(sample)
        similarity = cosine_similarity(embedding, enrolled)
        threshold = (
            threshold
            if threshold is not None
            else enrolled_payload.get("threshold", self.threshold)
        )
        return SpeakerDecision(
            verified=similarity >= threshold,
            confidence=round(similarity, 4),
            threshold=threshold,
            algorithm=self.name,
            speaker_id="owner",
            reason="voiceprint match" if similarity >= threshold else "voiceprint mismatch",
        )


# --------------------------------------------------------------------------- #
# Remote HTTP encoder (real branch for the http provider option)
# --------------------------------------------------------------------------- #


class HttpSpeakerVerifier:
    """Remote encoder service behind the regional remote-processing gate.

    The provider option is no longer a lie: ``http`` now constructs a real
    client, and the remote-processing policy is enforced here as well as in
    the lifecycle, so the transparency disclosure matches reality.
    """

    name = "http"
    embedding_dim = 192

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        dim: int | None = None,
        threshold: float | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        require_gate: bool = True,
    ) -> None:
        self.base_url = (base_url or settings.voiceprint_base_url or "").rstrip("/")
        self.api_key = api_key if api_key is not None else settings.voiceprint_api_key
        self.embedding_dim = dim or settings.voiceprint_dim
        self.threshold = (
            settings.voiceprint_threshold if threshold is None else threshold
        )
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        if require_gate:
            self._enforce_gate()

    def _enforce_gate(self) -> None:
        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("voice_enrollment"):
            raise RuntimeError(
                "Remote voiceprint processing is denied by regional policy; set "
                "EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING=true with explicit consent"
            )
        if not self.base_url:
            raise RuntimeError("EV_VOICEPRINT_BASE_URL is required for provider=http")

    async def _embed(self, sample: dict) -> list[float]:
        raw = sample_audio_bytes(sample)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        close = False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            close = True
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/embed",
                headers=headers,
                json={
                    "audio_b64": base64.b64encode(raw).decode("ascii"),
                    "sample_rate": 16000,
                },
            )
            response.raise_for_status()
            payload = response.json()
            embedding = payload.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("remote encoder returned no embedding")
            if len(embedding) != self.embedding_dim:
                raise ValueError(
                    f"remote encoder returned {len(embedding)} dims, expected "
                    f"{self.embedding_dim}"
                )
            return _normalize_vector([float(value) for value in embedding])
        finally:
            if close:
                await self._client.aclose()
                self._client = None

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        if len(samples) < 5:
            raise ValueError(f"Enrollment needs at least 5 samples, got {len(samples)}")
        embeddings = [await self._embed(sample) for sample in samples]
        mean = [
            sum(embedding[index] for embedding in embeddings) / len(embeddings)
            for index in range(self.embedding_dim)
        ]
        return {
            "algorithm": self.name,
            "embedding": _normalize_vector(mean),
            "dim": self.embedding_dim,
            "threshold": self.threshold,
            "sample_count": len(samples),
            "model": f"{self.base_url}/v1/embed",
        }

    async def verify(
        self,
        sample: dict,
        *,
        enrolled_payload: dict,
        threshold: float | None = None,
    ) -> SpeakerDecision:
        enrolled = enrolled_payload.get("embedding")
        if not enrolled:
            return SpeakerDecision(
                verified=False,
                confidence=0.0,
                threshold=threshold if threshold is not None else self.threshold,
                algorithm=self.name,
                reason="no enrolled voiceprint",
            )
        embedding = await self._embed(sample)
        similarity = cosine_similarity(embedding, enrolled)
        threshold = (
            threshold
            if threshold is not None
            else enrolled_payload.get("threshold", self.threshold)
        )
        return SpeakerDecision(
            verified=similarity >= threshold,
            confidence=round(similarity, 4),
            threshold=threshold,
            algorithm=self.name,
            speaker_id="owner",
            reason="voiceprint match" if similarity >= threshold else "voiceprint mismatch",
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def default_speaker_verifier() -> SpeakerVerifier:
    """Config-driven verifier selection, fail-closed in production.

    ``hash`` (and an unset ``EV_VOICEPRINT_PROVIDER``) resolves to the test
    double and is **refused outside pytest**. ``campp`` is the recommended
    production engine; ``speechbrain`` and ``http`` are supported alternatives.
    """

    provider = (settings.voiceprint_provider or "").strip().lower()
    if provider in ("", "hash", "profile-v1"):
        if not is_test_runtime():
            raise RuntimeError(
                "EV_VOICEPRINT_PROVIDER resolves to the hash test double, which is "
                "not a security control. Set EV_VOICEPRINT_PROVIDER=campp "
                "(recommended), speechbrain, or http before starting the voice path."
            )
        return HashTestDoubleSpeakerVerifier()
    if provider == "campp":
        return CamppSpeakerVerifier()
    if provider == "speechbrain":
        return SpeechBrainSpeakerVerifier()
    if provider == "http":
        return HttpSpeakerVerifier()
    raise RuntimeError(
        f"Unknown EV_VOICEPRINT_PROVIDER={provider!r} "
        "(expected hash | campp | speechbrain | http)"
    )


def _main(argv: Sequence[str] | None = None) -> int:
    """Calibration CLI: python -m app.voice.speaker owner.csv impostor.csv.

    Each CSV contains one score per cell/row (owner trials and impostor
    trials). Prints EER, the EER threshold, the FAR=0 operating threshold,
    TAR at that threshold, and optionally writes the ROC curve.

    ``eval`` mode runs the full measured gate end to end:

        python -m app.voice.speaker eval --owner-dir <wavs> --impostor-dir <wavs> \
            --roc-out roc.csv --report eval/ml/speaker_security.json

    It enrolls from the owner directory, scores every owner and impostor WAV,
    computes EER + the FAR=0 shipped threshold, emits the ROC, and writes the
    ``ev.speaker.eval.v1`` artifact (the same schema ``ev-eval speaker``
    consumes). Production refuses to run without the real CAM++ weights; under
    pytest the deterministic test double exercises the same harness.

    ``capture`` mode is the guided enrollment recorder (16 kHz mono, varied
    phrasing and distance) shared with Agent 3's ears capture layer.

    ``replay-test`` mode drives the physical loudspeaker replay acceptance
    test: wake, play the owner's own enrollment audio through the speakers,
    and attempt verification 20 times.
    """

    import argparse
    import csv
    import json

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["eval"]:
        return _eval_main(raw_argv[1:])
    if raw_argv[:1] == ["capture"]:
        return _capture_main(raw_argv[1:])
    if raw_argv[:1] == ["replay-test"]:
        return _replay_main(raw_argv[1:])

    parser = argparse.ArgumentParser(
        prog="python -m app.voice.speaker",
        description="Calibrate the speaker-verification threshold from scored trials",
    )
    parser.add_argument("owner_scores", help="CSV/text file of owner trial scores")
    parser.add_argument("impostor_scores", help="CSV/text file of impostor trial scores")
    parser.add_argument("--roc-out", default=None, help="optional ROC CSV output path")
    args = parser.parse_args(argv)

    def load_scores(path: str) -> list[float]:
        values: list[float] = []
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                for cell in row:
                    cell = cell.strip()
                    if cell:
                        values.append(float(cell))
        return values

    result = calibrate_operating_point(
        load_scores(args.owner_scores),
        load_scores(args.impostor_scores),
    )
    print(json.dumps(result, indent=2))
    if args.roc_out:
        with open(args.roc_out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["fpr", "tpr", "threshold"])
            writer.writerows(result["roc"])
    return 0


def _eval_main(argv: Sequence[str]) -> int:
    """End-to-end EER/ROC evaluation over owner and impostor WAV directories."""

    import argparse
    import csv
    import json

    parser = argparse.ArgumentParser(
        prog="python -m app.voice.speaker eval",
        description=(
            "Enroll owner WAVs, score owner + impostor WAVs, and emit the "
            "calibrated FAR=0 threshold with ROC"
        ),
    )
    parser.add_argument("--owner-dir", required=True, help="directory of owner 16 kHz WAVs (>=5)")
    parser.add_argument(
        "--impostor-dir",
        required=True,
        help="directory of impostor 16 kHz WAVs (>=50 for the acceptance gate)",
    )
    parser.add_argument("--roc-out", default=None, help="optional ROC CSV output path")
    parser.add_argument("--min-impostors", type=int, default=50)
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "write the ev.speaker.eval.v1 artifact (defaults to the ev-eval "
            "canonical path when --report=canonical)"
        ),
    )
    parser.add_argument(
        "--test-double",
        action="store_true",
        help=(
            "explicitly use the deterministic hash test double for a dry run; "
            "the resulting threshold is NOT a production threshold"
        ),
    )
    args = parser.parse_args(argv)

    if args.report == "canonical":
        args.report = str(
            Path(__file__).resolve().parents[2] / "eval" / "ml" / "speaker_security.json"
        )

    owner_dir = Path(args.owner_dir).expanduser().resolve()
    impostor_dir = Path(args.impostor_dir).expanduser().resolve()
    if not owner_dir.is_dir() or not impostor_dir.is_dir():
        raise SystemExit("owner-dir and impostor-dir must be readable directories")
    owner_files = _iter_wavs(owner_dir)
    impostor_files = _iter_wavs(impostor_dir)
    if len(owner_files) < 5:
        raise SystemExit(f"need at least 5 owner WAVs, found {len(owner_files)}")
    if len(impostor_files) < args.min_impostors:
        raise SystemExit(
            f"need at least {args.min_impostors} impostor WAVs, found {len(impostor_files)}"
        )

    # The operator explicitly supplied these directories, so they become the
    # audio_ref allowlist for this eval run (server/client inputs stay closed).
    os.environ["EV_VOICE_AUDIO_ALLOWED_DIRS"] = os.pathsep.join(
        [str(owner_dir), str(impostor_dir)]
    )

    import asyncio

    async def run() -> dict:
        if args.test_double:
            verifier: SpeakerVerifier = HashTestDoubleSpeakerVerifier()
            print(
                "WARNING: --test-double dry run. Scores are byte-fingerprints, "
                "not vocal identity; do not ship this threshold.",
                file=sys.stderr,
            )
        else:
            verifier = CamppSpeakerVerifier(require_available=False)
        payload = await verifier.enroll(
            [{"audio_ref": str(path)} for path in owner_files],
            reason="calibration enrollment",
        )
        if payload.get("degraded") and not args.test_double:
            print(
                "WARNING: encoder is degraded (test double). This run does not "
                "produce a production threshold.",
                file=sys.stderr,
            )
        owner_scores: list[float] = []
        impostor_scores: list[float] = []
        for path in owner_files:
            decision = await verifier.verify(
                {"audio_ref": str(path)},
                enrolled_payload=payload,
            )
            owner_scores.append(decision.confidence)
        for path in impostor_files:
            decision = await verifier.verify(
                {"audio_ref": str(path)},
                enrolled_payload=payload,
            )
            impostor_scores.append(decision.confidence)
        result = calibrate_operating_point(owner_scores, impostor_scores)
        false_accepts = sum(1.0 for score in impostor_scores if score >= result["threshold"])
        result["far_at_threshold"] = round(false_accepts / len(impostor_scores), 6)
        result["false_accepts_at_threshold"] = int(false_accepts)
        result["tar_at_threshold"] = round(
            sum(1.0 for score in owner_scores if score >= result["threshold"])
            / len(owner_scores),
            4,
        )
        result["algorithm"] = payload.get("algorithm")
        result["degraded"] = bool(payload.get("degraded"))
        return result

    result = asyncio.run(run())
    result["schema"] = "ev.speaker.eval.v1"
    result["schema_version"] = "ev.speaker.eval.v1"
    result["producer"] = "app.voice.speaker"
    result.setdefault("generated_at", _utc_now_iso())
    print(json.dumps(result, indent=2))
    print(f"SHIPPED_THRESHOLD={result['threshold']}")
    if args.roc_out:
        with open(args.roc_out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["fpr", "tpr", "threshold"])
            writer.writerows(result["roc"])
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"REPORT_WRITTEN={report_path}")
    return 0


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


ENROLLMENT_PROMPTS: tuple[tuple[str, str], ...] = (
    ("the sun rises in the east", "normal voice, arm's length from the microphone"),
    ("my favorite color is blue", "from across the room"),
    ("I am speaking to EVIE", "quiet voice, close to the microphone"),
    ("tomorrow is another day", "normal voice, arm's length from the microphone"),
    ("coffee before everything", "from across the room"),
    ("the password is safe with me", "louder voice, far from the microphone"),
)


def _open_mic_stream(device: str | None = None):
    """Open Agent 3's 16 kHz mono capture stream (shared ears capture layer)."""
    from app.audio.capture import MicrophoneStream

    return MicrophoneStream(sample_rate=16000, device=device)


def _confirm_prompt(prompt: str) -> str:
    return input(prompt)


def _poll_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def validate_capture_wav(
    raw: bytes,
    *,
    min_duration: float = 2.0,
    rms_floor: float = 0.005,
) -> dict:
    """Validate one enrollment WAV: 16 kHz, mono, 16-bit, audible, long enough."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            nframes = wav.getnframes()
            frames = wav.readframes(nframes)
    except (wave.Error, EOFError) as exc:
        return {"ok": False, "reason": f"not a WAV file: {exc}"}
    if channels != 1:
        return {"ok": False, "reason": f"expected mono, got {channels} channels"}
    if width != 2:
        return {"ok": False, "reason": f"expected 16-bit PCM, got {width * 8}-bit"}
    if rate != 16000:
        return {"ok": False, "reason": f"expected 16 kHz, got {rate} Hz"}
    duration = nframes / rate if rate else 0.0
    if duration < min_duration:
        return {
            "ok": False,
            "reason": f"too short: {duration:.2f}s < {min_duration:.2f}s",
        }
    import array

    samples = array.array("h", frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return {"ok": False, "reason": "no audio samples"}
    rms = math.sqrt(sum((sample / 32768.0) ** 2 for sample in samples) / len(samples))
    if rms < rms_floor:
        return {"ok": False, "reason": f"too quiet: rms={rms:.4f} < {rms_floor:.4f}"}
    return {
        "ok": True,
        "rate": rate,
        "channels": channels,
        "duration_s": round(duration, 2),
        "rms": round(rms, 4),
    }


def _validate_capture_dir(
    dir_path: Path,
    *,
    min_duration: float,
    rms_floor: float,
) -> int:
    files = _iter_wavs(dir_path)
    if not files:
        print(f"no WAV files in {dir_path}", file=sys.stderr)
        return 1
    valid = 0
    failed = 0
    for path in files:
        result = validate_capture_wav(
            path.read_bytes(),
            min_duration=min_duration,
            rms_floor=rms_floor,
        )
        detail = (
            f"{result['duration_s']}s rms={result['rms']}"
            if result["ok"]
            else result["reason"]
        )
        print(f"{'OK  ' if result['ok'] else 'FAIL'} {path.name}: {detail}")
        valid += 1 if result["ok"] else 0
        failed += 0 if result["ok"] else 1
    if valid < 5:
        print(f"need at least 5 valid samples, found {valid}", file=sys.stderr)
        return 1
    if failed:
        print(
            f"{failed} sample(s) are invalid; remove or re-record them, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"validated {valid} samples in {dir_path}")
    return 0


def _play_wav(raw: bytes) -> None:
    """Play one WAV through the default output device (loudspeaker attack)."""
    import sounddevice as sd

    waveform, rate = decode_waveform(raw)
    sd.play(waveform, samplerate=rate)
    sd.wait()


def _replay_main(argv: Sequence[str]) -> int:
    """Physical loudspeaker replay test: 20 fresh wake+verify attempts.

    Each round wakes a new session, plays the owner's own enrollment WAV
    through the loudspeaker, and verifies it. The hard gate is 0 accepts.
    """
    import argparse
    import asyncio
    import base64
    import contextlib
    import json

    parser = argparse.ArgumentParser(
        prog="python -m app.voice.speaker replay-test",
        description=(
            "Play the owner's enrollment audio through a loudspeaker and "
            "attempt verification 20 times; exits 0 only on zero accepts"
        ),
    )
    parser.add_argument("--api-url", required=True, help="base URL of the running EV API")
    parser.add_argument("--api-key", default=None, help="Bearer token for the device")
    parser.add_argument("--device-id", default="mac-replay")
    parser.add_argument("--enrollment-wav", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument(
        "--no-playback",
        action="store_true",
        help="skip actual loudspeaker playback (API-only rehearsal)",
    )
    args = parser.parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("rounds must be >= 1")

    wav_path = Path(args.enrollment_wav).expanduser().resolve()
    raw = wav_path.read_bytes()
    validation = validate_capture_wav(raw)
    if not validation["ok"]:
        print(f"enrollment wav invalid: {validation['reason']}", file=sys.stderr)
        return 2
    audio_b64 = base64.b64encode(raw).decode("ascii")

    async def run() -> int:
        import httpx

        headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
        async with httpx.AsyncClient(
            base_url=args.api_url,
            headers=headers,
            timeout=30.0,
        ) as client:
            attempts: list[dict] = []
            accepts = 0
            for round_no in range(1, args.rounds + 1):
                wake = await client.post(
                    "/v1/voice/wake",
                    json={"device_id": args.device_id, "text_hint": "hey evie"},
                )
                wake.raise_for_status()
                wake_body = wake.json()
                session_id = wake_body.get("session_id")
                nonce = wake_body.get("challenge_nonce")
                phrase = wake_body.get("challenge_phrase")
                if not args.no_playback:
                    _play_wav(raw)
                    _poll_sleep(args.settle_seconds)
                verify = await client.post(
                    "/v1/voice/verify",
                    json={
                        "session_id": session_id,
                        "nonce": nonce,
                        "phrase": phrase,
                        "samples": [audio_b64],
                    },
                )
                body = verify.json() if verify.content else {}
                accepted = bool(body.get("verified"))
                accepts += 1 if accepted else 0
                attempts.append(
                    {
                        "round": round_no,
                        "accepted": accepted,
                        "status": verify.status_code,
                        "reason": body.get("reason"),
                    }
                )
                if session_id:
                    with contextlib.suppress(Exception):
                        await client.post(
                            f"/v1/voice/sessions/{session_id}/end",
                            json={"reason": "replay test"},
                        )
            report = {
                "rounds": args.rounds,
                "accepts": accepts,
                "passed": accepts == 0,
                "attempts": attempts,
            }
            print(json.dumps(report, indent=2))
            print(f"REPLAY_ACCEPTS={accepts}")
            return 0 if accepts == 0 else 1

    return asyncio.run(run())


def _capture_main(argv: Sequence[str]) -> int:
    """Guided enrollment capture: 16 kHz mono, varied phrasing and distance."""
    import argparse
    import array
    import time

    parser = argparse.ArgumentParser(
        prog="python -m app.voice.speaker capture",
        description=(
            "Guided owner voiceprint enrollment: record 16 kHz mono WAVs with "
            "varied phrasing and distance. Uses Agent 3's ears capture layer."
        ),
    )
    parser.add_argument("--out-dir", default="owner_samples", help="output directory")
    parser.add_argument("--samples", type=int, default=6, help="samples to record (>=5)")
    parser.add_argument("--device", default=None, help="PortAudio device index or name")
    parser.add_argument("--seconds", type=float, default=4.0, help="recording length per sample")
    parser.add_argument("--min-duration", type=float, default=2.0)
    parser.add_argument("--rms-floor", type=float, default=0.005)
    parser.add_argument(
        "--from-dir",
        default=None,
        help="validate already-recorded WAVs instead of recording (no mic needed)",
    )
    args = parser.parse_args(argv)

    if args.from_dir:
        return _validate_capture_dir(
            Path(args.from_dir).expanduser().resolve(),
            min_duration=args.min_duration,
            rms_floor=args.rms_floor,
        )
    if args.samples < 5:
        raise SystemExit("at least 5 enrollment samples are required")
    if args.samples > len(ENROLLMENT_PROMPTS):
        raise SystemExit(f"only {len(ENROLLMENT_PROMPTS)} guided prompts exist")

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        stream = _open_mic_stream(args.device)
    except Exception as exc:
        print(f"microphone unavailable: {exc}", file=sys.stderr)
        print(
            "If your samples are already recorded, run with --from-dir DIR to validate them.",
            file=sys.stderr,
        )
        return 1

    target_samples = max(1, int(args.seconds * 16000))
    written = 0
    for index in range(1, args.samples + 1):
        phrase, guidance = ENROLLMENT_PROMPTS[index - 1]
        saved = False
        for attempt in range(1, 4):
            print(f"\nSample {index}/{args.samples} (attempt {attempt})")
            print(f'  Say: "{phrase}"')
            print(f"  Distance/volume: {guidance}.")
            _confirm_prompt("  Press Enter when ready, then speak the full length... ")
            collected = array.array("h")
            deadline = time.monotonic() + args.seconds
            with stream:
                while len(collected) < target_samples and time.monotonic() < deadline:
                    collected.extend(stream.ring.read_new())
                    _poll_sleep(0.05)
            from app.audio.capture import pcm_to_wav_bytes

            raw = pcm_to_wav_bytes(collected)
            validation = validate_capture_wav(
                raw,
                min_duration=args.min_duration,
                rms_floor=args.rms_floor,
            )
            if not validation["ok"]:
                print(f"  Rejected: {validation['reason']}. Please try again.")
                continue
            out_path = out_dir / f"owner-{index:02d}.wav"
            out_path.write_bytes(raw)
            written += 1
            saved = True
            print(
                f"  Saved {out_path} "
                f"({validation['duration_s']}s, rms={validation['rms']})."
            )
            break
        if not saved:
            print(f"Sample {index} failed after 3 attempts; re-run capture to retry.", file=sys.stderr)
            return 1

    print(f"\nCaptured {written} enrollment samples in {out_dir}.")
    print(
        "Next: run `python -m app.voice.speaker eval --owner-dir <dir> "
        "--impostor-dir <voxceleb> --report canonical` to calibrate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
