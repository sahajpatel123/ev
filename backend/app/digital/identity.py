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

from app.ev.resolve import pick_unique
from app.models import Entity

_PHONE = re.compile(r"\d+")


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
    rows = (
        await session.execute(select(Entity).where(Entity.entity_type == "person"))
    ).scalars().all()
    hits = []
    for row in rows:
        aliases = list(row.aliases or [])
        emails = [normalize_email(a) for a in aliases if "@" in str(a)]
        phones = [normalize_phone(a) for a in aliases if normalize_phone(str(a))]
        hits.append(
            PersonHit(
                entity_id=str(row.id),
                name=row.name,
                emails=emails,
                phones=[p for p in phones if len(p) >= 8],
                channels=["memory"],
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


def resolve_person(query: str, people: list[PersonHit]) -> dict[str, Any]:
    """Never guess a consequential recipient. Unique or clarify."""
    cleaned = extract_person_query(query) or re.sub(
        r"(?i)^(message|email|e-mail|whatsapp|contact|call|text|ping|send)\s+",
        "",
        query or "",
    ).strip() or (query or "")
    match = pick_unique(
        cleaned,
        people,
        labels=lambda p: [p.name, *p.emails, *p.phones, p.whatsapp_ref or "", p.google_contact_id or ""],
    )
    if match.status == "none" and cleaned != query:
        match = pick_unique(
            query,
            people,
            labels=lambda p: [p.name, *p.emails, *p.phones, p.whatsapp_ref or "", p.google_contact_id or ""],
        )
    if match.status == "unique" and match.item is not None:
        # Shared first name with another candidate is still ambiguous.
        token = cleaned.split()[0].lower() if cleaned else ""
        same = [p for p in people if token and token in (p.name or "").lower().split()[:1]]
        if len(same) > 1:
            return {
                "status": "ambiguous",
                "candidates": [c.as_public() for c in same[:4]],
                "sent": False,
            }
        return {"status": "unique", "person": match.item.as_public(), "sent": False}
    if match.status == "ambiguous":
        return {
            "status": "ambiguous",
            "candidates": [c.as_public() for c in match.candidates],
            "sent": False,
        }
    token = cleaned.split()[0] if cleaned else ""
    same = [p for p in people if token and token.lower() in (p.name or "").lower()]
    if len(same) > 1:
        return {
            "status": "ambiguous",
            "candidates": [c.as_public() for c in same[:4]],
            "sent": False,
        }
    return {"status": "none", "person": None, "sent": False}


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


def extract_person_query(text: str) -> str:
    """Pull a person mention from owner language. Empty if none."""
    raw = (text or "").strip()
    m = re.search(
        r"(?i)\b(?:message|email|whatsapp|contact|call|text|ping|brief me on|prepare me for(?: my conversation with)?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        raw,
    )
    if m:
        token = m.group(1).strip()
        if token.lower() not in {"the", "my", "an", "me"}:
            return token
    m2 = re.search(
        r"(?i)\b(?:from|to|with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        raw,
    )
    if m2:
        return m2.group(1).strip()
    m3 = re.search(r"(?i)\b([A-Z][a-z]{2,})\s+(?:sent|emailed|replied|said)", raw)
    return m3.group(1) if m3 else ""


def remember_channel_use(prefs: dict[str, str], person_name: str, channel: str) -> dict[str, str]:
    """Non-sensitive derived preference from actual use."""
    out = dict(prefs)
    if person_name and channel:
        out[person_name] = channel.upper()
    return out
