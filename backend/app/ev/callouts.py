"""Status callouts: persist always, speak only if may_speak_proactive allows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Callout
from app.notify.proactive import may_speak_proactive
from app.utils.text import utcnow


async def emit_callout(
    session: AsyncSession,
    text: str,
    *,
    source: str,
    hud: dict | None = None,
    source_item: str | None = None,
    emergency: bool = False,
    tts_available: bool = True,
    owner_scheduled: bool = False,
) -> Callout:
    """Write a replayable row; mark spoken only when policy and TTS allow.

    Owner-scheduled lines (timers the owner asked for) speak even during
    quiet hours. Unsolicited proactive speech still goes through the gate.
    """

    from app.voice.live.layer import OWNER_SCHEDULED_KEY

    hud_payload = dict(hud or {})
    if owner_scheduled:
        hud_payload[OWNER_SCHEDULED_KEY] = True
        may_speak = bool(tts_available)
    else:
        decision = await may_speak_proactive(
            session,
            emergency=emergency,
            fingerprint=f"callout:{source}:{source_item or text[:80]}",
        )
        may_speak = bool(decision.allowed and tts_available)
    row = Callout(
        text=text,
        source=source,
        source_item=source_item,
        hud=hud_payload,
        spoken=False,
        emergency=emergency,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    if may_speak:
        from app.voice.live.layer import speak_on_live, stamp_live_mail

        delivered = await speak_on_live(
            text,
            hud=hud_payload,
            emergency=emergency,
            db=session,
            persist_on_miss=False,
            bypass_quiet_hours=owner_scheduled,
        )
        if delivered:
            row.spoken = True
        else:
            stamp_live_mail(row)
    return row


async def list_callouts(session: AsyncSession, *, limit: int = 10) -> list[Callout]:
    result = await session.execute(
        select(Callout).order_by(Callout.created_at.desc()).limit(min(limit, 50))
    )
    return list(result.scalars().all())


def replay_text(rows: list[Callout]) -> str:
    if not rows:
        return "Nothing just happened that I logged."
    lines = [row.text for row in rows]
    if len(lines) == 1:
        return lines[0]
    return "Here's what just happened: " + " ".join(lines)


async def session_malfunction_callout(
    session: AsyncSession,
    *,
    session_key: str,
) -> Callout | None:
    """At most one red-calibrate self-status line per voice/text session."""

    from app.ev.assistant import last_calibration_report, malfunction_line

    existing = (
        await session.execute(
            select(Callout.id).where(
                Callout.source == "malfunction",
                Callout.source_item == session_key,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    report = await last_calibration_report(session)
    text = malfunction_line(report)
    if text is None:
        return None
    return await emit_callout(
        session,
        text,
        source="malfunction",
        source_item=session_key,
        hud={"schema_version": "ev.hud.card.v1", "title": "Diagnostics", "body": text},
    )
