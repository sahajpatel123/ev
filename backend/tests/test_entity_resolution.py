"""Entity canonicalization, candidates, human-confirmed merge, and rebuild."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef
from app.memory.entities import (
    duplicate_entity_stats,
    find_entity_candidates,
    merge_entities,
    nickname_family,
    normalize_entity_name,
    resolve_entity_ref,
)
from app.models import Entity, Memory, MemoryEntity
from app.services.entity_service import confirm_entity_merge


class IdentityEmbedder:
    """Deterministic test embedder: every name maps to the same vector."""

    async def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


async def _post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _entity_ids_by_name(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.execute(
            select(Entity).where(Entity.entity_type == "person")
        )
    ).scalars().all()
    return {row.name: str(row.id) for row in rows}


def test_normalize_entity_name_folds_accents() -> None:
    assert normalize_entity_name("  José  ") == "jose"
    assert normalize_entity_name("O'Brien") == "o'brien"
    assert normalize_entity_name("New York City") == "new york city"


def test_nickname_families_are_hints() -> None:
    assert nickname_family("Mike") == nickname_family("Michael")
    assert nickname_family("Sarah") is None


async def test_alias_resolution_maps_to_canonical_entity(db_session: AsyncSession) -> None:
    michael = Entity(
        name="Michael",
        entity_type="person",
        canonical_key="person:michael",
        aliases=["mike"],
    )
    db_session.add(michael)
    await db_session.flush()
    resolved = await resolve_entity_ref(
        db_session,
        EntityRef(name="Mike", entity_type="person"),
    )
    assert resolved.id == michael.id


async def test_candidates_suggest_nickname_but_do_not_auto_merge(
    db_session: AsyncSession,
) -> None:
    michael = Entity(
        name="Michael",
        entity_type="person",
        canonical_key="person:michael",
    )
    mike = Entity(
        name="Mike",
        entity_type="person",
        canonical_key="person:mike",
    )
    db_session.add_all([michael, mike])
    await db_session.flush()
    candidates = await find_entity_candidates(
        db_session,
        EntityRef(name="Mikey", entity_type="person"),
    )
    assert candidates
    assert any(c.reason == "nickname" for c in candidates)
    # Both rows still exist: candidates never merge silently.
    assert len((await db_session.execute(select(Entity))).scalars().all()) == 2


async def test_candidates_use_embedding_similarity_seam(
    db_session: AsyncSession,
) -> None:
    existing = Entity(
        name="Miheer",
        entity_type="person",
        canonical_key="person:miheer",
    )
    db_session.add(existing)
    await db_session.flush()
    candidates = await find_entity_candidates(
        db_session,
        EntityRef(name="Mihir", entity_type="person"),
        embeddings=IdentityEmbedder(),
    )
    assert candidates
    assert candidates[0].entity.id == existing.id
    assert candidates[0].reason == "embedding"


async def test_merge_moves_links_and_records_event(
    db_session: AsyncSession,
) -> None:
    target = Entity(name="Michael", entity_type="person", canonical_key="person:michael")
    absorbed = Entity(name="Mike", entity_type="person", canonical_key="person:mike")
    memory = Memory(
        memory_type="fact",
        text="Michael works at Acme.",
        payload={},
        importance=0.5,
        confidence=0.8,
        event_time=datetime.now(UTC),
        valid_from=datetime.now(UTC),
        fingerprint="0" * 32,
    )
    db_session.add_all([target, absorbed, memory])
    await db_session.flush()
    db_session.add(
        MemoryEntity(memory_id=memory.id, entity_id=absorbed.id, role="related")
    )
    await db_session.flush()

    report = await confirm_entity_merge(
        db_session,
        target_entity_id=target.id,
        absorbed_entity_id=absorbed.id,
        reason="same person",
        actor="test",
    )
    assert report["moved_memory_links"] == 1
    assert "mike" in [a.lower() for a in target.aliases]

    link = (
        await db_session.execute(
            select(MemoryEntity).where(MemoryEntity.memory_id == memory.id)
        )
    ).scalar_one()
    assert link.entity_id == target.id

    # The merge is recorded as a raw event for rebuild replay.
    from app.models import Event

    merge_events = (
        await db_session.execute(
            select(Event).where(Event.event_type == "entity.merge")
        )
    ).scalars().all()
    assert len(merge_events) == 1
    assert merge_events[0].content["target_canonical_key"] == "person:michael"


async def test_merge_is_idempotent(db_session: AsyncSession) -> None:
    target = Entity(name="Michael", entity_type="person", canonical_key="person:michael")
    absorbed = Entity(name="Mike", entity_type="person", canonical_key="person:mike")
    db_session.add_all([target, absorbed])
    await db_session.flush()
    from app.models import Event

    event = Event(
        source="entity",
        event_type="entity.merge",
        content={},
        occurred_at=datetime.now(UTC),
        sha256="a" * 64,
    )
    db_session.add(event)
    await db_session.flush()
    first = await merge_entities(
        db_session, target=target, absorbed=absorbed, event=event, reason="same person"
    )
    second = await merge_entities(
        db_session, target=target, absorbed=absorbed, event=event, reason="same person"
    )
    assert first["skipped"] is False
    assert second["skipped"] is True


async def test_duplicate_rate_before_and_after_merge(client: AsyncClient, db_session: AsyncSession) -> None:
    await _post_event(client, "Met my friend Michael for coffee.")
    await _post_event(client, "Met my friend Mike again.")

    before = await duplicate_entity_stats(db_session)
    assert before["duplicate_rate"] > 0
    assert before["by_type"]["person"]["duplicates"] >= 1
    before_rate = before["duplicate_rate"]

    resp = await client.get("/v1/entities", params={"entity_type": "person"})
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json() if row["name"] in ("Michael", "Mike")]
    assert len(ids) == 2
    resp = await client.post(
        "/v1/entities/merge",
        json={
            "target_entity_id": ids[0],
            "absorbed_entity_id": ids[1],
            "reason": "duplicate capture",
        },
    )
    assert resp.status_code == 201, resp.text

    after = await duplicate_entity_stats(db_session)
    assert after["duplicate_rate"] == 0.0
    assert after["duplicate_rate"] < before_rate

    # Rebuild replays the merge event into the same derived state.
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    final = await duplicate_entity_stats(db_session)
    assert final["duplicate_rate"] == 0.0
    merged_rows = (
        await db_session.execute(
            select(Entity).where(Entity.entity_type == "person")
        )
    ).scalars().all()
    assert any(
        "mike" in [a.lower() for a in (e.aliases or [])]
        or "michael" in [a.lower() for a in (e.aliases or [])]
        for e in merged_rows
    )


async def test_merge_rejects_self_and_cross_type(client: AsyncClient) -> None:
    await _post_event(client, "Met my friend Maya for coffee.")
    await _post_event(client, "Moved to New York.")
    ids: dict[str, list[str]] = {}
    rows = (await client.get("/v1/entities")).json()
    for row in rows:
        ids.setdefault(row["entity_type"], []).append(row["id"])
    self_resp = await client.post(
        "/v1/entities/merge",
        json={
            "target_entity_id": ids["person"][0],
            "absorbed_entity_id": ids["person"][0],
            "reason": "self",
        },
    )
    assert self_resp.status_code == 409
    cross_resp = await client.post(
        "/v1/entities/merge",
        json={
            "target_entity_id": ids["person"][0],
            "absorbed_entity_id": ids["place"][0],
            "reason": "cross",
        },
    )
    assert cross_resp.status_code == 409
