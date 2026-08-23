"""Deterministic target resolution for life I/O.

Not a capability registry. Callers still own names, scopes, and adapters.
This module only answers: which owned target, when does it fire, is it a
duplicate, and what should be spoken first on a plate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as date_parser

from app.config import settings
from app.utils.text import utcnow

T = TypeVar("T")

STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "our",
        "please",
        "turn",
        "switch",
        "set",
        "to",
        "on",
        "off",
        "for",
        "me",
        "now",
    }
)
DOMAIN_HINTS = frozenset({"light", "lights", "lamp", "lamps", "lock", "door", "garage", "cover"})
UNIQUE_SCORE = 0.62
UNIQUE_GAP = 0.12
NEAR_DUP_MINUTES = 15

_IN_RE = re.compile(
    r"\bin\s+(?:(?P<article>an?)\s+)?(?:(?P<num>\d+(?:\.\d+)?)\s*)?"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b",
    re.IGNORECASE,
)
_CLOCK_ONLY_RE = re.compile(
    r"^(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"^\+?\d[\d.\-\s()]{6,}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


MatchStatus = Literal["unique", "ambiguous", "none"]


def owner_tz() -> ZoneInfo:
    """Owner-local interpretation zone for time language.

    TIMEZONE CONTRACT: owner-local wall clock → canonical aware UTC storage
    → owner-local presentation. Relative day/clock words ("tomorrow at 7",
    "tonight") are interpreted in settings.timezone, never server-local or
    silently UTC. Fail-closed to UTC when unset/invalid.
    """
    name = (getattr(settings, "timezone", None) or "").strip()
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Match(Generic[T]):
    status: MatchStatus
    item: T | None
    score: float
    candidates: tuple[T, ...] = ()
    scores: tuple[float, ...] = ()

    @property
    def unique(self) -> bool:
        return self.status == "unique" and self.item is not None


def normalize_label(value: str) -> str:
    text = (value or "").strip().lower().replace("_", " ").replace(".", " ")
    tokens = [tok for tok in _TOKEN_RE.findall(text) if tok not in STOPWORDS]
    return " ".join(tokens)


def token_set(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(normalize_label(value)))


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def score_label(query: str, label: str, *, extra: Sequence[str] = ()) -> float:
    raw_query = (query or "").strip().lower()
    raw_label = (label or "").strip().lower()
    if not raw_query or not raw_label:
        return 0.0
    if raw_query == raw_label:
        return 1.0
    qn = normalize_label(query)
    ln = normalize_label(label)
    if qn and qn == ln:
        return 0.97
    extras = [normalize_label(item) for item in extra if item]
    if qn and qn in extras:
        return 0.95
    if qn and (qn in ln or ln in qn) and min(len(qn), len(ln)) >= 3:
        contained = 0.82 if qn in ln else 0.74
    else:
        contained = 0.0
    qt = token_set(query)
    lt = token_set(label)
    for item in extras:
        lt |= token_set(item)
    overlap = jaccard(qt, lt)
    subset = 0.75 + 0.25 * overlap if qt and qt <= lt else overlap
    hint = 0.04 if qt & DOMAIN_HINTS and lt & DOMAIN_HINTS else 0.0
    return min(1.0, max(contained, subset) + hint)


def pick_unique(
    query: str,
    items: Sequence[T],
    *,
    labels: Callable[[T], Sequence[str]],
) -> Match[T]:
    scored: list[tuple[float, T]] = []
    for item in items:
        names = [str(part) for part in labels(item) if str(part).strip()]
        if not names:
            continue
        best = max(
            score_label(query, name, extra=[other for other in names if other != name])
            for name in names
        )
        if best > 0:
            scored.append((best, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return Match(status="none", item=None, score=0.0)
    top_score, top = scored[0]
    close = tuple(
        item
        for score, item in scored
        if top_score - score < UNIQUE_GAP and score >= UNIQUE_SCORE
    )
    if len(close) > 1:
        return Match(
            status="ambiguous",
            item=None,
            score=top_score,
            candidates=close[:4],
            scores=tuple(score for score, _ in scored[:4]),
        )
    if top_score >= UNIQUE_SCORE:
        return Match(
            status="unique",
            item=top,
            score=top_score,
            candidates=tuple(item for _, item in scored[:4]),
            scores=tuple(score for score, _ in scored[:4]),
        )
    return Match(
        status="none",
        item=None,
        score=top_score,
        candidates=tuple(item for _, item in scored[:4]),
    )


def candidate_names(items: Sequence[Any], *, name_of: Callable[[Any], str]) -> list[str]:
    names: list[str] = []
    for item in items:
        label = str(name_of(item) or "").strip()
        if label and label not in names:
            names.append(label)
    return names


def ambiguous_spoken(kind: str, names: Sequence[str]) -> str:
    shown = [name for name in names if name][:4]
    if len(shown) >= 2:
        listed = ", ".join(shown[:-1]) + f" or {shown[-1]}"
        return f"I have more than one {kind}: {listed}. Which one?"
    return f"Which {kind} did you mean?"


def looks_like_destination(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    if "@" in raw and "." in raw.split("@", 1)[-1]:
        return True
    if raw.lower().startswith(("tel:", "facetime:")):
        return True
    digits = re.sub(r"\D", "", raw)
    return bool(_PHONE_RE.match(raw)) or len(digits) >= 7


def titles_match(left: str, right: str) -> bool:
    return bool(normalize_label(left)) and normalize_label(left) == normalize_label(right)


def parse_instant(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = date_parser.isoparse(raw) if "T" in raw or raw[:1].isdigit() and "-" in raw[:12] else date_parser.parse(raw)
        except (ValueError, TypeError, OverflowError, date_parser.ParserError):
            try:
                parsed = date_parser.parse(str(value))
            except (ValueError, TypeError, OverflowError, date_parser.ParserError):
                return None
    if parsed.tzinfo is None:
        # Naive absolute times are owner-local wall clock (contract above).
        parsed = parsed.replace(tzinfo=owner_tz())
    return parsed.astimezone(UTC)


def starts_close(
    left: str | datetime | None,
    right: str | datetime | None,
    *,
    minutes: int = NEAR_DUP_MINUTES,
) -> bool:
    a = parse_instant(left)
    b = parse_instant(right)
    if a is None or b is None:
        return str(left or "").strip() == str(right or "").strip() and bool(left)
    return abs((a - b).total_seconds()) <= minutes * 60


def is_near_duplicate(
    *,
    title: str,
    start: str | datetime | None,
    other_title: str,
    other_start: str | datetime | None,
    minutes: int = NEAR_DUP_MINUTES,
) -> bool:
    return titles_match(title, other_title) and starts_close(start, other_start, minutes=minutes)


def parse_owner_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse owner time language into an aware datetime. None if unknown."""

    clock = now or utcnow()
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    relative = _IN_RE.search(raw)
    if relative:
        unit = (relative.group("unit") or "minutes").lower()
        if relative.group("article"):
            amount = 1.0
        elif relative.group("num"):
            amount = float(relative.group("num"))
        else:
            amount = 1.0
        if unit.startswith("day"):
            return clock + timedelta(days=amount)
        if unit.startswith("hour") or unit.startswith("hr"):
            return clock + timedelta(hours=amount)
        return clock + timedelta(minutes=amount)

    tz = owner_tz()
    base_local = clock.astimezone(tz)
    shift_days = 0
    if "tomorrow" in lowered:
        shift_days = 1
    tonight = "tonight" in lowered
    clock_match = _CLOCK_RE.search(raw)
    if clock_match and (tonight or "tomorrow" in lowered or "at" in lowered or clock_match.group("ampm") or _CLOCK_ONLY_RE.match(raw)):
        hour = int(clock_match.group("hour"))
        minute = int(clock_match.group("minute") or 0)
        ampm = (clock_match.group("ampm") or "").lower()
        if tonight and not ampm and hour < 12 or ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            pass
        else:
            target_local = base_local.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) + timedelta(days=shift_days)
            if target_local <= base_local and shift_days == 0 and (_CLOCK_ONLY_RE.match(raw) or tonight):
                target_local += timedelta(days=1)
            return target_local.astimezone(UTC)

    # Date-only relative day words with no explicit clock: one documented
    # product default — end of that owner-local day (23:59:59 local → UTC).
    # Never an invented arbitrary clock time, never server-local silently.
    if tonight or "tomorrow" in lowered or "today" in lowered:
        end_local = base_local.replace(hour=23, minute=59, second=59, microsecond=0)
        if "tomorrow" in lowered:
            end_local += timedelta(days=1)
        return end_local.astimezone(UTC)

    parsed = parse_instant(raw) if any(ch.isdigit() for ch in raw) else None
    if parsed is None:
        return None
    if parsed <= clock and _CLOCK_ONLY_RE.match(raw):
        parsed += timedelta(days=1)
    return parsed


