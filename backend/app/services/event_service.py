from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event
from app.schemas import EventCreate
from app.services.access_log import log_access
from app.utils.text import canonical_json, sha256_hex, utcnow


class EventService:
    def __init__(self, session: AsyncSession, actor: str = "api") -> None:
        self.session = session
        self.actor = actor

    async def create(
        self,
        data: EventCreate,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        content = data.effective_content()
        occurred_at = data.occurred_at or utcnow()
        if occurred_at.tzinfo is not None:
            occurred_at = occurred_at.astimezone(UTC)
        # Canonical UTC-naive form survives SQLite's lossy tz round-trip, so an
        # exported bundle can verify the same hash after a restore.
        hash_occurred_at = occurred_at.replace(tzinfo=None).isoformat()
        canonical = canonical_json(
            {
                "content": content,
                "metadata": data.metadata,
                "source": data.source,
                "event_type": data.event_type,
                "occurred_at": hash_occurred_at,
                "privacy_level": data.privacy_level,
            }
        )
        event = Event(
            source=data.source,
            event_type=data.event_type,
            content=content,
            metadata_=data.metadata,
            occurred_at=occurred_at,
            device_id=data.device_id,
            conversation_id=data.conversation_id,
            privacy_level=data.privacy_level,
            sha256=sha256_hex(canonical),
            idempotency_key_hash=sha256_hex(idempotency_key) if idempotency_key else None,
        )
        self.session.add(event)
        await self.session.flush()
        await log_access(
            self.session,
            actor=self.actor,
            action="write",
            endpoint="POST /v1/events",
            resource_type="event",
            resource_ids=[event.id],
            request_id=request_id,
        )
        return event

    async def tombstone(self, event_id: UUID, reason: str, *, request_id: str | None = None) -> Event:
        event = await self.session.get(Event, event_id)
        if event is None:
            raise KeyError(f"Event {event_id} not found")
        if event.tombstoned_at is not None:
            raise ValueError(f"Event {event_id} already tombstoned")
        event.tombstoned_at = utcnow()
        event.tombstone_reason = reason
        await self.session.flush()
        await log_access(
            self.session,
            actor=self.actor,
            action="delete",
            endpoint="DELETE /v1/events/{id}",
            resource_type="event",
            resource_ids=[event_id],
            request_id=request_id,
            details={"reason": reason},
        )
        return event

    async def timeline(
        self,
        *,
        limit: int = 50,
        cursor: datetime | None = None,
        source: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_tombstoned: bool = False,
    ) -> list[Event]:
        stmt = select(Event).order_by(Event.occurred_at.desc(), Event.id.desc()).limit(min(limit, 500))
        if not include_tombstoned:
            stmt = stmt.where(Event.tombstoned_at.is_(None))
        if cursor is not None:
            stmt = stmt.where(Event.occurred_at < cursor)
        if source:
            stmt = stmt.where(Event.source == source)
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        if since:
            stmt = stmt.where(Event.occurred_at >= since)
        if until:
            stmt = stmt.where(Event.occurred_at <= until)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
