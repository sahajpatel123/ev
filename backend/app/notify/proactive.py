"""Single gate for proactive speech and push: decide() + quiet hours, fail closed."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.notify.policy import PolicyDecision, decide
from app.utils.text import utcnow

_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


def timezone_available() -> bool:
    tz = (settings.timezone or "").strip()
    if not tz:
        return False
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def parse_clock(value: str) -> str:
    raw = (value or "").strip().lower().replace(".", "")
    raw = raw.replace("am", "").replace("pm", "").strip()
    match = _CLOCK_RE.match(raw)
    if not match:
        raise ValueError(f"Invalid clock value: {value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour == 24:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid clock value: {value!r}")
    # Bare "8" after noon usually means 08:00 next morning.
    return f"{hour:02d}:{minute:02d}"


async def may_speak_proactive(
    session: AsyncSession,
    *,
    emergency: bool = False,
    now: datetime | None = None,
    fingerprint: str | None = None,
) -> PolicyDecision:
    """Whether EV may speak or push a proactive line.

    Missing/invalid clock TZ fails closed (treat as quiet) except emergencies.
    """

    now = now or utcnow()
    if not timezone_available():
        if emergency:
            return PolicyDecision(True, note="emergency_missing_timezone")
        return PolicyDecision(False, "missing_timezone")
    from app.ev.ev_sense import quiet_hours_active

    if quiet_hours_active(now) and not emergency:
        return PolicyDecision(False, "quiet_hours")
    return await decide(
        session,
        fingerprint=fingerprint or f"proactive:{now.isoformat()}",
        exclude_id=None,
        priority=1.0 if emergency else 0.3,
        tier="urgent" if emergency else "notify",
        emergency=emergency,
        allow_during_quiet_hours=emergency,
        bypass_policy=False,
        now=now,
    )


def set_quiet_hours(
    *,
    until: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Update in-process quiet hours immediately."""

    if until:
        settings.quiet_hours_start = utcnow().strftime("%H:%M")
        settings.quiet_hours_end = parse_clock(until)
    elif start and end:
        settings.quiet_hours_start = parse_clock(start)
        settings.quiet_hours_end = parse_clock(end)
    elif start:
        settings.quiet_hours_start = parse_clock(start)
    elif end:
        settings.quiet_hours_end = parse_clock(end)
    else:
        raise ValueError("set_quiet_hours requires until, or start/end")
    return {
        "start": settings.quiet_hours_start,
        "end": settings.quiet_hours_end,
    }


def apply_persisted_quiet_hours(
    start: str | None,
    end: str | None,
) -> None:
    """Copy stored quiet-hours prefs back into the in-process settings."""

    if start:
        settings.quiet_hours_start = start
    if end:
        settings.quiet_hours_end = end


async def persist_quiet_hours(session: AsyncSession) -> None:
    """Write current in-process hours to the profile row. Does not reload them."""

    from app.models import AssistantProfile

    profile = (
        await session.execute(
            select(AssistantProfile).order_by(AssistantProfile.created_at.asc()).limit(1)
        )
    ).scalars().first()
    if profile is None:
        from app.ev.assistant import get_profile

        profile = await get_profile(session)
    profile.quiet_hours_start = settings.quiet_hours_start
    profile.quiet_hours_end = settings.quiet_hours_end
    profile.updated_at = utcnow()
    await session.flush()
    from app.ev.training_wheels import mark_step_from_event

    await mark_step_from_event(session, "quiet_hours")


async def restore_quiet_hours(session: AsyncSession) -> dict | None:
    """Load persisted hours after restart. No-op when none have been set."""

    from app.models import AssistantProfile

    row = (
        await session.execute(
            select(AssistantProfile).order_by(AssistantProfile.created_at.asc()).limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    apply_persisted_quiet_hours(row.quiet_hours_start, row.quiet_hours_end)
    if not row.quiet_hours_start and not row.quiet_hours_end:
        return None
    return {
        "start": settings.quiet_hours_start,
        "end": settings.quiet_hours_end,
    }
