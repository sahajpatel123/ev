"""Speaker verification: production-refusal, CAM++/SpeechBrain/HTTP engines,
threshold calibration, and the audio_ref allowlist."""

from __future__ import annotations

import base64
import io
import json
import math
import random
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import MockTransport

from app.config import settings
from app.voice.speaker import (
    CamppSpeakerVerifier,
    HashTestDoubleSpeakerVerifier,
    HttpSpeakerVerifier,
    ProfileSpeakerVerifier,
    SpeechBrainSpeakerVerifier,
    calibrate_operating_point,
    decode_waveform,
    default_speaker_verifier,
    sample_audio_bytes,
)


def make_wav(
    *,
    duration: float = 0.5,
    rate: int = 16000,
    channels: int = 1,
    phase: float = 0.0,
) -> bytes:
    """Deterministic PCM WAV: phase 0 → positive start, phase pi → negative."""
    frames = int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        payload = b"".join(
            struct.pack(
                "<h",
                int(
                    max(
                        -1.0,
                        min(1.0, math.sin(2 * math.pi * 220 * i / rate + phase + channel * 0.25)),
                    )
                    * 32767
                ),
            )
            for i in range(frames)
            for channel in range(channels)
        )
        wav.writeframes(payload)
    return buffer.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _speaker_vector(owner: bool, dim: int = 192) -> list[float]:
    vector = [0.0] * dim
    vector[0] = 1.0
    vector[1] = 1.0 if owner else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class FakeCamppSession:
    """Minimal onnxruntime-shaped session (no numpy required)."""

    def __init__(self, dim: int = 192) -> None:
        self.dim = dim
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="speech", type="tensor(float)")]

    def run(self, _outputs, inputs):
        self.calls += 1
        audio = inputs["speech"][0]
        first = audio[1] if len(audio) > 1 else (audio[0] if audio else 0.0)
        return [_speaker_vector(first >= 0, self.dim)]


def _fake_campp_verifier(model_path, session: FakeCamppSession) -> CamppSpeakerVerifier:
    return CamppSpeakerVerifier(
        model_path=model_path,
        onnx_session_factory=lambda _path: session,
        threshold=0.72,
        require_available=False,
    )


# --------------------------------------------------------------------------- #
# Test double naming + production refusal
# --------------------------------------------------------------------------- #


def test_hash_double_is_renamed_and_alias_is_deprecated() -> None:
    assert HashTestDoubleSpeakerVerifier.__name__ == "HashTestDoubleSpeakerVerifier"
    assert ProfileSpeakerVerifier is HashTestDoubleSpeakerVerifier


def test_hash_double_selectable_only_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voiceprint_provider", "hash")
    assert isinstance(default_speaker_verifier(), HashTestDoubleSpeakerVerifier)


def test_hash_double_refused_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(settings, "voiceprint_provider", "hash")
    with pytest.raises(RuntimeError, match="not a security control"):
        default_speaker_verifier()
    monkeypatch.setattr(settings, "voiceprint_provider", "")
    with pytest.raises(RuntimeError, match="not a security control"):
        default_speaker_verifier()


def test_default_speaker_verifier_selects_real_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voiceprint_provider", "speechbrain")
    assert isinstance(default_speaker_verifier(), SpeechBrainSpeakerVerifier)
    monkeypatch.setattr(settings, "voiceprint_provider", "campp")
    assert isinstance(default_speaker_verifier(), CamppSpeakerVerifier)
    monkeypatch.setattr(settings, "voiceprint_provider", "bogus")
    with pytest.raises(RuntimeError, match="Unknown EV_VOICEPRINT_PROVIDER"):
        default_speaker_verifier()


# --------------------------------------------------------------------------- #
# CAM++ ONNX engine
# --------------------------------------------------------------------------- #