def overlapping_bounds(
    start_a: datetime | None,
    end_a: datetime | None,
    start_b: datetime | None,
    end_b: datetime | None,
) -> bool:
    if start_a is None or start_b is None:
        return False
    finish_a = end_a or (start_a + timedelta(hours=1))
    finish_b = end_b or (start_b + timedelta(hours=1))
    return start_a < finish_b and start_b < finish_a


def score_plate_item(
    kind: str,
    item: dict[str, Any],
    *,
    now: datetime | None = None,
    overlapping: bool = False,
) -> float:
    clock = now or utcnow()
    kind = (kind or "").lower()
    if kind == "timer":
        fire = parse_instant(item.get("fire_at") or item.get("when"))
        if fire is None:
            return 0.4
        minutes = (fire - clock).total_seconds() / 60.0
        if minutes <= 0:
            return 0.99
        if minutes <= 15:
            return 0.93
        if minutes <= 120:
            return 0.7
        return 0.45
    if kind == "calendar":
        start = parse_instant(item.get("start") or item.get("starts_at"))
        if start is None:
            return 0.35
        hours = (start - clock).total_seconds() / 3600.0
        score = 0.88 if hours <= 1 else 0.72 if hours <= 4 else 0.5 if hours <= 24 else 0.32
        if overlapping:
            score = min(1.0, score + 0.12)
        return score
    if kind == "deadline":
        return 0.84
    if kind == "mail":
        unread = bool(item.get("unread") or item.get("unseen") or str(item.get("status") or "").lower() == "unread")
        return 0.68 if unread else 0.48
    if kind == "github":
        labels = item.get("labels") or []
        text = " ".join(str(part).lower() for part in labels) + " " + str(item.get("title") or "").lower()
        if any(token in text for token in ("p0", "blocker", "critical", "bug")):
            return 0.64
        return 0.42
    return 0.3


