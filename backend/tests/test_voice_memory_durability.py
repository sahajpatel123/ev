"""Durable voice memory: provider transcription race, drain, fallback, session destroy."""

from __future__ import annotations

import asyncio
import json
import secrets
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.recall import build_explicit_recall_payload
from app.memory.turns import flush_live_turns
from app.models import Event
from app.voice.live.grok_voice import GrokVoiceBridge
from app.voice.live.session import LiveSession
from app.voice.live.voice_memory import health_snapshot


class _FakeRealtime:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


async def _wait_until(predicate, *, ticks: int = 200) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


async def _ack_session(bridge: GrokVoiceBridge, fake: _FakeRealtime, *, session_id: str) -> None:
    session = fake.sent[0]["session"]
    await bridge._handle_upstream(
        {
            "type": "session.updated",
            "session": {
                "id": session_id,
                "model": session.get("model"),
                "tools": session.get("tools", []),
                "audio": session.get("audio"),
            },
        }
    )


def _pcm() -> bytes:
    return b"\x00\x01" * 1600


async def _discard_outbound(live: LiveSession) -> None:
    while not live.outbound.empty():
        live.outbound.get_nowait()


async def _user_event_texts(needle: str) -> list[str]:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        rows = (await session.execute(select(Event).where(Event.event_type == "message.user"))).scalars().all()
    return [
        str((row.content or {}).get("text") or "")
        for row in rows
        if needle in str((row.content or {}).get("text") or "")
    ]


async def _bind_live(*, conversation_id: str, session_id: str, fake: _FakeRealtime, **bridge_kw):
    live = LiveSession(
        session_id=session_id,
        conversation_id=conversation_id,
        backchannel_enabled=False,
    )

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    live.grok_voice = GrokVoiceBridge(
        on_event=live.emit,
        connect=connect,
        api_key="sk-test",
        model="gpt-realtime-2.1-mini",
        provider="openai",
        now_ms=live.now,
        approved_tool_specs=[],
        **bridge_kw,
    )
    await live.grok_voice.start()
    return live


async def _owner_turn_events(
    fake: _FakeRealtime,
    *,
    item_id: str,
    phrase: str | None,
    delay_s: float = 0.0,
) -> None:
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
    await fake.incoming.put(
        json.dumps({"type": "input_audio_buffer.committed", "item_id": item_id})
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.created",
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_audio"}],
                },
            }
        )
    )
    await fake.incoming.put(json.dumps({"type": "response.created", "response_id": "resp_1"}))
    await fake.incoming.put(json.dumps({"type": "response.done", "response": {"status": "completed"}}))
    if phrase is None:
        return

    async def _later() -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item_id,
                    "transcript": phrase,
                }
            )
        )

    if delay_s:
        asyncio.create_task(_later())
    else:
        await _later()


@pytest.mark.asyncio
async def test_provider_session_ack_confirms_input_transcription() -> None:
    fake = _FakeRealtime()
    events: list = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        approved_tool_specs=[],
    )
    try:
        await bridge.start()
        sent = fake.sent[0]["session"]["audio"]["input"]["transcription"]
        assert sent["model"] == "gpt-4o-mini-transcribe"
        assert bridge._input_transcription_requested is True
        assert bridge._input_transcription_confirmed is False
        await _ack_session(bridge, fake, session_id="sess_alpha")
        assert bridge._input_transcription_confirmed is True
        assert bridge._input_transcription_model == "gpt-4o-mini-transcribe"
        assert bridge._provider_session_id == "sess_alpha"
        snap = health_snapshot()
        assert snap["realtime_input_transcription"]["requested"] is True
        assert snap["realtime_input_transcription"]["provider_confirmed"] is True
        assert snap["durable_voice_memory_ready"] is True
        assert snap["provider_session_id"] == "sess_alpha"
        blob = json.dumps(snap)
        assert "Project" not in blob
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_response_done_before_transcription_still_persists(db_session: AsyncSession) -> None:
    del db_session
    conversation_id = str(uuid4())
    fake = _FakeRealtime()
    live = await _bind_live(conversation_id=conversation_id, session_id=str(uuid4()), fake=fake)
    phrase = f"Remember that I'm calling this experiment Project {secrets.token_hex(3).title()}."
    try:
        await _ack_session(live.grok_voice, fake, session_id="sess_a")
        await live.grok_voice.append_pcm(_pcm())
        await _owner_turn_events(fake, item_id="item_late", phrase=None)
        await asyncio.sleep(0.05)
        assert live.grok_voice.pending_voice_turn_count() == 1
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item_late",
                    "transcript": phrase,
                }
            )
        )
        await _wait_until(lambda: live.grok_voice.pending_voice_turn_count() == 0)
        await live.flush_relationship_turns(timeout_s=4.0)
        texts = await _user_event_texts(phrase)
        assert texts, "owner Event must exist when transcription arrives after response.done"
    finally:
        await _discard_outbound(live)
        live.close()


