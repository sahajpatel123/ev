"""Tests for the ears capture core: ring buffer, mic stream, VAD, loop."""

from __future__ import annotations

import array
import asyncio
import json
import random

import pytest

from app.audio.capture import (
    MicrophoneDeniedError,
    MicrophoneStream,
    MicrophoneUnavailableError,
    list_input_devices,
    pcm_to_wav_bytes,
    wav_pcm16_samples,
)
from app.audio.diarize import (
    DiarizationConsentError,
    DiarizationUnavailableError,
    SpeakerTurn,
    diarize_meeting,
)
from app.audio.ring import PCM16RingBuffer, pcm16_bytes
from app.audio.vad import (
    EnergyVad,
    SileroVadOnnx,
    StreamingSegmenter,
    segment_utterances,
)
from app.voice.contracts import WakeDetection
from clients.ears.main import EarConfig, run_ears


class FakeIndata:
    """Minimal stand-in for a numpy input buffer in the PortAudio callback."""

    def __init__(self, values, ndim=1):
        self._values = list(values)
        self.ndim = ndim

    def astype(self, dtype):
        return self

    def reshape(self, *args):
        return self

    def tolist(self):
        return self._values

    def __getitem__(self, key):
        if isinstance(key, tuple) and key[1] == 0:
            return FakeIndata(self._values[0::2], ndim=1)
        return FakeIndata(self._values, ndim=1)


class FakeInputStream:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.callback = kwargs["callback"]

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeSoundDevice:
    def __init__(self, *, open_error=None, devices=None):
        self.open_error = open_error
        self._devices = devices
        self.last_kwargs = None

    def InputStream(self, **kwargs):
        self.last_kwargs = kwargs
        if self.open_error is not None:
            raise self.open_error
        return FakeInputStream(kwargs)

    def query_devices(self, device=None):
        if self._devices is None:
            raise RuntimeError("no audio host")
        if device is None:
            return self._devices
        return self._devices[int(device)]


def test_ring_wraps_and_keeps_newest() -> None:
    ring = PCM16RingBuffer(10)
    assert ring.capacity == 16
    assert ring.capacity_seconds(16000) == pytest.approx(16 / 16000)
    ring.write([1, 2, 3])
    assert ring.read_new().tolist() == [1, 2, 3]
    ring.write(list(range(100)))
    newest = ring.read_new()
    assert len(newest) == 16
    assert newest.tolist() == list(range(84, 100))
    # read_last is non-destructive
    ring.write([7] * 5)
    assert ring.read_last(3).tolist() == [7, 7, 7]
    assert len(ring.read_new()) == 5
    ring.clear()
    assert len(ring.read_new()) == 0


def test_ring_accepts_bytes_and_roundtrips_pcm16() -> None:
    ring = PCM16RingBuffer(8)
    ring.write(array.array("h", [1, -2, 3]).tobytes())
    assert ring.read_new().tolist() == [1, -2, 3]
    assert pcm16_bytes([1, -2, 3]) == array.array("h", [1, -2, 3]).tobytes()
    assert pcm16_bytes(b"\x01\x00") == b"\x01\x00"


def test_mic_stream_callback_writes_ring() -> None:
    fake = FakeSoundDevice()
    stream = MicrophoneStream(sounddevice_module=fake, sample_rate=16000, block_ms=20)
    stream.open()
    assert fake.last_kwargs["samplerate"] == 16000
    assert fake.last_kwargs["channels"] == 1
    assert fake.last_kwargs["dtype"] == "int16"
    callback = fake.last_kwargs["callback"]
    callback(FakeIndata([100, -100, 200]), 3, None, None)
    assert stream.ring.read_new().tolist() == [100, -100, 200]
    callback(FakeIndata([1, 2, 3, 4, 5, 6], ndim=2), 3, None, None)
    assert stream.ring.read_new().tolist() == [1, 3, 5]
    stream.close()
    assert fake.last_kwargs is not None


def test_mic_permission_denied_is_loud() -> None:
    fake = FakeSoundDevice(open_error=Exception("Error opening stream: Device unavailable"))
    stream = MicrophoneStream(sounddevice_module=fake)
    with pytest.raises(MicrophoneDeniedError, match="Microphone permission"):
        stream.open()


def test_missing_sounddevice_is_loud(monkeypatch) -> None:
    def boom():
        raise ImportError("no sounddevice")

    monkeypatch.setattr("app.audio.capture._import_sounddevice", boom)
    with pytest.raises(MicrophoneUnavailableError, match="sounddevice"):
        list_input_devices()


