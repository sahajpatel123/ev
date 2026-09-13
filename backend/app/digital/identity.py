"""Person identity bridge — derived mappings, not a second canonical people DB.

Canonical people stay Entity (entity_type=person). This layer maps
Google contact IDs, emails, phones, WhatsApp names/numbers, calendar attendees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.resolve import looks_like_destination
from app.models import Entity

_PHONE = re.compile(r"\d+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass
class PersonHit:
    entity_id: str | None
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    google_contact_id: str | None = None
    whatsapp_ref: str | None = None
    channels: list[str] = field(default_factory=list)
    score: float = 0.0

    def as_public(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "emails": self.emails,
            "phones": self.phones,
            "google_contact_id": self.google_contact_id,
            "whatsapp_ref": self.whatsapp_ref,
            "channels": self.channels,
        }


def normalize_phone(value: str) -> str:
    return "".join(_PHONE.findall(value or ""))


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


async def load_canonical_people(session: AsyncSession) -> list[PersonHit]:
    """Canonical Entity rows as recipient identities.

    A known phone number is also the WhatsApp handle (WhatsApp addresses by
    number), so canonical people without a bridged chat ref are still
    reachable instead of resolving to no channel at all.
    """
    rows = (
        await session.execute(select(Entity).where(Entity.entity_type == "person"))
    ).scalars().all()
    hits = []
    for row in rows:
        aliases = list(row.aliases or [])
        emails = [normalize_email(a) for a in aliases if "@" in str(a)]
        phones = [p for p in (normalize_phone(a) for a in aliases) if len(p) >= 8]
        channels = ["memory"]
        if phones:
            channels.append("whatsapp")
        if emails:
            channels.append("email")
        hits.append(
            PersonHit(
                entity_id=str(row.id),
                name=row.name,
                emails=emails,
                phones=phones,
                whatsapp_ref=phones[0] if phones else None,
                channels=channels,
            )
        )
    return hits


def merge_external(canonical: list[PersonHit], external: list[dict[str, Any]]) -> list[PersonHit]:
    """Bridge Google/WhatsApp identities onto Entity rows when emails/phones match."""
    by_email: dict[str, PersonHit] = {}
    by_phone: dict[str, PersonHit] = {}
    for hit in canonical:
        for e in hit.emails:
            by_email[e] = hit
        for p in hit.phones:
            by_phone[p] = hit
    out = list(canonical)
    for row in external:
        emails = [normalize_email(e) for e in (row.get("emails") or []) if e]
        phones = [normalize_phone(p) for p in (row.get("phones") or []) if p]
        name = str(row.get("name") or row.get("display_name") or "")
        matched: PersonHit | None = None
        for e in emails:
            matched = by_email.get(e)
            if matched:
                break
        if matched is None:
            for p in phones:
                matched = by_phone.get(p) if len(p) >= 8 else None
                if matched:
                    break
        if matched is None:
            hit = PersonHit(
                entity_id=None,
                name=name,
                emails=emails,
                phones=[p for p in phones if len(p) >= 8],
                google_contact_id=row.get("google_contact_id") or row.get("resourceName"),
                whatsapp_ref=row.get("whatsapp_ref") or row.get("chat_ref"),
                channels=[c for c in ("email", "whatsapp") if row.get(c) or row.get("emails") or row.get("chat_ref")],
            )
            out.append(hit)
            continue
        for e in emails:
            if e not in matched.emails:
                matched.emails.append(e)
        for p in phones:
            if p and p not in matched.phones:
                matched.phones.append(p)
        if row.get("resourceName"):
            matched.google_contact_id = str(row.get("resourceName"))
        if row.get("chat_ref"):
            matched.whatsapp_ref = str(row.get("chat_ref"))
            if "whatsapp" not in matched.channels:
                matched.channels.append("whatsapp")
        if emails and "email" not in matched.channels:
            matched.channels.append("email")
    return out


def _identity_tokens(value: str) -> tuple[str, ...]:
    """Whole-word tokens of a name/handle; stop words carry no identity."""
    return tuple(
        part.lower() for part in _WORD.findall(str(value or "")) if part.lower() not in _PERSON_STOP
    )


def _handle_matches(query: str, person: PersonHit) -> bool:
    """True only when the destination is exactly one of this person's handles."""
    email = normalize_email(query)
    if "@" in email:
        return email in {normalize_email(item) for item in person.emails}
    phone = normalize_phone(query)
    if len(phone) >= 7:
        known = {normalize_phone(item) for item in person.phones}
        known.add(normalize_phone(person.whatsapp_ref or ""))
        return phone in known
    raw = (query or "").strip().lower()
    handles = {
        str(person.whatsapp_ref or "").strip().lower(),
        str(person.google_contact_id or "").strip().lower(),
    }
    return raw in handles - {""}


