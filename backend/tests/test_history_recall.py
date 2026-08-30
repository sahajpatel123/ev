"""recall_history — chunked past-history retrieval (EV VOICE CONTROL PLAN §2).

Offline-safe: SQLite + hash embeddings via conftest. No network, no weights.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.tools import _handle, dispatch
from app.memory.history import (
    decode_cursor,
    encode_cursor,
    recall_history,
    truncate_brief,
)
from app.utils.text import utcnow


async def _capture(client, text: str, *, privacy: str = "normal", occurred_at=None) -> str:
    payload = {
        "source": "test",
        "event_type": "note",
        "text": text,
        "privacy_level": privacy,
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at.isoformat()
    response = await client.post("/v1/events", json=payload)
    assert response.status_code in {200, 201}, response.text
    return str(response.json()["event"]["id"])


async def test_recall_history_basic_decision(db_session: AsyncSession, client) -> None:
    await _capture(client, "I decided to move the project to SQLite for local testing")
    payload = await recall_history(
        db_session, "why did I choose SQLite", k=8, chunk_mode="brief"
    )
    assert payload["ok"] is True
    assert payload["count"] >= 1
    assert payload["total"] >= 1
    first = payload["results"][0]
    assert first["memory_type"] == "decision"
    assert first["source_event_ids"]
    assert "SQLite" in first["text"]
    assert first["date"] is not None
    assert "components" in first and "semantic" in first["components"]


async def test_recall_history_memory_type_filter(db_session: AsyncSession, client) -> None:
    await _capture(client, "I decided to buy the road bike")
    await _capture(client, "I prefer morning runs over evening runs")
    decision = await recall_history(db_session, "bike", memory_type="decision")
    preference = await recall_history(db_session, "bike", memory_type="preference")
    assert decision["count"] >= 1
    assert all(r["memory_type"] == "decision" for r in decision["results"])
    assert all(r["memory_type"] == "preference" for r in preference["results"])


async def test_recall_history_time_window(db_session: AsyncSession, client) -> None:
    old = utcnow() - timedelta(days=200)
    await _capture(client, "I decided to take up sailing lessons", occurred_at=old)
    await _capture(client, "I decided to take up piano lessons")
    recent = await recall_history(db_session, "lessons", time_range="recent_week")
    all_time = await recall_history(db_session, "lessons", time_range="all_time")
    assert all_time["total"] >= 2
    assert 0 <= recent["total"] < all_time["total"]
    assert all(
        datetime.fromisoformat(r["date"]).replace(tzinfo=UTC) >= utcnow() - timedelta(days=7)
        for r in recent["results"]
    )


async def test_recall_history_custom_dates(db_session: AsyncSession, client) -> None:
    inside = utcnow() - timedelta(days=30)
    outside = utcnow() - timedelta(days=120)
    await _capture(client, "I decided to reorganize the garage", occurred_at=inside)
    await _capture(client, "I decided to reorganize the attic", occurred_at=outside)
    window = await recall_history(
        db_session,
        "reorganize",
        start_date=(utcnow() - timedelta(days=60)).date().isoformat(),
        end_date=utcnow().date().isoformat(),
    )
    assert window["total"] == 1
    assert "garage" in window["results"][0]["text"]


async def test_recall_history_as_of_is_accepted(db_session: AsyncSession, client) -> None:
    await _capture(client, "I decided to adopt the new routing policy")
    payload = await recall_history(
        db_session,
        "routing policy",
        as_of=(utcnow() + timedelta(days=1)).isoformat(),
    )
    assert payload["ok"] is True
    assert payload["as_of"] is not None
    assert payload["total"] >= 1


async def test_recall_history_cursor_pagination(db_session: AsyncSession, client) -> None:
    # Deliberately distinct topics: the versioned writer supersedes repeated
    # same-subject decisions, so page sizes need truly separate memories.
    topics = (
        "I decided to repaint the front door",
        "I decided to learn the banjo",
        "I decided to adopt a rescue cat",
        "I decided to buy the standing desk",
        "I decided to rename the project Phoenix",
    )
    for text in topics:
        await _capture(client, text)
    page1 = await recall_history(db_session, "decided to", k=2)
    assert page1["ok"] is True
    assert page1["count"] == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"]
    page2 = await recall_history(
        db_session, "decided to", k=2, cursor=page1["next_cursor"]
    )
    assert page2["count"] == 2
    assert page2["offset"] == 2
    assert page2["next_cursor"]
    page3 = await recall_history(
        db_session, "decided to", k=2, cursor=page2["next_cursor"]
    )
    assert page3["count"] >= 1
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None
    seen = {r["memory_id"] for r in page1["results"] + page2["results"] + page3["results"]}
    assert len(seen) == 5


async def test_recall_history_cursor_mismatch_rejected(db_session: AsyncSession, client) -> None:
    await _capture(client, "I decided to try recovery numbering")
    await _capture(client, "I decided to reorganize the workshop")
    page1 = await recall_history(db_session, "recovery numbering", k=1)
    assert page1["next_cursor"]
    # A cursor belongs to exactly one query/parameter series.
    other = await recall_history(db_session, "reorganize the workshop", k=1)
    assert other["next_cursor"]
    mismatch = await recall_history(
        db_session, "recovery numbering", k=1, cursor=other["next_cursor"]
    )
    assert mismatch["ok"] is False
    assert mismatch["error"] == "invalid_cursor"
    bad = await recall_history(db_session, "recovery numbering", k=1, cursor="not-a-cursor")
    assert bad["ok"] is False
    assert bad["error"] == "invalid_cursor"


async def test_recall_history_chunk_modes(db_session: AsyncSession, client) -> None:
    long_text = (
        "I decided that the whole home workshop project should switch from "
        "pre-printed shelving to modular aluminium framing with adjustable "
        "brackets, because the old layout kept wasting vertical space and "
        "made every reconfiguration a full disassembly job."
    )
    await _capture(client, long_text)
    brief = await recall_history(db_session, "modular aluminium framing", chunk_mode="brief")
    full = await recall_history(db_session, "modular aluminium framing", chunk_mode="full")
    assert brief["count"] >= 1
    assert full["count"] >= 1
    assert len(brief["results"][0]["text"]) < len(long_text)
    assert brief["results"][0]["text"].endswith("…")
    # The typed memory text carries a "Decided: …" prefix; the full chunk
    # keeps the complete statement.
    assert "whole home workshop project should switch" in full["results"][0]["text"]


async def test_recall_history_privacy_boundary(db_session: AsyncSession, client) -> None:
    await _capture(
        client,
        "my credit card number is 4242 4242 4242 4242",
        privacy="never_send_to_model",
    )
    await _capture(client, "I decided to split the bill with cash")
    payload = await recall_history(db_session, "credit card number", k=10)
    assert payload["ok"] is True
    combined = " ".join(r["text"] for r in payload["results"])
    assert "4242" not in combined


async def test_recall_history_empty_query(db_session: AsyncSession) -> None:
    payload = await recall_history(db_session, "   ")
    assert payload["ok"] is False
    assert payload["error"] == "empty_query"


async def test_recall_history_dispatch_integration(db_session: AsyncSession, client) -> None:
    """End-to-end through the canonical dispatch boundary (authz + shape check)."""
    await _capture(client, "I decided to switch the notebook brand")
    response = await dispatch(
        db_session,
        "recall_history",
        {"query": "notebook brand", "chunk_mode": "brief", "k": 3},
        actor="master",
    )
    assert response.ok is True
    assert response.result is not None
    assert "results" in response.result
    assert response.result["count"] >= 1


async def test_recall_history_via_handle_spec_shape(db_session: AsyncSession, client) -> None:
    """_handle path used by live voice dispatch."""
    await _capture(client, "I decided to learn the fiddle")
    payload = await _handle(
        db_session, "recall_history", {"query": "fiddle"}, actor="master"
    )
    assert payload["count"] >= 1
    assert payload["results"][0]["memory_type"] == "decision"


def test_cursor_helpers() -> None:
    fp = "abc123"
    cursor = encode_cursor(8, fp)
    assert decode_cursor(cursor, fp) == 8
    assert decode_cursor(cursor, "other") is None
    assert decode_cursor("garbage!!", fp) is None
    assert decode_cursor("", fp) is None
    assert decode_cursor(cursor, fp, ) == 8  # noqa: PIE804 - readability


def test_truncate_brief() -> None:
    short = "short text"
    assert truncate_brief(short) == short
    long_text = "word " * 100
    cut = truncate_brief(long_text)
    assert len(cut) <= 210
    assert cut.endswith("…")
    assert "  " not in cut


async def test_recall_history_overlapping_queries_have_distinct_cursors(
    db_session: AsyncSession, client
) -> None:
    """Two different queries must never accept each other's cursors."""
    for index in range(3):
        await _capture(client, f"I decided to adopt policy alpha {index}")
        await _capture(client, f"I decided to adopt policy beta {index}")
    page_a = await recall_history(db_session, "policy alpha", k=1)
    page_b = await recall_history(db_session, "policy beta", k=1)
    assert page_a["next_cursor"] and page_b["next_cursor"]
    crossed = await recall_history(
        db_session, "policy alpha", k=1, cursor=page_b["next_cursor"]
    )
    assert crossed["ok"] is False
    assert crossed["error"] == "invalid_cursor"