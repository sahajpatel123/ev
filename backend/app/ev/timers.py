"""Durable timers and session elapsed time."""

from __future__ import annotations

from datetime import datetime, timedelta

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.callouts import emit_callout
from app.models import OwnerTimer, VoiceSession
from app.utils.text import utcnow

LATE_AFTER = timedelta(seconds=5)


async def start_timer(
    session: AsyncSession,
    *,
    minutes: float | None = None,
    at: str | None = None,
    text: str = "",
    now: datetime | None = None,
) -> dict:
    now = now or utcnow()
    fire_at: datetime | None = None
    if minutes is not None:
        fire_at = now + timedelta(minutes=float(minutes))
    elif at:
        try:
            parsed = date_parser.parse(str(at))
        except (ValueError, TypeError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            fire_at = parsed
    if fire_at is None:
        return {
            "ok": False,
            "error": "missing_when",
            "spoken": "Tell me how many minutes, or when.",
        }
    row = OwnerTimer(
        fire_at=fire_at,
        payload={"text": (text or "Time's up.").strip(), "minutes": minutes, "at": at},
        status="pending",
        late=False,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return {
        "ok": True,
        "id": str(row.id),
        "fire_at": fire_at.isoformat(),
        "status": row.status,
        "text": row.payload.get("text"),
        "spoken": f"Timer set for {fire_at.isoformat()}.",
    }


async def session_elapsed(session: AsyncSession, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    row = (
        await session.execute(
            select(VoiceSession).order_by(VoiceSession.created_at.desc()).limit(1)
        )
    ).scalars().first()
    if row is None or row.created_at is None:
        return {
            "ok": False,
            "error": "no_session",
            "spoken": "No voice session is open.",
            "seconds": 0,
        }
    started = row.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    delta = now - started
    seconds = max(0, int(delta.total_seconds()))
    minutes = seconds // 60
    spoken = (
        f"{minutes} minutes have passed."
        if minutes
        else f"{seconds} seconds have passed."
    )
    return {
        "ok": True,
        "seconds": seconds,
        "minutes": minutes,
        "started_at": started.isoformat(),
        "session_id": str(row.id),
        "spoken": spoken,
    }


async def due_scan(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    daemon_was_down: bool = False,
) -> dict:
    """Fire pending timers whose fire_at has passed. Late fires say so."""

    now = now or utcnow()
    rows = list(
        (
            await session.execute(
                select(OwnerTimer).where(
                    OwnerTimer.status == "pending",
                    OwnerTimer.fire_at <= now,
                )
            )
        ).scalars().all()
    )
    fired = 0
    for row in rows:
        fire_at = row.fire_at
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=now.tzinfo)
        late = daemon_was_down or (now - fire_at) >= LATE_AFTER
        body = str((row.payload or {}).get("text") or "Time's up.")
        if late:
            body = f"{body} this is late."
        row.status = "fired"
        row.late = late
        row.fired_at = now
        await emit_callout(
            session,
            body,
            source="15",
            source_item=str(row.id),
            hud={"schema_version": "ev.hud.card.v1", "title": "Timer", "body": body},
        )
        fired += 1
    await session.flush()
    return {"fired": fired, "scanned": len(rows)}
