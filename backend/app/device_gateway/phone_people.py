"""People the iPhone PWA can actually list.

Safari Evie cannot read the iPhone address book. Names come from Home Station:
Entity roster, Apple Contacts sync events, WhatsApp/iMessage/mail handles, and
life.person memories. Phone numbers never appear in the People JSON; Call
resolves a number only at action time and hands `tel:` to this iPhone.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, Event, Memory

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONEISH = re.compile(r"^\+?\d[\d\s\-().]{6,}$")
_ANGLE = re.compile(r"^(?P<name>.+?)\s*<[^>]+>$")
_DIGITS = re.compile(r"\d+")
_SKIP = frozenset(
    {
        "unknown",
        "someone",
        "you",
        "me",
        "status",
        "broadcast",
        "whatsapp",
        "group",
        "owner",
        "evie",
    }
)
_EVENT_TYPES = (
    "contact.discovered",
    "contact.updated",
    "message.whatsapp.received",
    "message.whatsapp.sent",
    "message.imessage.received",
    "message.imessage.sent",
    "mail.envelope.received",
    "life.person",
    "life.chat.thread",
)


def _digits(value: str) -> str:
    return "".join(_DIGITS.findall(value or ""))


def looks_like_phone(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    if _PHONEISH.match(raw):
        return True
    digits = _digits(raw)
    return 8 <= len(digits) <= 15 and len(digits) >= int(len(re.sub(r"[\s\-().+]", "", raw)) * 0.7)


def looks_like_email(value: str) -> bool:
    raw = (value or "").strip()
    if "<" in raw and ">" in raw:
        inner = raw[raw.find("<") + 1 : raw.find(">")]
        return bool(_EMAIL.match(inner.strip()))
    return bool(_EMAIL.match(raw))


def display_name(raw: str) -> str | None:
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    angled = _ANGLE.match(text)
    if angled:
        text = angled.group("name").strip().strip("\"'")
    if "@" in text and "<" not in text:
        local = text.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
        text = local.title() if local and not looks_like_phone(local) else ""
    if looks_like_phone(text) or looks_like_email(text):
        return None
    label = text.strip(" .,-")
    if "," in label:
        parts = [part.strip(" .") for part in label.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1] and " and " not in parts[1].lower():
            label = f"{parts[1]} {parts[0]}".strip()
        else:
            return None
    lowered = label.lower()
    if not label or lowered in _SKIP or len(label) < 2:
        return None
    if " and " in lowered or lowered.endswith(" group"):
        return None
    if len(label) > 80:
        label = label[:80].rstrip()
    return label


def callable_number(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if "@s.whatsapp.net" in text.lower() or "@g.us" in text.lower():
        text = text.split("@", 1)[0]
    if looks_like_email(text):
        return None
    digits = _digits(text)
    if digits in {"911", "112", "999", "000", "110", "119", "101", "102", "108"}:
        return None
    if not (8 <= len(digits) <= 15):
        return None
    if text.startswith("+"):
        return "+" + digits
    return digits if looks_like_phone(text) or len(digits) >= 10 else None


def _channel_for(event_type: str, source: str) -> str:
    kind = (event_type or "").lower()
    src = (source or "").lower()
    if "whatsapp" in kind or src == "whatsapp":
        return "whatsapp"
    if "imessage" in kind or src == "imessage":
        return "imessage"
    if "mail" in kind or src == "mail":
        return "mail"
    if "contact" in kind or src == "contacts":
        return "contacts"
    if "person" in kind:
        return "memory"
    return src or "home"


def _row(name: str, *, channel: str, callable_flag: bool) -> dict[str, Any]:
    return {
        "name": name,
        "channels": [channel] if channel else [],
        "callable": bool(callable_flag),
    }


def _merge_row(store: dict[str, dict[str, Any]], name: str, *, channel: str, callable_flag: bool) -> None:
    key = name.lower()
    existing = store.get(key)
    if existing is None:
        store[key] = _row(name, channel=channel, callable_flag=callable_flag)
        return
    if channel and channel not in existing["channels"]:
        existing["channels"].append(channel)
    if callable_flag:
        existing["callable"] = True


def _from_event(content: dict[str, Any], event_type: str) -> tuple[str | None, str | None]:
    blob = content if isinstance(content, dict) else {}
    kind = (event_type or "").lower()
    name = display_name(
        str(
            blob.get("name")
            or blob.get("full_name")
            or blob.get("partner_name")
            or blob.get("push_name")
            or blob.get("title")
            or blob.get("sender")
            or blob.get("handle")
            or blob.get("from")
            or ""
        )
    )
    if (kind.startswith("life.person") or kind.startswith("life.chat")) and not name:
        match = re.search(r"Person:\s*([^(.\n]+)", str(blob.get("text") or ""), re.I)
        name = display_name(match.group(1) if match else "")
        if not name:
            name = display_name(str(blob.get("title") or ""))
    number = callable_number(
        str(
            blob.get("phone")
            or blob.get("phone_number")
            or ("" if name else blob.get("handle") or blob.get("from") or "")
        )
    )
    if not number:
        phones = blob.get("phone_numbers") or blob.get("phones") or []
        if isinstance(phones, list) and phones:
            number = callable_number(str(phones[0] or ""))
    if number and not name and display_name(str(blob.get("handle") or "")) is None:
        # Phone-only iMessage handles are not people names.
        return None, number
    return name, number


async def list_phone_people(session: AsyncSession, *, limit: int = 40) -> list[dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {}
    entities = (
        await session.execute(
            select(Entity)
            .where(Entity.entity_type == "person")
            .order_by(Entity.updated_at.desc())
            .limit(80)
        )
    ).scalars().all()
    for row in entities:
        name = display_name(row.name)
        if not name:
            continue
        phones = [callable_number(str(alias)) for alias in (row.aliases or [])]
        _merge_row(store, name, channel="memory", callable_flag=any(phones))

    events = (
        await session.execute(
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(_EVENT_TYPES),
            )
            .order_by(Event.occurred_at.desc())
            .limit(400)
        )
    ).scalars().all()
    for event in events:
        name, number = _from_event(dict(event.content or {}), str(event.event_type or ""))
        channel = _channel_for(str(event.event_type or ""), str(event.source or ""))
        if name:
            _merge_row(store, name, channel=channel, callable_flag=bool(number))

    memories = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "life.person",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.updated_time.desc())
            .limit(80)
        )
    ).scalars().all()
    for mem in memories:
        payload = mem.payload if isinstance(mem.payload, dict) else {}
        name = display_name(str(payload.get("name") or ""))
        if not name:
            match = re.search(r"Person:\s*([^(.\n]+)", str(mem.text or ""), re.I)
            name = display_name(match.group(1) if match else "")
        if name:
            _merge_row(store, name, channel="memory", callable_flag=False)

    rows = list(store.values())
    rows.sort(key=lambda row: (not row["callable"], row["name"].lower()))
    return rows[: max(1, min(int(limit or 40), 80))]


def public_contact_rows(raw: list[Any]) -> list[dict[str, str]]:
    """Strip numbers from a device contacts snapshot."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            name = display_name(item)
        elif isinstance(item, dict):
            name = display_name(str(item.get("name") or item.get("full_name") or ""))
        else:
            name = None
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name})
    return out


