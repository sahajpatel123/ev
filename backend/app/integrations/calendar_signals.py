"""Calendar signal derivation: density, not raw dumps.

The intelligence modules (EV Sense, alert radar, morning brief, tactical
briefing) need compact answers: what is next, when do I leave, how dense is the
day, how close is the deadline, are quiet hours in effect, and who am I
meeting. This module derives exactly those signals from normalized calendar
event payloads (either fresh provider events or stored live-event payloads).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as date_parser

from app.config import settings
from app.ev.ev_sense import quiet_hours_active
from app.utils.text import utcnow

PREP_MINUTES = 30  # default lead time before an event start (estimated)
PROXIMITY_WINDOW_HOURS = 48.0  # deadline proximity decays over this horizon

def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def parse_event_time(value: Any) -> datetime | None:
    """Parse an RFC3339/ISO event timestamp; date-only values become midnight UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return _aware(parsed)


def _participant_name(attendee: dict) -> str:
    name = str(attendee.get("name") or "").strip()
    email = str(attendee.get("email") or "").strip()
    if name and name != email:
        return name
    if email:
        return email.split("@", 1)[0].replace(".", " ").strip() or email
    return "Unknown participant"


def _event_bounds(payload: dict) -> tuple[datetime | None, datetime | None]:
    start = parse_event_time(payload.get("start"))
    end = parse_event_time(payload.get("end"))
    if start is not None and end is None:
        end = start + timedelta(hours=1)
    return start, end


def derive_calendar_signals(events: list[dict], now: datetime | None = None) -> dict:
    """Derive signals from normalized calendar event payloads.

    ``events`` are provider-normalized payloads (as stored in live events or
    returned by :meth:`CalendarAdapter.sync`). The result is a compact JSON
    object with next event, leave-by, day density, deadline proximity, quiet
    hours truth, and participants.
    """
    now = _aware(now or utcnow())
    today = now.date()
    horizon = today + timedelta(days=settings.calendar_sync_days)

    dated: list[tuple[datetime, datetime, dict]] = []
    for payload in events:
        start, end = _event_bounds(payload)
        if start is None:
            continue
        end = end or (start + timedelta(hours=1))
        if end < now:  # already finished
            continue
        if start.date() > horizon:
            continue
        if not bool(payload.get("busy", True)):  # free/transparent blocks don't count
            continue
        dated.append((start, end, payload))
    dated.sort(key=lambda item: (item[0], item[1]))

    next_event: dict | None = None
    leave_by: str | None = None
    deadline_proximity = 0.0
    if dated:
        start, end, payload = dated[0]
        attendees = payload.get("attendees") or []
        participants = [
            {
                "name": _participant_name(attendee),
                "email": str(attendee.get("email") or "").strip() or None,
                "status": str(attendee.get("status") or "accepted"),
            }
            for attendee in attendees
            if isinstance(attendee, dict)
        ]
        next_event = {
            "summary": str(payload.get("summary") or "Untitled event"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "location": payload.get("location") or None,
            "all_day": bool(payload.get("all_day")),
            "busy": True,
            "participants": participants,
            "hangout_link": payload.get("hangout_link") or None,
            "html_link": payload.get("html_link") or None,
        }
        leave_by = (
            end.isoformat()
            if start <= now
            else (start - timedelta(minutes=PREP_MINUTES)).isoformat()
        )
        hours = max(0.0, (start - now).total_seconds() / 3600.0)
        deadline_proximity = round(min(1.0, max(0.0, 1.0 - hours / PROXIMITY_WINDOW_HOURS)), 3)

    by_day: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
    participant_counter: Counter[str] = Counter()
    participant_meta: dict[str, dict[str, Any]] = {}
    for start, end, payload in dated:
        by_day[start.date()].append((start, end))
        for attendee in payload.get("attendees") or []:
            if not isinstance(attendee, dict):
                continue
            email = str(attendee.get("email") or "").strip()
            name = _participant_name(attendee)
            key = email or name
            participant_counter[key] += 1
            meta = participant_meta.get(key)
            if meta is None:
                meta = {"name": name, "email": email or None}
                participant_meta[key] = meta

    day_density: list[dict] = []
    for offset in range(settings.calendar_sync_days + 1):
        day = today + timedelta(days=offset)
        spans = by_day.get(day, [])
        busy_minutes = 0
        for start, end in spans:
            busy_minutes += int(max(0, (end - start).total_seconds()) // 60)
        day_density.append(
            {
                "date": day.isoformat(),
                "event_count": len(spans),
                "busy_minutes": busy_minutes,
            }
        )

    participant_summary: list[dict[str, Any]] = []
    for key, count in participant_counter.most_common():
        meta = participant_meta[key]
        entry: dict[str, Any] = {
            "name": meta["name"],
            "email": meta.get("email"),
            "events": count,
        }
        participant_summary.append(entry)

    return {
        "generated_at": now.isoformat(),
        "next_event": next_event,
        "leave_by": leave_by,
        "today": day_density[0] if day_density else None,
        "day_density": day_density,
        "deadline_proximity": deadline_proximity,
        "quiet_hours": {
            "active": quiet_hours_active(now),
            "start": settings.quiet_hours_start,
            "end": settings.quiet_hours_end,
        },
        "participants": participant_summary,
    }
