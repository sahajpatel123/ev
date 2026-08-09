"""Retention policy for the live-data stream.

The raw ``live_events`` stream is append-only, so retention is the only path
that removes raw rows.  To preserve replay/rebuild and provenance guarantees
it is deliberately conservative: only **consumed** events older than the
window are eligible, the latest event of every channel is always kept (it is
the anchor of ``live_derived_state``), and events still referenced as
provenance by recognition logs or routine runs are protected.  The derived
per-channel rollups are recomputed from the retained stream so they remain
deterministic and rebuildable.  ``dry_run=True`` (the API default) only plans.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.live import derive_channel_signals
from app.models import (
    LiveChannel,
    LiveDerivedState,
    LiveEvent,
    RecognitionLog,
    RoutineRun,
)
from app.services.access_log import log_access
from app.utils.text import utcnow


async def apply_live_retention(
    session: AsyncSession,
    *,
    days: int | None = None,
    dry_run: bool = True,
    actor: str = "api",
    request_id: str | None = None,
) -> dict:
    """Apply the live-event retention window; returns a plan or executes it."""
    window_days = days or settings.live_event_retention_days
    cutoff = utcnow() - timedelta(days=window_days)

    candidates = list(
        (
            await session.execute(
                select(LiveEvent).where(
                    LiveEvent.occurred_at < cutoff,
                    LiveEvent.consumed.is_(True),
                )
            )
        ).scalars().all()
    )
    if not candidates:
        return {
            "completed_at": utcnow(),
            "days": window_days,
            "cutoff": cutoff,
            "dry_run": dry_run,
            "events_scanned": 0,
            "events_deleted": 0,
            "events_kept_latest": 0,
            "events_protected": 0,
            "channels_updated": 0,
        }

    candidate_ids = {event.id for event in candidates}
    protected_ids: set = set()

    # Latest event per channel anchors derived state; keep it.
    latest_rows = (
        await session.execute(
            select(LiveDerivedState.channel_id, LiveDerivedState.latest_event_id).where(
                LiveDerivedState.latest_event_id.in_(candidate_ids)
            )
        )
    ).all()
    protected_ids.update(row[1] for row in latest_rows if row[1] is not None)

    # Provenance links (recognition log, routine triggers) must survive.
    recognition_ids = (
        await session.execute(
            select(RecognitionLog.live_event_id).where(
                RecognitionLog.live_event_id.in_(candidate_ids)
            )
        )
    ).scalars().all()
    protected_ids.update(rid for rid in recognition_ids if rid is not None)
    routine_ids = (
        await session.execute(
            select(RoutineRun.trigger_live_event_id).where(
                RoutineRun.trigger_live_event_id.in_(candidate_ids)
            )
        )
    ).scalars().all()
    protected_ids.update(rid for rid in routine_ids if rid is not None)

    to_delete = [event for event in candidates if event.id not in protected_ids]
    deleted_ids = {event.id for event in to_delete}
    affected_channels = {event.channel_id for event in to_delete}

    if not dry_run and to_delete:
        channels = list(
            (
                await session.execute(
                    select(LiveChannel).where(LiveChannel.id.in_(affected_channels))
                )
            ).scalars().all()
        )
        channel_map = {channel.id: channel for channel in channels}
        derived_rows = {
            row.channel_id: row
            for row in (
                await session.execute(
                    select(LiveDerivedState).where(
                        LiveDerivedState.channel_id.in_(affected_channels)
                    )
                )
            ).scalars().all()
        }

        for channel_id in affected_channels:
            remaining = list(
                (
                    await session.execute(
                        select(LiveEvent)
                        .where(
                            LiveEvent.channel_id == channel_id,
                            LiveEvent.id.not_in(deleted_ids),
                        )
                        .order_by(LiveEvent.occurred_at.asc(), LiveEvent.id.asc())
                    )
                ).scalars().all()
            )
            channel = channel_map.get(channel_id)
            if channel is None:
                continue
            derived = derived_rows.get(channel_id)
            if derived is None and remaining:
                derived = LiveDerivedState(channel_id=channel_id)
                session.add(derived)
            if derived is not None:
                derived.event_count = len(remaining)
                derived.consumed_count = sum(1 for e in remaining if e.consumed)
                derived.first_event_at = remaining[0].occurred_at if remaining else None
                derived.last_event_at = remaining[-1].occurred_at if remaining else None
                derived.latest_event_id = remaining[-1].id if remaining else None
                derived.signals = derive_channel_signals(channel, remaining)
                derived.rebuilt_at = utcnow()

        await session.execute(delete(LiveEvent).where(LiveEvent.id.in_(deleted_ids)))
        await session.flush()

    await log_access(
        session,
        actor=actor,
        action="retention" if not dry_run else "retention_plan",
        endpoint="POST /v1/live/retention",
        resource_type="live_events",
        resource_ids=[],
        request_id=request_id,
        details={
            "days": window_days,
            "cutoff": cutoff.isoformat(),
            "dry_run": dry_run,
            "events_deleted": len(to_delete),
            "events_protected": len(candidates) - len(to_delete),
        },
    )

    return {
        "completed_at": utcnow(),
        "days": window_days,
        "cutoff": cutoff,
        "dry_run": dry_run,
        "events_scanned": len(candidates),
        "events_deleted": len(to_delete),
        "events_kept_latest": len(latest_rows),
        "events_protected": len(candidates) - len(to_delete),
        "channels_updated": len(affected_channels) if not dry_run else 0,
    }
