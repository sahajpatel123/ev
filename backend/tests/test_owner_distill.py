"""Owner distill path: capture -> distill with off/shadow/on modes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import owner_model as owner_store
from app.memory.extraction import Extractor
from app.memory.owner_distill import distill_owner_candidates
from app.models import Event, Memory
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.services.processor import ensure_processed


async def _seed_event(db_session: AsyncSession, text: str) -> Event:
    event = await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type="note", text=text)
    )
    await db_session.commit()
    return event


def _owner_kinds(event: Event) -> list[str]:
    return [c.owner_kind for c in Extractor().extract(event) if c.owner_kind]


async def test_extract_trait_phrasing(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session, "I always write tests first.")
    kinds = _owner_kinds(event)
    assert kinds == ["trait"]


async def test_extract_value_phrasing(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session, "I believe in craft over speed.")
    kinds = _owner_kinds(event)
    assert kinds == ["value"]


async def test_extract_thinking_style_phrasing(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session, "I make decisions by writing pros and cons.")
    kinds = _owner_kinds(event)
    assert kinds == ["thinking_style"]


async def test_transient_state_is_not_an_owner_fact(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session, "I am tired today after a long week.")
    assert _owner_kinds(event) == []


async def test_plain_fact_is_not_an_owner_fact(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session, "I'm 30 years old.")
    candidates = Extractor().extract(event)
    assert _owner_kinds(event) == []
    assert any(c.memory_type == "fact" and not c.owner_kind for c in candidates)


async def test_shadow_mode_default_writes_no_owner_rows(
    db_session: AsyncSession, monkeypatch
) -> None:
    assert settings.owner_distill_mode == "shadow"
    event = await _seed_event(db_session, "I always write tests first.")
    await ensure_processed(event.id)
    assert await owner_store.current_owner_rows(db_session, "trait") == []


async def test_off_mode_writes_no_owner_rows(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "off")
    event = await _seed_event(db_session, "I believe in craft over speed.")
    await ensure_processed(event.id)
    assert await owner_store.current_owner_rows(db_session, "value") == []


async def test_on_mode_writes_versioned_row_with_provenance(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "on")
    event = await _seed_event(db_session, "I always write tests first.")
    await ensure_processed(event.id)
    rows = await owner_store.current_owner_rows(db_session, "trait")
    assert len(rows) == 1
    assert rows[0].version == 1
    assert rows[0].source_type == "explicit"
    assert await owner_store.row_source_events(db_session, "trait", rows[0].id) == [event.id]


async def test_on_mode_dedupes_repeat_statements(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "on")
    event = await _seed_event(db_session, "I always write tests first.")
    await ensure_processed(event.id)
    # Same statement again (new event): fingerprint dedupe, still one row.
    event2 = await _seed_event(db_session, "I always write tests first.")
    await ensure_processed(event2.id)
    rows = await owner_store.current_owner_rows(db_session, "trait")
    assert len(rows) == 1
    assert rows[0].version == 1


async def test_owner_candidates_never_reach_memory_writer(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "on")
    event = await _seed_event(
        db_session,
        "I decided to use SQLite for local testing. I always write tests first.",
    )
    await ensure_processed(event.id)
    memories = list((await db_session.execute(select(Memory))).scalars().all())
    assert any(m.memory_type == "decision" for m in memories)
    assert not any("write tests first" in m.text for m in memories)
    assert len(await owner_store.current_owner_rows(db_session, "trait")) == 1


async def test_on_mode_drops_secret_shaped_owner_facts(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "on")
    event = await _seed_event(db_session, "I always use hunter2 as my password.")
    candidates = [c for c in Extractor().extract(event) if c.owner_kind]
    assert len(candidates) == 1
    summary = await distill_owner_candidates(db_session, event, candidates)
    assert summary["dropped_secret"] == 1
    assert summary["written"] == 0
    await db_session.commit()
    assert await owner_store.current_owner_rows(db_session, "trait") == []


async def test_distill_summary_counts_shadow_without_writing(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "shadow")
    event = await _seed_event(db_session, "I believe in craft over speed.")
    candidates = [c for c in Extractor().extract(event) if c.owner_kind]
    summary = await distill_owner_candidates(db_session, event, candidates)
    assert summary["mode"] == "shadow"
    assert summary["seen"] == 1
    assert summary["written"] == 0
    assert await owner_store.current_owner_rows(db_session, "value") == []
