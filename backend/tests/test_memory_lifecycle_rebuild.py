"""Mixed-lifecycle rebuild equivalence: every new derived path in one replay.

One scenario exercises rule extraction, LLM-assisted extraction, entity merge,
open conflicts, temporal payloads, and a state-of-me rollup; the derived layer
must be identical before and after rebuild (and after a second rebuild).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatResult
from app.models import Conflict, Entity, Event, Memory, MemoryEntity, MemoryEvent
from app.utils.text import canonical_json


class FakeLocalProvider:
    name = "local"

    async def chat(self, messages, *, model=None, temperature=0.7):
        return ChatResult(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "memory_type": "preference",
                            "text": "Prefers tea over coffee.",
                            "importance": 0.8,
                            "confidence": 0.85,
                            "source_type": "explicit",
                            "entities": [{"name": "tea", "entity_type": "topic"}],
                            "payload": {
                                "subject": "tea",
                                "value": "prefer",
                                "over": "coffee",
                            },
                        }
                    ]
                }
            ),
            usage={},
            model="qwen3-1.7b",
        )

    async def list_models(self):
        return ["qwen3-1.7b"]


def _utc_iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None).isoformat()


async def _post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _derived_snapshot(session: AsyncSession) -> dict:
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
    memory_rows = sorted(
        (
            m.memory_type,
            m.fingerprint,
            m.version,
            m.text,
            canonical_json(m.payload or {}),
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
    entity_rows = sorted(
        (e.entity_type, e.canonical_key, e.name, tuple(sorted(e.aliases or [])), e.summary)
        for e in (await session.execute(select(Entity))).scalars().all()
    )
    entity_key = {
        str(e.id): e.canonical_key
        for e in (await session.execute(select(Entity))).scalars().all()
    }
    links = sorted(
        (
            fp_by_id.get(str(link.memory_id)),
            entity_key.get(str(link.entity_id)),
            link.role,
        )
        for link in (await session.execute(select(MemoryEntity))).scalars().all()
    )
    conflicts = sorted(
        (
            fp_by_id.get(str(c.memory_id_a)),
            fp_by_id.get(str(c.memory_id_b)),
            c.status,
        )
        for c in (await session.execute(select(Conflict))).scalars().all()
    )
    return {
        "memories": memory_rows,
        "entities": entity_rows,
        "links": links,
        "conflicts": conflicts,
    }


async def test_mixed_lifecycle_rebuild_equivalence(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _post_event(client, "I prefer tea over coffee.")
    await _post_event(client, "I prefer coffee over tea.")
    await _post_event(client, "I decided to use SQLite for local testing.")
    await _post_event(client, "In March I want to focus on health.")
    await _post_event(client, "Met my friend Maya for coffee.")
    await _post_event(client, "Met my friend Maya again.")
    await _post_event(client, "Met my friend Mike for lunch.")

    # LLM-assisted extraction (fake local brain) enriches the first preference.
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: FakeLocalProvider())
    from app.services.llm_extraction import run_llm_extraction_for_event

    source = next(
        (
            event
            for event in (await db_session.execute(select(Event))).scalars().all()
            if (event.content or {}).get("text") == "I prefer tea over coffee."
        ),
        None,
    )
    assert source is not None
    report = await run_llm_extraction_for_event(db_session, source.id, force=True)
    assert report["memories_written"] == 1
    await db_session.commit()

    # Human-confirmed entity merge: Mike into Maya.
    persons = (
        await db_session.execute(select(Entity).where(Entity.entity_type == "person"))
    ).scalars().all()
    maya = next(e for e in persons if e.name == "Maya")
    mike = next(e for e in persons if e.name == "Mike")
    resp = await client.post(
        "/v1/entities/merge",
        json={
            "target_entity_id": str(maya.id),
            "absorbed_entity_id": str(mike.id),
            "reason": "same person",
        },
    )
    assert resp.status_code == 201, resp.text

    # State-of-me rollup over the current period.
    now = datetime.now(UTC)
    resp = await client.post(
        "/v1/state-of-me",
        params={
            "period_start": (now - timedelta(days=1)).isoformat(),
            "period_end": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"]

    state_before = await _derived_snapshot(db_session)
    assert state_before["memories"]
    assert state_before["conflicts"]
    assert any("mike" in alias for e in state_before["entities"] for alias in e[3])

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    state_after = await _derived_snapshot(db_session)
    assert state_after == state_before

    # Determinism: a second rebuild is identical.
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200
    assert await _derived_snapshot(db_session) == state_after
