"""Real-time live-event streaming with the same privacy slices as the API.

Collectors and EV-side consumers can tail newly ingested live events over
SSE.  ``access="user"`` streams full payloads (the user's own data);
``access="model"`` streams only the permitted slice: sensitive and
never_send_to_model channels/events are excluded and each event is rendered as
a minimal derived context line, never the raw payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.live import live_context_line
from app.models import LiveChannel, LiveEvent
from app.utils.text import utcnow

MODEL_PRIVACY_FILTERS = (
    LiveEvent.privacy_level.notin_(("never_send_to_model", "sensitive")),
    LiveChannel.privacy_level.notin_(("never_send_to_model", "sensitive")),
)


def stream_item(channel: LiveChannel | None, event: LiveEvent, *, access: str = "user") -> dict:
    """One streamed observation: provenance + permissioned content slice."""
    item: dict = {
        "id": str(event.id),
        "channel_id": str(event.channel_id),
        "channel_name": channel.name if channel else None,
        "kind": channel.kind if channel else None,
        "event_type": event.event_type,
        "collector": event.collector,
        "occurred_at": event.occurred_at.isoformat(),
        "ingested_at": event.ingested_at.isoformat(),
        "privacy_level": event.privacy_level,
        "access": access,
    }
    if access == "model":
        item["context"] = live_context_line(channel, event, access="model")
    else:
        item["payload"] = event.payload or {}
    return item


async def stream_live_events(
    session: AsyncSession,
    *,
    access: str = "user",
    since: datetime | None = None,
    poll_interval: float = 1.0,
    max_replay: int = 500,
    timeout_seconds: float | None = None,
) -> AsyncIterator[dict]:
    """Tail live events; with ``since``, replay that window before tailing."""
    cursor_at = since or utcnow()
    cursor_id: UUID | None = None
    initial = since is not None
    deadline = (
        utcnow() + timedelta(seconds=timeout_seconds) if timeout_seconds else None
    )

    while True:
        if deadline is not None and utcnow() >= deadline:
            return
        stmt = (
            select(LiveEvent, LiveChannel)
            .join(LiveChannel, LiveChannel.id == LiveEvent.channel_id)
            .where(LiveChannel.active.is_(True))
        )
        if cursor_id is None:
            if initial:
                stmt = stmt.where(LiveEvent.ingested_at >= cursor_at)
            else:
                stmt = stmt.where(LiveEvent.ingested_at > cursor_at)
        else:
            stmt = stmt.where(
                or_(
                    LiveEvent.ingested_at > cursor_at,
                    and_(LiveEvent.ingested_at == cursor_at, LiveEvent.id > cursor_id),
                )
            )
        if access == "model":
            stmt = stmt.where(*MODEL_PRIVACY_FILTERS)
        stmt = stmt.order_by(LiveEvent.ingested_at.asc(), LiveEvent.id.asc()).limit(max_replay)
        rows = (await session.execute(stmt)).all()
        for event, channel in rows:
            yield stream_item(channel, event, access=access)
            cursor_at = event.ingested_at
            cursor_id = event.id
        # Release the read transaction so the next poll sees newly committed
        # events (otherwise SQLite keeps a stale snapshot for the session).
        await session.rollback()
        initial = False
        await asyncio.sleep(max(0.05, poll_interval))
