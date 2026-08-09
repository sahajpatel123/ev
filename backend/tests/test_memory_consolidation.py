"""Long-horizon consolidation: period summaries, versioned reruns, changes query."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conflict, Entity, EntityRelationship, Event, Memory, MemoryEvent
from app.utils.text import canonical_json


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
    """Content-equivalent snapshot including payloads for strong equality."""
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
            canonical_json(m.payload or {}),
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


def _diff_snapshots(before: dict, after: dict) -> None:
    from collections import Counter

    for key in before:
        cb, ca = Counter(before[key]), Counter(after[key])
        if cb == ca:
            continue
        for item in sorted(set(before[key]) | set(after[key])):
            if cb[item] != ca[item]:
                print(f"DIFF {key} count {cb[item]} -> {ca[item]}: {item}")


async def test_period_summary_creation_and_provenance(client: AsyncClient) -> None:
    period_start = (datetime.now(UTC) - timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for i, text in enumerate(
        ["I decided to use SQLite for local testing.", "I prefer tea over coffee.", "My name is Sahaj."]
    ):
        await post_event(client, text, occurred_at=period_start + timedelta(hours=i + 8))

    resp = await client.post(
        "/v1/consolidate",
        params={"granularity": "day", "period_start": period_start.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["granularity"] == "day"
    assert len(report["written"]) == 1

    resp = await client.get("/v1/memories?memory_type=summary")
    summaries = [
        m for m in resp.json()["memories"] if (m["payload"] or {}).get("kind") == "period_summary"
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["payload"]["event_count"] == 3
    assert summary["payload"]["counts"]["decision"] == 1
    assert summary["payload"]["counts"]["preference"] == 1
    assert summary["payload"]["counts"]["fact"] == 1
    assert len(summary["source_events"]) == 3
    assert len(summary["payload"]["evidence"]) == 3
    assert summary["source_type"] == "derived"


async def test_consolidation_rerun_versions_and_rebuild(client: AsyncClient, db_session: AsyncSession) -> None:
    period_start = (datetime.now(UTC) - timedelta(days=3)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    await post_event(client, "I decided to use SQLite for local testing.", occurred_at=period_start + timedelta(hours=9))
    await post_event(client, "I prefer tea over coffee.", occurred_at=period_start + timedelta(hours=10))

    params = {"granularity": "day", "period_start": period_start.isoformat()}
    resp = await client.post("/v1/consolidate", params=params)
    assert resp.status_code == 200
    v1_id = resp.json()["written"][0]

    # More evidence in the same period -> rerun creates v2 superseding v1.
    await post_event(client, "My name is Sahaj.", occurred_at=period_start + timedelta(hours=11))
    resp = await client.post("/v1/consolidate", params=params)
    assert resp.status_code == 200
    v2_id = resp.json()["written"][0]
    assert v2_id != v1_id

    resp = await client.get(f"/v1/audit/{v2_id}")
    audit = resp.json()
    versions = audit["versions"]
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[0]["is_current"] is False
    assert versions[1]["version"] == 2
    assert versions[1]["is_current"] is True
    assert versions[1]["reason_for_change"] == "Period recomputed"
    assert versions[1]["payload"]["event_count"] == 3

    events_before = await event_snapshot(db_session)
    state_before = await derived_snapshot(db_session)

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    assert resp.json()["summaries_created"] >= 1

    assert await event_snapshot(db_session) == events_before
    state_after = await derived_snapshot(db_session)
    if state_after != state_before:
        _diff_snapshots(state_before, state_after)
    assert state_after == state_before

    # Determinism: second rebuild is identical.
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200
    state_after2 = await derived_snapshot(db_session)
    if state_after2 != state_before:
        _diff_snapshots(state_before, state_after2)
    assert state_after2 == state_before


async def test_memory_changes_since(client: AsyncClient) -> None:
    base = datetime.now(UTC) - timedelta(days=7)
    e1 = await post_event(client, "I decided to use SQLite for local testing.", occurred_at=base)
    await post_event(
        client,
        "I decided to use SQLite for local testing, and document the choice.",
        occurred_at=base + timedelta(hours=1),
    )

    since = base + timedelta(minutes=30)
    resp = await client.get("/v1/memories/changes", params={"since": since.isoformat()})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["total"] == 1
    group = payload["groups"][0]
    assert group["memory_type"] == "decision"
    assert len(group["versions"]) == 2
    assert group["versions"][1]["reason_for_change"] == "Value changed"
    assert group["versions"][0]["is_current"] is False
    assert group["versions"][1]["is_current"] is True

    # No changes after the last event.
    resp = await client.get(
        "/v1/memories/changes",
        params={"since": (base + timedelta(hours=2)).isoformat()},
    )
    assert resp.json()["total"] == 0

    # Filtering by a different memory type hides the decision change.
    resp = await client.get(
        "/v1/memories/changes",
        params={"since": since.isoformat(), "memory_type": "preference"},
    )
    assert resp.json()["total"] == 0

    # The change trail traces to the source events.
    resp = await client.get(f"/v1/events/{e1['id']}")
    assert resp.status_code == 200
    assert resp.json()["sha256"]
