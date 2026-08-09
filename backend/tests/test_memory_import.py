"""Import/restore round-trip: export -> fresh DB -> import -> equivalent state."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, SessionLocal, engine
from app.models import Conflict, Entity, EntityRelationship, Event, Memory, MemoryEvent


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None).isoformat()


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

    memory_snapshot = sorted(
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
            _utc_iso(m.valid_from),
            _utc_iso(m.valid_until),
            fp_by_id.get(str(m.superseded_by_id)),
            tuple(sorted(provenance.get(str(m.id), []))),
        )
        for m in memories
    )
    entities = sorted(
        (e.entity_type, e.canonical_key, e.name)
        for e in (await session.execute(select(Entity))).scalars().all()
    )
    entity_ids = {
        str(e.id): e.canonical_key for e in (await session.execute(select(Entity))).scalars().all()
    }
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


async def seed_scenario(client: AsyncClient) -> dict:
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
    corrected = resp.json()
    await client.post(f"/v1/memories/{corrected['id']}/forget", json={"reason": "stale"})
    await client.post(f"/v1/memories/{corrected['id']}/restore")
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
    await client.post(
        f"/v1/research/sessions/{session_id}/notes",
        json={"note": "nomic is strong for local retrieval.", "source_url": "https://example.com"},
    )
    resp = await client.post(
        f"/v1/research/sessions/{session_id}/conclude",
        json={"conclusion": "Use nomic-embed-text locally."},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post("/v1/patterns/analyze?window_days=30&min_count=3")
    assert resp.status_code == 200, resp.text

    resp = await client.post("/v1/export")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_export_import_roundtrip(client: AsyncClient, db_session: AsyncSession) -> None:
    bundle = await seed_scenario(client)
    events_before = await event_snapshot(db_session)
    state_before = await derived_snapshot(db_session)
    assert state_before["memories"]

    # Simulate a restore: fresh database, then import the bundle.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Tamper detection: a corrupted bundle must be rejected before any write.
    tampered = json.loads(json.dumps(bundle))
    tampered["events"][0]["sha256"] = "0" * 64
    resp = await client.post("/v1/import", json=tampered)
    assert resp.status_code == 409, resp.text
    assert "hash mismatch" in resp.json()["detail"]

    resp = await client.post("/v1/import", json=bundle)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["mode"] == "merge"
    assert report["events_imported"] == len(events_before)
    assert report["events_skipped"] == 0
    assert report["memories_created"] > 0
    assert report["summaries_created"] == 1
    assert report["lessons_created"] == 1
    assert report["operations_applied"] == 3
    assert report["patterns_created"] >= 1

    async with SessionLocal() as fresh:
        events_after = await event_snapshot(fresh)
        state_after = await derived_snapshot(fresh)
    assert events_after == events_before  # event ids + hashes preserved
    assert state_after == state_before  # derived state regenerated equivalently

    # Provenance invariant after restore.
    for memory in state_after["memories"]:
        assert memory[12], f"memory without provenance: {memory[:4]}"

    # Idempotent merge: importing the same bundle changes nothing.
    resp = await client.post("/v1/import", json=bundle)
    assert resp.status_code == 200, resp.text
    assert resp.json()["events_imported"] == 0
    assert resp.json()["events_skipped"] == len(events_before)
    async with SessionLocal() as fresh:
        assert await derived_snapshot(fresh) == state_before

    # Replace mode refuses an in-place destructive restore over live events.
    resp = await client.post("/v1/import", json=bundle, params={"mode": "replace"})
    assert resp.status_code == 409
    assert "empty event log" in resp.json()["detail"]
