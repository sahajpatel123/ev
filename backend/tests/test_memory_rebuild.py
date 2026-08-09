"""Invariant tests: deterministic regeneration of derived state from raw events."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conflict, Entity, EntityRelationship, Event, Memory, MemoryEvent


async def post_event(
    client: AsyncClient,
    text: str,
    *,
    occurred_at: datetime | None = None,
    event_type: str = "note",
) -> dict:
    payload = {"source": "test", "event_type": event_type, "text": text}
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at.isoformat()
    resp = await client.post("/v1/events", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def event_snapshot(session: AsyncSession) -> list[tuple]:
    session.expire_all()
    rows = (
        await session.execute(select(Event).order_by(Event.occurred_at, Event.id))
    ).scalars().all()
    return [
        (
            str(e.id),
            e.sha256,
            e.event_type,
            e.occurred_at.isoformat(),
            e.tombstoned_at.isoformat() if e.tombstoned_at else None,
        )
        for e in rows
    ]


async def derived_snapshot(session: AsyncSession) -> dict:
    """Content-equivalent snapshot of derived state (IDs/timestamps excluded)."""
    session.expire_all()
    memories = (
        await session.execute(
            select(Memory).order_by(Memory.memory_type, Memory.fingerprint, Memory.version)
        )
    ).scalars().all()
    fp_by_id = {str(m.id): m.fingerprint for m in memories}

    provenance: dict[str, list[str]] = defaultdict(list)
    for row in (await session.execute(select(MemoryEvent))).scalars().all():
        provenance[str(row.memory_id)].append(str(row.event_id))

    memory_snapshot = [
        (
            m.memory_type,
            m.fingerprint,
            m.version,
            m.text,
            round(m.importance, 3),
            round(m.confidence, 3),
            m.source_type,
            m.is_current,
            m.redacted,
            m.valid_from.isoformat(),
            m.valid_until.isoformat() if m.valid_until else None,
            fp_by_id.get(str(m.superseded_by_id)),
            tuple(sorted(provenance.get(str(m.id), []))),
        )
        for m in memories
    ]
    memory_snapshot.sort()

    entities = sorted(
        (e.entity_type, e.canonical_key, e.name)
        for e in (await session.execute(select(Entity))).scalars().all()
    )

    entity_ids = {str(e.id): e.canonical_key for e in (await session.execute(select(Entity))).scalars().all()}
    conflicts = sorted(
        (
            fp_by_id.get(str(c.memory_id_a)),
            fp_by_id.get(str(c.memory_id_b)),
            c.reason,
            c.status,
            fp_by_id.get(str(c.resolution_memory_id)) if c.resolution_memory_id else None,
        )
        for c in (await session.execute(select(Conflict))).scalars().all()
    )
    relationships = sorted(
        (
            entity_ids.get(str(r.from_entity_id)),
            entity_ids.get(str(r.to_entity_id)),
            r.relationship_type,
            r.weight,
            r.source_type,
        )
        for r in (await session.execute(select(EntityRelationship))).scalars().all()
    )
    return {
        "memories": memory_snapshot,
        "entities": entities,
        "conflicts": conflicts,
        "relationships": relationships,
    }


async def test_rebuild_regenerates_equivalent_derived_state(client: AsyncClient, db_session: AsyncSession) -> None:
    base = datetime.now(UTC) - timedelta(days=10)
    for i, text in enumerate(
        [
            "I decided to use SQLite for local testing.",
            "I decided to use SQLite for local testing, and document the choice.",
            "I prefer local-first storage over cloud-only solutions.",
            "My name is Sahaj.",
            "I want to build EV as a persistent personal AI.",
            "I feel like mornings are the best time to focus.",
            "I feel like mornings are not worth the effort.",
            "I feel like mornings are where I get the most done.",
            "Met my friend Maya for coffee.",
        ]
    ):
        await post_event(client, text, occurred_at=base + timedelta(minutes=i))

    resp = await client.get("/v1/decisions")
    decision = resp.json()["memories"][0]

    resp = await client.post(
        f"/v1/memories/{decision['id']}/correct",
        json={"corrected_text": "Decided: use Postgres for local testing.", "reason": "I misspoke"},
    )
    assert resp.status_code == 201, resp.text
    corrected = resp.json()

    resp = await client.post(
        f"/v1/memories/{corrected['id']}/forget",
        json={"reason": "stale"},
    )
    assert resp.status_code == 200
    resp = await client.post(f"/v1/memories/{corrected['id']}/restore")
    assert resp.status_code == 200

    resp = await client.post(
        f"/v1/decisions/{corrected['id']}/outcome",
        json={
            "expected_outcome": "faster local tests",
            "actual_outcome": "much slower than Postgres",
            "lesson": None,
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post("/v1/research/sessions", json={"question": "Which local embedding model is best?"})
    session_id = resp.json()["id"]
    resp = await client.post(
        f"/v1/research/sessions/{session_id}/notes",
        json={"note": "nomic is strong for local retrieval.", "source_url": "https://example.com"},
    )
    assert resp.status_code == 201
    resp = await client.post(
        f"/v1/research/sessions/{session_id}/conclude",
        json={"conclusion": "Use nomic-embed-text locally."},
    )
    assert resp.status_code == 200

    resp = await client.post("/v1/patterns/analyze?window_days=30&min_count=3")
    assert resp.status_code == 200, resp.text

    events_before = await event_snapshot(db_session)
    state_before = await derived_snapshot(db_session)
    assert state_before["memories"], "expected seeded derived state"

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["events_total"] == len(events_before)
    assert report["events_replayed"] == len(events_before)
    assert report["memories_created"] > 0
    assert report["patterns_created"] >= 1
    assert report["summaries_created"] == 1
    assert report["lessons_created"] == 1
    assert report["operations_applied"] == 3  # correction + forget + restore
    assert report["deleted_memories"] == len(state_before["memories"])

    state_after = await derived_snapshot(db_session)
    assert state_after == state_before
    assert await event_snapshot(db_session) == events_before  # raw events untouched

    # Provenance invariant: every memory traces to >=1 raw event.
    for memory in state_after["memories"]:
        assert memory[12], f"memory without provenance: {memory[:4]}"

    # Determinism: a second rebuild produces the identical derived state.
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200
    assert await derived_snapshot(db_session) == state_after


async def test_rebuild_preserves_tombstone_redaction(client: AsyncClient, db_session: AsyncSession) -> None:
    event = await post_event(client, "I decided to use SQLite for local testing.")

    resp = await client.get("/v1/decisions")
    assert len(resp.json()["memories"]) == 1

    resp = await client.delete(f"/v1/events/{event['id']}?reason=user-requested")
    assert resp.status_code == 200
    assert resp.json()["tombstoned_at"] is not None

    before = await derived_snapshot(db_session)
    decision_rows = [m for m in before["memories"] if m[0] == "decision"]
    assert decision_rows and all(m[8] for m in decision_rows)  # redacted preserved

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tombstoned_events"] == 1
    assert resp.json()["redacted_memories"] >= 1

    after = await derived_snapshot(db_session)
    assert after == before

    events = await event_snapshot(db_session)
    row = next(e for e in events if e[0] == str(event["id"]))
    assert row[4] is not None  # tombstoned_at preserved
