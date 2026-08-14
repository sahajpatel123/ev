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
) -> Callout:
    """Write a replayable row; mark spoken only when policy and TTS allow."""

    decision = await may_speak_proactive(
        session,
        emergency=emergency,
        fingerprint=f"callout:{source}:{source_item or text[:80]}",
    )
    spoken = bool(decision.allowed and tts_available)
    row = Callout(
        text=text,
        source=source,
        source_item=source_item,
        hud=hud or {},
        spoken=spoken,
        emergency=emergency,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
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
