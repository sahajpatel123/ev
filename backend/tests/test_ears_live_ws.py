"""Offline tests for the ears full-duplex WS client and barge-in playback stop.

No network and no real ``afplay``: the WebSocket and the playback subprocess
are fakes, so the tests are deterministic and safe to run anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app.audio.ring import pcm16_bytes
from app.voice.contracts import WakeDetection
from clients.ears import main as ears_main
from clients.ears.live import EarsLiveChannel, EarsLivePlayer, EarsLiveUnavailable
from clients.ears.main import EarConfig


def _speech_block(seed: int = 3, length: int = 320) -> bytes:
    import random

    rng = random.Random(seed)
    return pcm16_bytes(rng.randint(-6000, 6000) for _ in range(length))


def _silence_block(length: int = 320) -> bytes:
    return pcm16_bytes([0] * length)


class FakeWs:
    """Minimal ``websockets`` connection double."""

    def __init__(self, first: dict | None = None) -> None:
        self.outgoing: list[bytes | str] = []
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.closed = False
        if first is not None:
            self._queue.put_nowait(first)

    def queue_event(self, event: dict | None) -> None:
        self._queue.put_nowait(event)

    async def recv(self) -> str | bytes:
        event = await self._queue.get()
        if event is None:
            raise ConnectionError("closed")
        return json.dumps(event)

    async def send(self, payload: str | bytes) -> None:
        self.outgoing.append(payload)

    async def close(self) -> None:
        self.closed = True
        self.queue_event(None)


class FakeProcess:
    """Minimal asyncio subprocess double."""

    def __init__(self) -> None:
        self.terminated = False
        self.returncode: int | None = None
        self._waited = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        self._waited = True
        return self.returncode or 0


class FakeLiveChannel:
    """Controllable live socket double used by run_ears tests."""

    def __init__(self) -> None:
        self.closed = False
        self.pcm_chunks: list[bytes] = []
        self.sent_text: list[dict] = []
        self._events: asyncio.Queue[dict | None] = asyncio.Queue()

    def queue_event(self, event: dict) -> None:
        self._events.put_nowait(event)

    async def receive(self) -> dict:
        event = await self._events.get()
        if event is None:
            raise ConnectionError("closed")
        return event

    def offer_pcm(self, pcm: bytes) -> None:
        self.pcm_chunks.append(bytes(pcm))

    async def send_pcm(self, pcm: bytes) -> None:
        self.pcm_chunks.append(bytes(pcm))

    async def send_json(self, payload: dict) -> None:
        self.sent_text.append(payload)

    async def send_text(self, text: str, *, commit: bool = True) -> None:
        self.sent_text.append({"type": "text", "text": text, "commit": commit})

    async def send_control(self, action: str) -> None:
        self.sent_text.append({"type": "control", "action": action})

    async def send_audio_segment(self, pcm: bytes, *, chunk_size: int = 32_000) -> None:
        del chunk_size
        self.pcm_chunks.append(bytes(pcm))
        self.sent_text.append({"type": "speech", "active": False})

    async def close(self) -> None:
        self.closed = True
        self.queue_event(None)


class StubPlayer:
    """Records playback commands without spawning afplay."""

    def __init__(self, **kwargs) -> None:
        del kwargs
        self.stop_calls = 0
        self.enqueued: list[dict] = []
        self.closed = False

    async def start(self) -> None:
        pass

    def enqueue(self, *, audio_b64: str | None = None, audio_ref: str | None = None) -> None:
        self.enqueued.append({"audio_b64": audio_b64, "audio_ref": audio_ref})

    async def stop(self) -> None:
        self.stop_calls += 1

    async def aclose(self) -> None:
        self.closed = True


async def test_live_channel_open_validates_ready_and_streams_pcm() -> None:
    """The channel connects, validates ``ready``, and sends queued PCM bytes."""

    ws = FakeWs(first={"type": "ready", "at_ms": 0})
    calls: list[tuple[str, str | None]] = []

    async def fake_connect(url: str, *, api_key: str | None) -> FakeWs:
        calls.append((url, api_key))
        return ws

    channel = await EarsLiveChannel.open(
        api_url="http://127.0.0.1:9",
        session_id="s1",
        api_key="secret",
        connect=fake_connect,
    )
    assert calls[0][0].startswith("ws://127.0.0.1:9/v1/voice/live?session_id=s1")
    assert calls[0][1] == "secret"

    payload = _speech_block()
    channel.offer_pcm(payload)
    for _ in range(100):
        if ws.outgoing:
            break
        await asyncio.sleep(0.01)
    assert ws.outgoing and ws.outgoing[0] == payload

    await channel.close()
    assert channel.closed


async def test_live_channel_open_rejects_handshake_error() -> None:
    """A fatal server error on connect surfaces as EarsLiveUnavailable."""

    ws = FakeWs(
        first={
            "type": "error",
            "code": "session_not_live",
            "message": "wake EVIE first",
            "fatal": True,
        }
    )

    async def fake_connect(url: str, *, api_key: str | None) -> FakeWs:
        del url, api_key
        return ws

    with pytest.raises(EarsLiveUnavailable) as exc:
        await EarsLiveChannel.open(
            api_url="http://127.0.0.1:9",
            session_id="s1",
            api_key=None,
            connect=fake_connect,
        )
    assert exc.value.code == "session_not_live"


async def test_live_player_stop_terminates_process_and_drops_queue() -> None:
    """barge-in stops the current afplay process and discards queued chunks."""

    spawned: list[FakeProcess] = []
    idle_calls = 0

    async def fake_spawn(path: str) -> FakeProcess:
        del path
        process = FakeProcess()
        spawned.append(process)
        return process

    def on_idle() -> None:
        nonlocal idle_calls
        idle_calls += 1

    player = EarsLivePlayer(spawn=fake_spawn, on_idle=on_idle)
    await player.start()
    player.enqueue(audio_b64=base64.b64encode(b"RIFF-fake-audio").decode("ascii"))
    for _ in range(100):
        if spawned:
            break
        await asyncio.sleep(0.01)
    assert spawned, "playback worker never spawned afplay"

    player.enqueue(audio_b64=base64.b64encode(b"second-chunk").decode("ascii"))
    await player.stop()

    assert spawned[0].terminated
    assert idle_calls >= 1
    await player.aclose()


class FakeLiveOpen:
    """Class-level double for EarsLiveChannel inside run_ears."""

    instances: list[FakeLiveChannel] = []

    @classmethod
    async def open(cls, *, api_url: str, session_id: str, api_key: str | None = None):
        del api_url, session_id, api_key
        channel = FakeLiveChannel()
        cls.instances.append(channel)
        return channel


class FakeWakeOnce:
    """Wake engine that triggers exactly once, then stays silent."""

    name = "fake-once"

    def __init__(self) -> None:
        self._triggered = False

    async def detect(self, **kwargs) -> WakeDetection:
        if self._triggered:
            return WakeDetection(triggered=False, confidence=0.0, device_id=kwargs.get("device_id"))
        self._triggered = True
        return WakeDetection(
            triggered=True,
            confidence=0.99,
            device_id=kwargs.get("device_id"),
            details={"engine": "fake-once"},
        )


async def test_run_ears_streams_blocks_to_live_and_stops_on_barge_in(
    monkeypatch,
) -> None:
    """After wake, ears feeds raw PCM to the live socket and stops on barge_in."""

    from app.audio.vad import EnergyVad

    blocks = [_silence_block()] * 2 + [_speech_block(seed=9) for _ in range(8)] + [_silence_block()] * 3
    stream = _FakeStream(blocks)
    wake = FakeWakeOnce()
    players: list[StubPlayer] = []
    FakeLiveOpen.instances.clear()

    def fake_player_factory(**kwargs):
        player = StubPlayer(**kwargs)
        players.append(player)
        return player

    async def fake_sender(**kwargs):
        return {
            "sent": True,
            "status": 202,
            "accepted": True,
            "listening": True,
            "session_id": "live-session-1",
            "state": "awake",
        }

    monkeypatch.setattr(ears_main, "EarsLiveChannel", FakeLiveOpen)
    monkeypatch.setattr(ears_main, "EarsLivePlayer", fake_player_factory)

    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
        api_url="http://127.0.0.1:9",
        consent=True,
        live_enabled=True,
        duration_s=0.6,
    )
    stats_task = asyncio.create_task(ears_main.run_ears(
        cfg,
        stream=stream,
        wake_engine=wake,
        vad_engine=EnergyVad(),
        sender=fake_sender,
    ))

    for _ in range(200):
        if FakeLiveOpen.instances:
            break
        await asyncio.sleep(0.01)
    assert FakeLiveOpen.instances, "live channel was never opened"
    channel = FakeLiveOpen.instances[0]
    for _ in range(200):
        if channel.pcm_chunks:
            break
        await asyncio.sleep(0.01)
    assert channel.pcm_chunks, "live channel never received raw PCM"

    # The owner interrupts while EVIE is speaking: playback must stop.
    channel.queue_event({"type": "barge_in", "reason": "user_speech"})
    channel.queue_event({"type": "error", "code": "session_ended", "fatal": True})

    stats = await asyncio.wait_for(stats_task, timeout=10.0)
    assert stats.utterances_sent >= 1
    assert players, "live player was never created"
    assert players[0].stop_calls >= 1, "barge_in did not stop playback"


async def test_run_ears_falls_back_to_sse_when_live_connect_fails(
    monkeypatch,
) -> None:
    """A refused live door must not crash ears; SSE follow-up takes over."""

    from app.audio.vad import EnergyVad

    blocks = [_silence_block()] * 2 + [_speech_block(seed=9) for _ in range(8)] + [_silence_block()] * 3
    stream = ears_main.FakeStream(blocks) if hasattr(ears_main, "FakeStream") else _FakeStream(blocks)
    fallback: list[dict] = []

    async def fake_follow_up(cfg, session_id, **kwargs):
        fallback.append({"cfg": cfg, "session_id": session_id, **kwargs})
        return {"listening": False, "session_id": "live-session-1"}

    async def fake_sender(**kwargs):
        return {
            "sent": True,
            "status": 202,
            "accepted": True,
            "listening": True,
            "session_id": "live-session-1",
            "state": "awake",
        }

    async def refuse_open(*, api_url: str, session_id: str, api_key: str | None = None):
        del api_url, session_id, api_key
        raise EarsLiveUnavailable("live_connect_failed", "refused")

    monkeypatch.setattr(ears_main, "EarsLiveChannel", type("Refused", (), {"open": refuse_open}))
    monkeypatch.setattr(ears_main, "stream_follow_up", fake_follow_up)

    cfg = EarConfig(
        sample_rate=16000,
        block_ms=20,
        vad_pre_roll_s=0.02,
        vad_post_roll_s=0.04,
        vad_min_speech_s=0.02,
        api_url="http://127.0.0.1:9",
        consent=True,
        live_enabled=True,
        duration_s=0.5,
    )
    stats = await ears_main.run_ears(
        cfg,
        stream=stream,
        wake_engine=FakeWakeOnce(),
        vad_engine=EnergyVad(),
        sender=fake_sender,
    )

    assert fallback, "expected SSE fallback when the live door is refused"
    assert stats.utterances_sent >= 1


class _FakeStream:
    """Fallback ring/stream double mirroring the one in test_audio_capture."""

    def __init__(self, blocks: list[bytes]) -> None:
        self.ring = _FakeRing(blocks)
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False


class _FakeRing:
    def __init__(self, blocks: list[bytes]) -> None:
        import array

        self._blocks = [array.array("h", b) if isinstance(b, bytes) else b for b in blocks]
        self.capacity = 16000 * 10

    def read_new(self):
        if not self._blocks:
            import array

            return array.array("h")
        return self._blocks.pop(0)

    def read_last(self, count: int):
        import array

        return array.array("h", [0] * min(count, 320))

    def __len__(self) -> int:
        return sum(len(b) for b in self._blocks)