def test_list_input_devices_filters_inputs() -> None:
    fake = FakeSoundDevice(
        devices=[
            {"name": "Built-in Output", "max_input_channels": 0},
            {"name": "Built-in Microphone", "max_input_channels": 2, "default_samplerate": 48000},
        ]
    )
    devices = list_input_devices(fake)
    assert len(devices) == 1
    assert devices[0]["name"] == "Built-in Microphone"


def test_pcm_to_wav_roundtrip() -> None:
    samples = array.array("h", [100, -200, 300])
    data = pcm_to_wav_bytes(samples, 16000)
    decoded, rate = wav_pcm16_samples(data)
    assert rate == 16000
    assert decoded.tolist() == [100, -200, 300]


def _speech_block(seed: int = 3, length: int = 320) -> array.array:
    rng = random.Random(seed)
    return array.array("h", (rng.randint(-6000, 6000) for _ in range(length)))


def _silence_block(length: int = 320) -> array.array:
    return array.array("h", [0] * length)


async def test_streaming_segmenter_applies_pre_and_post_roll() -> None:
    seg = StreamingSegmenter(
        sample_rate=16000,
        pre_roll_s=0.04,
        post_roll_s=0.06,
        min_speech_s=0.02,
        speech_threshold=0.5,
    )
    pre_roll = _silence_block()
    for _ in range(2):
        assert seg.push(_silence_block(), 0.05, pre_roll_samples=pre_roll) is None
    for _ in range(5):
        assert seg.push(_speech_block(), 0.9, pre_roll_samples=pre_roll) is None
    for _ in range(3):
        assert seg.push(_silence_block(), 0.05, pre_roll_samples=pre_roll) is None
    tail = seg.flush()
    assert tail is not None
    # 1 pre-roll block (320) + 5 speech (1600) + 3 post-roll blocks (960)
    assert len(tail.samples) == 320 + 1600 + 960
    assert seg.flush() is None


async def test_streaming_segmenter_caps_memory() -> None:
    seg = StreamingSegmenter(
        sample_rate=16000,
        pre_roll_s=0.0,
        post_roll_s=0.0,
        min_speech_s=0.0,
        max_segment_s=0.1,  # 1600 samples
    )
    segments = []
    for _ in range(10):
        emitted = seg.push(_speech_block(), 0.9)
        if emitted is not None:
            segments.append(emitted)
    assert len(segments) == 2
    assert all(len(s.samples) <= 1600 for s in segments)


async def test_energy_vad_and_offline_segmentation() -> None:
    engine = EnergyVad()
    samples = _silence_block() * 2 + _speech_block(seed=5) * 8 + _silence_block() * 2
    segments = await segment_utterances(
        engine,
        samples,
        sample_rate=16000,
        pre_roll_s=0.02,
        post_roll_s=0.02,
        min_speech_s=0.02,
    )
    assert len(segments) == 1
    assert segments[0].engine == "energy"
    assert len(segments[0].samples) > 8 * 320


def test_silero_onnx_session_contract() -> None:
    np = pytest.importorskip("numpy")

    class FakeSession:
        def __init__(self, probability):
            self.probability = probability

        def run(self, outputs, feed):
            return [np.asarray([[self.probability]], dtype=np.float32)]

    vad = SileroVadOnnx(session_factory=lambda: FakeSession(0.9))
    probabilities = asyncio.run(vad.frame_probabilities([0] * 2048, 16000))
    assert probabilities == pytest.approx([0.9, 0.9, 0.9, 0.9])
    assert asyncio.run(vad.block_probability([0] * 512, 16000)) == pytest.approx(0.9)
    # A partial 320-sample block buffers internally and yields no decision.
    assert asyncio.run(vad.block_probability([0] * 320, 16000)) is None


def test_diarization_requires_consent(tmp_path) -> None:
    with pytest.raises(DiarizationConsentError, match="explicit consent"):
        diarize_meeting(tmp_path / "meeting.wav", consent=False)
    with pytest.raises(DiarizationUnavailableError, match="not found"):
        diarize_meeting(tmp_path / "meeting.wav", consent=True)


def test_diarization_fake_pipeline_returns_turns(tmp_path) -> None:
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"RIFF")

    class FakeTurn:
        start = 1.0
        end = 2.5

    class FakeDiarization:
        def itertracks(self, yield_label=True):
            yield FakeTurn(), None, "SPEAKER_00"

    class FakePipeline:
        def __call__(self, path):
            return FakeDiarization()

    turns = diarize_meeting(audio, consent=True, pipeline_factory=FakePipeline)
    assert turns == [SpeakerTurn(start_s=1.0, end_s=2.5, speaker="SPEAKER_00")]


def test_ears_wake_schema_contract() -> None:
    from app.schemas import EarsWakeRequest, EarsWakeResponse

    request = EarsWakeRequest(device_id="mac-ears", frames_b64="AAAA", consent=True)
    assert request.sample_rate == 16000
    assert request.consent is True
    response = EarsWakeResponse(accepted=True, message="ok")
    assert response.accepted is True