def rank_plate(
    *,
    calendar: Sequence[dict] | None = None,
    mail: Sequence[dict] | None = None,
    github: Sequence[dict] | None = None,
    timers: Sequence[dict] | None = None,
    deadlines: Sequence[str] | None = None,
    now: datetime | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    clock = now or utcnow()
    cal_items = list(calendar or [])
    overlap_ids: set[int] = set()
    bounds: list[tuple[int, datetime | None, datetime | None]] = []
    for index, event in enumerate(cal_items):
        bounds.append(
            (
                index,
                parse_instant(event.get("start") or event.get("starts_at")),
                parse_instant(event.get("end") or event.get("ends_at")),
            )
        )
    for i, start_a, end_a in bounds:
        for j, start_b, end_b in bounds:
            if i >= j:
                continue
            if overlapping_bounds(start_a, end_a, start_b, end_b):
                overlap_ids.add(i)
                overlap_ids.add(j)

    ranked: list[dict[str, Any]] = []
    for index, event in enumerate(cal_items):
        ranked.append(
            {
                "kind": "calendar",
                "title": str(event.get("summary") or event.get("title") or "Event"),
                "when": event.get("start") or event.get("starts_at"),
                "score": score_plate_item("calendar", event, now=clock, overlapping=index in overlap_ids),
                "conflict": index in overlap_ids,
            }
        )
    for item in mail or []:
        ranked.append(
            {
                "kind": "mail",
                "title": str(item.get("subject") or item.get("title") or item.get("from") or "Mail"),
                "when": item.get("date") or item.get("received"),
                "score": score_plate_item("mail", item, now=clock),
            }
        )
    for item in github or []:
        ranked.append(
            {
                "kind": "github",
                "title": str(item.get("title") or f"#{item.get('number') or ''}").strip(),
                "when": item.get("updated_at"),
                "score": score_plate_item("github", item, now=clock),
            }
        )
    for item in timers or []:
        ranked.append(
            {
                "kind": "timer",
                "title": str(item.get("text") or "Timer"),
                "when": item.get("fire_at"),
                "score": score_plate_item("timer", item, now=clock),
            }
        )
    for value in deadlines or []:
        ranked.append(
            {
                "kind": "deadline",
                "title": str(value),
                "when": None,
                "score": score_plate_item("deadline", {"title": value}, now=clock),
            }
        )
    ranked.sort(key=lambda row: row["score"], reverse=True)
    return ranked[:limit]
