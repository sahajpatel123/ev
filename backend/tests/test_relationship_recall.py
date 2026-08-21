"""Relationship recall corpus: vague queries, names, restraint, no invention."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.continuity import classify_memory_intent
from app.memory.extraction import Extractor
from app.memory.recall import build_explicit_recall_payload, expand_recall_queries
from app.memory.select import select_context_memories
from app.memory.turns import flush_live_turns, record_conversation_turn
from app.models import Event


def _user_event(text: str) -> Event:
    return Event(
        source="voice",
        event_type="message.user",
        content={"text": text},
        sha256="a" * 64,
    )


def test_query_expansion_does_not_invent_the_name() -> None:
    arms = expand_recall_queries("What name did I give that experiment?")
    blob = " ".join(arms).lower()
    assert "experiment" in blob
    assert "calling this experiment" in blob
    assert "silver" not in blob
    assert "lantern" not in blob


def test_naming_extracts_owner_label() -> None:
    event = _user_event("Remember that I'm calling this experiment Project Silver Lantern.")
    facts = [row for row in Extractor().extract(event) if row.memory_type == "fact"]
    assert facts
    assert any("Project Silver Lantern" in (row.payload or {}).get("value", "") for row in facts)


def test_called_form_extracts_title() -> None:
    event = _user_event("The new memory system is called Continuity Layer.")
    values = [row.payload.get("value") for row in Extractor().extract(event) if row.memory_type == "fact"]
    assert "Continuity Layer" in values


def test_hypothetical_and_negation_and_quoted_are_not_owner_labels() -> None:
    extractor = Extractor()
    hypo = extractor.extract(_user_event("Imagine I called it Project London."))
    assert not any(row.memory_type == "fact" for row in hypo)
    negated = extractor.extract(_user_event("I'm not calling it Project Red."))
    assert not any(
        "Project Red" in str((row.payload or {}).get("value") or "")
        for row in negated
        if row.memory_type == "fact"
    )
    quoted = extractor.extract(_user_event("Rahul said 'Project Green.'"))
    assert not any(
        "Project Green" in str((row.payload or {}).get("value") or "")
        for row in quoted
        if row.memory_type == "fact"
    )


@pytest.mark.asyncio
async def test_vague_experiment_recall_recovers_owner_event(
    db_session: AsyncSession,
) -> None:
    name = f"Project {secrets.token_hex(4).title()}"
    event = await record_conversation_turn(
        db_session,
        text=f"Remember that I'm calling this experiment {name}.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
        modality="live_realtime",
    )
    assert event is not None
    await db_session.commit()
    payload = await build_explicit_recall_payload(
        db_session, "What name did I give that experiment?", k=8
    )
    blob = " ".join(item.get("text") or "" for item in payload.get("evidence") or [])
    assert name in blob
    assert payload.get("grounding") == "evidence"
    assert any(item.get("source") == "owner" for item in payload["evidence"])


@pytest.mark.asyncio
async def test_five_randomized_named_value_recalls(db_session: AsyncSession) -> None:
    misses: list[str] = []
    for _ in range(5):
        name = f"Project {secrets.token_hex(3).title()} {secrets.token_hex(2).title()}"
        event = await record_conversation_turn(
            db_session,
            text=f"Remember that I'm calling this experiment {name}.",
            role="user",
            source="chat",
            conversation_id=uuid4(),
        )
        assert event is not None, name
        await db_session.commit()
        payload = await build_explicit_recall_payload(
            db_session, "What name did I give that experiment?", k=8
        )
        blob = " ".join(item.get("text") or "" for item in payload.get("evidence") or [])
        if name not in blob:
            misses.append(f"{name} evidence={blob!r}")
    assert misses == []


@pytest.mark.asyncio
async def test_number_person_and_thing_recall(db_session: AsyncSession) -> None:
    thread = uuid4()
    await record_conversation_turn(
        db_session,
        text="The test code is 48317.",
        role="user",
        source="chat",
        conversation_id=thread,
    )
    await record_conversation_turn(
        db_session,
        text="Rahul is the person helping me with the firmware.",
        role="user",
        source="chat",
        conversation_id=thread,
    )
    await record_conversation_turn(
        db_session,
        text="The new memory system is called Continuity Layer.",
        role="user",
        source="chat",
        conversation_id=thread,
    )
    await db_session.commit()
    code = await build_explicit_recall_payload(db_session, "What was the test code?", k=8)
    assert "48317" in " ".join(item.get("text") or "" for item in code["evidence"])
    person = await build_explicit_recall_payload(
        db_session, "Who is helping me with the firmware?", k=8
    )
    assert "Rahul" in " ".join(item.get("text") or "" for item in person["evidence"])
    thing = await build_explicit_recall_payload(db_session, "What did I call that thing?", k=8)
    assert "Continuity Layer" in " ".join(item.get("text") or "" for item in thing["evidence"])


@pytest.mark.asyncio
async def test_multiple_projects_remain_in_event_evidence(
    db_session: AsyncSession,
) -> None:
    for name in ("Project Silver Lantern", "Project Blue River", "Project Atlas"):
        await record_conversation_turn(
            db_session,
            text=f"I'm calling this experiment {name}.",
            role="user",
            source="chat",
            conversation_id=uuid4(),
        )
    await db_session.commit()
    payload = await build_explicit_recall_payload(
        db_session, "What names did I give those experiments?", k=8
    )
    blob = " ".join(item.get("text") or "" for item in payload["evidence"])
    assert "Silver Lantern" in blob
    assert "Blue River" in blob
    assert "Atlas" in blob


@pytest.mark.asyncio
async def test_correction_keeps_original_in_events(db_session: AsyncSession) -> None:
    thread = uuid4()
    first = f"Project {secrets.token_hex(3).title()}"
    second = f"Project {secrets.token_hex(3).title()}"
    await record_conversation_turn(
        db_session,
        text=f"Remember that I'm calling this experiment {first}.",
        role="user",
        source="chat",
        conversation_id=thread,
    )
    await record_conversation_turn(
        db_session,
        text=f"Actually call it {second} now.",
        role="user",
        source="chat",
        conversation_id=thread,
    )
    await db_session.commit()
    original = await build_explicit_recall_payload(
        db_session, "What did I originally call that experiment?", k=8
    )
    current = await build_explicit_recall_payload(
        db_session, "What's it called now?", k=8
    )
    orig_blob = " ".join(item.get("text") or "" for item in original["evidence"])
    now_blob = " ".join(item.get("text") or "" for item in current["evidence"])
    assert first in orig_blob
    assert second in now_blob


@pytest.mark.asyncio
async def test_false_memory_has_no_invented_evidence(db_session: AsyncSession) -> None:
    payload = await build_explicit_recall_payload(
        db_session, "What name did I give Project Neptune?", k=8
    )
    blob = " ".join(item.get("text") or "" for item in payload.get("evidence") or []).lower()
    assert "neptune" not in blob
    assert payload.get("grounding") == "no_reliable_record"
    assert payload.get("count") == 0


@pytest.mark.asyncio
async def test_fresh_weather_does_not_select_project_name(
    db_session: AsyncSession,
) -> None:
    name = f"Project {secrets.token_hex(4).title()}"
    await record_conversation_turn(
        db_session,
        text=f"Remember that I'm calling this experiment {name}.",
        role="user",
        source="chat",
        conversation_id=uuid4(),
    )
    await db_session.commit()
    intent, hits = await select_context_memories(db_session, "what's the weather?", k=8)
    assert intent == "fresh"
    blob = " ".join(hit.text for hit in hits)
    assert name not in blob


@pytest.mark.asyncio
async def test_voice_store_is_typed_recallable(db_session: AsyncSession) -> None:
    name = f"Project {secrets.token_hex(4).title()}"
    await record_conversation_turn(
        db_session,
        text=f"I'm calling this experiment {name}.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
        modality="live_realtime",
    )
    await db_session.commit()
    payload = await build_explicit_recall_payload(
        db_session, "What name did I give that experiment?", k=8
    )
    assert name in " ".join(item.get("text") or "" for item in payload["evidence"])


@pytest.mark.asyncio
async def test_typed_store_is_voice_recallable(db_session: AsyncSession) -> None:
    name = f"Project {secrets.token_hex(4).title()}"
    await record_conversation_turn(
        db_session,
        text=f"I'm calling this experiment {name}.",
        role="user",
        source="chat",
        conversation_id=uuid4(),
        modality="typed",
    )
    await db_session.commit()
    assert classify_memory_intent("What name did I give that experiment?") == "explicit_recall"
    payload = await build_explicit_recall_payload(
        db_session, "What name did I give that experiment?", k=8
    )
    assert name in " ".join(item.get("text") or "" for item in payload["evidence"])


@pytest.mark.asyncio
async def test_flush_live_turns_awaits_outstanding() -> None:
    import asyncio

    from app.memory import turns

    finished = asyncio.Event()

    async def slow() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    task = asyncio.create_task(slow())
    turns._PENDING.add(task)
    task.add_done_callback(turns._PENDING.discard)
    await flush_live_turns(timeout_s=1.0)
    assert finished.is_set()