async def test_campp_enrolls_and_verifies_with_fake_onnx_session(tmp_path) -> None:
    model_path = tmp_path / "campp.onnx"
    model_path.write_bytes(b"not-real-onnx")
    session = FakeCamppSession()
    verifier = _fake_campp_verifier(model_path, session)

    owner_samples = [{"audio_b64": b64(make_wav(phase=0.0))} for _ in range(5)]
    payload = await verifier.enroll(owner_samples)
    assert payload["algorithm"] == "campp"
    assert payload["dim"] == 192
    assert payload["sample_count"] == 5
    assert payload["degraded"] is False
    assert payload["model"] == str(model_path.resolve())

    decision = await verifier.verify(owner_samples[0], enrolled_payload=payload)
    assert decision.verified is True
    assert decision.algorithm == "campp"
    impostor = await verifier.verify(
        {"audio_b64": b64(make_wav(phase=math.pi))},
        enrolled_payload=payload,
    )
    assert impostor.verified is False
    assert impostor.reason == "voiceprint mismatch"
    assert session.calls == 7


def test_campp_resolver_skips_unrelated_onnx(tmp_path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "embed-granite-r2.onnx").write_bytes(b"granite")
    (models / "face-sface.onnx").write_bytes(b"face")
    (models / "tts-kokoro-82m-int8.onnx").write_bytes(b"tts")
    (models / "campp.onnx").write_bytes(b"speaker")
    verifier = CamppSpeakerVerifier(
        model_path=models,
        require_available=False,
        onnx_session_factory=lambda path: FakeCamppSession(),
    )
    assert verifier._resolve_model_path() == (models / "campp.onnx").resolve()


def test_campp_fbank_layout_feeds_feats(tmp_path) -> None:
    pytest.importorskip("kaldi_native_fbank")
    class FakeFbankSession:
        def __init__(self) -> None:
            self.calls = 0
            self.last_feats = None

        def get_inputs(self):
            return [SimpleNamespace(name="feats", shape=["batch", "time", 80], type="tensor(float)")]

        def run(self, _outputs, inputs):
            self.calls += 1
            self.last_feats = inputs["feats"]
            return [_speaker_vector(True, 192)]

    model_path = tmp_path / "campp.onnx"
    model_path.write_bytes(b"fbank-onnx")
    session = FakeFbankSession()
    verifier = CamppSpeakerVerifier(
        model_path=model_path,
        onnx_session_factory=lambda _path: session,
        require_available=False,
        threshold=0.5,
    )
    waveform = [0.01 * ((i % 17) - 8) for i in range(16000)]
    vector = verifier._embed_onnx(session, waveform)
    assert len(vector) == 192
    assert session.calls == 1
    feats = session.last_feats
    assert feats is not None
    # [B, T, 80]
    shape = getattr(feats, "shape", None)
    if shape is not None:
        assert len(shape) == 3
        assert shape[0] == 1
        assert shape[2] == 80
    else:
        assert isinstance(feats, list) and len(feats[0][0]) == 80


async def test_campp_degrades_to_test_double_only_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = CamppSpeakerVerifier(require_available=False)
    samples = [{"audio_b64": b64(make_wav())} for _ in range(5)]
    payload = await verifier.enroll(samples)
    assert payload["degraded"] is True
    decision = await verifier.verify(samples[0], enrolled_payload=payload)
    assert decision.verified is True

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="refusing the voice path"):
        await verifier.enroll(samples)


def test_campp_refuses_production_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="not available"):
        CamppSpeakerVerifier(require_available=True)


def test_campp_require_available_accepts_existing_model(tmp_path) -> None:
    model_path = tmp_path / "campp.onnx"
    model_path.write_bytes(b"fake")
    verifier = CamppSpeakerVerifier(model_path=model_path, require_available=True)
    assert str(verifier._resolve_model_path()) == str(model_path.resolve())


def test_campp_missing_model_fails_closed_with_onnxruntime(
    tmp_path,
) -> None:
    pytest.importorskip("onnxruntime")
    verifier = CamppSpeakerVerifier(
        model_path=tmp_path / "missing.onnx",
        require_available=False,
    )
    with pytest.raises(RuntimeError, match="model file is not available"):
        verifier._load_session()


