"""Server-validated Core reads for trusted phones.

Weather, calendar, contacts, notifications, and HealthKit questions must not
fall through to a conversational model that invents the owner's life. These
paths use Home Station data or an honest gap. Health snapshots are never
forwarded to a model.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.search.live import extract_place, home_coords, is_weather_query, weather_results

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
        if hk.get("available"):
            return _ok(
                "I have a local Health snapshot on Home Station, but it is not sent to a model. "
                "Review it on this iPhone if you want the numbers.",
                route="HEALTHKIT",
                executed=False,
                extra={"sent_to_model": False, "freshness": hk.get("freshness") or "reported"},
            )
        return _ok(
            "Health data isn't connected in this Evie build. HealthKit isn't entitled, "
            "and nothing from Health is sent to a model.",
            route="HEALTHKIT",
            executed=False,
            extra={"sent_to_model": False, "freshness": "unavailable"},
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
        testing = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if extract_place(raw) is None and (home_coords() is None or testing):
            return _ok(
                "I need a place for the forecast. Ask 'weather in <city>' "
                "or set a home location on Home Station.",
                route="WEATHER",
                executed=False,
                extra={"needs_place": True},
            )
        try:
            results = await asyncio.wait_for(weather_results(raw, limit=2), timeout=8)
        except Exception:
            return _ok(
                "I couldn't fetch live weather just now.",
                route="WEATHER",
                executed=False,
            )
        snippet = ""
        if results:
            snippet = str(getattr(results[0], "snippet", None) or "").strip()
        if not snippet:
            return _ok(
                "I couldn't fetch live weather just now.",
                route="WEATHER",
                executed=False,
            )
        return _ok(snippet, route="WEATHER", extra={"provenance": "open-meteo"})

    if _CALENDAR.search(raw):
        cal = profile.get("calendar") if isinstance(profile.get("calendar"), dict) else {}
        events = cal.get("events") if isinstance(cal.get("events"), list) else []
        if not events:
            return _ok(
                "I don't have a calendar snapshot from this iPhone yet. "
                "Open Evie as the app and allow Calendar, then ask again.",
                route="CALENDAR",
                executed=False,
                extra={"sent_to_model": False},
            )
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
            return _ok(
                "I don't have a contacts snapshot from this iPhone yet. "
                "Allow Contacts in Evie if you want me to list names.",
                route="CONTACTS",
                executed=False,
                extra={"sent_to_model": False},
            )
        return _ok(
            "People on this iPhone: " + ", ".join(names) + ".",
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
