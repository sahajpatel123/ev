"""Relationship memory: modes, hypotheticals, persist, anti-intrusion."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.compiler import ContextCompiler
from app.ev.continuity import classify_memory_intent, is_continuation, is_hypothetical
from app.memory.episodes import maybe_update_episode, recent_episodes
from app.memory.extraction import Extractor
from app.memory.relationship import MEMORY_BEHAVIOR, live_memory_instructions
from app.memory.select import _keep_implicit, _temporal_window, select_context_memories
from app.memory.turns import record_conversation_turn
from app.models import Event, Memory
from app.schemas import UserStateOut
from app.services.processor import ensure_processed
from app.voice.live.grok_voice import grok_session_update


def test_memory_thing_is_continuation_not_stranger() -> None:
    text = "I think I figured out how I want that memory thing to work."
    assert is_continuation(text)
    assert classify_memory_intent(text) == "continuation"


def test_weather_stays_fresh() -> None:
    assert classify_memory_intent("what's the weather?") == "fresh"
    assert not is_continuation("what's the weather in Gujarat?")


def test_explicit_recall_intent() -> None:
    assert classify_memory_intent("What did we talk about yesterday?") == "explicit_recall"
    assert classify_memory_intent("Do you remember what I said about the camera?") == "explicit_recall"
    assert classify_memory_intent("What name did I give that memory feature?") == "explicit_recall"
    assert classify_memory_intent("What name did I give that experiment?") == "explicit_recall"
    assert classify_memory_intent("What's it called now?") == "explicit_recall"
    assert classify_memory_intent("What did I originally call it?") == "explicit_recall"
    assert classify_memory_intent("What did I prefer before?") == "explicit_recall"


def test_remember_that_statement_is_pin_not_recall() -> None:
    assert classify_memory_intent("Remember that I'm calling this experiment Project Harbor.") == "pin"
    assert classify_memory_intent("Do you remember that I mentioned the camera?") == "explicit_recall"


def test_architecture_idea_is_continuation() -> None:
    text = "I figured out what I want to do with that Evie architecture idea."
    assert is_continuation(text)
    assert classify_memory_intent(text) == "continuation"


def test_hypothetical_language_is_not_a_fact() -> None:
    assert is_hypothetical("Imagine I moved to London.")
    event = Event(
        source="test",
        event_type="message.user",
        content={"text": "Imagine I moved to London."},
        sha256="0" * 64,
    )
    types = {candidate.memory_type for candidate in Extractor().extract(event)}
    assert "fact" not in types
    assert types == set()


def test_question_is_not_extracted_as_preference() -> None:
    event = Event(
        source="test",
        event_type="message.user",
        content={"text": "Do I prefer tea or coffee?"},
        sha256="0" * 64,
    )
    assert Extractor().extract(event) == []


def test_real_preference_still_extracts() -> None:
    event = Event(
        source="test",
        event_type="message.user",
        content={"text": "I prefer Cursor now."},
        sha256="0" * 64,
    )
    types = {candidate.memory_type for candidate in Extractor().extract(event)}
    assert "preference" in types


def test_assistant_turns_are_not_owner_facts() -> None:
    event = Event(
        source="voice",
        event_type="message.assistant",
        content={"text": "You probably prefer dark mode."},
        sha256="0" * 64,
    )
    assert Extractor().extract(event) == []


def test_implicit_filter_drops_unrelated_memories() -> None:
    camera = SimpleNamespace(
        memory_type="observation",
        text="Observed: we spent an evening on the Evie camera.",
        score=0.4,
        components={"relationship": 0.0, "semantic_raw": 0.12},
    )
    assert _keep_implicit("what's the weather?", camera) is False
    pref = SimpleNamespace(
        memory_type="preference",
        text="Preference: Cursor — prefer",
        score=0.6,
        components={"relationship": 0.0, "semantic_raw": 0.2},
    )
    assert _keep_implicit("which editor do I prefer?", pref) is True


def test_compiler_does_not_dump_crm_scores() -> None:
    state = UserStateOut(
        activity="coding",
        active_project="EV",
        active_goal="Ship",
        current_task="memory",
        recent_topics=["memory"],
        live_context=[],
    )
    memory = SimpleNamespace(
        memory_type="preference",
        score=0.91,
        event_time=None,
        confidence=0.9,
        text="You prefer Cursor.",
    )
    plan = ContextCompiler().compile(
        memories=[memory],
        user_state=state,
        strategy_text="STRATEGY: test",
        budget=4000,
        memory_intent="fresh",
    )
    assert "You prefer Cursor." in plan.text
    assert "score 0.91" not in plan.text
    assert "conf 0.90" not in plan.text
    assert plan.metadata.get("token_breakdown")


def test_live_instructions_include_memory_behavior() -> None:
    text = live_memory_instructions({"relationship": "RELATIONSHIP: owner is Sahaj."})
    assert "Remember broadly" in MEMORY_BEHAVIOR
    assert "Sahaj" in text
    assert "would you like to know more" not in text.lower()
    assert "automatic offers to elaborate" in text.lower()


def test_realtime_session_update_carries_memory_behavior() -> None:
    instructions = grok_session_update(
        provider="openai",
        capability_manifest={"live_tool_projection": [], "capabilities": []},
    )["session"]["instructions"]
    assert "continuous relationship" in instructions.lower()


@pytest.mark.asyncio
async def test_record_conversation_turn_is_durable(db_session: AsyncSession) -> None:
    thread = uuid4()
    user = await record_conversation_turn(
        db_session,
        text="I'm planning to call this Relationship Memory.",
        role="user",
        source="voice",
        conversation_id=thread,
        modality="live_realtime",
    )
    assistant = await record_conversation_turn(
        db_session,
        text="Got it — Relationship Memory.",
        role="assistant",
        source="voice",
        conversation_id=thread,
        modality="live_realtime",
    )
    await db_session.commit()
    assert user is not None
    assert assistant is not None
    assert user.event_type == "message.user"
    assert assistant.event_type == "message.assistant"
    assert user.source == "voice"
    dup = await record_conversation_turn(
        db_session,
        text="I'm planning to call this Relationship Memory.",
        role="user",
        source="voice",
        conversation_id=thread,
    )
    assert dup is None


@pytest.mark.asyncio
async def test_typed_chat_does_not_intrude_old_topic(client: AsyncClient) -> None:
    await client.post(
        "/v1/chat",
        json={"message": "I spent hours designing the Evie camera vision path."},
    )
    weather = await client.post("/v1/chat", json={"message": "How many calories are in an apple?"})
    assert weather.status_code == 200, weather.text
    body = weather.json()
    plan = body.get("context_plan") or {}
    assert plan.get("metadata", {}).get("memory_intent") == "fresh"
    reply = (body.get("reply") or body.get("text") or "").lower()
    assert "camera" not in reply


@pytest.mark.asyncio
async def test_select_explicit_recall_includes_summary_layer(
    db_session: AsyncSession,
) -> None:
    await record_conversation_turn(
        db_session,
        text="We should keep the orb animation.",
        role="user",
        source="chat",
        conversation_id=uuid4(),
    )
    await db_session.commit()
    intent, hits = await select_context_memories(
        db_session, "What have we talked about recently?", k=8
    )
    assert intent == "explicit_recall"
    assert isinstance(hits, list)


def test_march_is_content_time_not_conversation_filter() -> None:
    assert _temporal_window("Why did I decide to use Postgres in March?") == (None, None)
    since, until = _temporal_window("What did we talk about yesterday?")
    assert since is not None
    assert until is not None


def test_anti_annoyance_unrelated_questions_drop_old_topic() -> None:
    camera = SimpleNamespace(
        memory_type="observation",
        text="Observed: we spent an evening on the Evie camera.",
        score=0.42,
        components={"relationship": 0.0, "semantic_raw": 0.11},
    )
    questions = [
        "what's the weather?",
        "How many calories are in an apple?",
        "what time is it?",
        "tell me a joke",
        "is it going to rain?",
        "how far is the moon?",
        "what's 2 plus 2?",
        "who won the game?",
        "translate hello to french",
        "play some music",
    ]
    intrusions = sum(1 for question in questions if _keep_implicit(question, camera))
    assert intrusions == 0


def test_switched_to_extracts_as_preference_correction() -> None:
    event = Event(
        source="chat",
        event_type="message.user",
        content={"text": "I've switched to Cursor now."},
        sha256="1" * 64,
    )
    types = {candidate.memory_type for candidate in Extractor().extract(event)}
    assert "preference" in types
    payload = Extractor().extract(event)[0].payload
    assert payload.get("replaces_latest") is True
    assert "cursor" in payload.get("subject", "").lower()


@pytest.mark.asyncio
async def test_preference_correction_keeps_historical_version(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    first = await client.post(
        "/v1/events",
        json={"source": "chat", "event_type": "message.user", "text": "I prefer VS Code."},
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/v1/events",
        json={
            "source": "chat",
            "event_type": "message.user",
            "text": "Actually, I prefer Cursor now.",
        },
    )
    assert second.status_code == 201, second.text
    db_session.expire_all()
    rows = list((await db_session.execute(select(Memory).where(Memory.memory_type == "preference"))).scalars().all())
    current = [row for row in rows if row.is_current]
    historical = [row for row in rows if not row.is_current]
    assert any("cursor" in (row.text or "").lower() for row in current)
    assert any("vs code" in (row.text or "").lower() for row in historical)
    intent, hits = await select_context_memories(
        db_session, "What did I prefer before?", k=8
    )
    assert intent == "explicit_recall"
    blob = " ".join(hit.text for hit in hits).lower()
    assert "vs code" in blob


@pytest.mark.asyncio
async def test_voice_turn_is_available_to_typed_recall(db_session: AsyncSession) -> None:
    thread = uuid4()
    event = await record_conversation_turn(
        db_session,
        text="I'm planning to call this new feature Relationship Memory.",
        role="user",
        source="voice",
        conversation_id=thread,
        modality="live_realtime",
    )
    assert event is not None
    await db_session.commit()
    await ensure_processed(event.id)
    await maybe_update_episode(db_session, thread, seed_event=event)
    await db_session.commit()
    intent, hits = await select_context_memories(
        db_session, "What name did I give that memory feature?", k=8
    )
    assert intent == "explicit_recall"
    blob = " ".join(hit.text for hit in hits).lower()
    assert "relationship memory" in blob
    episodes = await recent_episodes(db_session, conversation_id=thread)
    assert episodes
    assert "relationship memory" in episodes[0].text.lower()


@pytest.mark.asyncio
async def test_health_exposes_memory_runtime(client: AsyncClient) -> None:
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    memory = (resp.json().get("runtime") or {}).get("memory") or {}
    assert memory.get("provider_is_not_source_of_truth") is True
    assert "processing_mode" in memory
    assert "sync_fallback" in memory
    assert "ingestion" in memory
    assert memory.get("raw_event_store_ready") is True
    assert memory.get("memory_gate_mode") == "off"
    assert "deep_recall_ready" in memory
    blob = json.dumps(memory).lower()
    assert "project " not in blob or "provider_is_not" in blob
    assert "i'm calling" not in blob
    assert "remember that" not in blob

