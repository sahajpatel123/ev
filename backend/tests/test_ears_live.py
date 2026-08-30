"""Ears → WS /v1/voice/live transport and barge-in playback tests."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app.audio.vad import EnergyVad
from clients.ears.live import (
    EarsLiveChannel,
    EarsLivePlayer,
    EarsLiveUnavailable,
    live_ws_url,
)
from clients.ears.main import EarConfig, run_ears
from tests.test_audio_capture import FakeStream, FakeWakeEngine, _silence_block, _speech_block


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeWs:
    def __init__(self, events: list[dict]) -> None:
        self.events = list(events)
        self.sent: list[object] = []
        self.closed = False

    async def recv(self) -> str:
        if self.events:
            return json.dumps(self.events.pop(0))
        await asyncio.sleep(10)
        return "{}"

    async def send(self, payload: object) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def test_live_ws_url_keeps_scheme_and_session() -> None:
    assert live_ws_url("http://ev.local:8000", "abc") == (
        "ws://ev.local:8000/v1/voice/live?session_id=abc"
    )
    assert live_ws_url("https://ev.example.com", "abc") == (
        "wss://ev.example.com/v1/voice/live?session_id=abc"
    )


async def test_channel_rejects_error_handshake() -> None:
    ws = FakeWs([{"type": "error", "code": "live_disabled", "fatal": True}])

    async def connect(url: str, api_key: str | None = None) -> FakeWs:
        assert url.endswith("/v1/voice/live?session_id=sid")
        assert api_key == "k"
        return ws

    with pytest.raises(EarsLiveUnavailable) as exc:
        await EarsLiveChannel.open(
            api_url="http://ev.local:8000",
            session_id="sid",
            api_key="k",
            connect=connect,
        )
    assert exc.value.code == "live_disabled"
    assert ws.closed


async def test_channel_requires_ready_event() -> None:
    ws = FakeWs([{"type": "state"}])

    async def connect(url: str, api_key: str | None = None) -> FakeWs:
        return ws

    with pytest.raises(EarsLiveUnavailable) as exc:
        await EarsLiveChannel.open(
            api_url="http://ev.local:8000",
            session_id="sid",
            connect=connect,
        )
    assert exc.value.code == "live_not_ready"


async def test_channel_streams_pcm_and_marks_segment_end() -> None:
    ws = FakeWs([{"type": "ready", "session_id": "sid"}])

    async def connect(url: str, api_key: str | None = None) -> FakeWs:
        return ws

    channel = await EarsLiveChannel.open(
        api_url="http://ev.local:8000",
        session_id="sid",
        connect=connect,
    )
    channel.offer_pcm(b"\x01\x00")
    await channel.send_audio_segment(b"\x02\x00\x03\x00")
    await channel.close()

    binary = [frame for frame in ws.sent if isinstance(frame, bytes)]
    text = [frame for frame in ws.sent if isinstance(frame, str)]
    assert binary[0] == b"\x01\x00"
    assert b"\x02\x00\x03\x00" in binary
    assert any('"speech"' in frame and '"active": false' in frame for frame in text)
    assert ws.closed


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.returncode: int | None = None
        self._done = asyncio.Event()

    async def wait(self) -> None:
        await self._done.wait()

    def terminate(self) -> None:
        self.terminated = True
        self._done.set()


async def test_player_stops_current_process_on_barge_in() -> None:
    spawned: list[FakeProcess] = []
    idle: list[bool] = []

    async def spawn(path: str) -> FakeProcess:
        process = FakeProcess()
        spawned.append(process)
        return process

    player = EarsLivePlayer(spawn=spawn, on_idle=lambda: idle.append(True))
    await player.start()
    player.enqueue(audio_b64=_b64(b"audio-bytes"))
    await asyncio.sleep(0.05)
    assert spawned and not spawned[0].terminated

    await player.stop()
    assert spawned[0].terminated
    assert idle == [True]
    await player.aclose()


class FakeLiveChannel:
    instances: list[FakeLiveChannel] = []

    def __init__(self) -> None:
        self.closed_flag = False
        self.close_event = asyncio.Event()
        self.events: list[dict] = []
        self.pcm_offered: list[bytes] = []
        self.sent_text: list[str] = []
        self.sent_audio: list[bytes] = []
        FakeLiveChannel.instances.append(self)

    @property
    def closed(self) -> bool:
        return self.closed_flag

    @classmethod
    async def open(cls, *, api_url: str, session_id: str, api_key: str | None = None) -> FakeLiveChannel:
        return cls()

    def offer_pcm(self, pcm: bytes) -> None:
        self.pcm_offered.append(pcm)

    async def send_audio_segment(self, pcm: bytes, *, chunk_size: int = 32000) -> None:
        self.sent_audio.append(pcm)

    async def send_text(self, text: str, *, commit: bool = True) -> None:
        self.sent_text.append(text)

    async def send_control(self, action: str) -> None:
        pass

    async def receive(self) -> dict:
        if self.events:
            return self.events.pop(0)
        await self.close_event.wait()
        raise ConnectionError("channel closed")

    async def close(self) -> None:
        self.closed_flag = True
        self.close_event.set()


class FakePlayer:
    def __init__(self, **kwargs) -> None:
        self.enqueued: list[tuple[str | None, str | None]] = []
        self.stops = 0

    async def start(self) -> None:
        pass

    def enqueue(self, *, audio_b64: str | None = None, audio_ref: str | None = None) -> None:
        self.enqueued.append((audio_b64, audio_ref))

    async def stop(self) -> None:
        self.stops += 1

    async def aclose(self) -> None:
        pass


class YieldingEnergyVad(EnergyVad):
    """Yield to the event loop on every block so ingest tasks interleave."""

    async def block_probability(self, samples, sample_rate):
        await asyncio.sleep(0)
        return await super().block_probability(samples, sample_rate)


async def test_run_ears_streams_blocks_and_stops_playback_on_barge_in(monkeypatch) -> None:
    FakeLiveChannel.instances.clear()
    channel = FakeLiveChannel()
    channel.events = [
        {"type": "tts_chunk", "audio_b64": _b64(b"chunk"), "text": "hi"},
        {"type": "barge_in", "reason": "user_speech"},
    ]
    player = FakePlayer()

    async def open_channel(*args, **kwargs) -> FakeLiveChannel:
        return channel

    def make_player(*args, **kwargs) -> FakePlayer:
        return player

    monkeypatch.setattr("clients.ears.main.EarsLiveChannel.open", open_channel)
    monkeypatch.setattr("clients.ears.main.EarsLivePlayer", make_player)

    async def fake_sender(**kwargs) -> dict:
        return {
            "sent": True,
            "accepted": True,
            "listening": True,
            "session_id": "11111111-1111-1111-1111-111111111111",
        }

    blocks = (
        [_silence_block()] * 2
        + [_speech_block(seed=4) for _ in range(8)]
        + [_silence_block()] * 20
    )
    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
        idle_min_rms=0.0,
        idle_min_peak=0,
        api_url="http://127.0.0.1:9",
        consent=True,
        live_enabled=True,
        duration_s=0.8,
    )
    stats = await run_ears(
        cfg,
        stream=FakeStream(blocks),
        wake_engine=FakeWakeEngine(),
        vad_engine=YieldingEnergyVad(),
        sender=fake_sender,
    )

    assert stats.utterances_sent >= 1
    assert channel.pcm_offered, "raw blocks should stream once the live door is open"
    assert player.enqueued
    assert player.stops >= 1, "barge_in must stop playback"
    assert channel.closed_flag