async def test_campp_requires_five_samples(tmp_path) -> None:
    model_path = tmp_path / "campp.onnx"
    model_path.write_bytes(b"fake")
    verifier = _fake_campp_verifier(model_path, FakeCamppSession())
    with pytest.raises(ValueError, match="at least 5"):
        await verifier.enroll([{"audio_b64": b64(make_wav())}] * 4)


# --------------------------------------------------------------------------- #
# SpeechBrain real decode path (skips when weights/deps are absent)
# --------------------------------------------------------------------------- #


def test_speechbrain_waveform_decode_skips_without_torch() -> None:
    pytest.importorskip("torch")
    verifier = SpeechBrainSpeakerVerifier()
    waveform = verifier._waveform(make_wav())
    assert waveform.dim() == 1


def test_speechbrain_real_encoder_runs_when_weights_exist() -> None:
    pytest.importorskip("speechbrain")
    savedir = settings.voiceprint_model_dir
    if not savedir or not Path(savedir).exists():
        pytest.skip("speechbrain model weights not present")
    verifier = SpeechBrainSpeakerVerifier()
    embedding = verifier._encode_sync({"audio_b64": b64(make_wav())})
    assert len(embedding) == 192


# --------------------------------------------------------------------------- #
# HTTP provider (real branch, gate-enforced)
# --------------------------------------------------------------------------- #


def test_http_verifier_gate_denied_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", raising=False)
    with pytest.raises(RuntimeError, match="denied"):
        HttpSpeakerVerifier(base_url="https://encoder.test")


def test_http_verifier_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", "true")
    with pytest.raises(RuntimeError, match="EV_VOICEPRINT_BASE_URL"):
        HttpSpeakerVerifier()


async def test_http_verifier_enrolls_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", "true")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embed"
        payload = json.loads(request.content)
        raw = base64.b64decode(payload["audio_b64"])
        with wave.open(io.BytesIO(raw), "rb") as audio:
            frames = audio.readframes(audio.getnframes())
        first = struct.unpack("<h", frames[2:4])[0] if len(frames) >= 4 else 0
        return httpx.Response(200, json={"embedding": _speaker_vector(first >= 0)})

    client = httpx.AsyncClient(transport=MockTransport(handler))
    verifier = HttpSpeakerVerifier(
        base_url="https://encoder.test",
        client=client,
        threshold=0.72,
    )
    owner = [{"audio_b64": b64(make_wav(phase=0.0))} for _ in range(5)]
    payload = await verifier.enroll(owner)
    assert payload["algorithm"] == "http"
    assert payload["dim"] == 192
    decision = await verifier.verify(owner[0], enrolled_payload=payload)
    assert decision.verified is True
    impostor = await verifier.verify(
        {"audio_b64": b64(make_wav(phase=math.pi))},
        enrolled_payload=payload,
    )
    assert impostor.verified is False
    await client.aclose()


# --------------------------------------------------------------------------- #
# audio_ref allowlist
# --------------------------------------------------------------------------- #


def test_audio_ref_disabled_without_allowlist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "clip.wav"
    audio.write_bytes(make_wav())
    monkeypatch.delenv("EV_VOICE_AUDIO_ALLOWED_DIRS", raising=False)
    with pytest.raises(ValueError, match="disabled"):
        sample_audio_bytes({"audio_ref": str(audio)})


def test_audio_ref_outside_allowlist_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "clip.wav"
    audio.write_bytes(make_wav())
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EV_VOICE_AUDIO_ALLOWED_DIRS", str(allowed))
    with pytest.raises(ValueError, match="outside"):
        sample_audio_bytes({"audio_ref": str(audio)})
    inside = allowed / "clip.wav"
    inside.write_bytes(make_wav())
    assert sample_audio_bytes({"audio_ref": str(inside)}) == make_wav()


