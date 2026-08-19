"""Unit tests for the life I/O resolver (no provider I/O)."""

from datetime import datetime, timedelta, timezone

from app.ev.resolve import (
    is_near_duplicate,
    looks_like_destination,
    parse_owner_when,
    pick_unique,
    rank_plate,
    score_label,
)


def test_score_and_pick_unique_vs_ambiguous() -> None:
    lights = [
        {"id": "light.lab", "name": "lab lights", "area": "lab"},
        {"id": "light.kitchen", "name": "kitchen lights", "area": "kitchen"},
    ]
    unique = pick_unique(
        "the lab lights",
        lights,
        labels=lambda row: [row["id"], row["name"], row["area"]],
    )
    assert unique.unique
    assert unique.item["id"] == "light.lab"
    ambiguous = pick_unique("lights", lights, labels=lambda row: [row["id"], row["name"]])
    assert ambiguous.status == "ambiguous"
    assert len(ambiguous.candidates) == 2
    assert score_label("lab lights", "lab lights") == 1.0


def test_parse_owner_when_relative_and_clock() -> None:
    now = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
    relative = parse_owner_when("in 10 minutes", now=now)
    assert relative == now + timedelta(minutes=10)
    hour = parse_owner_when("in an hour", now=now)
    assert hour == now + timedelta(hours=1)
    tonight = parse_owner_when("tonight at 8", now=now)
    assert tonight is not None
    assert tonight.hour == 20
    assert parse_owner_when("drink water", now=now) is None


def test_near_duplicate_and_destination() -> None:
    assert is_near_duplicate(
        title="Dinner",
        start="2026-08-21T19:00:00+00:00",
        other_title="dinner",
        other_start="2026-08-21T19:10:00+00:00",
    )
    assert not is_near_duplicate(
        title="Dinner",
        start="2026-08-21T19:00:00+00:00",
        other_title="Standup",
        other_start="2026-08-21T19:10:00+00:00",
    )
    assert looks_like_destination("+15551212")
    assert looks_like_destination("ned@example.com")
    assert not looks_like_destination("Ned")


def test_rank_plate_puts_imminent_timer_first() -> None:
    now = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
    ranked = rank_plate(
        calendar=[{"summary": "Dinner", "start": (now + timedelta(hours=8)).isoformat()}],
        mail=[{"subject": "Hello", "unread": False}],
        github=[{"title": "Ship lights", "number": 1}],
        timers=[{"text": "stand up", "fire_at": (now + timedelta(minutes=4)).isoformat()}],
        now=now,
    )
    assert ranked[0]["kind"] == "timer"
    assert ranked[0]["title"] == "stand up"
