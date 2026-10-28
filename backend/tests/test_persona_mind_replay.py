"""Replay end to end against a temporary event-DB fixture."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


@pytest.fixture()
def mind_db(tmp_path, monkeypatch):
    db = tmp_path / "mind.sqlite"
    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(db))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", True)
    monkeypatch.setenv("EV_PERSONA_MIND_NO_WORKER", "1")
    from app.persona_mind import fast, store

    store.reset_connections()
    fast.invalidate()
    yield db
    store.reset_connections()
    fast.invalidate()


def _make_event_db(path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Event, EventStreamPosition

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    async def build():
        async with engine.begin() as conn:
            await conn.run_sync(Event.__table__.create, checkfirst=True)
            await conn.run_sync(EventStreamPosition.__table__.create, checkfirst=True)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        thread = uuid4()
        now = datetime.now(UTC)
        async with maker() as session:
            session.add_all([
                Event(source="test", event_type="message.user",
                      content={"text": "run the tests"}, sha256="a",
                      conversation_id=thread,
                      occurred_at=now - timedelta(days=2)),
                Event(source="test", event_type="message.assistant",
                      content={"text": "On it, green so far"}, sha256="b",
                      conversation_id=thread,
                      occurred_at=now - timedelta(days=2) + timedelta(minutes=1)),
                Event(source="test", event_type="message.user",
                      content={"text": "haha good one"}, sha256="c",
                      conversation_id=thread,
                      occurred_at=now - timedelta(days=1)),
                Event(source="test", event_type="message.assistant",
                      content={"text": "Glad it landed"}, sha256="d",
                      conversation_id=thread,
                      occurred_at=now - timedelta(days=1) + timedelta(minutes=1)),
                Event(source="test", event_type="message.user",
                      content={"text": "ancient history"}, sha256="e",
                      conversation_id=thread,
                      occurred_at=now - timedelta(days=60)),
            ])
            await session.commit()
        return maker

    maker = asyncio.run(build())
    return maker, engine


def test_replay_since_end_to_end(mind_db, tmp_path, monkeypatch):
    maker, engine = _make_event_db(tmp_path / "events.sqlite")
    monkeypatch.setattr("app.db.SessionLocal", maker)
    try:
        from app.persona_mind import store
        from app.persona_mind.replay import main

        since = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        assert main(["--since", since, "--limit", "50"]) == 0
        # 4 recent events -> 2 turns; the 60-day-old user message is excluded
        # (and unpaired assistant-less besides).
        assert store.queued_count() + 2 >= 2
        row = store.get_state("owner")
        assert row is not None  # drain committed state
        assert len(row["data"].get("recent_replies") or []) == 2
    finally:
        asyncio.run(engine.dispose())


def test_replay_refuses_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", False)
    from app.persona_mind.replay import main

    assert main(["--since", "2026-01-01"]) == 2
    assert "MIND_ENABLED is False" in capsys.readouterr().err