def test_audio_ref_symlink_escape_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret.wav"
    secret.write_bytes(make_wav())
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EV_VOICE_AUDIO_ALLOWED_DIRS", str(allowed))
    link = allowed / "link.wav"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside"):
        sample_audio_bytes({"audio_ref": str(link)})


def test_decode_waveform_resamples_and_mono() -> None:
    raw = make_wav(duration=0.5, rate=8000, channels=2, phase=math.pi / 2)
    waveform, rate = decode_waveform(raw)
    assert rate == 16000
    assert len(waveform) == 8000
    assert all(-1.0 <= value <= 1.0 for value in waveform)
    with pytest.raises(ValueError, match="RIFF"):
        decode_waveform(b"not a wav")
    pcm = (b"\x00\x10" * 2000)
    waveform, rate = decode_waveform(pcm)
    assert rate == 16000
    assert len(waveform) == 2000


# --------------------------------------------------------------------------- #
# Threshold calibration
# --------------------------------------------------------------------------- #


def test_calibrate_operating_point_ships_far_zero() -> None:
    rng = random.Random(7)
    owners = [0.75 + 0.05 * rng.gauss(0, 1) for _ in range(40)]
    impostors = [0.45 + 0.05 * rng.gauss(0, 1) for _ in range(60)]
    result = calibrate_operating_point(owners, impostors)
    assert result["owner_count"] == 40
    assert result["impostor_count"] == 60
    assert all(score < result["threshold"] for score in impostors)
    assert 0.0 <= result["eer"] < 0.05
    assert result["tar_at_far0"] >= 0.9
    assert result["roc"]
    assert result["roc"][0] == [1.0, 1.0, pytest.approx(min(owners + impostors))]


def test_calibrate_requires_both_classes() -> None:
    with pytest.raises(ValueError, match="at least one"):
        calibrate_operating_point([0.8], [])


def test_calibrate_keeps_owner_boundary_when_no_higher_candidate() -> None:
    result = calibrate_operating_point([1.0, 1.0, 1.0], [0.1, 0.2, 0.3])
    assert result["threshold"] == 1.0
    assert result["tar_at_far0"] == 1.0
    assert all(score < result["threshold"] for score in [0.1, 0.2, 0.3])


