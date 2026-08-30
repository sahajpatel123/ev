"""End-to-end EV LIVE WebSocket transport: protocol in, events out.

Drives ``serve_live_websocket`` with a fake socket so the whole
recv → LiveSession → responder → send path is exercised without a real
server, database, or audio stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from uuid import UUID

from httpx import AsyncClient

from app.voice.live.events import BargeInEvent, ErrorEvent, FinalTranscriptEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.session import LiveSession
from app.voice.live.transport import serve_live_websocket
from tests.test_voice_lifecycle import grant_voice_consent


class FakeWebSocket:
    """Minimal FastAPI WebSocket double used by the transport layer."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.sent_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        await self.sent_queue.put(data)

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        del reason
        self.closed = True
        self.close_code = code

    async def put_text(self, payload: dict) -> None:
        await self.incoming.put(
            {"type": "websocket.receive", "text": json.dumps(payload)}
        )

    async def put_disconnect(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect"})

    async def next_event(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.sent_queue.get(), timeout=timeout)


async def test_live_ws_roundtrip_text_turn() -> None:
    """A committed text turn flows through the socket into TTS + reply events."""

    async def responder(text: str, envelope) -> object:
        assert text == "What's the weather?"
        assert envelope is not None
        yield TtsChunkEvent(at_ms=1, index=0, text="Let me check.", audio_b64="QUJD")
        yield ReplyEvent(at_ms=2, text="Let me check.", conversation_id="c1")

    ws = FakeWebSocket()
    session = LiveSession(synthesizer=None, transcriber=None, respond=responder)
    server = asyncio.create_task(serve_live_websocket(ws, live=session, tick_ms=20))
    try:
        ready = await ws.next_event()
        assert ready["type"] == "ready"
        assert ready["config"]["sample_rate"] == 16000

        await ws.put_text({"type": "text", "text": "What's the weather?", "commit": True})
        seen: list[dict] = []
        while True:
            event = await ws.next_event()
            seen.append(event)
            if event["type"] == "reply":
                break

        kinds = [event["type"] for event in seen]
        assert "final_transcript" in kinds
        assert "tts_chunk" in kinds
        assert kinds[-1] == "reply"
        chunk = next(event for event in seen if event["type"] == "tts_chunk")
        assert chunk["text"] == "Let me check."
        assert chunk["audio_b64"] == "QUJD"
        reply = seen[-1]
        assert reply["conversation_id"] == "c1"
    finally:
        await ws.put_disconnect()
        await asyncio.wait_for(server, timeout=5.0)


async def test_live_outbound_audio_preserves_chunk_order() -> None:
    """A burst of streamed audio never creates missing syllables."""

    session = LiveSession(synthesizer=None, transcriber=None)
    for index in range(7):
        await session.emit(
            TtsChunkEvent(
                at_ms=index,
                index=index,
                text="" if index else "Hello",
                audio_b64="QUJD",
                duration_ms=160,
            )
        )

    queued_audio = [event for event in session.outbound._queue if event.type == "tts_chunk"]
    assert len(queued_audio) == 7
    assert [event.index for event in queued_audio] == list(range(7))

    # A late user transcript is the turn she is answering. Dropping queued
    # speech here is what made replies stall mid-sentence.
    await session.emit(
        FinalTranscriptEvent(at_ms=20, text="new question", provider="text")
    )
    kinds = [event.type for event in session.outbound._queue]
    assert kinds.count("tts_chunk") == 7
    assert kinds[-1] == "final_transcript"

    await session.emit(BargeInEvent(at_ms=21, reason="user_speech"))
    assert [event.type for event in session.outbound._queue] == [
        "final_transcript",
        "barge_in",
    ]


async def test_live_outbound_audio_is_released_at_render_speed() -> None:
    """A fast producer cannot put a whole reply into the playback backlog."""

    session = LiveSession(synthesizer=None, transcriber=None)
    await session.emit(
        TtsChunkEvent(
            at_ms=1,
            index=0,
            text="first",
            audio_b64="QUJD",
            duration_ms=80,
        )
    )
    started = asyncio.get_running_loop().time()
    await session.emit(
        TtsChunkEvent(
            at_ms=2,
            index=1,
            text="second",
            audio_b64="QUJD",
            duration_ms=80,
        )
    )
    assert asyncio.get_running_loop().time() - started >= 0.06


async def test_live_s2s_audio_survives_its_own_transcript() -> None:
    """Realtime audio must not wait on pacing or die when the transcript lands."""

    session = LiveSession(synthesizer=None, transcriber=None)
    started = asyncio.get_running_loop().time()
    await session.emit(
        TtsChunkEvent(
            at_ms=1,
            index=0,
            text="hello",
            audio_b64="QUJD",
            duration_ms=400,
            provider="openai-realtime",
        )
    )
    await session.emit(
        TtsChunkEvent(
            at_ms=2,
            index=1,
            text="",
            audio_b64="QUJD",
            duration_ms=400,
            provider="grok-voice",
        )
    )
    assert asyncio.get_running_loop().time() - started < 0.15
    await session.emit(
        FinalTranscriptEvent(at_ms=3, text="hello", provider="openai-realtime")
    )
    kinds = [event.type for event in session.outbound._queue]
    assert kinds.count("tts_chunk") == 2
    assert kinds[-1] == "final_transcript"


async def test_live_boundary_cancels_waiting_audio() -> None:
    """A new turn wakes a paced producer instead of leaving it stuck."""

    session = LiveSession(synthesizer=None, transcriber=None)
    await session.emit(
        TtsChunkEvent(
            at_ms=1,
            index=0,
            text="first",
            audio_b64="QUJD",
            duration_ms=500,
        )
    )
    waiting = asyncio.create_task(
        session.emit(
            TtsChunkEvent(
                at_ms=2,
                index=1,
                text="stale",
                audio_b64="QUJD",
                duration_ms=500,
            )
        )
    )
    await asyncio.sleep(0.01)
    await session.emit(BargeInEvent(at_ms=3, reason="user_speech"))
    await asyncio.wait_for(waiting, timeout=0.2)
    assert [event.type for event in session.outbound._queue] == ["barge_in"]


async def test_live_boundary_releases_a_full_queue_audio_waiter() -> None:
    """A blocked stale audio putter must not deadlock a new turn."""

    session = LiveSession(synthesizer=None, transcriber=None)
    for index in range(8):
        await session.emit(TtsChunkEvent(at_ms=index, index=index, text=""))
    waiting = asyncio.create_task(
        session.emit(
            TtsChunkEvent(
                at_ms=9,
                index=9,
                text="stale",
                audio_b64="QUJD",
                duration_ms=80,
            )
        )
    )
    await asyncio.sleep(0)
    await session.emit(BargeInEvent(at_ms=10, reason="user_speech"))
    await asyncio.wait_for(waiting, timeout=0.2)
    assert [event.type for event in session.outbound._queue] == ["barge_in"]


async def test_live_realtime_disconnect_drops_old_playback() -> None:
    """A reconnect boundary must not replay audio from the dead provider."""

    session = LiveSession(synthesizer=None, transcriber=None)
    await session.emit(
        TtsChunkEvent(at_ms=1, index=0, text="old", audio_b64="QUJD", duration_ms=80)
    )
    await session.emit(
        ErrorEvent(
            at_ms=2,
            code="realtime_disconnect",
            message="provider disconnected",
            fatal=False,
        )
    )
    assert [event.type for event in session.outbound._queue] == ["error"]


async def test_live_ws_barge_in_cancels_in_flight_reply() -> None:
    """User speech during assistant playback cancels the pending responder."""

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def responder(text: str, envelope) -> object:
        del text, envelope
        started.set()
        try:
            yield TtsChunkEvent(at_ms=1, index=0, text="Here is a very long answer")
            await asyncio.sleep(30)
            yield ReplyEvent(at_ms=2, text="done")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ws = FakeWebSocket()
    session = LiveSession(synthesizer=None, transcriber=None, respond=responder)
    server = asyncio.create_task(serve_live_websocket(ws, live=session, tick_ms=20))
    try:
        assert (await ws.next_event())["type"] == "ready"
        await ws.put_text({"type": "text", "text": "Explain this in detail", "commit": True})
        await asyncio.wait_for(started.wait(), timeout=5.0)
        while (await ws.next_event())["type"] != "tts_chunk":
            pass

        # The owner starts talking while EVIE is still producing output.
        await ws.put_text({"type": "speech", "active": True})
        while (await ws.next_event())["type"] != "barge_in":
            pass
        for _ in range(50):
            if cancelled.is_set():
                break
            await asyncio.sleep(0.02)
        assert cancelled.is_set(), "responder was not cancelled on barge-in"
    finally:
        await ws.put_disconnect()
        await asyncio.wait_for(server, timeout=5.0)


async def test_live_ws_control_end_closes_channel() -> None:
    """A control ``end`` frame closes the channel with a fatal error event."""

    ws = FakeWebSocket()
    session = LiveSession(synthesizer=None, transcriber=None)
    server = asyncio.create_task(serve_live_websocket(ws, live=session, tick_ms=20))
    try:
        assert (await ws.next_event())["type"] == "ready"
        await ws.put_text({"type": "control", "action": "end"})
        error = await ws.next_event()
        assert error["type"] == "error"
        assert error["code"] == "session_ended"
        assert error["fatal"] is True
        await asyncio.wait_for(server, timeout=5.0)
        assert ws.closed
    finally:
        if not ws.closed:
            await ws.put_disconnect()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server, timeout=5.0)


