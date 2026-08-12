"""Temporal expression resolution for memory extraction.

Relative expressions ("last Tuesday", "in March", "two years running") resolve
against the event's own timestamp, so resolution is deterministic: the same
event always resolves to the same absolute instants, which keeps the
rebuild-from-events invariant intact.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "hundred": 100,
}

_WEEKDAY_RE = re.compile(
    r"\b(last|this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_REL_MONTH_RE = re.compile(
    r"\b(?:in|during|by|for)?\s*(last|this|next)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
_REL_PERIOD_RE = re.compile(
    r"\b(?:in|during|for|over)?\s*(last|this|next)\s+(week|month|year)\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?\b")
_NAMED_DAY_RE = re.compile(r"\b(yesterday|today|tomorrow)\b", re.IGNORECASE)
_N_AGO_RE = re.compile(
    r"\b(?:in the (?:last|past)|(?:for|over)?)\s*"
    r"(?P<number>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|twenty|thirty|forty|fifty|hundred)"
    r"\s+(?P<unit>days?|weeks?|months?|years?)\s*(?P<direction>ago|back|running)?\b",
    re.IGNORECASE,
)
_SINCE_YEAR_RE = re.compile(r"\b(?:since|from)\s+(20\d{2})\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


@dataclass
class TemporalResolution:
    """One resolved temporal expression."""

    expression: str
    start: datetime | None
    end: datetime | None
    kind: str  # point | range

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("start", "end"):
            value = data[key]
            data[key] = value.isoformat() if value is not None else None
        return data


def _aware(value: datetime, anchor: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=anchor.tzinfo or UTC)
    return value.astimezone(UTC)


def _day_start(day: date, anchor: datetime) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=anchor.tzinfo or UTC)


def _day_span(day: date, anchor: datetime) -> tuple[datetime, datetime]:
    start = _day_start(day, anchor)
    return start, start + timedelta(days=1)


def _month_span(year: int, month: int, anchor: datetime) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=anchor.tzinfo or UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo or UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=anchor.tzinfo or UTC)
    return start, end


def _year_span(year: int, anchor: datetime) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, tzinfo=anchor.tzinfo or UTC)
    return start, datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo or UTC)


def _weekday_span(relative: str, weekday: int, anchor: datetime) -> tuple[datetime, datetime]:
    anchor_date = anchor.date()
    anchor_wd = anchor_date.weekday()
    if relative == "last":
        delta = 7 + (anchor_wd - weekday) % 7
        day = anchor_date - timedelta(days=delta)
    elif relative == "this":
        day = anchor_date - timedelta(days=(anchor_wd - weekday) % 7)
    else:  # next
        delta = (weekday - anchor_wd) % 7
        if delta == 0:
            delta = 7
        day = anchor_date + timedelta(days=delta)
    return _day_span(day, anchor)


def _relative_period_span(relative: str, unit: str, anchor: datetime) -> tuple[datetime, datetime]:
    anchor_date = anchor.date()
    if unit == "week":
        monday = anchor_date - timedelta(days=anchor_date.weekday())
        if relative == "last":
            start = monday - timedelta(days=7)
            return _day_span(start, anchor)[0], _day_span(monday, anchor)[0]
        if relative == "this":
            return _day_span(monday, anchor)[0], _day_span(monday + timedelta(days=7), anchor)[0]
        start = monday + timedelta(days=7)
        return _day_span(start, anchor)[0], _day_span(start + timedelta(days=7), anchor)[0]
    if unit == "month":
        if relative == "last":
            year, month = (anchor_date.year - 1, 12) if anchor_date.month == 1 else (anchor_date.year, anchor_date.month - 1)
            return _month_span(year, month, anchor)
        if relative == "this":
            return _month_span(anchor_date.year, anchor_date.month, anchor)
        year, month = (anchor_date.year + 1, 1) if anchor_date.month == 12 else (anchor_date.year, anchor_date.month + 1)
        return _month_span(year, month, anchor)
    # year
    if relative == "last":
        return _year_span(anchor_date.year - 1, anchor)
    if relative == "this":
        return _year_span(anchor_date.year, anchor)
    return _year_span(anchor_date.year + 1, anchor)


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    return NUMBER_WORDS.get(value.lower(), 1)


def _shift_months(day: date, delta_months: int) -> date:
    total = day.year * 12 + (day.month - 1) + delta_months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _dedupe(results: list[TemporalResolution]) -> list[TemporalResolution]:
    seen: set[tuple] = set()
    unique: list[TemporalResolution] = []
    for result in results:
        key = (
            result.expression.lower(),
            result.start.isoformat() if result.start else None,
            result.end.isoformat() if result.end else None,
        )
        if key not in seen:
            seen.add(key)
            unique.append(result)
    return unique


def resolve_temporal_expressions(
    text: str,
    anchor: datetime | None = None,
) -> list[TemporalResolution]:
    """Resolve relative/absolute temporal expressions against ``anchor``."""
    if not text or not text.strip():
        return []
    anchor = anchor or datetime.now(UTC)
    anchor = _aware(anchor, anchor)
    results: list[TemporalResolution] = []

    for match in _ISO_DATE_RE.finditer(text):
        iso_year, iso_month, iso_day = (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3) or 1),
        )
        try:
            span = _day_span(date(iso_year, iso_month, iso_day), anchor)
        except ValueError:
            continue
        results.append(
            TemporalResolution(
                expression=match.group(0),
                start=span[0],
                end=span[1],
                kind="point",
            )
        )

    for match in _WEEKDAY_RE.finditer(text):
        relative = match.group(1).lower()
        name = match.group(2).lower()
        span = _weekday_span(relative, WEEKDAY_NAMES[name], anchor)
        results.append(
            TemporalResolution(
                expression=match.group(0),
                start=span[0],
                end=span[1],
                kind="point",
            )
        )

    for match in _MONTH_YEAR_RE.finditer(text):
        if re.search(r"\b(?:last|this|next)\s+$", text[: match.start()], re.IGNORECASE):
            continue  # handled by the relative-month rule below
        month_name = match.group(1).lower()
        year: int | None = int(match.group(2)) if match.group(2) else None
        expression = match.group(0)
        if year is not None:
            span = _month_span(year, MONTH_NAMES[month_name], anchor)
        else:
            month = MONTH_NAMES[month_name]
            resolved_year = anchor.year if anchor.month < month else anchor.year + 1
            span = _month_span(resolved_year, month, anchor)
        results.append(
            TemporalResolution(
                expression=expression,
                start=span[0],
                end=span[1],
                kind="range",
            )
        )

    for match in _REL_MONTH_RE.finditer(text):
        relative = match.group(1).lower()
        month_name = match.group(2).lower()
        target_month = MONTH_NAMES[month_name]
        target_year = anchor.year
        if relative == "last" and anchor.month <= target_month:
            target_year -= 1
        elif relative == "next" and anchor.month >= target_month:
            target_year += 1
        span = _month_span(target_year, target_month, anchor)
        results.append(
            TemporalResolution(
                expression=match.group(0).strip(),
                start=span[0],
                end=span[1],
                kind="range",
            )
        )

    for match in _REL_PERIOD_RE.finditer(text):
        relative = match.group(1).lower()
        unit = match.group(2).lower()
        span = _relative_period_span(relative, unit, anchor)
        results.append(
            TemporalResolution(
                expression=match.group(0).strip(),
                start=span[0],
                end=span[1],
                kind="range",
            )
        )

    for match in _NAMED_DAY_RE.finditer(text):
        name = match.group(1).lower()
        if name == "yesterday":
            day = anchor.date() - timedelta(days=1)
        elif name == "today":
            day = anchor.date()
        else:
            day = anchor.date() + timedelta(days=1)
        span = _day_span(day, anchor)
        results.append(
            TemporalResolution(
                expression=name,
                start=span[0],
                end=span[1],
                kind="point",
            )
        )

    for match in _N_AGO_RE.finditer(text):
        number = _number(match.group("number"))
        unit = match.group("unit").lower()
        direction = (match.group("direction") or "").lower()
        if unit.startswith("day"):
            delta = timedelta(days=number)
        elif unit.startswith("week"):
            delta = timedelta(weeks=number)
        lowered = match.group(0).lower()
        if (
            direction in ("ago", "back", "running")
            or any(token in lowered for token in ("last", "past", "for", "over"))
        ):
            if unit.startswith("month"):
                start = _day_start(_shift_months(anchor.date(), -number), anchor)
            elif unit.startswith("year"):
                start = _day_start(_shift_months(anchor.date(), -12 * number), anchor)
            else:
                start = anchor - delta
            results.append(
                TemporalResolution(
                    expression=match.group(0).strip(),
                    start=start,
                    end=anchor,
                    kind="range",
                )
            )

    for match in _SINCE_YEAR_RE.finditer(text):
        year = int(match.group(1))
        span = _year_span(year, anchor)
        results.append(
            TemporalResolution(
                expression=match.group(0).strip(),
                start=span[0],
                end=None,
                kind="range",
            )
        )

    return _dedupe(results)


def temporal_bounds(
    text: str,
    anchor: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return the (start, end) of the first resolved temporal expression."""
    resolutions = resolve_temporal_expressions(text, anchor=anchor)
    if not resolutions:
        return None, None
    first = min(resolutions, key=lambda r: r.start or datetime.max.replace(tzinfo=UTC))
    return first.start, first.end


def memory_temporal_bounds(memory) -> tuple[datetime | None, datetime | None]:
    """Earliest start / latest end across a memory's resolved temporal entries."""
    entries = (memory.payload or {}).get("temporal") or []
    starts: list[datetime] = []
    ends: list[datetime] = []
    for entry in entries:
        for key, bucket in (("start", starts), ("end", ends)):
            raw = entry.get(key)
            if not raw:
                continue
            try:
                value = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            bucket.append(value)
    return (
        min(starts) if starts else None,
        max(ends) if ends else None,
    )


def temporal_overlap(
    a_start: datetime | None,
    a_end: datetime | None,
    b_start: datetime | None,
    b_end: datetime | None,
) -> bool:
    """Half-open interval overlap with open-ended intervals."""
    return not (
        (a_start is not None and b_end is not None and a_start >= b_end)
        or (b_start is not None and a_end is not None and b_start >= a_end)
    )