def test_eval_cli_runs_end_to_end_and_writes_roc(
    tmp_path,
    capsys,
) -> None:
    import json

    from app.voice.speaker import _eval_main

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    impostor_dir = tmp_path / "impostor"
    impostor_dir.mkdir()
    owner_wav = make_wav(duration=0.5, phase=0.0)
    for index in range(5):
        (owner_dir / f"owner-{index}.wav").write_bytes(owner_wav)
    for index in range(50):
        (impostor_dir / f"imp-{index}.wav").write_bytes(
            make_wav(duration=0.3 + index * 0.001, phase=math.pi + index * 0.001)
        )
    roc_path = tmp_path / "roc.csv"

    exit_code = _eval_main(
        [
            "--owner-dir",
            str(owner_dir),
            "--impostor-dir",
            str(impostor_dir),
            "--roc-out",
            str(roc_path),
            "--report",
            str(tmp_path / "speaker_security.json"),
            "--test-double",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SHIPPED_THRESHOLD=" in captured.out
    assert "REPORT_WRITTEN=" in captured.out
    assert '"impostor_count": 50' in captured.out
    assert roc_path.is_file()
    rows = roc_path.read_text().strip().splitlines()
    assert rows[0] == "fpr,tpr,threshold"
    assert len(rows) > 2

    artifact = json.loads((tmp_path / "speaker_security.json").read_text())
    assert artifact["schema"] == "ev.speaker.eval.v1"
    assert artifact["schema_version"] == "ev.speaker.eval.v1"
    assert artifact["producer"] == "app.voice.speaker"
    assert artifact["degraded"] is True
    assert artifact["eer"] >= 0.0
    assert artifact["false_accepts_at_threshold"] == 0
    assert artifact["far_at_threshold"] == 0.0
    assert artifact["tar_at_threshold"] == 1.0
    assert artifact["impostor_count"] == 50
    assert isinstance(artifact["roc"], list) and artifact["roc"]


class FakeMicStream:
    """MicrophoneStream-shaped fake: 16 kHz mono PCM16 sine blocks."""

    def __init__(self, chunk: int = 800) -> None:
        self.chunk = chunk
        self.position = 0
        self.ring = SimpleNamespace(read_new=self._read_new)

    def _read_new(self):
        import array

        out = array.array(
            "h",
            (
                int(5000 * math.sin((self.position + index) / 40))
                for index in range(self.chunk)
            ),
        )
        self.position += self.chunk
        return out

    def __enter__(self) -> FakeMicStream:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def test_capture_guided_records_valid_wavs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.speaker import _capture_main, validate_capture_wav

    monkeypatch.setattr("app.voice.speaker._open_mic_stream", lambda device=None: FakeMicStream())
    monkeypatch.setattr("app.voice.speaker._confirm_prompt", lambda prompt: "")
    monkeypatch.setattr("app.voice.speaker._poll_sleep", lambda seconds: None)

    out_dir = tmp_path / "owner"
    exit_code = _capture_main(
        ["--out-dir", str(out_dir), "--samples", "6", "--seconds", "3.0"]
    )
    assert exit_code == 0
    files = sorted(out_dir.glob("owner-*.wav"))
    assert len(files) == 6
    for path in files:
        result = validate_capture_wav(path.read_bytes())
        assert result["ok"], (path.name, result)
        assert result["rate"] == 16000
        assert result["channels"] == 1
        assert result["duration_s"] >= 2.0


def test_capture_requires_at_least_five_samples(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.speaker import _capture_main

    monkeypatch.setattr("app.voice.speaker._open_mic_stream", lambda device=None: FakeMicStream())
    with pytest.raises(SystemExit, match="at least 5"):
        _capture_main(["--out-dir", str(tmp_path / "x"), "--samples", "4"])


def test_capture_from_dir_validates_existing_wavs(tmp_path) -> None:
    from app.voice.speaker import _capture_main

    samples = tmp_path / "samples"
    samples.mkdir()
    for index in range(5):
        (samples / f"ok-{index}.wav").write_bytes(make_wav(duration=2.5, rate=16000))
    (samples / "bad-rate.wav").write_bytes(make_wav(duration=2.5, rate=8000))

    assert _capture_main(["--from-dir", str(samples)]) == 1
    (samples / "bad-rate.wav").unlink()
    assert _capture_main(["--from-dir", str(samples)]) == 0


def test_eval_discovers_nested_impostor_wavs(tmp_path, capsys) -> None:
    from app.voice.speaker import _eval_main

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    impostor_dir = tmp_path / "impostor" / "speaker-a" / "session1"
    impostor_dir.mkdir(parents=True)
    owner_wav = make_wav(duration=0.5, phase=0.0)
    for index in range(5):
        (owner_dir / f"owner-{index}.wav").write_bytes(owner_wav)
    for index in range(50):
        (impostor_dir / f"imp-{index}.wav").write_bytes(
            make_wav(duration=0.3 + index * 0.001, phase=math.pi + index * 0.001)
        )

    assert (
        _eval_main(
            [
                "--owner-dir",
                str(owner_dir),
                "--impostor-dir",
                str(tmp_path / "impostor"),
                "--test-double",
            ]
        )
        == 0
    )
    assert '"impostor_count": 50' in capsys.readouterr().out


def test_eval_refuses_degraded_artifact_without_test_double(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from app.voice.speaker import _eval_main

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    impostor_dir = tmp_path / "impostor"
    impostor_dir.mkdir()
    owner_wav = make_wav(duration=0.5, phase=0.0)
    for index in range(5):
        (owner_dir / f"owner-{index}.wav").write_bytes(owner_wav)
    for index in range(50):
        (impostor_dir / f"imp-{index}.wav").write_bytes(
            make_wav(duration=0.3 + index * 0.001, phase=math.pi + index * 0.001)
        )
    artifact = tmp_path / "speaker_security.json"

    # Outside pytest, the encoder is unavailable (no weights): refuse loudly.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert (
        _eval_main(
            [
                "--owner-dir",
                str(owner_dir),
                "--impostor-dir",
                str(impostor_dir),
                "--report",
                str(artifact),
            ]
        )
        == 2
    )
    assert "speaker eval refused" in capsys.readouterr().err
    assert not artifact.exists()


def test_replay_test_requires_zero_accepts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from app.voice.speaker import _replay_main

    wav_path = tmp_path / "owner.wav"
    wav_path.write_bytes(make_wav(duration=2.5, rate=16000))
    state = {"accept": False}
    session_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/voice/wake":
            session_counter["n"] += 1
            return httpx.Response(
                200,
                json={
                    "session_id": f"s{session_counter['n']}",
                    "challenge_nonce": f"n{session_counter['n']}",
                    "challenge_phrase": "the sun rises in the east",
                    "owner_enrolled": True,
                    "state": "verifying",
                },
            )
        if request.url.path == "/v1/voice/verify":
            return httpx.Response(
                200,
                json={
                    "verified": state["accept"],
                    "reason": "owner verified" if state["accept"] else "unknown voice",
                    "state": "awake" if state["accept"] else "ended",
                },
            )
        if request.url.path.endswith("/end"):
            return httpx.Response(200, json={"state": "ended"})
        return httpx.Response(404, json={"detail": "not found"})

    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs) -> httpx.AsyncClient:
        return real_async_client(
            transport=MockTransport(handler),
            base_url="http://test",
        )

    monkeypatch.setattr(
        "app.voice.speaker.httpx.AsyncClient",
        client_factory,
    )
    monkeypatch.setattr("app.voice.speaker._play_wav", lambda raw: None)
    monkeypatch.setattr("app.voice.speaker._poll_sleep", lambda seconds: None)

    assert (
        _replay_main(
            [
                "--api-url",
                "http://test",
                "--enrollment-wav",
                str(wav_path),
                "--rounds",
                "20",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "REPLAY_ACCEPTS=0" in out
    assert session_counter["n"] == 20

    state["accept"] = True
    assert (
        _replay_main(
            [
                "--api-url",
                "http://test",
                "--enrollment-wav",
                str(wav_path),
                "--rounds",
                "20",
            ]
        )
        == 1
    )
    assert "REPLAY_ACCEPTS=20" in capsys.readouterr().out


def test_enroll_convert_only_converts_and_validates(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from app.voice.speaker import _enroll_main

    source_dir = tmp_path / "voice-sample"
    source_dir.mkdir()
    for index in range(1, 6):
        (source_dir / f"Sample {index}.m4a").write_bytes(b"fake-m4a-bytes")

    def fake_run(args, **kwargs):
        dst = Path(args[-1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(make_wav(duration=2.5, rate=16000))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr("app.voice.speaker.subprocess.run", fake_run)
    assert (
        _enroll_main(
            [
                "--source-dir",
                str(source_dir),
                "--convert-only",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "CONVERTED=5 WAVS=5" in out
    wavs = sorted((source_dir / "wav").glob("*.wav"))
    assert len(wavs) == 5


def test_enroll_fails_closed_without_voiceprint_model(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from app.voice.speaker import _enroll_main

    monkeypatch.setattr(settings, "voiceprint_provider", "campp")
    monkeypatch.setattr(settings, "voiceprint_model_dir", str(tmp_path / "missing-campp"))
    wav_dir = tmp_path / "wav"
    wav_dir.mkdir()
    for index in range(5):
        (wav_dir / f"sample-{index}.wav").write_bytes(
            make_wav(duration=2.5, rate=16000)
        )

    assert (
        _enroll_main(
            [
                "--source-dir",
                str(wav_dir),
                "--out-dir",
                str(wav_dir),
                "--voiceprint-model",
                str(tmp_path / "no-such-campp.onnx"),
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "voiceprint model unavailable" in err
