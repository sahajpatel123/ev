"""Seed a few demo events so EV has something to retrieve (dev only)."""

from __future__ import annotations

import asyncio

from app.db import SessionLocal, init_db
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.services.processor import ensure_processed

DEMO_EVENTS = [
    EventCreate(
        source="seed",
        event_type="note",
        text="Decided to use SQLite for local testing and Postgres with pgvector in production.",
        privacy_level="normal",
    ),
    EventCreate(
        source="seed",
        event_type="note",
        text="Goal: build EV as a persistent personal AI companion.",
        privacy_level="normal",
    ),
    EventCreate(
        source="seed",
        event_type="note",
        text="I prefer local-first storage over cloud-only solutions.",
        privacy_level="normal",
    ),
]


async def run() -> None:
    await init_db()
    async with SessionLocal() as session:
        service = EventService(session)
        ids = []
        for data in DEMO_EVENTS:
            event = await service.create(data)
            ids.append(event.id)
        await session.commit()
        for event_id in ids:
            await ensure_processed(event_id)
        print(f"Seeded {len(ids)} events.")


if __name__ == "__main__":
    asyncio.run(run())

