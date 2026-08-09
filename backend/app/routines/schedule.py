"""Minimal, dependency-free 5-field cron parsing and next-run computation.

Fields: minute hour day-of-month month day-of-week.
Day-of-week is 0-6 with Sunday = 0 (7 is also accepted as Sunday).  The
standard cron rule applies when both day fields are restricted: a day matches
if *either* day-of-month or day-of-week matches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

MINUTE = (0, 59)
HOUR = (0, 23)
DOM = (1, 31)
MONTH = (1, 12)
DOW = (0, 6)


def _parse_field(field: str, lo: int, hi: int, *, dow: bool = False) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty cron field component in {field!r}")
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            step = int(step_text)
            if step <= 0:
                raise ValueError(f"Invalid cron step in {part!r}")
        else:
            base = part

        def _normalize(value: int) -> int:
            if dow and value == 7:
                return 0
            return value

        if base == "*":
            values.update(range(lo, hi + 1, step))
            continue
        if "-" in base:
            start_text, _, end_text = base.partition("-")
            start = _normalize(int(start_text))
            end = _normalize(int(end_text))
            if not (lo <= start <= hi and lo <= end <= hi):
                raise ValueError(f"Cron field {base!r} out of range")
            values.update(range(start, end + 1, step))
            continue
        value = _normalize(int(base))
        if not lo <= value <= hi:
            raise ValueError(f"Cron field {base!r} out of range")
        values.add(value)
    if not values:
        raise ValueError(f"Empty cron field {field!r}")
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = [part.strip() for part in expr.split()]
    if len(fields) != 5:
        raise ValueError("Cron expression must have exactly 5 fields")
    minutes = _parse_field(fields[0], *MINUTE)
    hours = _parse_field(fields[1], *HOUR)
    doms = _parse_field(fields[2], *DOM)
    months = _parse_field(fields[3], *MONTH)
    dows = _parse_field(fields[4], *DOW, dow=True)
    return minutes, hours, doms, months, dows


def validate_cron(expr: str) -> None:
    """Raise ValueError if ``expr`` is not a valid 5-field cron expression."""
    parse_cron(expr)


def next_run_after(
    expr: str,
    after: datetime,
    *,
    timezone: str = "UTC",
    max_days: int = 366,
) -> datetime | None:
    """Return the next UTC occurrence strictly after ``after`` (or None)."""
    minutes, hours, doms, months, dows = parse_cron(expr)
    try:
        tz = ZoneInfo(timezone)
    except Exception as exc:  # noqa: BLE001 - invalid tz is a configuration error
        raise ValueError(f"Unknown timezone {timezone!r}") from exc

    local_after = after.astimezone(tz)
    all_doms = set(range(DOM[0], DOM[1] + 1))
    all_dows = set(range(DOW[0], DOW[1] + 1))
    dom_restricted = doms != all_doms
    dow_restricted = dows != all_dows

    for offset in range(max_days + 1):
        day = local_after.date() + timedelta(days=offset)
        if day.month not in months:
            continue
        cron_weekday = (day.weekday() + 1) % 7
        if dom_restricted and dow_restricted:
            if day.day not in doms and cron_weekday not in dows:
                continue
        elif dom_restricted:
            if day.day not in doms:
                continue
        elif dow_restricted:
            if cron_weekday not in dows:
                continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                candidate = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=tz
                )
                if candidate > local_after:
                    return candidate.astimezone(UTC)
    return None
