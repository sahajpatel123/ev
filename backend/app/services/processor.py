from __future__ import annotations

from uuid import UUID

from app.config import settings


async def process_event_sync(event_id: UUID) -> list[dict]:
    """Run extraction + memory writing for one event (sync mode)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.embeddings import get_embedder
    from app.memory.extraction import Extractor
    from app.memory.writer import MemoryWriter
    from app.models import Event

    async with SessionLocal() as session:
        result = await session.execute(select(Event).where(Event.id == event_id))
        event = result.scalar_one_or_none()
        if event is None or event.tombstoned_at is not None:
            return []
        writer = MemoryWriter(session, embeddings=get_embedder())
        candidates = Extractor().extract(event)
        deltas = await writer.write_all(event, candidates)
        await session.commit()
        return [
            {"id": str(d.memory_id), "memory_type": d.memory_type, "action": d.action, "text": d.text}
            for d in deltas
        ]


def enqueue_event(event_id: UUID) -> None:
    from redis import Redis
    from rq import Queue

    queue = Queue("ingestion", connection=Redis.from_url(settings.redis_url))
    queue.enqueue("app.workers.jobs.process_event", str(event_id))


async def ensure_processed(event_id: UUID) -> list[dict]:
    if settings.processing_mode == "queue":
        enqueue_event(event_id)
        return []
    return await process_event_sync(event_id)

