"""On-demand locator for live (non-Takeout) life events.

The Mac is the continuity hub. iCloud, SMS forwarding, and call forwarding
already copy owner life onto this machine. The follower records those local
copies (iMessage/SMS, WhatsApp Desktop, call history, Contacts, Mail,
Calendar, Health, Photos filenames). They are never injected into casual
turns. This module opens one shelf only when recall already chose that drawer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event
from app.utils.text import utcnow

LIVE_SOURCES = frozenset(
    {"imessage", "whatsapp", "calls", "mail", "contacts", "calendar", "health", "photos"}
)

LIVE_SHELVES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "chats": (
        ("imessage", "whatsapp"),
        (
            "message.imessage.received",
            "message.imessage.sent",
            "message.whatsapp.received",
            "message.whatsapp.sent",
        ),
    ),
    "mail": (("mail",), ("mail.envelope.received",)),
    "contacts": (("contacts",), ("contact.discovered", "contact.updated")),
    "calendar": (("calendar",), ("calendar.event.recorded",)),
    "health": (("health",), ("health.snapshot.recorded",)),
    "calls": (("calls",), ("call.history.recorded",)),
    "inbox": (
        ("imessage", "whatsapp", "calls", "mail"),
        (
            "message.imessage.received",
            "message.imessage.sent",
            "message.whatsapp.received",
            "message.whatsapp.sent",
            "call.history.recorded",
            "mail.envelope.received",
        ),
    ),
    "photos": (("photos",), ("photo.library.indexed",)),
    "people": (
        ("imessage", "whatsapp", "contacts"),
        (
            "message.imessage.received",
            "message.imessage.sent",
            "message.whatsapp.received",
            "message.whatsapp.sent",
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
    """Model-facing envelope text. No file paths, no raw mail bodies, no photo pixels."""
    content = event.content if isinstance(event.content, dict) else {}
    source = str(event.source or "")
    if source in {"imessage", "whatsapp"}:
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
    if source == "calls":
        return str(content.get("text") or "").strip()
    if source == "photos":
        return str(content.get("filename") or content.get("text") or "").strip()
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
    from app.memory.life_archive.locate import life_channel

    channel = life_channel(query)
    if shelf == "chats":
        if channel == "whatsapp":
            sources = ("whatsapp",)
            types = ("message.whatsapp.received", "message.whatsapp.sent")
        elif channel == "imessage":
            sources = ("imessage",)
            types = ("message.imessage.received", "message.imessage.sent")
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


def peek_mac_life(
    query: str,
    *,
    shelf: str,
    tokens: list[str] | None = None,
    k: int = 8,
    daemon: Any | None = None,
) -> list[dict[str, Any]]:
    """Read WhatsApp / iMessage / calls / photos / mail / contacts from this Mac.

    Apps stay closed. iMessage is ``chat.db``, WhatsApp is Desktop sqlite, mail
    is the Envelope Index, contacts are ``CNContactStore``. No Event writes.
    """
    if shelf not in {"chats", "calls", "photos", "inbox", "mail", "contacts"}:
        return []
    from app.services.life_stream_daemon import get_life_stream_daemon, life_stream_should_run

    if daemon is None:
        if not life_stream_should_run():
            return []
        try:
            daemon = get_life_stream_daemon()
        except Exception:
            return []
    distinctive = [token for token in (tokens or []) if token]
    limit = max(1, min(int(k or 8), 8))
    try:
        if shelf == "chats":
            from app.memory.life_archive.locate import life_channel

            channel = life_channel(query)
            if channel == "whatsapp":
                return daemon.peek_whatsapp(tokens=distinctive, limit=limit)
            if channel == "imessage":
                return daemon.peek_imessage(tokens=distinctive, limit=limit)
            hits = daemon.peek_whatsapp(tokens=distinctive, limit=limit)
            hits.extend(daemon.peek_imessage(tokens=distinctive, limit=limit))
            hits.sort(key=lambda item: str(item.get("when") or ""), reverse=True)
            return hits[:limit]
        if shelf == "calls":
            return daemon.peek_calls(tokens=distinctive, limit=limit)
        if shelf == "photos":
            return daemon.peek_photos(tokens=distinctive, limit=limit)
        if shelf == "mail":
            return daemon.peek_mail(tokens=distinctive, limit=limit, query=query)
        if shelf == "contacts":
            cached = list(getattr(daemon, "_cached_contacts", []) or [])
            return daemon.peek_contacts(cached, tokens=distinctive, limit=limit)
        if shelf == "inbox":
            # Keep one noisy WhatsApp thread from hiding calls, iMessage, or mail.
            per = max(2, (limit + 3) // 4)
            buckets = [
                daemon.peek_whatsapp(tokens=distinctive, limit=per),
                daemon.peek_imessage(tokens=distinctive, limit=per),
                daemon.peek_calls(tokens=distinctive, limit=per),
                daemon.peek_mail(tokens=distinctive, limit=per, query=query),
            ]
            mixed: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index in range(per):
                for bucket in buckets:
                    if index >= len(bucket):
                        continue
                    item = bucket[index]
                    key = str(item.get("id") or "") or str(item.get("text") or "")[:80]
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    mixed.append(item)
                    if len(mixed) >= limit:
                        return mixed
            mixed.sort(key=lambda item: str(item.get("when") or ""), reverse=True)
            return mixed[:limit]
        return []
    except Exception:
        return []


async def _helper_account_rows(
    command: str,
    key: str,
    *,
    args: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from app.config import settings
    from app.integrations.life_helper import run_life_helper

    helper = (getattr(settings, "life_helper_path", "") or "").strip()
    if not helper:
        return []
    try:
        result = await run_life_helper(command, args or {}, helper_path=helper, timeout=8.0)
    except Exception:
        return []
    raw = (result.data or {}).get(key) if result.data else None
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


async def peek_account_life(
    query: str,
    *,
    shelf: str,
    tokens: list[str] | None = None,
    k: int = 8,
    contacts: list[dict[str, Any]] | None = None,
    mail: list[dict[str, Any]] | None = None,
    daemon: Any | None = None,
) -> list[dict[str, Any]]:
    """Ask-time Contacts (helper) and Mail fallback. No Event writes.

    Apple Contacts have no safe sqlite peek; EVLifeHelper reads CNContactStore.
    Mail prefers the Envelope Index sqlite via peek_mac_life; the helper is
    only a fallback when that file is missing.
    """
    if shelf not in {"contacts", "mail"}:
        return []
    from app.services.life_stream_daemon import get_life_stream_daemon, life_stream_should_run

    injected = contacts is not None or mail is not None
    if not injected and not life_stream_should_run():
        return []
    if daemon is None:
        try:
            daemon = get_life_stream_daemon()
        except Exception:
            return []
    distinctive = [token for token in (tokens or []) if token]
    limit = max(1, min(int(k or 8), 8))
    if shelf == "contacts":
        rows = contacts
        if rows is None:
            if distinctive:
                rows = await _helper_account_rows(
                    "contacts.resolve", "matches", args={"query": distinctive[0]}
                )
            if not rows:
                rows = await _helper_account_rows("contacts.list", "contacts")
            if not rows:
                rows = list(getattr(daemon, "_cached_contacts", []) or [])
        return daemon.peek_contacts(rows, tokens=distinctive, limit=limit)
    rows = mail
    if rows is None:
        sqlite_hits = daemon.peek_mail(tokens=distinctive, limit=limit, query=query)
        if sqlite_hits:
            return sqlite_hits
        rows = await _helper_account_rows("mail.list", "messages", args={"limit": limit})
        if not rows:
            return []
    return daemon.peek_mail(rows, tokens=distinctive, limit=limit, query=query)
