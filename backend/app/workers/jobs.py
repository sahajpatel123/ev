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
