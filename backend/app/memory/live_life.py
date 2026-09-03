"""On-demand locator for live (non-Takeout) life events.

iMessage, Contacts, Mail, Calendar, and Health envelopes are recorded
continuously by the headless follower and snapshot ingest. They are never
injected into casual turns. This module opens one shelf only when recall
already chose that drawer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event
from app.utils.text import utcnow

LIVE_SOURCES = frozenset({"imessage", "mail", "contacts", "calendar", "health"})

LIVE_SHELVES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "chats": (
        ("imessage",),
        ("message.imessage.received", "message.imessage.sent"),
    ),
    "mail": (("mail",), ("mail.envelope.received",)),
    "contacts": (("contacts",), ("contact.discovered", "contact.updated")),
    "calendar": (("calendar",), ("calendar.event.recorded",)),
    "health": (("health",), ("health.snapshot.recorded",)),
    "people": (
        ("imessage", "contacts"),
        (
            "message.imessage.received",
            "message.imessage.sent",
            "contact.discovered",
            "contact.updated",
        ),
    ),
}


def is_live_life_event(event: Any) -> bool:
    """True for follower envelopes. They stay events; they are not general memories."""
    return str(getattr(event, "source", "") or "") in LIVE_SOURCES


SCAN_CAP = 48


def live_event_text(event: Event) -> str:
    """Model-facing envelope text. No file paths, no raw mail bodies."""
    content = event.content if isinstance(event.content, dict) else {}
    source = str(event.source or "")
    if source == "imessage":
        who = "You" if content.get("is_from_me") else str(content.get("handle") or "someone")
        body = str(content.get("text") or "").strip()
        return f"{who}: {body}".strip(": ")
    if source == "contacts":
        return str(content.get("name") or content.get("full_name") or "").strip()
    if source == "mail":
        text = str(content.get("text") or "").strip()
        if text:
            return text
        subject = str(content.get("subject") or "").strip()
        sender = str(content.get("sender") or "").strip()
        if subject and sender:
            return f"{subject} from {sender}"
        return subject or sender
    if source == "calendar":
        summary = str(content.get("summary") or content.get("text") or "").strip()
        start = str(content.get("start") or "").strip()
        location = str(content.get("location") or "").strip()
        if summary and start:
            line = f"{summary} at {start}"
            return f"{line} ({location})" if location else line
        return summary or start
    if source == "health":
        return str(content.get("text") or "").strip()
    return str(content.get("text") or "").strip()


def _score(tokens: list[str], text: str) -> float:
    if not tokens:
        return 0.15
    blob = text.lower()
    hits = sum(1 for token in tokens if token and token in blob)
    if hits == 0:
        return 0.0
    return hits / len(tokens)


async def locate_live_life(
    session: AsyncSession,
    query: str,
    *,
    shelf: str,
    tokens: list[str] | None = None,
    k: int = 8,
) -> list[dict[str, Any]]:
    """Return a tiny live-life pack for one shelf. Empty when none exist."""
    spec = LIVE_SHELVES.get(shelf)
    if spec is None:
        return []
    sources, types = spec
    distinctive = [token for token in (tokens or []) if token]
    limit = max(1, min(int(k or 8), 8))
    stmt = (
        select(Event)
        .where(
            Event.source.in_(sources),
            Event.event_type.in_(types),
            Event.tombstoned_at.is_(None),
            Event.privacy_level != "never_send_to_model",
        )
        .order_by(Event.occurred_at.desc())
        .limit(SCAN_CAP if distinctive else limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    scored: list[tuple[float, Event, str]] = []
    for event in rows:
        text = live_event_text(event)
        if not text:
            continue
        score = _score(distinctive, text)
        if distinctive and score <= 0:
            continue
        scored.append((score, event, text))
    if distinctive and not scored:
        return []
    if distinctive:
        scored.sort(
            key=lambda item: (item[0], (item[1].occurred_at or utcnow()).timestamp()),
            reverse=True,
        )
    else:
        scored.sort(key=lambda item: item[1].occurred_at or utcnow(), reverse=True)
    hits: list[dict[str, Any]] = []
    for score, event, text in scored[:limit]:
        hits.append(
            {
                "id": str(event.id),
                "source": str(event.source),
                "when": event.occurred_at.isoformat() if event.occurred_at else None,
                "text": text[:400],
                "kind": "live_life",
                "memory_type": event.event_type,
                "confidence": "live_locator",
                "score": round(max(score, 0.15), 4),
                "provenance": [str(event.id)],
                "shelf": shelf,
            }
        )
    return hits


def merge_life_hits(
    live: list[dict[str, Any]],
    archive: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Live envelopes first (they are current), then Takeout shelves."""
    cap = max(1, min(int(limit or 8), 8))
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in [*live, *archive]:
        key = str(item.get("id") or "") or str(item.get("text") or "")[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= cap:
            break
    return merged
