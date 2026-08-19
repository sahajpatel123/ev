"""Durable timers and session elapsed time."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.actuator import evidence_base, fingerprint, record_actuator
from app.ev.callouts import emit_callout
from app.ev.resolve import ambiguous_spoken, candidate_names, parse_owner_when, pick_unique
from app.models import OwnerTimer, VoiceSession
from app.utils.text import utcnow

LOGGER = logging.getLogger("ev.timers")

LATE_AFTER = timedelta(seconds=5)
PENDING = "pending"
FIRED = "fired"
CANCELLED = "cancelled"
WATCH_INTERVAL_S = 1.0

_SWEEP_LOCK: asyncio.Lock | None = None
_SWEEP_LOOP: asyncio.AbstractEventLoop | None = None


def _aware(value: datetime, now: datetime) -> datetime:
    if value.tzinfo is None and now.tzinfo is not None:
        return value.replace(tzinfo=now.tzinfo)
    return value


async def _pending_by_key(session: AsyncSession, key: str) -> OwnerTimer | None:
    rows = list(
        (
            await session.execute(select(OwnerTimer).where(OwnerTimer.status == PENDING))
        ).scalars().all()
    )
    for row in rows:
        if str((row.payload or {}).get("idempotency_key") or "") == key:
            return row
    return None


def _sweep_lock() -> asyncio.Lock:
    """Bind the sweep lock to the current event loop so tests stay isolated."""

    global _SWEEP_LOCK, _SWEEP_LOOP
    loop = asyncio.get_running_loop()
    if _SWEEP_LOCK is None or _SWEEP_LOOP is not loop:
        _SWEEP_LOCK = asyncio.Lock()
        _SWEEP_LOOP = loop
    return _SWEEP_LOCK


def _spoken_set(
    fire_at: datetime,
    now: datetime,
    *,
    replay: bool = False,
) -> str:
    prefix = "Timer already set for " if replay else "Timer set for "
    delta = _aware(fire_at, now) - now
    seconds = int(round(delta.total_seconds()))
    if seconds <= 0:
        return "Timer already due." if replay else "Timer set. I'll ring now."
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{prefix}{seconds} {unit}."
    minutes = int(round(seconds / 60))
    if minutes == 1:
        return f"{prefix}one minute."
    return f"{prefix}{minutes} minutes."


def _timer_payload(row: OwnerTimer, *, now: datetime, spoken: str, replay: bool = False) -> dict:
    fire_at = row.fire_at
    evidence = evidence_base(
        source="owner_timer",
        accepted=True,
        observed=True,
        now=now,
        timer_id=str(row.id),
        fire_at=fire_at.isoformat() if fire_at is not None else None,
        status=row.status,
    )
    result = {
        "ok": True,
        "id": str(row.id),
        "timer_id": str(row.id),
        "fire_at": fire_at.isoformat() if fire_at is not None else None,
        "status": row.status,
        "text": (row.payload or {}).get("text"),
        "spoken": spoken,
        "evidence": evidence,
    }
    if replay:
        result["idempotent_replay"] = True
    return result


async def start_timer(
    session: AsyncSession,
    *,
    minutes: float | None = None,
    at: str | None = None,
    text: str = "",
    now: datetime | None = None,
    actor: str = "master",
    idempotency_key: str | None = None,
) -> dict:
    now = now or utcnow()
    fire_at: datetime | None = None
    if minutes is not None:
        try:
            fire_at = now + timedelta(minutes=float(minutes))
        except (TypeError, ValueError):
            fire_at = None
    elif at:
        fire_at = parse_owner_when(str(at), now=now)
    if fire_at is None:
        return {
            "ok": False,
            "error": "missing_when",
            "spoken": "Tell me how many minutes, or when.",
        }
    body = (text or "Time's up.").strip()
    key = idempotency_key or fingerprint("start_timer", body, fire_at.replace(microsecond=0).isoformat())
    existing = await _pending_by_key(session, key)
    if existing is not None:
        replayed = _timer_payload(
            existing,
            now=now,
            spoken=_spoken_set(existing.fire_at, now, replay=True),
            replay=True,
        )
        await record_actuator(
            session,
            name="start_timer",
            actor=actor,
            key=key,
            result=replayed,
            target=body,
        )
        return replayed
    row = OwnerTimer(
        fire_at=fire_at,
        payload={"text": body, "minutes": minutes, "at": at, "idempotency_key": key},
        status=PENDING,
        late=False,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    result = _timer_payload(
        row,
        now=now,
        spoken=_spoken_set(fire_at, now),
    )
    await record_actuator(
        session,
        name="start_timer",
        actor=actor,
        key=key,
        result=result,
        target=body,
    )
    return result


async def cancel_timer(
    session: AsyncSession,
    *,
    timer_id: str | None = None,
    text: str | None = None,
    now: datetime | None = None,
    actor: str = "master",
) -> dict:
    now = now or utcnow()
    row: OwnerTimer | None = None
    if timer_id:
        from uuid import UUID

        try:
            row = await session.get(OwnerTimer, UUID(str(timer_id)))
        except (ValueError, TypeError):
            row = None
    if row is None and text:
        wanted = text.strip()
        candidates = list(
            (
                await session.execute(
                    select(OwnerTimer)
                    .where(OwnerTimer.status == PENDING)
                    .order_by(OwnerTimer.created_at.desc())
                )
            ).scalars().all()
        )
        match = pick_unique(
            wanted,
            candidates,
            labels=lambda item: [str((item.payload or {}).get("text") or ""), str(item.id)],
        )
        if match.status == "ambiguous":
            names = candidate_names(
                match.candidates,
                name_of=lambda item: str((item.payload or {}).get("text") or item.id),
            )
            return {
                "ok": False,
                "error": "ambiguous",
                "candidates": [str(item.id) for item in match.candidates],
                "spoken": ambiguous_spoken("timer", names),
            }
        row = match.item if match.unique else None
    if row is None:
        return {
            "ok": False,
            "error": "not_found",
            "spoken": "I don't have a matching pending timer.",
        }
    if row.status == CANCELLED:
        evidence = evidence_base(
            source="owner_timer",
            accepted=True,
            observed=True,
            now=now,
            timer_id=str(row.id),
            status=CANCELLED,
        )
        return {
            "ok": True,
            "id": str(row.id),
            "status": CANCELLED,
            "idempotent_replay": True,
            "spoken": "That timer is already cancelled.",
            "evidence": evidence,
        }
    if row.status != PENDING:
        return {
            "ok": False,
            "error": "not_pending",
            "id": str(row.id),
            "status": row.status,
            "spoken": "That timer already fired. I cannot cancel it.",
        }
    row.status = CANCELLED
    payload = dict(row.payload or {})
    payload["cancelled_at"] = now.isoformat()
    row.payload = payload
    await session.flush()
    evidence = evidence_base(
        source="owner_timer",
        accepted=True,
        observed=True,
        now=now,
        timer_id=str(row.id),
        status=CANCELLED,
    )
    result = {
        "ok": True,
        "id": str(row.id),
        "status": CANCELLED,
        "spoken": "Timer cancelled.",
        "evidence": evidence,
    }
    await record_actuator(
        session,
        name="cancel_timer",
        actor=actor,
        key=str(row.id),
        result=result,
        target=str((row.payload or {}).get("text") or row.id),
    )
    return result


def _timer_public(row: OwnerTimer) -> dict:
    fire_at = row.fire_at
    return {
        "id": str(row.id),
        "status": row.status,
        "text": (row.payload or {}).get("text"),
        "fire_at": fire_at.isoformat() if fire_at is not None else None,
        "late": bool(row.late),
    }


async def list_timers(session: AsyncSession, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    rows = list(
        (
            await session.execute(
                select(OwnerTimer)
                .where(OwnerTimer.status == PENDING)
                .order_by(OwnerTimer.fire_at.asc())
            )
        ).scalars().all()
    )
    items = [_timer_public(row) for row in rows]
    if not items:
        spoken = "No pending timers."
    elif len(items) == 1:
        spoken = f"One timer: {items[0]['text'] or 'untitled'} at {items[0]['fire_at']}."
    else:
        spoken = f"{len(items)} pending timers. Next: {items[0]['text'] or 'untitled'}."
    return {
        "ok": True,
        "count": len(items),
        "timers": items,
        "spoken": spoken,
        "evidence": evidence_base(source="owner_timer", accepted=True, observed=True, now=now, count=len(items)),
    }


async def snooze_timer(
    session: AsyncSession,
    *,
    timer_id: str | None = None,
    text: str | None = None,
    minutes: float = 5,
    now: datetime | None = None,
    actor: str = "master",
) -> dict:
    now = now or utcnow()
    delay = max(0.5, float(minutes))
    row: OwnerTimer | None = None
    if timer_id:
        from uuid import UUID

        try:
            row = await session.get(OwnerTimer, UUID(str(timer_id)))
        except (ValueError, TypeError):
            row = None
    if row is None:
        listed = list(
            (
                await session.execute(
                    select(OwnerTimer)
                    .where(OwnerTimer.status.in_([PENDING, FIRED]))
                    .order_by(OwnerTimer.created_at.desc())
                )
            ).scalars().all()
        )
        if text:
            match = pick_unique(
                text,
                listed,
                labels=lambda item: [str((item.payload or {}).get("text") or ""), str(item.id)],
            )
            if match.status == "ambiguous":
                names = candidate_names(
                    match.candidates,
                    name_of=lambda item: str((item.payload or {}).get("text") or item.id),
                )
                return {
                    "ok": False,
                    "error": "ambiguous",
                    "spoken": ambiguous_spoken("timer", names),
                }
            row = match.item if match.unique else None
        elif listed:
            row = listed[0]
    if row is None:
        return {
            "ok": False,
            "error": "not_found",
            "spoken": "I don't have a timer to snooze.",
        }
    body = str((row.payload or {}).get("text") or "Time's up.")
    if row.status == PENDING:
        fire_at = _aware(row.fire_at, now) + timedelta(minutes=delay)
        row.fire_at = fire_at
        payload = dict(row.payload or {})
        payload["snoozed_at"] = now.isoformat()
        row.payload = payload
        await session.flush()
        result = _timer_payload(
            row,
            now=now,
            spoken=f"Snoozed. I'll remind you at {fire_at.isoformat()}.",
        )
        await record_actuator(
            session, name="snooze_timer", actor=actor, key=str(row.id), result=result, target=body
        )
        return result
    if row.status == FIRED:
        return await start_timer(
            session,
            minutes=delay,
            text=body,
            now=now,
            actor=actor,
            idempotency_key=fingerprint("snooze", str(row.id), delay, now.replace(microsecond=0).isoformat()),
        )
    return {
        "ok": False,
        "error": "not_pending",
        "spoken": "That timer cannot be snoozed.",
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
        "evidence": evidence_base(source="voice_session", accepted=True, observed=True, now=now),
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
                    OwnerTimer.status == PENDING,
                    OwnerTimer.fire_at <= now,
                )
            )
        ).scalars().all()
    )
    fired = 0
    for row in rows:
        fire_at = _aware(row.fire_at, now)
        late = daemon_was_down or (now - fire_at) >= LATE_AFTER
        body = str((row.payload or {}).get("text") or "Time's up.")
        if late:
            body = f"{body} this is late."
        claimed = await session.execute(
            update(OwnerTimer)
            .where(OwnerTimer.id == row.id, OwnerTimer.status == PENDING)
            .values(status=FIRED, late=late, fired_at=now)
        )
        if int(claimed.rowcount or 0) != 1:
            continue
        row.status = FIRED
        row.late = late
        row.fired_at = now
        await emit_callout(
            session,
            body,
            source="15",
            source_item=str(row.id),
            hud={"schema_version": "ev.hud.card.v1", "title": "Timer", "body": body},
            owner_scheduled=True,
        )
        fired += 1
    await session.flush()
    return {"fired": fired, "scanned": len(rows)}


async def sweep_due_timers() -> dict:
    """Fire due owner timers in this process so live sockets can speak them."""

    async with _sweep_lock():
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await due_scan(session)
            await session.commit()
            return result


async def timer_watch_loop() -> None:
    """API-process timer fire loop. Does not require the runtime daemon worker."""

    while True:
        try:
            await sweep_due_timers()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - watch must not die on one tick
            LOGGER.info("timer watch skipped: %s", exc)
        await asyncio.sleep(WATCH_INTERVAL_S)
