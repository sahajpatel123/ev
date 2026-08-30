"""G2 P0.2 — EVENT STREAM ORDERING contract tests (PART 10-18).

LAWS UNDER TEST:
- occurred_at = semantic time; events.stream_seq = delivery position in the
  current lineage. They are independent truths.
- A late-arriving event with a HISTORICAL occurred_at but a NEW stream_seq
  MUST be delivered to any client whose cursor predates its import.
- Same-timestamp events get unique monotonic positions.
- Duplicate stream_seq is impossible (unique index).
- v2 cursors only; legacy shapes reset.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.auth import ActorContext
from app.everywhere.sync import (
    changes,
    format_v2_cursor,
    parse_cursor,
    state_epoch,
)
from app.models import Event

MASTER = "master"

def _master_ctx():
    return ActorContext(actor=MASTER, is_master=True)

MASTER_CTX = None


async def _insert_event(session, *, etype: str, at_iso: str, seq: int | None) -> Event:
    dt = datetime.fromisoformat(at_iso)
    row = Event(
        source="everywhere",
        event_type=etype,
        content={"title": etype},
        privacy_level="normal",
        sha256=etype + at_iso + str(seq),
        occurred_at=dt,
        ingested_at=dt,
        stream_seq=seq,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_late_arrival_historical_event_is_delivered(db_session):
    """PART 10 — THE regression this pass exists for.

    Client receives cursor N. Later, an authentic historical event with an
    occurred_at BEFORE the cursor's own timestamp enters the stream. The
    next changes(cursor=N) MUST deliver it (old time-keyed cursors missed it).
    """
    # Existing stream: one current event -> cursor after it.
    await _insert_event(
        db_session, etype="note", at_iso="2026-08-25T10:00:00+00:00", seq=900
    )
    await db_session.commit()
    epoch = await state_epoch(db_session)
    cursor_n = format_v2_cursor(epoch, 900)

    # LATE ARRIVAL: authentic history from YESTERDAY, imported TODAY with a
    # NEW stream position (901) — exactly what the recovery importer does.
    late = await _insert_event(
        db_session,
        etype="project.created",
        at_iso="2026-08-24T20:00:00+00:00",
        seq=901,
    )
    await db_session.commit()

    delta = await changes(db_session, _master_ctx(), cursor=cursor_n)
    types = [(e["type"], e["id"]) for e in delta["events"]]
    assert ("project.created", str(late.id)) in types, (
        "late-arriving historical event must be delivered after cursor N"
    )

    # And it must NOT have been delivered by the pre-import cursor either
    # way around ordering-by-time would break: verify ascending seq order.
    seqs = [e.get("stream_seq") for e in delta["events"]]
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_same_timestamp_events_get_unique_ordered_positions(db_session):
    """PART 11 — identical occurred_at values cannot collide or be skipped."""
    at = "2026-08-25T09:00:00+00:00"
    rows = []
    for i in range(5):
        rows.append(
            await _insert_event(
                db_session, etype=f"project.note_same_{i}", at_iso=at, seq=1000 + i
            )
        )
    await db_session.commit()
    cursor = format_v2_cursor(await state_epoch(db_session), 999)
    out = await changes(db_session, _master_ctx(), cursor=cursor, limit=200)
    [e["content"]["title"] for e in out["events"]]
    for i in range(5):
        assert f"note_same_{i}" in " ".join(e["content"].get("title","") for e in out["events"])
    seqs = [e["stream_seq"] for e in out["events"]]
    assert len(set(seqs)) == len(seqs), "duplicate stream_seq delivered"


def test_parse_cursor_versions():
    p = parse_cursor("v2|epoch-uuid|42")
    assert p["kind"] == "v2" and p["seq"] == 42

    legacy = parse_cursor("2026-08-24T20:00:00+00:00|11111111-1111-1111-1111-111111111111")
    assert legacy["kind"] == "legacy"

    v1 = parse_cursor("epoch-uuid|2026-08-24T20:00:00+00:00|22222222-2222-2222-2222-222222222222")
    assert v1["kind"] == "v1"

    assert parse_cursor(None)["kind"] == "none"
    assert parse_cursor("garbage") == "invalid"


@pytest.mark.asyncio
async def test_duplicate_stream_seq_rejected_by_unique_index(db_session):
    await _insert_event(
        db_session, etype="project.dup_a", at_iso="2026-08-25T09:30:00+00:00", seq=7777
    )
    await db_session.commit()
    b = Event(
        source="everywhere",
        event_type="project.dup_b",
        content={"title": "dup"},
        privacy_level="normal",
        sha256="dup-sha",
        occurred_at=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        ingested_at=datetime(2026, 8, 25, 9, 31, tzinfo=UTC),
        stream_seq=7777,  # collision
    )
    db_session.add(b)
    try:
        await db_session.commit()
        raised = False
    except Exception:
        await db_session.rollback()
        raised = True
    assert raised, "unique index must reject duplicate stream_seq"


@pytest.mark.asyncio
async def test_concurrent_position_assignment_is_unique(db_session):
    """PART 12: two turns inserting without explicit seq still receive
    distinct positions via the allocator (sequence on PG / MAX+1 on tests)."""
    e1 = Event(
        source="everywhere", event_type="note.c1", content={"i": 1},
        privacy_level="normal", sha256="c1",
        occurred_at=datetime(2026, 8, 25, 9, 40, tzinfo=UTC), ingested_at=datetime(2026, 8, 25, 9, 40, tzinfo=UTC),
    )
    e2 = Event(
        source="everywhere", event_type="note.c2", content={"i": 2},
        privacy_level="normal", sha256="c2",
        occurred_at=datetime(2026, 8, 25, 9, 41, tzinfo=UTC), ingested_at=datetime(2026, 8, 25, 9, 41, tzinfo=UTC),
    )
    db_session.add(e1)
    await db_session.commit()
    db_session.add(e2)
    await db_session.commit()
    assert e1.stream_seq is not None and e2.stream_seq is not None
    assert e1.stream_seq != e2.stream_seq