def test_ears_selfcheck_reports_state_without_crashing() -> None:
    from clients.ears.selfcheck import check_environment

    report = check_environment()
    assert set(report["dependencies"]) >= {"numpy", "sounddevice", "openwakeword"}
    assert "models" in report
    assert "data" in report
    assert "microphone" in report
    assert isinstance(report["arbiter"], dict)


def test_capture_plan_naming_and_missing_report(tmp_path) -> None:
    from app.audio.capture_eval import CapturePlan, missing_report

    plan = CapturePlan(out_dir=tmp_path, ambient_minutes=1)
    assert plan.is_far(3) is True
    assert "-3m" in plan.clip_path(3).name
    assert "-close" in plan.clip_path(1).name
    assert plan.negative_path(1).name == "negative-01.wav"
    report = missing_report(plan)
    assert report["clips"]["required"] == 30
    assert report["clips"]["present"] == 0
    assert report["far_clips"]["required"] == 10
    assert any("3 m" in item for item in report["still_missing"])
    assert any("ambient" in item for item in report["still_missing"])


def test_ingest_ambient_counts_toward_missing_report(tmp_path) -> None:
    from app.audio.capture_eval import (
        CapturePlan,
        ingest_ambient,
        missing_report,
        wav_duration,
    )

    plan = CapturePlan(out_dir=tmp_path / "wake", ambient_minutes=1)
    source = tmp_path / "existing.wav"
    source.write_bytes(pcm_to_wav_bytes(array.array("h", [0] * 16000), 16000))
    chunks = ingest_ambient(source, plan)
    assert len(chunks) == 1
    assert wav_duration(chunks[0]) == pytest.approx(1.0)
    report = missing_report(plan)
    assert report["ambient_seconds"] == pytest.approx(1.0)


async def test_run_ears_simulated_resource_report(tmp_path) -> None:
    wav = tmp_path / "sim.wav"
    wav.write_bytes(pcm_to_wav_bytes(array.array("h", [0] * 1600), 16000))
    resource = tmp_path / "resources.json"
    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        simulate_wav=str(wav),
        resource_report=str(resource),
        duration_s=1.0,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
    )
    stats = await run_ears(
        cfg,
        wake_engine=FakeWakeEngine(),
        vad_engine=EnergyVad(),
    )
    assert stats.blocks == 5
    data = json.loads(resource.read_text(encoding="utf-8"))
    assert data["rss_max_mb"] > 0
    assert data["avg_cpu_fraction"] >= 0
    assert data["blocks"] == 5
    assert data["bounded"]["max_segment_samples"] > 0


class FakeRing:
    def __init__(self, blocks):
        self._blocks = list(blocks)
        self.capacity = 16000 * 10

    def read_new(self):
        if not self._blocks:
            return array.array("h")
        return self._blocks.pop(0)

    def read_last(self, count):
        return array.array("h", [0] * min(count, 320))

    def __len__(self):
        return sum(len(b) for b in self._blocks)


class FakeStream:
    def __init__(self, blocks):
        self.ring = FakeRing(blocks)
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False


class FakeWakeEngine:
    name = "fake"

    async def detect(self, **kwargs):
        return WakeDetection(
            triggered=True,
            confidence=0.99,
            device_id=kwargs.get("device_id"),
            details={"engine": "fake"},
        )


async def test_run_ears_delivers_wake_utterance_only_with_consent() -> None:
    blocks = [_silence_block()] * 2 + [_speech_block(seed=9) for _ in range(8)] + [_silence_block()] * 3
    sent: list[dict] = []

    async def fake_sender(**kwargs):
        sent.append(kwargs)
        return {"sent": True, "status": 202}

    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
        api_url="http://127.0.0.1:9",
        consent=True,
        duration_s=0.5,
    )
    stats = await run_ears(
        cfg,
        stream=FakeStream(blocks),
        wake_engine=FakeWakeEngine(),
        vad_engine=EnergyVad(),
        sender=fake_sender,
    )
    assert stats.wake_hits == 1
    assert stats.utterances_sent == 1
    assert len(sent) == 1
    assert sent[0]["cfg"].consent is True
    assert "frames_b64" in sent[0]


async def test_run_ears_blocks_delivery_without_consent() -> None:
    blocks = [_speech_block(seed=4) for _ in range(6)] + [_silence_block()] * 3
    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
        consent=False,
        duration_s=0.5,
    )
    stats = await run_ears(
        cfg,
        stream=FakeStream(blocks),
        wake_engine=FakeWakeEngine(),
        vad_engine=EnergyVad(),
    )
    assert stats.wake_hits == 1
    assert stats.utterances_sent == 0
