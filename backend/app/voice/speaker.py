"""Owner speaker verification.

Production intent is ECAPA-TDNN voiceprints (SpeechBrain ``spkrec-ecapa-voxceleb``,
192–512 dim) with asymmetric enroll/verify. The dev provider derives a
deterministic embedding from enrollment phrases so the owner-only gate and
threshold behavior are testable without model weights. Any ECAPA/on-device
provider can replace it by implementing :class:`SpeakerVerifier`.
"""

from __future__ import annotations

import base64
import hashlib
import math

from app.voice.contracts import SpeakerDecision, SpeakerVerifier
from app.voice.wake import normalize


def _token_embedding(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-token embedding with signed hash buckets."""
    vector = [0.0] * dim
    for token in normalize(text).split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _audio_embedding(sample: bytes, dim: int) -> list[float]:
    """Deterministic byte-level embedding for dev/test audio samples.

    Uses 128-byte windows so near-identical captures (a few changed bytes)
    still match; a production ECAPA-TDNN encoder replaces this entirely.
    """
    vector = [0.0] * dim
    for offset in range(0, len(sample), 128):
        chunk = sample[offset : offset + 128]
        digest = hashlib.sha256(chunk).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Embedding dimension mismatch")
    return sum(x * y for x, y in zip(a, b, strict=True))


class ProfileSpeakerVerifier:
    """Dev/test verifier: text/features → deterministic voiceprint embedding."""

    name = "profile-v1"
    embedding_dim = 192

    def __init__(self, dim: int = 192) -> None:
        self.embedding_dim = dim

    def _embedding_for(self, sample: dict) -> list[float]:
        features = sample.get("features")
        if features:
            if len(features) != self.embedding_dim:
                raise ValueError(
                    f"Expected {self.embedding_dim} features, got {len(features)}"
                )
            norm = math.sqrt(sum(v * v for v in features)) or 1.0
            return [v / norm for v in features]
        audio_b64 = sample.get("audio_b64")
        if audio_b64:
            try:
                raw = base64.b64decode(audio_b64, validate=True)
            except Exception as exc:
                raise ValueError("audio_b64 must be valid base64") from exc
            if not raw:
                raise ValueError("audio_b64 must not be empty")
            return _audio_embedding(raw, self.embedding_dim)
        text = sample.get("text")
        if not text:
            raise ValueError("Verification sample needs 'text', 'features', or 'audio_b64'")
        return _token_embedding(text, self.embedding_dim)

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        if len(samples) < 5:
            raise ValueError(f"Enrollment needs at least 5 samples, got {len(samples)}")
        embeddings = [self._embedding_for(sample) for sample in samples]
        mean = [sum(vals[i] for vals in embeddings) / len(embeddings) for i in range(self.embedding_dim)]
        norm = math.sqrt(sum(v * v for v in mean)) or 1.0
        return {
            "algorithm": self.name,
            "embedding": [v / norm for v in mean],
            "dim": self.embedding_dim,
            "threshold": 0.82,
            "sample_count": len(samples),
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
                threshold=threshold or 0.82,
                algorithm=self.name,
                reason="no enrolled voiceprint",
            )
        embedding = self._embedding_for(sample)
        similarity = cosine_similarity(embedding, enrolled)
        threshold = threshold if threshold is not None else enrolled_payload.get("threshold", 0.82)
        return SpeakerDecision(
            verified=similarity >= threshold,
            confidence=round(similarity, 4),
            threshold=threshold,
            algorithm=self.name,
            speaker_id="owner",
            reason="voiceprint match" if similarity >= threshold else "voiceprint mismatch",
        )


def default_speaker_verifier() -> SpeakerVerifier:
    return ProfileSpeakerVerifier()