@pytest.mark.asyncio
async def test_teardown_drain_waits_for_late_transcription(db_session: AsyncSession) -> None:
    del db_session
    conversation_id = str(uuid4())
    fake = _FakeRealtime()
    live = await _bind_live(conversation_id=conversation_id, session_id=str(uuid4()), fake=fake)
    phrase = f"Remember that I'm calling this experiment Project {secrets.token_hex(3).title()}."
    try:
        await _ack_session(live.grok_voice, fake, session_id="sess_drain")
        await live.grok_voice.append_pcm(_pcm())
        await _owner_turn_events(fake, item_id="item_drain", phrase=phrase, delay_s=0.25)
        await _wait_until(lambda: live.grok_voice.pending_voice_turn_count() == 1)
        live.note_client_gone()
        assert fake.closed is False
        await live.drain_durable_voice_memory(timeout_s=2.0)
        assert fake.closed is False
        await live.flush_relationship_turns(timeout_s=4.0)
        texts = await _user_event_texts(phrase)
        assert texts
        assert live.grok_voice.pending_voice_turn_count() == 0
    finally:
        await _discard_outbound(live)
        live.close()


@pytest.mark.asyncio
async def test_teardown_fallback_asr_when_provider_transcript_missing(
    db_session: AsyncSession,
) -> None:
    del db_session
    conversation_id = str(uuid4())
    fake = _FakeRealtime()
    phrase = f"Remember that I'm calling this experiment Project {secrets.token_hex(3).title()}."

    async def fallback(pcm: bytes, sample_rate: int) -> str:
        del pcm, sample_rate
        return phrase

    live = await _bind_live(
        conversation_id=conversation_id,
        session_id=str(uuid4()),
        fake=fake,
        fallback_transcriber=fallback,
    )
    try:
        await _ack_session(live.grok_voice, fake, session_id="sess_fb")
        await live.grok_voice.append_pcm(_pcm())
        await _owner_turn_events(fake, item_id="item_fb", phrase=None)
        await _wait_until(lambda: live.grok_voice.pending_voice_turn_count() == 1)
        live.note_client_gone()
        await live.drain_durable_voice_memory(timeout_s=0.2)
        await live.flush_relationship_turns(timeout_s=4.0)
        texts = await _user_event_texts(phrase)
        assert texts
        from app.db import SessionLocal

        async with SessionLocal() as session:
            rows = (
                await session.execute(select(Event).where(Event.event_type == "message.user"))
            ).scalars().all()
        sources = [
            (row.metadata_ or {}).get("transcript_source")
            for row in rows
            if phrase in str((row.content or {}).get("text") or "")
        ]
        assert "fallback_asr" in sources
    finally:
        await _discard_outbound(live)
        live.close()


