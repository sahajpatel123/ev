"""Anti-spoofing: server-side fingerprints, strict liveness, transcript-bound
challenge verification, and the audio-liveness model wiring."""

from __future__ import annotations

import base64
import hashlib
import io
import math
import struct
import wave
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, SessionLocal, engine
from app.models import VoiceAttemptLog
from app.voice.anti_spoof import (
    AudioFingerprint,
    AudioLivenessModel,
    LivenessGate,
    ReplayGuard,
    compute_audio_sha256,
    default_liveness_checker,
    transcript_matches_expected,
)


def make_wav(duration: float = 0.4, rate: int = 16000, phase: float = 0.0) -> bytes:
    frames = int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    int(
                        max(
                            -1.0,
                            min(1.0, math.sin(2 * math.pi * 220 * i / rate + phase)),
                        )
                        * 32767
                    ),
                )
                for i in range(frames)
            )
        )
    return buffer.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
async def db_session() -> AsyncSession:
    """Self-contained DB session (works with and without the shared conftest)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class FakeLivenessModel:
    def __init__(self, live: float = 0.97) -> None:
        self.live = live
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def score(self, raw: bytes) -> float:
        self.calls += 1
        return self.live


class FakeAsr:
    def __init__(self, text: str = "the sun rises in the east") -> None:
        self.text = text

    async def transcribe(self, *, audio_b64=None, audio_ref=None, **kwargs):
        return SimpleNamespace(text=self.text, confidence=0.98, provider="fake")


class FakeLivenessSession:
    def get_inputs(self):
        return [SimpleNamespace(name="waveform", type="tensor(float)")]

    def run(self, _outputs, inputs):
        assert len(inputs["waveform"]) == 1
        return [[0.91]]


# --------------------------------------------------------------------------- #
# Server-side fingerprint
# --------------------------------------------------------------------------- #


def test_compute_audio_sha256_is_server_side() -> None:
    raw = b"owner audio bytes"
    assert compute_audio_sha256(raw) == hashlib.sha256(raw).hexdigest()
    assert compute_audio_sha256(raw) != compute_audio_sha256(raw + b"x")


def test_transcript_matches_expected_is_normalized() -> None:
    assert transcript_matches_expected("The sun rises in the east!", "the sun rises in the east")
    assert transcript_matches_expected("sun rises in the east", "the sun rises in the east")
    assert not transcript_matches_expected("open the garage", "the sun rises in the east")


# --------------------------------------------------------------------------- #
# Strict liveness gate
# --------------------------------------------------------------------------- #


async def test_strict_gate_rejects_client_claims_without_audio() -> None:
    gate = LivenessGate(strict=True)
    ok, confidence, reason = await gate.check(
        sample={"liveness_proof": "live", "live_score": 0.99}
    )
    assert ok is False
    assert confidence == 0.0
    assert "audio evidence" in reason


async def test_strict_gate_fails_closed_without_liveness_model() -> None:
    gate = LivenessGate(strict=True)
    ok, _confidence, reason = await gate.check(
        sample={
            "audio_b64": b64(make_wav()),
            "liveness_proof": "live",
            "live_score": 0.99,
        },
        expected_phrase="the sun rises in the east",
        transcript="the sun rises in the east",
    )
    assert ok is False
    assert "model unavailable" in reason
    assert gate.last_audio_sha256 == hashlib.sha256(make_wav()).hexdigest()


async def test_strict_gate_passes_with_model_and_matching_transcript() -> None:
    model = FakeLivenessModel(0.97)
    gate = LivenessGate(strict=True, liveness_model=model)
    raw = make_wav()
    ok, confidence, reason = await gate.check(
        sample={"audio_b64": b64(raw)},
        expected_phrase="the sun rises in the east",
        transcript="The sun rises in the east!",
    )
    assert ok is True
    assert confidence == pytest.approx(0.97)
    assert "server-side" in reason
    assert model.calls == 1
    assert gate.last_audio_sha256 == hashlib.sha256(raw).hexdigest()
    assert gate.last_server_fingerprint == AudioFingerprint(hashlib.sha256(raw).hexdigest())


async def test_strict_gate_rejects_transcript_mismatch() -> None:
    gate = LivenessGate(strict=True, liveness_model=FakeLivenessModel())
    ok, _confidence, reason = await gate.check(
        sample={"audio_b64": b64(make_wav())},
        expected_phrase="the sun rises in the east",
        transcript="open the garage",
    )
    assert ok is False
    assert "mismatch" in reason


async def test_strict_gate_requires_transcript_for_challenge() -> None:
    gate = LivenessGate(strict=True, liveness_model=FakeLivenessModel())
    ok, _confidence, reason = await gate.check(
        sample={"audio_b64": b64(make_wav())},
        expected_phrase="the sun rises in the east",
    )
    assert ok is False
    assert "ASR transcript" in reason


async def test_strict_gate_uses_asr_seam_when_transcript_omitted() -> None:
    gate = LivenessGate(strict=True, liveness_model=FakeLivenessModel())
    ok, _confidence, reason = await gate.check(
        sample={"audio_b64": b64(make_wav())},
        expected_phrase="the sun rises in the east",
        asr=FakeAsr(),
    )
    assert ok is True
    assert "server-side" in reason


async def test_strict_gate_rejects_explicit_negative_proof_early() -> None:
    gate = LivenessGate(strict=True)
    ok, _confidence, reason = await gate.check(
        sample={"audio_b64": b64(make_wav()), "liveness_proof": "replay"}
    )
    assert ok is False
    assert "replay" in reason


async def test_check_with_evidence_exposes_server_fingerprint() -> None:
    gate = LivenessGate(strict=True, liveness_model=FakeLivenessModel())
    raw = make_wav()
    evidence = await gate.check_with_evidence(
        sample={"audio_b64": b64(raw)},
        transcript="the sun rises in the east",
    )
    assert evidence.ok is True
    assert evidence.audio_sha256 == hashlib.sha256(raw).hexdigest()
    assert evidence.server_fingerprint is not None
    assert evidence.server_fingerprint.sha256 == hashlib.sha256(raw).hexdigest()
    assert evidence.degraded is False


# --------------------------------------------------------------------------- #
# Dev/test double semantics (kept for offline CI)
# --------------------------------------------------------------------------- #


async def test_dev_gate_preserves_deterministic_semantics() -> None:
    gate = LivenessGate(strict=False)
    ok, confidence, _reason = await gate.check(sample={"liveness_proof": "live"})
    assert ok is True
    assert confidence == 1.0
    ok, _confidence, _reason = await gate.check(sample={"liveness_proof": "replay"})
    assert ok is False
    ok, _confidence, _reason = await gate.check(
        sample={"live_score": 0.4}
    )
    assert ok is False
    ok, _confidence, _reason = await gate.check(
        challenge_phrase="the sun rises in the east",
        expected_phrase="the sun rises in the east",
    )
    assert ok is True
    ok, _confidence, _reason = await gate.check(sample={})
    assert ok is False


def test_default_liveness_checker_is_strict_outside_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    gate = default_liveness_checker()
    assert isinstance(gate, LivenessGate)
    assert gate.strict is True


# --------------------------------------------------------------------------- #
# Replay fingerprint trust boundary
# --------------------------------------------------------------------------- #


async def test_replay_guard_strict_ignores_client_hashes(
    db_session: AsyncSession,
) -> None:
    guard = ReplayGuard(db_session, strict=True)
    db_session.add(
        VoiceAttemptLog(
            kind="verify",
            outcome="accepted",
            metadata_={"audio_sha256": "client-hash"},
        )
    )
    await db_session.flush()
    assert await guard.fingerprint_replayed("client-hash") is False
    assert (
        await guard.fingerprint_replayed(
            AudioFingerprint("client-hash", server_computed=True)
        )
        is True
    )
    assert (
        await guard.fingerprint_replayed(
            AudioFingerprint("client-hash", server_computed=False)
        )
        is False
    )


async def test_replay_guard_dev_mode_keeps_string_fingerprints(
    db_session: AsyncSession,
) -> None:
    guard = ReplayGuard(db_session, strict=False)
    db_session.add(
        VoiceAttemptLog(
            kind="verify",
            outcome="accepted",
            metadata_={"audio_sha256": "legacy-hash"},
        )
    )
    await db_session.flush()
    assert await guard.fingerprint_replayed("legacy-hash") is True


# --------------------------------------------------------------------------- #
# Audio liveness model
# --------------------------------------------------------------------------- #


def test_audio_liveness_model_scores_with_fake_session(tmp_path) -> None:
    model_path = tmp_path / "liveness-audio.onnx"
    model_path.write_bytes(b"fake")
    model = AudioLivenessModel(
        model_path=model_path,
        onnx_session_factory=lambda _path: FakeLivenessSession(),
    )
    assert model.available is True
    assert model.score(make_wav()) == pytest.approx(0.91)


def test_audio_liveness_model_unavailable_without_weights() -> None:
    model = AudioLivenessModel()
    assert model.available is False
    assert model.score(make_wav()) is None


def test_audio_liveness_model_fails_closed_with_onnxruntime_installed(
    tmp_path,
) -> None:
    pytest.importorskip("onnxruntime")
    model = AudioLivenessModel(model_path=tmp_path / "missing.onnx")
    assert model.available is False
    assert model.score(make_wav()) is None