class LaunchWebSocket(FakeWebSocket):
    """FastAPI WebSocket surface used by ``voice_live`` (accept + headers)."""

    def __init__(self) -> None:
        super().__init__()
        self.accepted = asyncio.Event()
        self.headers = {"authorization": "Bearer test-key"}

    async def accept(self) -> None:
        self.accepted.set()


async def _drive_live_entry(session_id: str, text: str) -> list[dict]:
    """POST-opened session → real ``voice_live`` ASGI handler → events."""

    from app.api.voice import voice_live

    ws = LaunchWebSocket()
    server = asyncio.create_task(
        voice_live(ws, session_id=UUID(session_id), token="test-key")
    )
    seen: list[dict] = []
    try:
        await asyncio.wait_for(ws.accepted.wait(), timeout=5.0)
        ready = await ws.next_event()
        seen.append(ready)
        assert ready["type"] == "ready", ready
        await ws.put_text({"type": "text", "text": text, "commit": True})
        deadline = asyncio.get_running_loop().time() + 20.0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no reply from live entry; events={ [e.get('type') for e in seen] }")
            event = await ws.next_event(timeout=min(5.0, remaining))
            seen.append(event)
            if event.get("type") == "reply":
                break
            if event.get("type") == "error" and event.get("fatal"):
                break
        return seen
    finally:
        await ws.put_disconnect()
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(server, timeout=8.0)


async def test_live_http_ws_entry_launches_twice(client: AsyncClient) -> None:
    """Real POST /v1/voice/live/open + WS /v1/voice/live, twice in a row."""

    await grant_voice_consent(client)
    transcripts: list[list[dict]] = []
    for index in (1, 2):
        opened = await client.post(
            "/v1/voice/live/open",
            json={"device_id": f"mac-live-launch-{index}"},
        )
        assert opened.status_code == 201, opened.text
        body = opened.json()
        assert body["session_id"]
        assert body.get("live") is True
        events = await _drive_live_entry(
            body["session_id"],
            "what's next on my calendar",
        )
        transcripts.append(events)
        assert events[0]["type"] == "ready"
        spoken = [
            event
            for event in events
            if event.get("type") in {"reply", "tts_chunk"} and (event.get("text") or "").strip()
        ]
        assert spoken, events
        text = spoken[-1]["text"]
        assert text.strip()
        assert "error" not in text.lower() or "mock" in text.lower() or "EV:" in text

    assert len(transcripts) == 2
    assert all(run[0]["type"] == "ready" for run in transcripts)
