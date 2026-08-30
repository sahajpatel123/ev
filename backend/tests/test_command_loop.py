"""Gating tests for the four command-loop failures: stream, tools, ears, queue/heartbeat."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.api import core as core_api
from app.audio.vad import EnergyVad
from app.config import settings
from app.contracts import ChatMessage, ChatProvider, ChatResult, RequestEnvelope
from app.ev import conversation
from app.ev.briefing import infer_args, infer_send_message_args, plan_life_tool_calls
from app.gateway.service import ModelGateway, tool_specs_from_dicts
from app.integrations import service as integrations
from app.models import AccessLog, Device, Memory, RuntimeHeartbeat, VoiceSession
from app.schemas import ChatRequest, EventCreate
from app.services import processor
from app.services.event_service import EventService
from app.services.processor import ensure_processed, queue_worker_available
from app.services.tool_loop import run_tool_loop
from app.voice.lifecycle import (
    VoiceRuntime,
    VoiceState,
    clear_session_in_flight,
    mark_session_in_flight,
    pending_ingest_for,
    pop_pending_ingest,
    queue_pending_ingest,
    session_in_flight,
)
from clients.ears.main import EarConfig, deliver_wake_utterance, ingest_http_timeout, run_ears
from tests.test_audio_capture import FakeStream, FakeWakeEngine, _silence_block, _speech_block
from tests.test_life_agency import _add_bridge, _life_spec
from tests.test_streaming_refinement import _parse_sse

# --------------------------------------------------------------------------- #
# 1. Chat SSE is progressive, not a post-hoc slice of a finished reply
# --------------------------------------------------------------------------- #


def test_stream_chat_source_does_not_await_then_fake_chunk() -> None:
    source = inspect.getsource(core_api._stream_chat)
    assert "create_task" in source
    assert "_text_chunks(" not in source
    assert 'stage": "accepted"' in source or "stage': 'accepted'" in source or '"accepted"' in source


async def test_chat_sse_first_event_is_status_not_finished_slice(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat",
        json={
            "message": "Why did I decide to use SQLite for local testing?",
            "stream": True,
        },
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    assert events, "expected SSE events"
    assert events[0][0] == "status"
    assert events[0][1].get("stage") == "accepted"
    refined = next(data for name, data in events if name == "refined")
    assert events[0][1].get("text") != refined["text"][:24]
    deltas = [data["text"] for name, data in events if name == "delta" and data.get("text")]
    assert deltas
    assert "".join(deltas)
    stages = [data.get("stage") for name, data in events if name == "status"]
    assert "accepted" in stages
    assert any(stage in {"filter", "retrieve", "briefing", "model"} for stage in stages)


async def test_stream_chat_yields_status_before_slow_model_returns(
    db_session, monkeypatch
) -> None:
    released = asyncio.Event()
    model_entered = asyncio.Event()

    class SlowProvider(ChatProvider):
        name = "slow-stream"
        supports_media = False

        async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
            model_entered.set()
            await released.wait()
            return ChatResult(
                text="SLOW_COMPLETE_REPLY_TEXT_XXXX_NOT_A_STATUS",
                usage={},
                model=model or self.name,
            )

        async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
            return await self.chat(messages, model=model, temperature=temperature)

        async def stream_chat(self, messages, *, model=None, temperature=0.7):
            from app.gateway.streaming import ChatStreamChunk

            model_entered.set()
            await released.wait()
            text = "SLOW_COMPLETE_REPLY_TEXT_XXXX_NOT_A_STATUS"
            yield ChatStreamChunk(text=text, model=model or self.name)
            yield ChatStreamChunk(usage={}, model=model or self.name, done=True)

        async def list_models(self) -> list[str]:
            return [self.name]

    monkeypatch.setattr(core_api, "get_chat_provider", lambda: SlowProvider())
    thread = await conversation.resolve_thread(db_session, None)
    data = ChatRequest(message="hello there friend", stream=True)
    agen = core_api._stream_chat(data, db_session, "master", thread_id=thread.id)
    first = await asyncio.wait_for(agen.__anext__(), timeout=2)
    assert "event: status" in first
    assert "SLOW_COMPLETE_REPLY_TEXT_XXXX" not in first
    assert "accepted" in first
    released.set()
    rest: list[str] = []
    async for frame in agen:
        rest.append(frame)
    body = first + "".join(rest)
    assert "SLOW_COMPLETE_REPLY_TEXT_XXXX" in body
    assert "event: done" in body


# --------------------------------------------------------------------------- #
# 2. OpenCode-shaped providers still execute send_message
# --------------------------------------------------------------------------- #


class OpenCodeShapedProvider(ChatProvider):
    """Matches production: no native tools, optional prose instead of calls."""

    name = "opencode"
    supports_media = False
    supports_tools = False

    def __init__(self) -> None:
        self.rounds = 0
        self.messages: list[ChatMessage] = []

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        return await self.chat_with_tools(messages, [], model=model, temperature=temperature)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7) -> ChatResult:
        self.rounds += 1
        self.messages = list(messages)
        tool_msgs = [m for m in messages if m.role == "tool"]
        if tool_msgs:
            return ChatResult(
                text="Done — used the tool result.",
                usage={},
                model=model or self.name,
            )
        return ChatResult(
            text="I'll send a text to Mom saying I'm late.",
            tool_calls=[],
            usage={"degraded": True, "degradation": {"kind": "tool_emulation_unparsed"}},
            model=model or self.name,
        )

    async def list_models(self) -> list[str]:
        return [self.name]


def test_infer_send_message_args_text_mom() -> None:
    assert infer_args("send_message", "text Mom I'm late") is None
    args = infer_send_message_args("text Mom I'm late")
    assert args == {"to": "Mom", "text": "I'm late"}
    planned = plan_life_tool_calls("text Mom I'm late", {"send_message", "resolve_contact"})
    names = [call.name for call in planned]
    assert "send_message" in names
    send = next(call for call in planned if call.name == "send_message")
    assert send.arguments["to"] == "Mom"
    assert send.arguments["text"] == "I'm late"


async def test_opencode_shaped_text_mom_invokes_messaging_send(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="contacts", adapter="contacts", scopes=["contacts:read"])
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])

    calls: list[tuple[str, dict]] = []

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        calls.append((action, dict(args)))
        if action == "contacts.resolve":
            return SimpleNamespace(
                result={"ok": True, "contact": {"name": "Mom", "phone": "+15551234567"}}
            )
        if action == "messaging.send":
            return SimpleNamespace(
                result={
                    "ok": True,
                    "delivery": {
                        "confirmed": True,
                        "evidence": {
                            "recipient": args.get("to") or "Mom",
                            "channel": "Messages",
                            "sent_at": "2026-08-14T10:00:00Z",
                        },
                    },
                }
            )
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)
    provider = OpenCodeShapedProvider()
    gateway = ModelGateway(provider)
    specs = tool_specs_from_dicts([_life_spec("resolve_contact"), _life_spec("send_message")])
    call = await run_tool_loop(
        db_session,
        gateway,
        [ChatMessage(role="user", content="text Mom I'm late")],
        envelope=RequestEnvelope(request_id="opencode-mom", strategy={}),
        tool_specs=specs,
        actor="owner",
        allow_sensitive_tools=True,
    )
    send_calls = [args for action, args in calls if action == "messaging.send"]
    assert send_calls, f"dispatcher never sent; calls={calls}"
    assert send_calls[0].get("to") == "Mom"
    assert "late" in str(send_calls[0].get("text") or "").lower()
    assert call.result.text
    lowered = call.result.text.lower()
    assert "sent" in lowered or "mom" in lowered
    assert "i'll send a text" not in lowered
    assert any(message.role == "tool" for message in provider.messages)
    logged = (
        await db_session.execute(select(AccessLog).where(AccessLog.action == "tool_call"))
    ).scalars().all()
    assert any(row.resource_ids and row.resource_ids[0] == "send_message" for row in logged)


async def test_chat_text_mom_path_dispatches(client, db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_autonomy", "full")
    await _add_bridge(db_session, slug="messaging", adapter="messaging", scopes=["messaging:act"])
    calls: list[tuple[str, dict]] = []

    async def fake_execute_action(session, integration_id, action, args, *, actor):
        calls.append((action, dict(args)))
        return SimpleNamespace(
            result={
                "ok": True,
                "delivery": {
                    "confirmed": True,
                    "evidence": {
                        "recipient": "Mom",
                        "channel": "Messages",
                        "sent_at": "2026-08-14T10:00:00Z",
                    },
                },
            }
        )

    monkeypatch.setattr(integrations, "execute_action", fake_execute_action)
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: OpenCodeShapedProvider())
    resp = await client.post("/v1/chat", json={"message": "text Mom I'm late"})
    assert resp.status_code == 200, resp.text
    send_calls = [args for action, args in calls if action == "messaging.send"]
    assert send_calls
    assert send_calls[0].get("to") == "Mom"
    assert "late" in str(send_calls[0].get("text") or "").lower()
    assert "i'll send a text" not in resp.json()["reply"].lower()


# --------------------------------------------------------------------------- #
# 3. Ears clip/timeout + busy follow-up is queued, not dropped
# --------------------------------------------------------------------------- #


def test_posted_clip_is_shorter_than_http_timeout() -> None:
    cfg = EarConfig()
    timeout = ingest_http_timeout(cfg)
    assert cfg.listen_max_segment_s < timeout
    assert cfg.max_segment_s < timeout
    assert cfg.wake_chunk_s < timeout
    assert timeout >= cfg.max_segment_s + 15.0


async def test_deliver_wake_retries_busy_and_timeout() -> None:
    cfg = EarConfig(consent=True, api_url="http://ears.test", api_key="k")

    class BusySender:
        async def __call__(self, **kwargs):
            return {"sent": False, "reason": "busy", "accepted": False, "retryable": True}

    busy = await deliver_wake_utterance(
        cfg, frames_b64="AAAA", scene={}, wake_confidence=1.0, sender=BusySender()
    )
    assert busy["retryable"] is True
    assert busy["sent"] is False

    class TimeoutClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            import httpx

            raise httpx.TimeoutException("too slow")

    import httpx

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kwargs: TimeoutClient(kwargs.get("timeout"))  # type: ignore[misc]
    try:
        timed = await deliver_wake_utterance(
            cfg, frames_b64="AAAA", scene={}, wake_confidence=1.0
        )
    finally:
        httpx.AsyncClient = original
    assert timed["reason"] == "timeout"
    assert timed["retryable"] is True
    assert timed["sent"] is False


async def test_run_ears_replays_follow_up_queued_during_busy_ingest() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    sent: list[dict] = []

    async def fake_sender(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            first_started.set()
            await release_first.wait()
            return {"sent": True, "accepted": True, "listening": True}
        return {"sent": True, "accepted": True, "listening": True}

    blocks = (
        [_speech_block(seed=1) for _ in range(8)]
        + [_silence_block()] * 5
        + [_speech_block(seed=2) for _ in range(8)]
        + [_silence_block()] * 5
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
        duration_s=1.5,
    )
    task = asyncio.create_task(
        run_ears(
            cfg,
            stream=FakeStream(blocks),
            wake_engine=FakeWakeEngine(),
            vad_engine=EnergyVad(),
            sender=fake_sender,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=2)
    await asyncio.sleep(0.25)
    assert len(sent) == 1
    release_first.set()
    stats = await asyncio.wait_for(task, timeout=4)
    assert len(sent) >= 2
    assert stats.utterances_sent >= 2


async def test_busy_in_flight_follow_up_is_queued_then_drained(db_session) -> None:
    from app.voice.contracts import Transcript
    from app.voice.lifecycle import UtteranceOutcome

    runtime = VoiceRuntime.__new__(VoiceRuntime)
    runtime.session = db_session
    captured: list[dict] = []

    async def fake_handle_utterance(**kwargs):
        captured.append(kwargs)
        return UtteranceOutcome(
            session_id=str(kwargs["session_id"]),
            state=VoiceState.FOLLOW_UP,
            transcript=Transcript(
                text=kwargs.get("text") or "what's next",
                confidence=1.0,
                provider="echo",
            ),
            reply="Your next thing is lunch.",
        )

    async def fake_get_session(session_id):
        return SimpleNamespace(id=session_id, state=VoiceState.AWAKE, device_id="mac-ears-q")

    runtime.handle_utterance = fake_handle_utterance  # type: ignore[method-assign]
    runtime._get_session = fake_get_session  # type: ignore[method-assign]

    sid = "11111111-1111-1111-1111-111111111111"
    mark_session_in_flight(sid)
    assert session_in_flight(sid)
    queue_pending_ingest(
        "mac-ears-q",
        {"text_hint": "what's next", "session_id": sid, "audio_b64": None, "audio_ref": None},
    )
    assert pending_ingest_for("mac-ears-q") is not None
    clear_session_in_flight(sid)
    drained = await runtime.drain_pending_ingest(device_id="mac-ears-q", session_id=sid)
    assert drained is not None
    assert drained.accepted is True
    assert "lunch" in (drained.reply or "")
    assert pending_ingest_for("mac-ears-q") is None
    assert captured
    assert captured[0].get("text") == "what's next"


async def test_handle_ears_ingest_queues_when_in_flight(db_session) -> None:
    from app.utils.text import utcnow

    row = VoiceSession(
        device_id="mac-ears-if",
        wake_word="evie",
        state=VoiceState.PROCESSING,
        owner_verified=True,
        updated_at=utcnow(),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    mark_session_in_flight(str(row.id))
    runtime = VoiceRuntime(db_session, master_key=settings.master_key)
    outcome = await runtime.handle_ears_ingest(
        device_id="mac-ears-if",
        frames_b64=None,
        consent=True,
        text_hint="what's next",
    )
    assert outcome.queued is True
    assert outcome.accepted is True
    assert outcome.listening is True
    pending = pending_ingest_for("mac-ears-if")
    assert pending is not None
    assert pending.get("text_hint") == "what's next"
    pop_pending_ingest("mac-ears-if")
    clear_session_in_flight(str(row.id))


async def test_in_flight_follow_up_is_ingested_when_turn_finishes(
    db_session, monkeypatch
) -> None:
    """A question posted while the first turn is in-flight must be heard."""

    from uuid import uuid4

    from app.db import SessionLocal
    from app.voice.contracts import SpeechStyle, SynthesisResult
    from app.voice.pipeline import PipelineOutcome
    from app.voice.tts import MetaSynthesizer

    heard: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_pipeline(session, *, transcript, **kwargs):
        heard.append(transcript.text)
        if len(heard) == 1:
            await session.commit()
            started.set()
            await release.wait()
        return PipelineOutcome(
            transcript=transcript,
            reply=f"heard:{transcript.text}",
            conversation_id=str(uuid4()),
            tts=SynthesisResult(text=transcript.text, provider="meta"),
            style=SpeechStyle(),
            model="mock",
            context_tokens=1,
            memory_deltas=[],
        )

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", slow_pipeline)

    row = VoiceSession(
        device_id="mac-ears-drain",
        wake_word="evie",
        state=VoiceState.AWAKE,
        owner_verified=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    session_id = row.id

    async def first_turn() -> None:
        async with SessionLocal() as session:
            runtime = VoiceRuntime(
                session,
                master_key=settings.master_key,
                synthesizer=MetaSynthesizer(),
            )
            await runtime.handle_utterance(session_id=session_id, text="first question")
            await session.commit()

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(started.wait(), timeout=3)
    async with SessionLocal() as session:
        runtime = VoiceRuntime(
            session,
            master_key=settings.master_key,
            synthesizer=MetaSynthesizer(),
        )
        queued = await runtime.handle_ears_ingest(
            device_id="mac-ears-drain",
            frames_b64=None,
            consent=True,
            text_hint="what's next",
        )
        await session.commit()
    assert queued.queued is True, queued
    assert queued.accepted is True
    assert pending_ingest_for("mac-ears-drain") is not None
    release.set()
    await asyncio.wait_for(task, timeout=4)
    assert pending_ingest_for("mac-ears-drain") is None
    assert heard[0] == "first question"
    assert any("what's next" in text.lower() for text in heard)
    assert len(heard) >= 2


def test_ensure_schema_adds_missing_voice_session_columns() -> None:
    """Live Postgres was stamped at head without conversation_id. Repair it."""

    from sqlalchemy import create_engine, inspect, text

    from app.db import ensure_schema

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE voice_sessions ("
                "id VARCHAR(32) PRIMARY KEY, "
                "device_id VARCHAR(128), "
                "state VARCHAR(24)"
                ")"
            )
        )
        before = {c["name"] for c in inspect(conn).get_columns("voice_sessions")}
        assert "conversation_id" not in before
        from app import models  # noqa: F401

        ensure_schema(conn)
        after = {c["name"] for c in inspect(conn).get_columns("voice_sessions")}
    assert "conversation_id" in after
    assert "greeted_at" in after
    assert "malfunction_spoken_at" in after


# --------------------------------------------------------------------------- #
# 4. Queue-mode memories + Mac mac-<host> heartbeat
# --------------------------------------------------------------------------- #


async def test_queue_mode_without_worker_still_writes_memories(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "processing_mode", "queue")
    monkeypatch.setattr(processor, "queue_worker_available", lambda: False)
    service = EventService(db_session, actor="owner")
    event = await service.create(
        EventCreate(source="test", event_type="note", text="I decided to keep the blue notebook on the bench.")
    )
    await db_session.commit()
    deltas = await ensure_processed(event.id)
    assert deltas, "enqueue-and-forget with no worker must not return []"
    rows = list((await db_session.execute(select(Memory))).scalars().all())
    assert rows, "queue fallback must write at least one memory row"


async def test_queue_mode_with_worker_enqueues(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "processing_mode", "queue")
    monkeypatch.setattr(processor, "queue_worker_available", lambda: True)
    enqueued: list[UUID] = []
    monkeypatch.setattr(processor, "enqueue_event", lambda event_id: enqueued.append(event_id))
    monkeypatch.setattr(processor, "maybe_enqueue_llm_extraction", lambda event_id: None)
    service = EventService(db_session, actor="owner")
    event = await service.create(
        EventCreate(source="test", event_type="note", text="queue path only")
    )
    await db_session.commit()
    deltas = await ensure_processed(event.id)
    assert deltas == []
    assert enqueued == [event.id]


def test_queue_worker_available_is_honest_without_redis() -> None:
    # This environment typically has no RQ worker. The shipped probe must
    # return False rather than pretending a consumer exists.
    assert queue_worker_available() is False or isinstance(queue_worker_available(), bool)


async def test_mac_hostname_heartbeat_is_accepted(client: AsyncClient, db_session) -> None:
    resp = await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": "mac-sahajs-macbook-pro", "status": "ok", "listener_state": "listening"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["listener_state"] == "listening"
    device_id = UUID(body["device_id"])
    device = await db_session.get(Device, device_id)
    assert device is not None
    assert device.name == "mac-sahajs-macbook-pro"
    assert device.last_seen_at is not None
    beats = list(
        (await db_session.execute(select(RuntimeHeartbeat).where(RuntimeHeartbeat.device_id == device.id)))
        .scalars()
        .all()
    )
    assert beats

    again = await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": "mac-sahajs-macbook-pro", "status": "ok", "listener_state": "listening"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["device_id"] == body["device_id"]


async def test_unknown_uuid_heartbeat_still_404s(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/runtime/heartbeat",
        json={
            "device_id": "00000000-0000-0000-0000-000000000099",
            "status": "ok",
            "listener_state": "listening",
        },
    )
    assert resp.status_code == 404