@pytest.mark.asyncio
async def test_provider_disconnect_falls_back_to_local_pcm(db_session: AsyncSession) -> None:
    del db_session
    conversation_id = str(uuid4())
    fake = _FakeRealtime()
    phrase = f"Remember that I'm calling this experiment Project {secrets.token_hex(3).title()}."

    async def fallback(pcm: bytes, sample_rate: int) -> str:
        del pcm, sample_rate
        return phrase

    live = await _bind_live(
        conversation_id=conversation_id,
        session_id=str(uuid4()),
        fake=fake,
        fallback_transcriber=fallback,
    )
    try:
        await _ack_session(live.grok_voice, fake, session_id="sess_dc")
        await live.grok_voice.append_pcm(_pcm())
        await _owner_turn_events(fake, item_id="item_dc", phrase=None)
        await _wait_until(lambda: live.grok_voice.pending_voice_turn_count() == 1)
        await fake.incoming.put(None)
        await asyncio.sleep(0.3)
        await live.flush_relationship_turns(timeout_s=4.0)
        texts = await _user_event_texts(phrase)
        assert texts
    finally:
        await _discard_outbound(live)
        live.close()


@pytest.mark.asyncio
async def test_task_flush_alone_cannot_save_a_turn_that_never_transcribed(
    db_session: AsyncSession,
) -> None:
    del db_session
    conversation_id = str(uuid4())
    fake = _FakeRealtime()
    live = await _bind_live(conversation_id=conversation_id, session_id=str(uuid4()), fake=fake)
    phrase = f"Remember that I'm calling this experiment Project {secrets.token_hex(3).title()}."
    try:
        await _ack_session(live.grok_voice, fake, session_id="sess_flush")
        await live.grok_voice.append_pcm(_pcm())
        await _owner_turn_events(fake, item_id="item_flush", phrase=None)
        await _wait_until(lambda: live.grok_voice.pending_voice_turn_count() == 1)
        flushed = await flush_live_turns(timeout_s=0.4)
        assert flushed == 0
        assert not await _user_event_texts(phrase)
        assert live.grok_voice.pending_voice_turn_count() == 1
    finally:
        await _discard_outbound(live)
        live.close()


@pytest.mark.asyncio
async def test_five_session_destruction_trials_persist_and_recall(db_session: AsyncSession) -> None:
    successes = 0
    old_ids: list[str] = []
    new_ids: list[str] = []
    for trial in range(5):
        name = f"Project {secrets.token_hex(3).title()}"
        phrase = f"Remember that I'm calling this experiment {name}."
        conversation_id = str(uuid4())
        fake_a = _FakeRealtime()
        live_a = await _bind_live(
            conversation_id=conversation_id,
            session_id=str(uuid4()),
            fake=fake_a,
        )
        provider_a = f"sess_a_{trial}_{secrets.token_hex(2)}"
        try:
            await _ack_session(live_a.grok_voice, fake_a, session_id=provider_a)
            await live_a.grok_voice.append_pcm(_pcm())
            await _owner_turn_events(
                fake_a,
                item_id=f"item_a_{trial}",
                phrase=phrase,
                delay_s=0.15,
            )
            await _wait_until(lambda: live_a.grok_voice.pending_voice_turn_count() == 1)
            live_a.note_client_gone()
            await live_a.drain_durable_voice_memory(timeout_s=2.0)
            await live_a.flush_relationship_turns(timeout_s=4.0)
        finally:
            await _discard_outbound(live_a)
            live_a.close()
        old_ids.append(provider_a)
        stored = await _user_event_texts(name)
        if not stored:
            continue
        fake_b = _FakeRealtime()
        live_b = await _bind_live(
            conversation_id=conversation_id,
            session_id=str(uuid4()),
            fake=fake_b,
        )
        provider_b = f"sess_b_{trial}_{secrets.token_hex(2)}"
        try:
            await _ack_session(live_b.grok_voice, fake_b, session_id=provider_b)
            from app.db import SessionLocal

            async with SessionLocal() as session:
                payload = await build_explicit_recall_payload(
                    session, "What name did I give that experiment?", k=8
                )
            blob = " ".join(item.get("text") or "" for item in payload.get("evidence") or [])
            if name in blob and provider_a != provider_b:
                successes += 1
        finally:
            await _discard_outbound(live_b)
            live_b.close()
        new_ids.append(provider_b)
    assert successes == 5
    assert old_ids and new_ids
    assert all(old != new for old, new in zip(old_ids, new_ids, strict=False))