def _name_tier(query_tokens: tuple[str, ...], person: PersonHit) -> int:
    """Whole-token identity tier; 0 means not this person.

    ``john`` scores 0 against ``Johnson`` — a substring is not an identity.
    3 is the exact name, 2 a leading run ("Rahul" for "Rahul Shah"), 1 a run
    inside a longer name ("Shah" for "Rahul Shah").
    """
    name = _identity_tokens(person.name)
    if not name or not query_tokens:
        return 0
    if query_tokens == name:
        return 3
    if query_tokens == name[: len(query_tokens)]:
        return 2
    width = len(query_tokens)
    for start in range(len(name) - width + 1):
        if name[start : start + width] == query_tokens:
            return 1
    return 0


def _verdict(found: list[PersonHit]) -> dict[str, Any]:
    """One candidate is unique; several stay a question, never a winner."""
    seen: set[str] = set()
    distinct: list[PersonHit] = []
    for person in found:
        if person.entity_id:
            if person.entity_id in seen:
                continue
            seen.add(person.entity_id)
        distinct.append(person)
    if not distinct:
        return {"status": "none", "person": None, "sent": False}
    if len(distinct) > 1:
        return {
            "status": "ambiguous",
            "candidates": [person.as_public() for person in distinct[:4]],
            "sent": False,
        }
    return {"status": "unique", "person": distinct[0].as_public(), "sent": False}


def _match(query: str, people: list[PersonHit]) -> dict[str, Any]:
    """Strict identity verdict for one spoken recipient. Never a first hit."""
    who = (query or "").strip()
    if not who or not people:
        return {"status": "none", "person": None, "sent": False}
    exact = [person for person in people if _handle_matches(who, person)]
    if exact:
        return _verdict(exact)
    if looks_like_destination(who):
        return {"status": "none", "person": None, "sent": False}
    tokens = _identity_tokens(who)
    if not tokens:
        return {"status": "none", "person": None, "sent": False}
    tiers: dict[int, list[PersonHit]] = {}
    for person in people:
        tier = _name_tier(tokens, person)
        if tier:
            tiers.setdefault(tier, []).append(person)
    if not tiers:
        return {"status": "none", "person": None, "sent": False}
    return _verdict(tiers[max(tiers)])


def _explicit_handle_verdict(query: str, people: list[PersonHit]) -> dict[str, Any] | None:
    """A destination spoken in full outranks any name mention in the same ask."""
    for chunk in re.split(r"[\s,;]+", (query or "").strip()):
        token = chunk.strip(".,;:!?()[]<>\"'")
        if not token or not looks_like_destination(token):
            continue
        verdict = _verdict([person for person in people if _handle_matches(token, person)])
        if verdict["status"] != "none":
            return verdict
    return None


