"""Recipient resolution that refuses to guess.

The macOS contacts bridge returns unordered substring matches; taking the
first one is how "John" reaches "Johnson" and how two real Johns collapse to
whichever card the address book enumerated first. This module scores whole
name tokens, prefers an exact identity, and reports ambiguity with the
candidate names instead of picking one.

Callers decide the consequence of ambiguity; the resolver never sends.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.ev.resolve import ambiguous_spoken, looks_like_destination

RecipientStatus = Literal["direct", "unique", "ambiguous", "none"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
UNIQUE_SCORE = 0.8
UNIQUE_GAP = 0.1


@dataclass(frozen=True)
class RecipientMatch:
    """Outcome of resolving one spoken recipient."""

    status: RecipientStatus
    display: str
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    contact_id: str = ""
    candidates: tuple[str, ...] = ()
    score: float = 0.0

    @property
    def unique(self) -> bool:
        return self.status in {"direct", "unique"}

    @property
    def found(self) -> bool:
        return self.status != "none"

    @property
    def primary_phone(self) -> str:
        return self.phones[0] if self.phones else ""

    @property
    def primary_email(self) -> str:
        return self.emails[0] if self.emails else ""

    @property
    def handle(self) -> str:
        return self.primary_phone or self.primary_email or self.display


def name_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall((value or "").lower()))


def score_person_name(query: str | None, label: str | None) -> float:
    """Word-boundary identity score in [0, 1]. Substrings never match.

    ``john`` does not match ``Johnson``; ``john`` does match ``John Smith``
    partially, and two such partials are reported as ambiguous by
    :func:`match_recipient` rather than one being chosen.
    """

    query_tokens = name_tokens(query)
    name = name_tokens(label)
    if not query_tokens or not name:
        return 0.0
    if query_tokens == name:
        return 1.0
    prefix_len = len(query_tokens)
    if prefix_len <= len(name) and name[:prefix_len] == query_tokens:
        if prefix_len == len(name):
            return 1.0
        if prefix_len == 1:
            return 0.9
        return 0.85
    if all(token in name for token in query_tokens):
        positions = [name.index(token) for token in query_tokens]
        if positions == sorted(positions):
            return 0.8
    return 0.0


def _contact_label(row: dict[str, Any]) -> str:
    full = str(row.get("full_name") or row.get("name") or "").strip()
    if full:
        return full
    given = str(row.get("given_name") or "").strip()
    family = str(row.get("family_name") or "").strip()
    return " ".join(part for part in (given, family) if part).strip()


def _candidate_label(row: dict[str, Any]) -> str:
    """Name plus first handle so an ambiguity is pickable over voice."""

    name = _contact_label(row) or "Unnamed"
    phones = _phones(row)
    emails = _emails(row)
    handle = phones[0] if phones else (emails[0] if emails else "")
    return f"{name} · {handle}" if handle else name


def _contact_scores(query: str, matches: Sequence[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in matches:
        if not isinstance(row, dict):
            continue
        label = _contact_label(row)
        if not label:
            continue
        score = score_person_name(query, label)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _phones(row: dict[str, Any]) -> tuple[str, ...]:
    values = [str(item).strip() for item in (row.get("phone_numbers") or []) if str(item).strip()]
    usable = [value for value in values if len(re.sub(r"\D", "", value)) >= 7]
    return tuple(usable or values)


def _emails(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item).strip()
        for item in (row.get("email_addresses") or [])
        if str(item).strip()
    )


def match_recipient(query: str | None, matches: Iterable[dict[str, Any]]) -> RecipientMatch:
    """Unique-or-clarify recipient match. Never returns a guessed winner."""

    who = (query or "").strip()
    if not who:
        return RecipientMatch(status="none", display="")
    if looks_like_destination(who):
        return RecipientMatch(status="direct", display=who)
    scored = _contact_scores(who, list(matches))
    if not scored:
        return RecipientMatch(status="none", display=who)
    top_score, top = scored[0]
    if top_score < UNIQUE_SCORE:
        return RecipientMatch(status="none", display=who, score=top_score)
    tied = [row for score, row in scored if top_score - score < UNIQUE_GAP]
    if len(tied) > 1:
        names = tuple(_candidate_label(row) for row in tied)
        return RecipientMatch(
            status="ambiguous",
            display=who,
            candidates=names,
            score=top_score,
        )
    label = _contact_label(top)
    return RecipientMatch(
        status="unique",
        display=label or who,
        phones=_phones(top),
        emails=_emails(top),
        contact_id=str(top.get("id") or ""),
        candidates=(label,) if label else (),
        score=top_score,
    )


def choose_handle(
    match: RecipientMatch,
    *,
    address: str = "handle",
    channel: str | None = None,
) -> str:
    """Best destination for a channel's address kind; "" when none exists."""

    if match.status == "direct":
        return match.display
    if address == "phone":
        return match.primary_phone
    if address == "email":
        return match.primary_email
    if channel == "whatsapp":
        return match.primary_phone
    return match.primary_phone or match.primary_email


def ambiguous_recipient_spoken(match: RecipientMatch) -> str:
    return ambiguous_spoken("contact", match.candidates)


def verify_peer(query: str | None, peer: dict[str, Any] | None) -> bool:
    """True only when a WhatsApp chat partner is a strong identity match."""

    if not isinstance(peer, dict):
        return False
    handle = str(peer.get("handle") or peer.get("name") or "").strip()
    if not handle:
        return False
    return score_person_name(query, handle) >= 0.85
