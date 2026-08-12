"""LLM-assisted extraction: fail-closed offline, deterministic replay."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatMessage, ChatResult
from app.memory.llm_extractor import (
    LLMExtractor,
    candidates_from_content,
    candidates_to_content,
    llm_extraction_enabled,
    replay_llm_extraction_event,
)
from app.models import Conflict, Entity, Event, Memory, MemoryEntity, MemoryEvent
from app.services.llm_extraction import run_llm_extraction_for_event
from app.utils.text import canonical_json, normalize_text


def _event(text: str = "I decided to use SQLite for local testing.") -> Event:
    return Event(
        source="test",
        event_type="note",
        content={"text": text},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        privacy_level="normal",
        sha256="0" * 64,
    )


async def _derived_snapshot(db_session: AsyncSession) -> dict:
    db_session.expire_all()
    memories = (await db_session.execute(select(Memory))).scalars().all()
    fp_by_id = {str(m.id): m.fingerprint for m in memories}
    entity_key = {
        str(e.id): e.canonical_key
        for e in (await db_session.execute(select(Entity))).scalars().all()
    }
    memories = sorted(
        (
            m.memory_type,
            m.text,
            canonical_json(m.payload or {}),
            m.version,
            m.is_current,
        )
        for m in memories
    )
    prov = sorted(
        (fp_by_id.get(str(row.memory_id)), str(row.event_id))
        for row in (await db_session.execute(select(MemoryEvent))).scalars().all()
    )
    conflicts = sorted(
        (
            fp_by_id.get(str(row.memory_id_a)),
            fp_by_id.get(str(row.memory_id_b)),
            row.status,
        )
        for row in (await db_session.execute(select(Conflict))).scalars().all()
    )
    links = sorted(
        (fp_by_id.get(str(row.memory_id)), entity_key.get(str(row.entity_id)))
        for row in (await db_session.execute(select(MemoryEntity))).scalars().all()
    )
    return {"memories": memories, "prov": prov, "conflicts": conflicts, "links": links}


class FakeLocalProvider:
    name = "local"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        self.calls.append(list(messages))
        return ChatResult(text=json.dumps(self.payload), usage={}, model="qwen3-1.7b")

    async def list_models(self) -> list[str]:
        return ["qwen3-1.7b"]


@pytest.fixture
def llm_enabled(monkeypatch) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")


def test_extractor_fails_closed_when_disabled() -> None:
    assert llm_extraction_enabled() is False
    extractor = LLMExtractor(provider=FakeLocalProvider({"candidates": []}))
    assert extractor.available is False
    assert asyncio.run(extractor.extract(_event())) is None


def test_local_provider_is_recognized_when_enabled(llm_enabled) -> None:
    from app.gateway.providers import LocalModelProvider

    extractor = LLMExtractor(
        provider=LocalModelProvider(
            base_url="http://localhost:11434/v1",
            default_model="qwen3-1.7b",
        )
    )
    assert extractor.available is True


def test_non_local_provider_fails_closed(llm_enabled) -> None:
    from app.gateway.providers import MockProvider

    extractor = LLMExtractor(provider=MockProvider())
    assert extractor.available is False
    assert asyncio.run(extractor.extract(_event())) is None


def test_extractor_parses_structured_output(llm_enabled) -> None:
    provider = FakeLocalProvider(
        {
            "candidates": [
                {
                    "memory_type": "decision",
                    "text": "Decided: use SQLite for local testing.",
                    "importance": 0.9,
                    "confidence": 0.8,
                    "source_type": "explicit",
                    "entities": [
                        {"name": "SQLite", "entity_type": "project", "role": "related"}
                    ],
                    "payload": {"topic": "sqlite"},
                },
                {
                    "memory_type": "observation",
                    "text": "Local-first storage is feeling right.",
                    "importance": 0.4,
                    "confidence": 0.55,
                    "source_type": "inferred",
                    "entities": [],
                    "payload": {},
                },
            ]
        }
    )
    extractor = LLMExtractor(provider=provider)
    candidates = asyncio.run(extractor.extract(_event()))
    assert candidates is not None
    assert len(candidates) == 2
    decision = candidates[0]
    assert decision.memory_type == "decision"
    assert decision.entities[0].name == "SQLite"
    assert decision.payload["llm_extracted"] is True
    assert 0.0 <= decision.importance <= 1.0
    assert 0.0 <= decision.confidence <= 1.0
    assert provider.calls and provider.calls[0][0].role == "system"


def test_extractor_rejects_fabricated_memory_types(llm_enabled) -> None:
    provider = FakeLocalProvider(
        {
            "candidates": [
                {
                    "memory_type": "romance_novel",
                    "text": "not a memory",
                    "importance": 1.0,
                    "confidence": 1.0,
                    "source_type": "explicit",
                }
            ]
        }
    )
    assert asyncio.run(LLMExtractor(provider=provider).extract(_event())) == []


def test_extractor_skips_never_send_to_model(llm_enabled) -> None:
    event = _event("My password is hunter2.")
    event.privacy_level = "never_send_to_model"
    provider = FakeLocalProvider({"candidates": []})
    assert asyncio.run(LLMExtractor(provider=provider).extract(event)) == []
    assert provider.calls == []


def test_extractor_batch_uses_indexes(llm_enabled) -> None:
    provider = FakeLocalProvider(
        {
            "results": [
                {
                    "index": 0,
                    "candidates": [
                        {
                            "memory_type": "goal",
                            "text": "Ship the iOS app.",
                            "importance": 0.8,
                            "confidence": 0.7,
                            "source_type": "explicit",
                            "entities": [],
                            "payload": {},
                        }
                    ],
                }
            ]
        }
    )
    events = [_event("I want to ship the iOS app."), _event("Second capture.")]
    output = asyncio.run(LLMExtractor(provider=provider).extract_batch(events))
    assert len(output) == 1
    assert output[0][0] is events[0]
    assert output[0][1][0].memory_type == "goal"


def test_content_roundtrip_preserves_candidates(llm_enabled) -> None:
    event = _event("In March I want to focus on health.")
    provider = FakeLocalProvider(
        {
            "candidates": [
                {
                    "memory_type": "goal",
                    "text": "Focus on health in March.",
                    "importance": 0.7,
                    "confidence": 0.6,
                    "source_type": "explicit",
                    "entities": [{"name": "health", "entity_type": "topic"}],
                    "payload": {"status": "active"},
                }
            ]
        }
    )
    candidates = asyncio.run(LLMExtractor(provider=provider).extract(event))
    assert candidates
    content = candidates_to_content(event, candidates)
    rebuilt = candidates_from_content(content, event)
    assert len(rebuilt) == 1
    assert rebuilt[0].memory_type == candidates[0].memory_type
    assert rebuilt[0].text == candidates[0].text
    assert rebuilt[0].entities[0].name == "health"
    assert "temporal" in rebuilt[0].payload


async def test_replay_skips_rule_based_duplicates(
    db_session: AsyncSession,
    llm_enabled,
) -> None:
    event = _event("I decided to use SQLite for local testing.")
    db_session.add(event)
    await db_session.flush()
    provider = FakeLocalProvider(
        {
            "candidates": [
                {
                    "memory_type": "decision",
                    "text": "Decided: use SQLite for local testing.",
                    "importance": 0.9,
                    "confidence": 0.8,
                    "source_type": "explicit",
                    "entities": [],
                    "payload": {"topic": "sqlite"},
                }
            ]
        }
    )
    candidates = await LLMExtractor(provider=provider).extract(event)
    extraction_event = Event(
        source="memory",
        event_type="extraction.llm",
        content=candidates_to_content(event, candidates),
        metadata={"source_event_id": str(event.id)},
        occurred_at=event.occurred_at,
        sha256="1" * 64,
    )
    db_session.add(extraction_event)
    await db_session.flush()
    written = await replay_llm_extraction_event(db_session, extraction_event)
    assert written == 0


async def test_llm_extraction_event_roundtrip(
    client,
    db_session: AsyncSession,
    llm_enabled,
) -> None:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "I prefer tea over coffee.",
        },
    )
    assert resp.status_code == 201, resp.text
    event_id = resp.json()["event"]["id"]

    provider = FakeLocalProvider(
        {
            "candidates": [
                {
                    "memory_type": "preference",
                    "text": "Prefers tea over coffee.",
                    "importance": 0.8,
                    "confidence": 0.85,
                    "source_type": "explicit",
                    "entities": [{"name": "tea", "entity_type": "topic"}],
                    "payload": {"subject": "tea", "value": "prefer", "over": "coffee"},
                }
            ]
        }
    )
    monkeypatch = pytest.MonkeyPatch()
    from app.gateway import providers as gateway_providers

    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    try:
        report = await run_llm_extraction_for_event(
            db_session,
            UUID(event_id),
            force=True,
        )
    finally:
        monkeypatch.undo()
    assert report["status"] == "ok"
    assert report["memories_written"] == 1

    rows = (
        await db_session.execute(
            select(Memory).where(Memory.memory_type == "preference")
        )
    ).scalars().all()
    assert any("tea" in normalize_text(m.text) for m in rows)
    memory = next(m for m in rows if "prefers tea" in normalize_text(m.text))
    prov = (
        await db_session.execute(
            select(MemoryEvent).where(MemoryEvent.memory_id == memory.id)
        )
    ).scalars().all()
    assert len(prov) == 2  # extraction.llm event + original source event
    extraction_rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == "extraction.llm")
        )
    ).scalars().all()
    assert extraction_rows
    source_event = await db_session.get(Event, UUID(event_id))
    assert source_event is not None
    for row in extraction_rows:
        assert row.occurred_at > source_event.occurred_at
    await db_session.commit()

    # Rebuild replays the extraction.llm event deterministically.
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    after = (
        await db_session.execute(
            select(Memory).where(Memory.memory_type == "preference")
        )
    ).scalars().all()
    rebuilt = next(m for m in after if "prefers tea" in normalize_text(m.text))
    rebuilt_prov = (
        await db_session.execute(
            select(MemoryEvent).where(MemoryEvent.memory_id == rebuilt.id)
        )
    ).scalars().all()
    assert len(rebuilt_prov) == 2  # provenance survives rebuild

    # A second rebuild produces an identical derived state.
    first = await _derived_snapshot(db_session)
    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    assert await _derived_snapshot(db_session) == first
