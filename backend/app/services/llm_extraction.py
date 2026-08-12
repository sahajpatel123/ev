"""Asynchronous enrichment service (Follow-up Order 6).

Rule-based extraction writes memories immediately at ingestion; this service
is the optional enrichment pass that runs later (queued job or backfill). It is
batched, deduplicated by content hash, triaged, budget-capped, and it stores
its structured output as an immutable ``extraction.llm`` event so rebuilds stay
deterministic and offline CI never needs a model.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.llm_extractor import (
    LLMExtractor,
    candidates_to_content,
    llm_extraction_batch_size,
    llm_extraction_daily_call_cap,
    llm_extraction_daily_token_cap,
    llm_extraction_enabled,
    llm_extraction_monthly_call_cap,
    llm_extraction_monthly_token_cap,
    replay_llm_extraction_event,
    should_enrich,
    text_fingerprint,
)
from app.models import Event, ModelCallLog
from app.schemas import EventCreate, PrivacyLevel
from app.services.event_service import EventService
from app.utils.text import utcnow


async def enrichment_usage(session: AsyncSession, *, now: datetime | None = None) -> dict:
    """Call/token usage of the enrichment pass from the audited model-call log."""
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    day_start = datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)
    month_start = day_start.replace(day=1)
    rows = (
        await session.execute(
            select(ModelCallLog).where(
                ModelCallLog.actor == "memory",
                ModelCallLog.created_at >= month_start,
            )
        )
    ).scalars().all()
    day_rows = [
        row
        for row in rows
        if (
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None
            else row.created_at
        )
        >= day_start
    ]
    return {
        "day_calls": len(day_rows),
        "day_tokens": sum((r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in day_rows),
        "month_calls": len(rows),
        "month_tokens": sum((r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in rows),
    }


async def budget_available(session: AsyncSession) -> dict:
    """Hard daily/monthly caps; enrichment pauses silently when exhausted."""
    usage = await enrichment_usage(session)
    ok = (
        usage["day_calls"] < llm_extraction_daily_call_cap()
        and usage["day_tokens"] < llm_extraction_daily_token_cap()
        and usage["month_calls"] < llm_extraction_monthly_call_cap()
        and usage["month_tokens"] < llm_extraction_monthly_token_cap()
    )
    return {"ok": ok, "usage": usage}


async def _existing_fingerprints(
    session: AsyncSession,
    *,
    since: datetime | None = None,
) -> set[str]:
    stmt = select(Event.metadata_).where(Event.event_type == "extraction.llm")
    if since is not None:
        stmt = stmt.where(Event.occurred_at >= since)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        str(row.get("source_text_fingerprint"))
        for row in rows
        if isinstance(row, dict) and row.get("source_text_fingerprint")
    }


async def _persist_extraction(
    session: AsyncSession,
    *,
    event: Event,
    candidates,
    actor: str,
    source_fingerprint: str,
) -> dict:
    record = await EventService(session, actor=actor).create(
        EventCreate(
            source="memory",
            event_type="extraction.llm",
            content=candidates_to_content(event, candidates),
            metadata={
                "source_event_id": str(event.id),
                "source_text_fingerprint": source_fingerprint,
                "extracted_at": utcnow().isoformat(),
            },
            privacy_level=cast(PrivacyLevel, event.privacy_level or "normal"),
        )
    )
    await session.flush()
    written = await replay_llm_extraction_event(session, record)
    return {
        "extraction_event_id": str(record.id),
        "memories_written": written,
        "candidates": len(candidates),
    }


async def run_llm_extraction_for_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    actor: str = "memory",
    force: bool = False,
) -> dict:
    """Enrich one event: triage -> dedup -> budget -> one audited call."""
    if not llm_extraction_enabled():
        return {"event_id": str(event_id), "status": "disabled"}
    event = await session.get(Event, event_id)
    if event is None:
        return {"event_id": str(event_id), "status": "not_found"}
    if event.tombstoned_at is not None:
        return {"event_id": str(event_id), "status": "tombstoned"}
    if not force and not should_enrich(event):
        return {"event_id": str(event_id), "status": "skipped_triage"}

    source_fingerprint = text_fingerprint((event.content or {}).get("text") or "")
    if source_fingerprint in await _existing_fingerprints(session):
        return {"event_id": str(event_id), "status": "duplicate"}

    budget = await budget_available(session)
    if not budget["ok"]:
        return {"event_id": str(event_id), "status": "budget_paused", **budget}

    extractor = LLMExtractor(session=session)
    candidates = await extractor.extract(event)
    if extractor.last_error is not None:
        return {
            "event_id": str(event_id),
            "status": "error",
            "error": extractor.last_error,
        }
    if not candidates:
        return {
            "event_id": str(event_id),
            "status": "no_candidates",
            "model_available": extractor.available,
        }
    result = await _persist_extraction(
        session,
        event=event,
        candidates=candidates,
        actor=actor,
        source_fingerprint=source_fingerprint,
    )
    return {"event_id": str(event_id), "status": "ok", **result}


async def run_llm_extraction_batch(
    session: AsyncSession,
    *,
    limit: int = 200,
    before=None,
    after=None,
    force: bool = False,
) -> dict:
    """Backfill enrichment: triage, dedup, budget, batched calls."""
    stmt = (
        select(Event)
        .where(
            Event.event_type != "message.assistant",
            Event.tombstoned_at.is_(None),
            Event.privacy_level != "never_send_to_model",
        )
        .order_by(Event.occurred_at.asc(), Event.id.asc())
        .limit(min(limit, 500))
    )
    if before is not None:
        stmt = stmt.where(Event.occurred_at < before)
    if after is not None:
        stmt = stmt.where(Event.occurred_at >= after)
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return {"scanned": 0, "processed": 0, "api_calls": 0, "events": []}

    existing = await _existing_fingerprints(session)
    budget = await budget_available(session)
    pending: list[Event] = []
    skipped = {"triage": 0, "duplicate": 0, "budget": 0}
    for event in rows:
        fp = text_fingerprint((event.content or {}).get("text") or "")
        if not force and not should_enrich(event):
            skipped["triage"] += 1
        elif fp in existing:
            skipped["duplicate"] += 1
        elif not budget["ok"]:
            skipped["budget"] += 1
        else:
            pending.append(event)

    extractor = LLMExtractor(session=session)
    reports: list[dict] = []
    api_calls = 0
    batch_size = llm_extraction_batch_size()
    if not extractor.available:
        return {
            "scanned": len(rows),
            "processed": 0,
            "api_calls": 0,
            "skipped": skipped,
            "model_available": False,
            "events": [],
        }
    seen = set(existing)
    for start in range(0, len(pending), batch_size):
        if not budget["ok"]:
            break
        chunk: list[Event] = []
        for event in pending[start : start + batch_size]:
            fp = text_fingerprint((event.content or {}).get("text") or "")
            if fp in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(fp)
            chunk.append(event)
        if not chunk:
            continue
        for event, candidates in await extractor.extract_batch(chunk):
            if not candidates:
                continue
            fp = text_fingerprint((event.content or {}).get("text") or "")
            result = await _persist_extraction(
                session,
                event=event,
                candidates=candidates,
                actor="memory",
                source_fingerprint=fp,
            )
            existing.add(fp)
            reports.append({"event_id": str(event.id), **result})
        api_calls += 1
        budget = await budget_available(session)

    return {
        "scanned": len(rows),
        "processed": len(reports),
        "api_calls": api_calls,
        "skipped": skipped,
        "model_available": extractor.available,
        "events": reports,
    }


def run_llm_extraction_job(event_id: str) -> dict:
    """RQ entrypoint: one event, one session, one commit."""
    import asyncio

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await run_llm_extraction_for_event(session, UUID(event_id))
            await session.commit()
            return result

    return asyncio.run(_run())


def run_llm_extraction_batch_job(*, limit: int = 200) -> dict:
    """Scheduler/RQ entrypoint: enrich a batch, one session, one commit."""
    import asyncio

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await run_llm_extraction_batch(session, limit=limit)
            await session.commit()
            return result

    return asyncio.run(_run())
