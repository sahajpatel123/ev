"""Deterministic regeneration of live derived state from immutable live events.

This is the live-data counterpart of ``services/rebuild.py``: ``live_events``
is the permanent source of truth; the per-channel derived layer
(``live_derived_state``) and the ``consumed`` flags can be dropped and replayed
back into an equivalent state.  No ``LiveEvent`` row is ever updated or
deleted here except for the derived ``consumed`` lifecycle flag.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.live import derive_channel_signals
from app.models import LiveChannel, LiveDerivedState, LiveEvent
from app.services.access_log import log_access
from app.utils.text import utcnow


async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def rebuild_live_derived_state(
    session: AsyncSession,
    *,
    actor: str = "api",
    reason: str = "manual rebuild",
    request_id: str | None = None,
) -> dict:
    """Drop the derived live layer and replay every live event into it.

    The replay is deterministic: events are processed in ``(occurred_at, id)``
    order, each event is folded into its channel's derived rollup (and marked
    ``consumed``), and signal flags carry basis live-event ids so every
    insight remains traceable to the recorded stream.
    """
    events = list(
        (
            await session.execute(
                select(LiveEvent).order_by(LiveEvent.occurred_at.asc(), LiveEvent.id.asc())
            )
        ).scalars().all()
    )
    channels = list((await session.execute(select(LiveChannel))).scalars().all())
    channel_map = {channel.id: channel for channel in channels}

    deleted_derived_rows = await _count(session, LiveDerivedState)
    await session.execute(delete(LiveDerivedState))
    # Reset the lifecycle flag so replay starts from a clean derived layer.
    await session.execute(update(LiveEvent).values(consumed=False))
    await session.flush()

    by_channel: dict = {}
    for event in events:
        by_channel.setdefault(event.channel_id, []).append(event)

    rebuilt: list[dict] = []
    for channel_id, channel_events in by_channel.items():
        channel = channel_map.get(channel_id)
        if channel is None:
            continue
        ordered = sorted(channel_events, key=lambda e: (e.occurred_at, e.id))
        row = LiveDerivedState(
            channel_id=channel_id,
            event_count=len(ordered),
            consumed_count=len(ordered),
            first_event_at=ordered[0].occurred_at,
            last_event_at=ordered[-1].occurred_at,
            latest_event_id=ordered[-1].id,
            signals=derive_channel_signals(channel, ordered),
        )
        session.add(row)
        rebuilt.append(
            {
                "channel_id": channel_id,
                "channel_name": channel.name,
                "kind": channel.kind,
                "event_count": len(ordered),
                "consumed_count": len(ordered),
                "first_event_at": ordered[0].occurred_at,
                "last_event_at": ordered[-1].occurred_at,
                "latest_event_id": ordered[-1].id,
                "signals": row.signals,
            }
        )

    # Every replayed event has now been folded into the derived layer.
    if events:
        await session.execute(update(LiveEvent).values(consumed=True))
    await session.flush()

    counts = {
        "events_total": len(events),
        "events_replayed": len(events),
        "consumed_count": len(events),
        "channels_rebuilt": len(rebuilt),
        "deleted_derived_rows": deleted_derived_rows,
    }
    await log_access(
        session,
        actor=actor,
        action="rebuild",
        endpoint="POST /v1/live/rebuild",
        resource_type="live_derived",
        resource_ids=[],
        request_id=request_id,
        details={"reason": reason, **counts},
    )

    return {
        "completed_at": utcnow(),
        "reason": reason,
        **counts,
        "channels": rebuilt,
    }
