"""Calendar signal feed for EDITH surfaces (Agent 12's real calendar data).

Calendar events are ingested by Agent 12's integration layer into live events
on an integration-scoped channel. This module is the *consumer* side: it reads
the stored live events, derives the compact signals via
``app.integrations.calendar_signals.derive_calendar_signals``, and preserves
the live-event ids as provenance so HUD cards, route briefings, tactical
briefings, EV Sense, and the alert radar can all cite a real source instead of
a synthetic deadline row.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.calendar_signals import derive_calendar_signals
from app.models import Integration, LiveChannel, LiveEvent
from app.utils.text import utcnow

CALENDAR_EVENT_TYPE = "calendar.event.updated"


async def calendar_signals(session: AsyncSession, *, limit: int = 500) -> dict:
    """Derived calendar signals from every active calendar integration.

    Returns the same compact shape as
    ``app.integrations.calendar_signals.derive_calendar_signals`` with an extra
    ``source`` field carrying per-event provenance (live event ids). When no
    calendar integration has stored events the result is the empty derivation,
    so callers can rely on the shape without guessing.
    """
    payloads, event_ids = await _calendar_payloads(session, limit=limit)
    signals = derive_calendar_signals(payloads, now=utcnow())
    signals["source"] = {
        "kind": "calendar_live_events",
        "event_ids": event_ids,
        "event_count": len(event_ids),
    }
    return signals


async def calendar_event_payloads(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> tuple[list[dict], list[str]]:
    """Return ``(payloads, live_event_ids)`` for stored calendar events."""
    return await _calendar_payloads(session, limit=limit)


async def _calendar_payloads(
    session: AsyncSession,
    *,
    limit: int,
) -> tuple[list[dict], list[str]]:
    integrations = (
        await session.execute(
            select(Integration).where(
                Integration.adapter == "calendar",
                Integration.status == "active",
                Integration.live_channel_id.is_not(None),
            )
        )
    ).scalars().all()
    if not integrations:
        return [], []

    channel_ids = [integration.live_channel_id for integration in integrations if integration.live_channel_id]
    channel_rows = (
        await session.execute(
            select(LiveChannel).where(
                LiveChannel.id.in_(channel_ids),
                LiveChannel.active.is_(True),
            )
        )
    ).scalars().all()
    active_ids = [channel.id for channel in channel_rows]
    if not active_ids:
        return [], []

    rows = (
        await session.execute(
            select(LiveEvent)
            .where(
                LiveEvent.channel_id.in_(active_ids),
                LiveEvent.event_type == CALENDAR_EVENT_TYPE,
            )
            .order_by(LiveEvent.occurred_at.desc())
            .limit(min(limit, 500))
        )
    ).scalars().all()
    # Newest-first is convenient for truncation, but signal derivation expects
    # an unordered bag; derivation sorts internally, so any order is safe.
    payloads = [row.payload for row in rows if isinstance(row.payload, dict)]
    event_ids = [str(row.id) for row in rows]
    return payloads, event_ids
