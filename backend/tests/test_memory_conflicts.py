"""Open-conflict detection across observations, facts, preferences, decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import MemoryCandidate
from app.memory.writer import MemoryWriter
from app.models import Conflict, Event, Memory, MemoryEvent


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _open_conflicts(db_session: AsyncSession) -> list[Conflict]:
    return list(
        (
            await db_session.execute(
                select(Conflict).where(Conflict.status == "open")
            )
        ).scalars().all()
    )


async def test_observation_conflict_is_open(client: AsyncClient, db_session: AsyncSession) -> None:
    await post_event(client, "Mornings are a time I like to focus.")
    await post_event(client, "Mornings are a time I hate to focus.")
    conflicts = await _open_conflicts(db_session)
    assert conflicts
    assert any("Conflicting observations" in c.reason for c in conflicts)


async def test_decision_conflict_is_open(client: AsyncClient, db_session: AsyncSession) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I decided to use Postgres for local testing.")
    conflicts = await _open_conflicts(db_session)
    assert any("SQLite" in c.reason or "sqlite" in c.reason for c in conflicts) or any(
        "Conflicting decisions" in c.reason for c in conflicts
    )
    assert any(c.reason.startswith("Conflicting decisions") for c in conflicts)


async def test_preference_reversal_is_open(client: AsyncClient, db_session: AsyncSession) -> None:
    await post_event(client, "I prefer tea over coffee.")
    await post_event(client, "I prefer coffee over tea.")
    conflicts = await _open_conflicts(db_session)
    assert any("Conflicting preferences" in c.reason for c in conflicts)


async def test_fact_conflict_is_open(db_session: AsyncSession) -> None:
    event = Event(
        source="test",
        event_type="note",
        content={"text": "My city is Pune."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="c" * 64,
    )
    db_session.add(event)
    await db_session.flush()
    a = Memory(
        memory_type="fact",
        text="I: city = Pune",
        payload={"subject": "I", "property": "city", "value": "Pune"},
        importance=0.85,
        confidence=0.95,
        source_type="explicit",
        event_time=datetime(2026, 8, 1, tzinfo=UTC),
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        fingerprint="1" * 32,
    )
    b = Memory(
        memory_type="fact",
        text="I: city = Mumbai",
        payload={"subject": "I", "property": "city", "value": "Mumbai"},
        importance=0.85,
        confidence=0.95,
        source_type="explicit",
        event_time=datetime(2026, 8, 2, tzinfo=UTC),
        valid_from=datetime(2026, 8, 2, tzinfo=UTC),
        fingerprint="2" * 32,
    )
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add_all(
        [
            MemoryEvent(memory_id=a.id, event_id=event.id),
            MemoryEvent(memory_id=b.id, event_id=event.id),
        ]
    )
    await db_session.flush()

    # A new current fact with the same property triggers the open conflict.
    c = Memory(
        memory_type="fact",
        text="I: city = Nagpur",
        payload={"subject": "I", "property": "city", "value": "Nagpur"},
        importance=0.85,
        confidence=0.95,
        source_type="explicit",
        event_time=datetime(2026, 8, 3, tzinfo=UTC),
        valid_from=datetime(2026, 8, 3, tzinfo=UTC),
        fingerprint="3" * 32,
    )
    db_session.add(c)
    await db_session.flush()
    writer = MemoryWriter(db_session)
    candidate = MemoryCandidate(
        memory_type="fact",
        text="I: city = Nagpur",
        payload={"subject": "I", "property": "city", "value": "Nagpur"},
        importance=0.85,
        confidence=0.95,
        source_type="explicit",
    )
    await writer._detect_conflicts(c, candidate)
    conflicts = await _open_conflicts(db_session)
    assert any("Conflicting facts" in conflict.reason for conflict in conflicts)


async def test_conflicts_survive_rebuild(client: AsyncClient, db_session: AsyncSession) -> None:
    await post_event(client, "I prefer tea over coffee.")
    await post_event(client, "I prefer coffee over tea.")
    before = len(await _open_conflicts(db_session))
    assert before >= 1
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    after = len(await _open_conflicts(db_session))
    assert after == before


async def test_open_conflicts_surface_in_context(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.services.conflicts import open_conflict_lines

    await post_event(client, "I prefer tea over coffee.")
    await post_event(client, "I prefer coffee over tea.")
    lines = await open_conflict_lines(db_session)
    assert lines
    assert "Conflicting preferences" in lines[0]

    from app.context.compiler import ContextCompiler

    user_state = SimpleNamespace(
        activity="coding",
        active_project="EV",
        active_goal="ship",
        current_task="conflicts",
        recent_topics=["tea"],
        live_context=[],
        open_decisions=[],
    )
    plan = ContextCompiler().compile(
        memories=[],
        user_state=user_state,
        strategy_text="STRATEGY: casual",
        budget=10_000,
        open_conflicts=lines,
    )
    assert "OPEN CONFLICTS" in plan.text
    assert "Conflicting preferences" in plan.text
