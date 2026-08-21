"""Postgres outbox for Memory OS curation. Independent of Redis RQ ingestion."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import MemoryCurationJob
from app.utils.text import utcnow

_CONVERSATION_TYPES = {"message.user", "message.assistant"}


async def enqueue_curation_job(
    session: AsyncSession,
    *,
    event_id: UUID | None,
    kind: str = "curate",
    priority: int = 0,
    source_event_ids: list[str] | None = None,
) -> MemoryCurationJob | None:
    if event_id is None:
        return None
    key = f"{kind}:{event_id}:{settings.memory_curator_version}"
    existing = (
        await session.execute(select(MemoryCurationJob).where(MemoryCurationJob.job_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    job = MemoryCurationJob(
        id=uuid4(),
        job_key=key,
        event_id=event_id,
        kind=kind,
        status="pending",
        curator_version=settings.memory_curator_version,
        priority=priority,
        source_event_ids=source_event_ids or [str(event_id)],
        available_at=utcnow(),
    )
    session.add(job)
    return job


async def enqueue_for_event(session: AsyncSession, event) -> MemoryCurationJob | None:
    if getattr(event, "event_type", None) not in _CONVERSATION_TYPES:
        return None
    if getattr(event, "privacy_level", None) == "never_send_to_model":
        return None
    kind = "curate"
    priority = 0
    text = str((event.content or {}).get("text") or "").strip().lower()
    if text.startswith("remember ") or "remember that" in text[:80]:
        kind = "remember"
        priority = 10
    try:
        return await enqueue_curation_job(
            session,
            event_id=event.id,
            kind=kind,
            priority=priority,
            source_event_ids=[str(event.id)],
        )
    except Exception:  # noqa: BLE001 - Event commit must not depend on outbox
        return None


async def pending_counts(session: AsyncSession) -> tuple[int, int]:
    counts = await job_counts(session)
    return counts["pending"] + counts["retryable_failed"], counts["permanent_failed"]


async def job_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(MemoryCurationJob.status, MemoryCurationJob.last_error))).all()
    pending = retryable = permanent = 0
    for status, error in rows:
        if status == "pending":
            pending += 1
        elif status == "retryable_failed" or (status == "skipped" and error == "curator_unavailable"):
            retryable += 1
        elif status in {"permanent_failed", "failed"}:
            permanent += 1
    return {
        "pending": pending,
        "retryable_failed": retryable,
        "permanent_failed": permanent,
    }


async def claim_jobs(session: AsyncSession, *, limit: int = 4) -> list[MemoryCurationJob]:
    rows = (
        await session.execute(
            select(MemoryCurationJob)
            .where(
                or_(
                    MemoryCurationJob.status == "pending",
                    MemoryCurationJob.status == "retryable_failed",
                    (MemoryCurationJob.status == "skipped")
                    & (MemoryCurationJob.last_error == "curator_unavailable"),
                ),
                MemoryCurationJob.available_at <= utcnow(),
            )
            .order_by(MemoryCurationJob.priority.desc(), MemoryCurationJob.created_at.asc())
            .limit(max(1, limit))
        )
    ).scalars().all()
    now = utcnow()
    claimed: list[MemoryCurationJob] = []
    for job in rows:
        job.status = "running"
        job.started_at = now
        job.attempts = int(job.attempts or 0) + 1
        claimed.append(job)
    return claimed
