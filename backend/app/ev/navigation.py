"""Route briefings: E.V.'s 'street navigation' adapted to the user's next commitment."""

from __future__ import annotations

from datetime import timedelta

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import calendar as calendar_feed
from app.ev.user_state import build_user_state
from app.models import WatchlistItem
from app.schemas import RouteBriefingOut
from app.utils.text import utcnow


async def route_briefing(session: AsyncSession) -> RouteBriefingOut:
    # Real calendar first: the next commitment from an active calendar
    # integration (Agent 12) is the ground truth for "where am I headed".
    cal = await calendar_feed.calendar_signals(session)
    next_event = cal.get("next_event")
    destination = None
    leave_by = None
    travel_time = None
    notes: list[str] = []

    if next_event:
        from app.ev.travel import leave_by_iso, maps_eta, owner_coarse_origin, weather_note

        destination = next_event.get("location") or next_event.get("summary")
        origin = await owner_coarse_origin(session)
        eta = maps_eta(
            origin=origin,
            destination=destination,
            provider="local",
        )
        travel_time = int(eta["minutes"])
        start = next_event.get("start")
        computed_leave = leave_by_iso(start, travel_time)
        leave_by = computed_leave or cal.get("leave_by")
        notes.append(
            f"Next commitment from live calendar: {next_event.get('summary')} "
            f"at {next_event.get('start')}."
        )
        if eta.get("estimate"):
            notes.append(eta.get("honesty") or "Travel time is an estimate.")
        if origin:
            notes.append(f"Origin: {origin}.")
        weather = await weather_note(destination)
        if weather:
            notes.append(f"Weather: {weather}.")
            if "rain" in weather and eta.get("estimate"):
                travel_time = int(travel_time) + 5
                recomputed = leave_by_iso(start, travel_time)
                if recomputed:
                    leave_by = recomputed
        if next_event.get("participants"):
            names = ", ".join(
                participant.get("name") or participant.get("email")
                for participant in next_event["participants"][:3]
            )
            notes.append(f"Participants: {names}.")
        source_event_ids = (cal.get("source") or {}).get("event_ids") or []
        if source_event_ids:
            notes.append(
                "Provenance: " + ", ".join(source_event_ids[:5]) + "."
            )

    # Fallback to an explicit deadline watch item when the calendar is empty.
    rows = (
        await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.active.is_(True),
                WatchlistItem.kind == "deadline",
            )
        )
    ).scalars().all()
    state = await build_user_state(session)

    if destination is None:
        for item in rows:
            metadata = item.metadata_ or {}
            destination = metadata.get("location") or item.value
            raw_date = metadata.get("date")
            if raw_date:
                try:
                    when = date_parser.parse(str(raw_date))
                except (ValueError, TypeError, OverflowError):
                    when = None
                if when is not None:
                    travel_time = int(metadata.get("travel_minutes", 30))
                    leave_by = (when - timedelta(minutes=travel_time)).isoformat()
            if metadata.get("prep"):
                notes.append(str(metadata["prep"]))
            break

    prep_checklist: list[str] = []
    if destination:
        prep_checklist.append(f"Confirm what you need for {destination} before leaving.")
    if state.active_goal:
        prep_checklist.append(f"Keep {state.active_goal} in mind while you are out.")
    if state.open_decisions:
        prep_checklist.append("Carry one open decision and settle it before the day ends.")
    if not prep_checklist:
        prep_checklist.append("No active destination — set a deadline watch item with a location and date.")
    location_lines = [line for line in state.live_context if "] location " in line]
    if location_lines:
        notes.append(f"Live context: {location_lines[0]}")

    if destination and not any(note.lower().startswith("weather") for note in notes):
        from app.ev.travel import weather_note

        overlay = await weather_note(destination)
        if overlay:
            notes.append(f"Weather: {overlay}.")

    return RouteBriefingOut(
        schema_version="ev.hud.route.v1",
        generated_at=utcnow(),
        destination=destination,
        leave_by=leave_by,
        travel_time_minutes=travel_time,
        prep_checklist=prep_checklist,
        notes=notes,
    )
