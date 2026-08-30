"""RQ job entrypoints for background memory processing."""

from __future__ import annotations

from app.services.processor import process_event_sync


def process_event(event_id: str) -> list[dict]:
    """Called by RQ workers; runs extraction + memory writing."""
    import asyncio
    from uuid import UUID

    try:
        result = asyncio.run(process_event_sync(UUID(event_id)))
    except Exception as exc:  # noqa: BLE001 - worker boundary: record and re-raise
        from app.services.runtime import record_dead_letter_sync

        record_dead_letter_sync(
            queue="ingestion",
            job_id=event_id,
            payload={
                "event_id": event_id,
                "entrypoint": "app.workers.jobs.process_event",
                "args": [event_id],
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        from app.services.runtime import resolve_dead_letter_sync

        resolve_dead_letter_sync(queue="ingestion", job_id=event_id)
        return result


def run_live_retention(days: int | None = None) -> dict:
    """Scheduled/CLI entrypoint for the live-event retention window."""
    import asyncio

    from app.services.live_retention import apply_live_retention

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await apply_live_retention(session, days=days, actor="scheduler")
            await session.commit()
            return result

    return asyncio.run(_run())


def run_live_rebuild(reason: str = "scheduled rebuild") -> dict:
    """Scheduled/CLI entrypoint for the live-derived-state rebuild."""
    import asyncio

    from app.services.live_rebuild import rebuild_live_derived_state

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await rebuild_live_derived_state(
                session,
                actor="scheduler",
                reason=reason,
            )
            await session.commit()
            return result

    return asyncio.run(_run())


def run_compliance_retention(reason: str = "retention policy") -> dict:
    """Scheduled/CLI entrypoint for the biometric retention sweep.

    Enforces the configured voiceprint retention window (``EV_RETENTION_*`` /
    ``EV_REGION``) so revocation and residency rules are applied by the
    scheduler, not only on demand.
    """
    import asyncio

    from app.compliance.erasure import retention_sweep

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await retention_sweep(session, reason=reason, actor="scheduler")
            await session.commit()
            return result

    return asyncio.run(_run())


def run_research_job(job_id: str) -> dict:
    """RQ entry point for one durable research job.

    The job row is the checkpoint/restart boundary; the worker owns no
    in-memory state that must survive a process restart.
    """
    import asyncio
    from uuid import UUID

    async def _run() -> dict:
        from app.db import SessionLocal
        from app.ev.research import ResearchService

        async with SessionLocal() as session:
            result = await ResearchService(session, actor="worker").run_job(UUID(job_id))
            await session.commit()
            return result

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - worker boundary: record and re-raise
        from app.services.runtime import record_dead_letter_sync

        record_dead_letter_sync(
            queue="research",
            job_id=job_id,
            payload={
                "research_job_id": job_id,
                "entrypoint": "app.workers.jobs.run_research_job",
                "args": [job_id],
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        from app.services.runtime import resolve_dead_letter_sync

        resolve_dead_letter_sync(queue="research", job_id=job_id)
        return result
