"""Memory OS v1.1: open loops, reflection structure, temporal, curator retry.

Does not enable EV_MEMORY_GATE. Prefetch stays off unless a test opts in.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.continuity import classify_memory_intent
from app.memory.curator import process_curation_jobs, validate_curator_payload
from app.memory.extraction import Extractor
from app.memory.loops import extract_loop_candidates, list_loops
from app.memory.materialize import rebuild_memory_cards
from app.memory.prefetch import lookup, prefetch, prefetch_mode, reset_prefetch, snapshot
from app.memory.router import select_context
from app.memory.service import MemoryService
from app.memory.state import classify_temporal_query, memories_as_of
from app.memory.turns import record_conversation_turn
from app.models import Event, Memory, MemoryCurationJob
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.services.processor import ensure_processed
from app.utils.text import utcnow
from app.voice.live.grok_voice import grok_session_update


def _user_event(text: str) -> Event:
    return Event(
        source="voice",
        event_type="message.user",
        content={"text": text},
        sha256="a" * 64,
    )


def _blob(payload: dict) -> str:
    parts = [str(item.get("text") or "") for item in payload.get("evidence") or []]
    parts.extend(str(item.get("text") or "") for item in payload.get("results") or [])
    parts.extend(str(item.get("title") or "") for item in payload.get("open_loops") or [])
    return " ".join(parts)


async def _commit_turn(
    session: AsyncSession,
    text: str,
    *,
    occurred_at=None,
    process: bool = True,
) -> Event:
    if occurred_at is not None:
        event = await EventService(session, actor="owner").create(
            EventCreate(
                source="voice",
                event_type="message.user",
                text=text,
                occurred_at=occurred_at,
                conversation_id=uuid4(),
                metadata={"speaker": "owner", "modality": "live_realtime"},
            )
        )
    else:
        event = await record_conversation_turn(
            session,
            text=text,
            role="user",
            source="voice",
            conversation_id=uuid4(),
            modality="live_realtime",
        )
    assert event is not None
    await session.commit()
    if process:
        await ensure_processed(event.id)
        session.expire_all()
    return event


def test_loop_extractor_keeps_casual_hypothetical_quoted_out() -> None:
    extractor = Extractor()
    meaningful = extractor.extract(_user_event("Safari clicking still doesn't work."))
    assert any(row.memory_type == "open_loop" for row in meaningful)
    assert extract_loop_candidates(_user_event("I'm drinking coffee."), "I'm drinking coffee.", []) == []
    assert extract_loop_candidates(
        _user_event("Imagine if Mac Control were broken."),
        "Imagine if Mac Control were broken.",
        [],
    ) == []
    quoted = extractor.extract(_user_event("Rahul said Safari clicking still doesn't work."))
    assert not any(row.memory_type == "open_loop" for row in quoted)


def test_decision_rejection_hypothesis_are_typed() -> None:
    extractor = Extractor()
    decision = extractor.extract(_user_event("We're going with Postgres as the source of truth."))
    assert any(row.memory_type == "decision" for row in decision)
    rejection = extractor.extract(_user_event("Don't build memory_v3."))
    assert any(row.memory_type == "rejection" for row in rejection)
    maybe = extractor.extract(_user_event("Maybe someday we could rewrite Memory OS."))
    assert not any(row.memory_type == "decision" for row in maybe)
    hypo = extractor.extract(
        _user_event("The Music failure may originate in stale Realtime tool schema.")
    )
    assert any(row.memory_type == "hypothesis" for row in hypo)
    assert all((row.payload or {}).get("status") == "active" for row in hypo if row.memory_type == "hypothesis")


def test_curator_will_not_resolve_without_evidence() -> None:
    payload = validate_curator_payload(
        {
            "open_loops": [
                {
                    "title": "Safari clicking still doesn't work",
                    "status": "resolved",
                    "confidence": 0.4,
                    "evidence_type": "inferred",
                }
            ],
            "possible_next_steps": ["rewrite Mac control"],
        },
        event_ids=["e1"],
    )
    assert payload["open_loops"][0]["status"] == "open"
    assert payload["next_steps_are_suggestions"] is True
    assert "rewrite Mac control" in payload["possible_next_steps"]


def test_flagship_queries_are_explicit_recall() -> None:
    assert classify_memory_intent("Where did we leave off?") == "explicit_recall"
    assert classify_memory_intent("What's still unresolved?") == "explicit_recall"
    assert classify_memory_intent("What issue did we solve?") == "explicit_recall"
    assert classify_memory_intent("What changed since yesterday?") == "explicit_recall"
    assert classify_memory_intent("What did we think before?") == "explicit_recall"
    assert classify_memory_intent("what's the weather?") == "fresh"
    assert classify_memory_intent("What's 21 times 17?") == "fresh"
    assert classify_temporal_query("Where did we leave off?").mode == "leave_off"
    assert classify_temporal_query("What's still open?").mode == "still_open"
    assert classify_temporal_query("What did we solve?").mode == "solved"
    assert classify_temporal_query("What changed?").mode == "changes"


def test_memory_gate_and_vad_unchanged() -> None:
    assert (settings.memory_gate or "off").strip().lower() == "off"
    assert prefetch_mode() == "off"
    session = grok_session_update(
        provider="openai",
        capability_manifest={"live_tool_projection": [], "capabilities": []},
    )["session"]
    assert session["audio"]["input"]["turn_detection"]["create_response"] is True
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"


@pytest.mark.asyncio
async def test_open_loop_lifecycle_and_no_duplicates(db_session: AsyncSession) -> None:
    service = MemoryService()
    await _commit_turn(db_session, "Safari clicking still doesn't work.")
    await _commit_turn(db_session, "Safari result click doesn't work.")
    opens = await service.get_open_loops(db_session)
    safari = [row for row in opens if "safari" in (row.get("title") or "").lower()]
    assert len(safari) == 1
    await _commit_turn(db_session, "That issue is fixed now.")
    still = await service.get_open_loops(db_session)
    assert not any("safari" in (row.get("title") or "").lower() for row in still)
    solved = await service.recall(db_session, "What issue did we solve?")
    assert solved.get("facet") == "solved"
    assert "safari" in _blob(solved).lower() or any(
        "safari" in str(item.get("title") or "").lower() for item in solved.get("open_loops") or []
    )
    historical = await list_loops(db_session, status="resolved", k=8)
    assert historical


@pytest.mark.asyncio
async def test_leave_off_still_open_and_anti_intrusion(db_session: AsyncSession) -> None:
    service = MemoryService()
    await _commit_turn(
        db_session,
        "Music opens, but Evie still can't actually play the playlist.",
    )
    await _commit_turn(db_session, "We're going with Postgres as the source of truth.")
    leave = await service.recall(db_session, "Where did we leave off?")
    assert leave.get("facet") == "leave_off"
    assert leave.get("open_loops")
    stuck = await service.recall(db_session, "What's still unresolved?")
    assert stuck.get("facet") == "still_open"
    assert any("music" in (item.get("title") or "").lower() for item in stuck.get("open_loops") or [])
    changed = await service.recall(db_session, "What changed?")
    assert changed.get("facet") == "changes"
    await service.build_bootstrap(db_session)
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
        "What's 21 times 17?",
    ]
    for question in questions:
        packet = await select_context(db_session, question)
        assert packet["mode"] == "fresh", question
        assert packet["would_inject"] is False, question
        assert packet["historical_evidence"] == []


@pytest.mark.asyncio
async def test_editor_current_vs_historical(db_session: AsyncSession) -> None:
    service = MemoryService()
    t1 = utcnow() - timedelta(days=10)
    t2 = utcnow() - timedelta(hours=1)
    await _commit_turn(db_session, "I prefer VS Code.", occurred_at=t1)
    await _commit_turn(db_session, "I've switched to Cursor.", occurred_at=t2)
    current = (
        await db_session.execute(
            select(Memory).where(Memory.memory_type == "preference", Memory.is_current.is_(True))
        )
    ).scalars().all()
    assert any("Cursor" in (row.text or "") for row in current)
    assert not any("VS Code" in (row.text or "") for row in current)
    past = await memories_as_of(db_session, boundary=t1 + timedelta(days=1), memory_types=["preference"], k=8)
    assert any("VS Code" in (row.text or "") for row in past)
    historical = await service.recall(db_session, "What did I prefer before?")
    assert "VS Code" in _blob(historical)
    now = await service.recall(db_session, "Which one do I prefer?")
    assert "Cursor" in _blob(now)


@pytest.mark.asyncio
async def test_music_as_of_resolution(db_session: AsyncSession) -> None:
    service = MemoryService()
    t1 = utcnow() - timedelta(days=3)
    t2 = utcnow() - timedelta(hours=2)
    await _commit_turn(db_session, "Music control is still not working.", occurred_at=t1)
    before = await memories_as_of(db_session, boundary=t1 + timedelta(hours=1), memory_types=["open_loop"], k=8)
    assert any(
        (row.payload or {}).get("status") in {"open", "blocked", "waiting", "unknown"} for row in before
    )
    await _commit_turn(
        db_session,
        "Music playback now works through the semantic adapter.",
        occurred_at=t2,
    )
    opens = await service.get_open_loops(db_session, scope="Music")
    assert not any("not working" in (row.get("title") or "").lower() for row in opens)
    solved = await service.recall(db_session, "What did we solve?")
    assert "music" in _blob(solved).lower() or any(
        "music" in str(item.get("title") or "").lower() for item in solved.get("open_loops") or []
    )


@pytest.mark.asyncio
async def test_curator_outage_is_retryable_then_catches_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory_curator_enabled = False
    event = await _commit_turn(db_session, "Safari clicking still doesn't work.")
    processed = await process_curation_jobs(db_session, limit=4)
    assert processed >= 1
    job = (
        await db_session.execute(select(MemoryCurationJob).where(MemoryCurationJob.event_id == event.id))
    ).scalar_one()
    assert job.status == "retryable_failed"
    recall = await MemoryService().recall(db_session, "What's still unresolved?")
    assert "safari" in _blob(recall).lower() or recall.get("open_loops")
    settings.memory_curator_enabled = True
    monkeypatch.setattr("app.memory.curator.curator_available", lambda: True)

    async def _fake_call(prompt: str) -> tuple[str, int]:
        return (
            json.dumps(
                {
                    "memories": [],
                    "open_loops": [
                        {
                            "title": "Safari clicking still doesn't work",
                            "scope": "Safari",
                            "status": "open",
                            "confidence": 0.9,
                            "evidence_type": "owner_asserted",
                        }
                    ],
                    "possible_next_steps": ["measure curator telemetry"],
                }
            ),
            24,
        )

    monkeypatch.setattr("app.memory.curator._call_deepseek", _fake_call)
    job.available_at = utcnow() - timedelta(seconds=2)
    await db_session.commit()
    await process_curation_jobs(db_session, limit=4)
    await db_session.refresh(job)
    assert job.status == "completed"
    rows = (
        await db_session.execute(
            select(Memory).where(Memory.memory_type == "open_loop", Memory.is_current.is_(True))
        )
    ).scalars().all()
    safari = [row for row in rows if "safari" in f"{row.text} {(row.payload or {}).get('title')}".lower()]
    assert len(safari) == 1


@pytest.mark.asyncio
async def test_cards_rebuild_from_postgres(db_session: AsyncSession) -> None:
    await _commit_turn(db_session, "Safari clicking still doesn't work.")
    await _commit_turn(db_session, "We're going with Postgres as the source of truth.")
    first = await rebuild_memory_cards(db_session)
    root = Path(first["root"])
    state = root / "cards" / "current_state.json"
    assert state.is_file()
    before = json.loads(state.read_text(encoding="utf-8"))
    shutil.rmtree(root / "cards")
    assert not state.exists()
    second = await rebuild_memory_cards(db_session)
    restored = Path(second["root"]) / "cards" / "current_state.json"
    assert restored.is_file()
    after = json.loads(restored.read_text(encoding="utf-8"))
    assert after.get("kind") == "current_state"
    assert after.get("open_loops")
    assert after.get("schema_version") == before.get("schema_version")


@pytest.mark.asyncio
async def test_prefetch_shadow_does_not_inject(db_session: AsyncSession) -> None:
    settings.memory_prefetch = "shadow"
    reset_prefetch()
    await _commit_turn(db_session, "Safari clicking still doesn't work.")
    entry = await prefetch(db_session, "Safari")
    assert entry is not None
    assert lookup("Safari") is not None
    stats = snapshot()
    assert stats["prefetch_triggers"] >= 1
    assert stats["prefetch_hit_rate"] is not None
    packet = await select_context(db_session, "what's the weather?")
    assert packet["would_inject"] is False
    assert packet["mode"] == "fresh"
    settings.memory_prefetch = "off"
    reset_prefetch()
    assert await prefetch(db_session, "Safari") is None


@pytest.mark.asyncio
async def test_long_history_lookups_stay_fast(db_session: AsyncSession) -> None:
    now = utcnow()
    for index in range(400):
        db_session.add(
            Event(
                source="test",
                event_type="message.user",
                content={"text": f"noise event {index} calories weather {index}"},
                sha256=f"{index:064x}",
                occurred_at=now - timedelta(days=80, seconds=index),
            )
        )
    for index in range(60):
        db_session.add(
            Memory(
                memory_type="open_loop",
                text=f"Open: Evie — leftover {index}",
                payload={
                    "kind": "open_loop",
                    "scope": "Evie" if index % 2 == 0 else "Mac Control",
                    "title": f"leftover {index}",
                    "status": "open" if index < 10 else "resolved",
                    "loop_key": f"leftover {index}",
                    "evidence_type": "owner_asserted",
                    "source_event_ids": [],
                },
                fingerprint=uuid4().hex,
                importance=0.4,
                is_current=True,
                event_time=now - timedelta(days=index),
                valid_from=now - timedelta(days=index),
            )
        )
    await db_session.commit()
    started = time.perf_counter()
    opens = await list_loops(db_session, k=12)
    open_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    historical = await memories_as_of(db_session, boundary=now - timedelta(days=60), k=24)
    as_of_ms = (time.perf_counter() - started) * 1000
    assert opens
    assert historical is not None
    assert open_ms < 1500
    assert as_of_ms < 1500


@pytest.mark.asyncio
async def test_health_v11_fields_omit_owner_text(client: AsyncClient) -> None:
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    memory = (resp.json().get("runtime") or {}).get("memory") or {}
    blob = json.dumps(memory)
    assert "Safari" not in blob
    assert memory.get("memory_gate_mode") == "off"
    assert memory.get("temporal_retrieval_ready") is True
    assert memory.get("prefetch_mode") == "off"
    assert "open_loop_count" in memory
    assert "curator_retryable_failed" in memory
    assert memory.get("curator_version") == settings.memory_curator_version