async def resolve_callable_number(session: AsyncSession, query: str) -> dict[str, Any]:
    """Return a dialable number for an explicit Call, never for the model."""
    want = display_name(query) or " ".join(str(query or "").split())
    if not want:
        return {"number": None, "name": "", "source": None}
    needle = want.lower()
    direct = callable_number(query)
    if direct:
        return {"number": direct, "name": want, "source": "typed"}

    entities = (
        await session.execute(select(Entity).where(Entity.entity_type == "person"))
    ).scalars().all()
    for row in entities:
        aliases = [str(a) for a in (row.aliases or [])]
        names = [row.name, *aliases]
        if not any(needle == str(n).strip().lower() or needle in str(n).strip().lower() for n in names if n):
            continue
        for alias in aliases:
            number = callable_number(alias)
            if number:
                return {"number": number, "name": row.name, "source": "memory"}

    events = (
        await session.execute(
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(_EVENT_TYPES),
            )
            .order_by(Event.occurred_at.desc())
            .limit(400)
        )
    ).scalars().all()
    for event in events:
        content = dict(event.content or {})
        name, number = _from_event(content, str(event.event_type or ""))
        label = name or display_name(str(content.get("name") or ""))
        if not label or needle not in label.lower():
            continue
        if not number:
            number = callable_number(str(content.get("phone") or content.get("handle") or ""))
        if number:
            return {
                "number": number,
                "name": label,
                "source": _channel_for(str(event.event_type or ""), str(event.source or "")),
            }
    return {"number": None, "name": want, "source": None}
