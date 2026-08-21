from __future__ import annotations

from uuid import UUID

from app.config import settings


def queue_worker_available() -> bool:
    """True only when Redis is up and an RQ worker is registered on ingestion."""

    try:
        from redis import Redis
        from rq import Queue, Worker

        conn = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.4,
            socket_timeout=0.4,
        )
        if not conn.ping():
            return False
        queue = Queue("ingestion", connection=conn)
        try:
            workers = list(Worker.all(queue=queue))
        except TypeError:
            workers = [
                worker
                for worker in Worker.all(connection=conn)
                if "ingestion" in set(worker.queue_names() or [])
            ]
        return any(workers)
    except Exception:  # noqa: BLE001 - missing Redis/RQ is a real "no consumer"
        return False


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
        from app.memory.observe import log_memory

        log_memory(
            "memory.extraction_started",
            extra={"event_id": str(event.id), "event_type": event.event_type},
        )
        deltas = await writer.write_all(event, candidates)
        await session.commit()
        log_memory(
            "memory.extraction_completed",
            extra={"event_id": str(event.id), "deltas": len(deltas)},
        )
        # LLM refinement never blocks ingestion; in sync mode it is invoked
        # explicitly (service/tests), and in queue mode it is a separate job.
        maybe_enqueue_llm_extraction(event_id)
        return [
            {"id": str(d.memory_id), "memory_type": d.memory_type, "action": d.action, "text": d.text}
            for d in deltas
        ]


def enqueue_event(event_id: UUID) -> None:
    from redis import Redis
    from rq import Queue

    queue = Queue("ingestion", connection=Redis.from_url(settings.redis_url))
    queue.enqueue("app.workers.jobs.process_event", str(event_id))


def maybe_enqueue_llm_extraction(event_id: UUID) -> None:
    """Queue the optional enrichment pass without touching the hot path."""
    from app.memory.llm_extractor import llm_extraction_enabled

    if not llm_extraction_enabled() or settings.processing_mode != "queue":
        return
    from redis import Redis
    from rq import Queue

    queue = Queue("ingestion", connection=Redis.from_url(settings.redis_url))
    queue.enqueue("app.services.llm_extraction.run_llm_extraction_job", str(event_id))


async def ensure_processed(event_id: UUID) -> list[dict]:
    if settings.processing_mode == "queue" and queue_worker_available():
        enqueue_event(event_id)
        maybe_enqueue_llm_extraction(event_id)
        return []
    return await process_event_sync(event_id)
