"""Server-validated Core reads for trusted phones.

Weather, calendar, contacts, notifications, identity, clock, and HealthKit
questions must not fall through to a conversational model that invents the
owner's life. These paths use Home Station data or an honest gap. Health
snapshots are never forwarded to a model.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.search.live import default_place, extract_place, home_coords, is_weather_query, weather_results

_CALENDAR = re.compile(
    r"\b(what'?s on my (?:calendar|schedule|day)|upcoming events|"
    r"my calendar|calendar today|what do i have (?:today|tomorrow))\b",
    re.I,
)
_CONTACTS = re.compile(r"\b(my contacts|who'?s in (?:my )?contacts|list my contacts)\b", re.I)
_INBOX = re.compile(
    r"\b(any notifications|my notifications|notification inbox|"
    r"what(?:'s| is) in my inbox|did i miss anything)\b",
    re.I,
)
_HEALTH = re.compile(
    r"\b(healthkit|how many steps|my (?:heart rate|sleep)|sleep last night)\b",
    re.I,
)
_HISTORY = re.compile(
    r"\b(what did we (?:talk|say|discuss)|do you remember (?:when|what i)|"
    r"what did i (?:tell|ask) you|last time we talked)\b",
    re.I,
)
_IDENTITY = re.compile(
    r"\b("
    r"what(?:'s| is) my name|"
    r"who am i(?!\s+becoming)|"
    r"do you know (?:my name|who i am)|"
    r"what do you call me|"
    r"who(?:'s| is) your owner"
    r")\b",
    re.I,
)
_CAPABILITIES = re.compile(
    r"\b(what can you do|what are you able to do|what can you help (?:me )?with)\b",
    re.I,
)
_PLACEHOLDER_NAMES = frozenset({"owner", "evie", "ev", "e.v.", "e v"})


def _profile(device: Device) -> dict[str, Any]:
    raw = getattr(device, "endpoint_profile", None) or {}
    return raw if isinstance(raw, dict) else {}


def _ok(reply: str, *, route: str, executed: bool = True, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reply": reply,
        "ok": True,
        "route": route,
        "operation": route.lower(),
        "turn_id": None,
        "executed": executed,
        "verified": executed,
        "conversational": False,
    }
    if extra:
        payload.update(extra)
    return payload


async def _owner_spoken_name(session: AsyncSession) -> str | None:
    from app.ev.assistant import get_profile
    from app.identity.service import get_owner

    profile = await get_profile(session)
    name = str(getattr(profile, "owner_preferred_name", None) or "").strip()
    if not name:
        owner = await get_owner(session)
        if owner is not None:
            name = str(owner.display_name or "").strip()
    if not name or name.lower() in _PLACEHOLDER_NAMES:
        return None
    return name


def _metric_bits(metrics: dict[str, Any]) -> list[str]:
    bits: list[str] = []
    blob = metrics if isinstance(metrics, dict) else {}
    steps = blob.get("steps")
    if isinstance(steps, (int, float)):
        bits.append(f"{int(steps)} steps")
    sleep = blob.get("sleep_hours")
    if isinstance(sleep, (int, float)):
        hours = f"{sleep:g}"
        bits.append(f"{hours} hours of sleep")
    hr = blob.get("heart_rate") or blob.get("resting_hr")
    if isinstance(hr, (int, float)):
        bits.append(f"heart rate {int(hr)}")
    return bits


def _speak_health(metrics: dict[str, Any], *, source: str) -> str:
    bits = _metric_bits(metrics)
    if not bits:
        return (
            "I have a local Health snapshot on Home Station, but it is not sent to a model. "
            "Review it on this iPhone if you want the numbers."
        )
    return (
        f"On {source}: {', '.join(bits)}. "
        "Those numbers stay on Home Station and are not sent to a model."
    )


async def _latest_series_metrics(session: AsyncSession) -> dict[str, Any] | None:
    from app.models import HealthSnapshot

    row = (
        await session.execute(select(HealthSnapshot).order_by(HealthSnapshot.occurred_at.desc()).limit(1))
    ).scalars().first()
    if row is None:
        return None
    metrics = dict(row.metrics or {})
    keep = {k: v for k, v in metrics.items() if isinstance(v, (int, float))}
    if row.readiness is not None:
        keep["readiness"] = row.readiness
    return keep or None


async def maybe_phone_core_read(
    session: AsyncSession,
    *,
    device: Device,
    text: str,
) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    profile = _profile(device)

    if _HEALTH.search(raw):
        hk = profile.get("healthkit") if isinstance(profile.get("healthkit"), dict) else {}
        from .sandbox import is_sandbox_device as _sandbox_device

        series = None
        if not _sandbox_device(device):
            series = await _latest_series_metrics(session)
        has_numbers = bool(_metric_bits(hk.get("snapshot") if isinstance(hk.get("snapshot"), dict) else {}) or _metric_bits(series or {}))
        if hk.get("available") or has_numbers:
            return _ok(
                "I have Health numbers on this iPhone's Health sheet. "
                "They stay on Home Station and are not sent to a model.",
                route="HEALTHKIT",
                executed=False,
                extra={"sent_to_model": False, "freshness": hk.get("freshness") or ("home_station_series" if series else "reported")},
            )
        return _ok(
            "Health data isn't connected in this Evie build. HealthKit isn't entitled, "
            "and nothing from Health is sent to a model.",
            route="HEALTHKIT",
            executed=False,
            extra={"sent_to_model": False, "freshness": "unavailable"},
        )

    if _IDENTITY.search(raw):
        name = await _owner_spoken_name(session)
        if name:
            return _ok(f"Your name is {name}.", route="IDENTITY", extra={"provenance": "assistant.profile"})
        return _ok(
            "I don't have your preferred name saved on Home Station yet.",
            route="IDENTITY",
            executed=False,
        )

    from app.ev.tool_select import TIME_RE

    if TIME_RE.search(raw) and not _CALENDAR.search(raw):
        from app.ev.resolve import spoken_clock

        return _ok(spoken_clock(raw), route="CLOCK", extra={"provenance": "owner.clock"})

    if _CAPABILITIES.search(raw):
        return _ok(
            "On this iPhone I can talk with you, look through the camera, "
            "tell you the date and time, your name if it's saved, weather, "
            "inbox, and what I remember. Timers and reminders run on Home Station "
            "and can ping this iPhone with an Evie alert — not Clock or Reminders.app. "
            "Call opens Phone when Home Station has a number. "
            "Health numbers stay on Home Station and are never sent to a model.",
            route="CAPABILITIES",
        )

    from app.memory.visual import is_visual_recall_query

    if _HISTORY.search(raw) and not is_visual_recall_query(raw) and not is_weather_query(raw):
        from app.memory.history import recall_history

        recalled = await recall_history(session, raw, k=3)
        spoken = str(recalled.get("spoken") or "").strip() or "I don't have that in memory yet."
        return _ok(
            spoken,
            route="MEMORY",
            executed=bool(recalled.get("count")),
            extra={"provenance": "memory.history", "count": recalled.get("count") or 0},
        )

    if is_weather_query(raw):
        requested_place = extract_place(raw)
        home_location = default_place()
        has_place = requested_place is not None
        has_home = home_coords() is not None or bool(home_location)
        if not has_place and not has_home:
            return _ok(
                "I need a place for the forecast. Ask 'weather in <city>' "
                "or set a home location on Home Station.",
                route="WEATHER",
                executed=False,
                extra={"needs_place": True},
            )
        try:
            results = await asyncio.wait_for(weather_results(raw, limit=2), timeout=8)
        except asyncio.TimeoutError:
            return _ok(
                "Home Station weather lookup timed out. I won't guess the forecast.",
                route="WEATHER",
                executed=False,
                extra={"error_code": "WEATHER_TIMEOUT", "retryable": True},
            )
        except Exception:
            return _ok(
                "I couldn't fetch live weather from Home Station just now.",
                route="WEATHER",
                executed=False,
                extra={"error_code": "WEATHER_UNAVAILABLE", "retryable": True},
            )
        snippet = ""
        if results:
            snippet = str(getattr(results[0], "snippet", None) or "").strip()
        if not snippet:
            return _ok(
                "I couldn't fetch live weather from Home Station just now.",
                route="WEATHER",
                executed=False,
                extra={"error_code": "WEATHER_EMPTY", "retryable": True},
            )
        lowered = snippet.lower()
        if "weather location needed" in lowered or "no coarse place is configured" in lowered:
            return _ok(
                "I need a Home Station location for the forecast. Ask for weather in a city "
                "or set the Home Station location.",
                route="WEATHER",
                executed=False,
                extra={"error_code": "WEATHER_LOCATION_REQUIRED", "needs_place": True},
            )
        return _ok(
            snippet,
            route="WEATHER",
            extra={
                "provenance": "open-meteo",
                "location": requested_place or home_location or "Home Station",
                "location_source": (
                    "query"
                    if requested_place
                    else ("home_station_coordinates" if home_coords() is not None else "home_station_place")
                ),
            },
        )

    if _CALENDAR.search(raw):
        cal = profile.get("calendar") if isinstance(profile.get("calendar"), dict) else {}
        events = cal.get("events") if isinstance(cal.get("events"), list) else []
        if not events:
            return None
        lines = []
        for item in events[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Event").strip()
            start = str(item.get("start") or "").strip()
            lines.append(f"{title} at {start}" if start else title)
        spoken = "Upcoming: " + "; ".join(lines) if lines else "No upcoming events in the snapshot."
        return _ok(spoken, route="CALENDAR", extra={"sent_to_model": False})

    if _CONTACTS.search(raw):
        book = profile.get("contacts") if isinstance(profile.get("contacts"), dict) else {}
        people = book.get("contacts") if isinstance(book.get("contacts"), list) else []
        names = []
        for item in people[:12]:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        if not names:
            try:
                from .phone_people import list_phone_people
                from .sandbox import is_sandbox_device

                if not is_sandbox_device(device):
                    harvested = await list_phone_people(session, limit=12)
                    names = [row["name"] for row in harvested if row.get("name")]
            except Exception:
                names = []
        if not names:
            return _ok(
                "I don't have people on Home Station yet, and Safari Evie can't read "
                "the iPhone address book.",
                route="CONTACTS",
                executed=False,
                extra={"sent_to_model": False},
            )
        return _ok(
            "People I know: " + ", ".join(names) + ".",
            route="CONTACTS",
            extra={"sent_to_model": False},
        )

    if _INBOX.search(raw):
        from app.everywhere.inbox import list_inbox

        items = await list_inbox(session, device_id=device.id, limit=8)
        if not items:
            return _ok("No notifications waiting on this phone.", route="INBOX")
        titles = [str(item.get("title") or item.get("kind") or "notice") for item in items[:5]]
        return _ok(
            "Inbox: " + "; ".join(titles) + ".",
            route="INBOX",
            extra={"delivery": "in_app_poll"},
        )

    return None