def resolve_person(query: str, people: list[PersonHit] | None = None) -> dict[str, Any]:
    """Never guess a consequential recipient. Unique or clarify.

    Pure matching, no I/O: callers holding a loaded roster pay nothing, and an
    empty roster honestly answers "no identity known". When the canonical store
    still has to be read, call :func:`resolve_person_live` instead.
    """
    roster = [person for person in (people or []) if person is not None]
    if not roster:
        return {"status": "none", "person": None, "sent": False}
    raw = (query or "").strip()
    explicit = _explicit_handle_verdict(raw, roster)
    if explicit is not None:
        return explicit
    cleaned = extract_person_query(query) or re.sub(
        r"(?i)^(message|email|e-mail|whatsapp|contact|call|text|ping|send)\s+",
        "",
        query or "",
    ).strip() or (query or "")
    match = _match(cleaned, roster)
    if match["status"] == "none" and cleaned != raw:
        match = _match(raw, roster)
    return match


async def resolve_person_live(
    query: str,
    session: AsyncSession,
    people: list[PersonHit] | None = None,
    *,
    external: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch-and-resolve in one call: the send seam when a session exists.

    Loads canonical people only when none are supplied, then bridges any
    Google/WhatsApp rows onto them. No import-time or eager DB work: the query
    runs exactly once, at call time.
    """
    hits = [person for person in (people or []) if person is not None]
    if not hits:
        hits = await load_canonical_people(session)
    if external:
        hits = merge_external(hits, external)
    return resolve_person(query, hits)


def preferred_channel(person: dict[str, Any], *, explicit: str | None = None, prefs: dict[str, str] | None = None) -> str | None:
    if explicit:
        return explicit.upper()
    name = str(person.get("name") or "")
    if prefs and name in prefs:
        return prefs[name]
    channels = [c.upper() for c in (person.get("channels") or [])]
    if "WHATSAPP" in channels and person.get("whatsapp_ref"):
        return "WHATSAPP"
    if person.get("emails"):
        return "EMAIL"
    if "WHATSAPP" in channels:
        return "WHATSAPP"
    return None


_PERSON_STOP = frozenset(
    {
        "a",
        "about",
        "an",
        "any",
        "chat",
        "chats",
        "conversation",
        "do",
        "email",
        "emails",
        "for",
        "from",
        "gmail",
        "have",
        "i",
        "imessage",
        "inbox",
        "just",
        "last",
        "latest",
        "mail",
        "me",
        "message",
        "messages",
        "more",
        "my",
        "new",
        "newest",
        "particular",
        "please",
        "recent",
        "someone",
        "some",
        "text",
        "texts",
        "that",
        "the",
        "these",
        "this",
        "those",
        "thread",
        "threads",
        "to",
        "whatsapp",
        "with",
        "you",
        "your",
    }
)


def _person_token_ok(token: str) -> bool:
    parts = [p for p in str(token or "").split() if p]
    if not parts:
        return False
    return all(part.lower() not in _PERSON_STOP for part in parts)


def extract_person_query(text: str) -> str:
    """Pull a person mention from owner language. Empty if none."""
    raw = (text or "").strip()
    m = re.search(
        r"(?i)\b(?:message|email|whatsapp|contact|call|text|ping|brief me on|prepare me for(?: my conversation with)?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        raw,
    )
    if m:
        token = m.group(1).strip()
        if _person_token_ok(token):
            return token
    m2 = re.search(
        r"(?i)\b(?:from|to|with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        raw,
    )
    if m2:
        token = m2.group(1).strip()
        if _person_token_ok(token):
            return token
    m3 = re.search(r"(?i)\b([A-Z][a-z]{2,})\s+(?:sent|emailed|replied|said)", raw)
    if m3:
        token = m3.group(1).strip()
        if _person_token_ok(token):
            return token
    return ""


def remember_channel_use(prefs: dict[str, str], person_name: str, channel: str) -> dict[str, str]:
    """Non-sensitive derived preference from actual use."""
    out = dict(prefs)
    if person_name and channel:
        out[person_name] = channel.upper()
    return out
