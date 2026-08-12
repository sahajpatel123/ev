"""Temporal expressions resolve to absolute timestamps (plan 2.5 / as-of)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.memory.temporal import resolve_temporal_expressions, temporal_bounds


def _anchor() -> datetime:
    # Wednesday, 2026-08-12
    return datetime(2026, 8, 12, 9, 30, tzinfo=UTC)


def test_last_tuesday_resolves_to_previous_week() -> None:
    results = resolve_temporal_expressions("We met last Tuesday for coffee.", _anchor())
    assert results
    resolved = results[0]
    assert resolved.start.date().isoformat() == "2026-08-04"  # previous Tuesday
    assert resolved.end.date().isoformat() == "2026-08-05"


def test_this_and_next_weekday() -> None:
    this_tuesday = resolve_temporal_expressions("Session is this Tuesday.", _anchor())
    assert this_tuesday[0].start.date().isoformat() == "2026-08-11"
    next_tuesday = resolve_temporal_expressions("Session is next Tuesday.", _anchor())
    assert next_tuesday[0].start.date().isoformat() == "2026-08-18"


def test_in_march_resolves_to_month_range() -> None:
    results = resolve_temporal_expressions("In March I want to focus on health.", _anchor())
    assert results
    resolved = results[0]
    # Unqualified "March" resolves to the next occurrence (deterministic rule).
    assert resolved.start.date().isoformat() == "2027-03-01"
    assert resolved.end.date().isoformat() == "2027-04-01"


def test_last_march_resolves_to_past_month() -> None:
    resolved = resolve_temporal_expressions("Last March I was running daily.", _anchor())[0]
    assert resolved.start.date().isoformat() == "2026-03-01"
    assert resolved.end.date().isoformat() == "2026-04-01"


def test_named_month_with_year() -> None:
    results = resolve_temporal_expressions("Back in March 2025 I was running daily.", _anchor())
    resolved = results[0]
    assert resolved.start.date().isoformat() == "2025-03-01"
    assert resolved.end.date().isoformat() == "2025-04-01"


def test_two_years_running_is_a_range() -> None:
    results = resolve_temporal_expressions("I have been learning piano for two years running.", _anchor())
    assert results
    resolved = results[0]
    assert resolved.kind == "range"
    assert resolved.start is not None
    assert resolved.end is not None
    assert resolved.start.date().isoformat() == "2024-08-12"
    assert resolved.end.date().isoformat() == "2026-08-12"


def test_in_the_last_three_months() -> None:
    start, end = temporal_bounds("I have changed jobs in the last three months.", _anchor())
    assert start is not None and end is not None
    assert start.date().isoformat() == "2026-05-12"
    assert end.date().isoformat() == "2026-08-12"


def test_yesterday_today_tomorrow() -> None:
    assert temporal_bounds("That happened yesterday.", _anchor())[0].date().isoformat() == "2026-08-11"
    assert temporal_bounds("That happened today.", _anchor())[0].date().isoformat() == "2026-08-12"
    assert temporal_bounds("That happens tomorrow.", _anchor())[0].date().isoformat() == "2026-08-13"


def test_relative_week_and_year() -> None:
    last_week = resolve_temporal_expressions("Last week was rough.", _anchor())[0]
    assert last_week.start.date().isoformat() == "2026-08-03"
    assert last_week.end.date().isoformat() == "2026-08-10"
    this_year = resolve_temporal_expressions("This year has been busy.", _anchor())[0]
    assert this_year.start.date().isoformat() == "2026-01-01"
    assert this_year.end.date().isoformat() == "2027-01-01"


def test_since_year_is_open_ended() -> None:
    results = resolve_temporal_expressions("I have lived here since 2023.", _anchor())
    resolved = results[0]
    assert resolved.start.date().isoformat() == "2023-01-01"
    assert resolved.end is None


def test_extraction_embeds_temporal_payload() -> None:
    from app.models import Event

    event = Event(
        source="test",
        event_type="note",
        content={"text": "In March I want to focus on health."},
        occurred_at=_anchor(),
        sha256="0" * 64,
    )
    from app.memory.extraction import Extractor

    candidates = Extractor().extract(event)
    temporal = [c for c in candidates if c.payload.get("temporal")]
    assert temporal
    assert temporal[0].payload["temporal"][0]["start"] == "2027-03-01T00:00:00+00:00"
