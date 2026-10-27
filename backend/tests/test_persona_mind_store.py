"""Store + worker: restart survival, single-worker lock, triggers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.persona_mind.types import TurnRecord


@pytest.fixture()
def mind_db(tmp_path, monkeypatch):
    db = tmp_path / "mind.sqlite"
    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(db))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", True)
    from app.persona_mind import fast, store

    store.reset_connections()
    fast.invalidate()
    yield db
    store.reset_connections()
    fast.invalidate()


def _record(i: int, **over) -> TurnRecord:
    base = {
        "owner_id": "o", "thread_id": "t", "turn_id": f"k{i}",
        "ts": datetime.now(UTC),
        "owner_msg": f"message {i}", "evie_reply": f"reply {i}",
        "channel": "text", "audience": "private",
        "tone": {"roast": 1}, "reaction_to_prev": "neutral",
        "receipts": (), "now_line": None,
    }
    base.update(over)
    return TurnRecord(**base)


def test_queue_survives_restart(mind_db):
    from app.persona_mind import observe, store

    observe(_record(1))
    observe(_record(2))
    assert store.queued_count() == 2
    store.reset_connections()  # simulate process restart
    assert store.queued_count() == 2
    turns = store.queued_turns()
    assert [t["turn_id"] for t in turns] == ["k1", "k2"]
    assert turns[0]["owner_msg"] == "message 1"


def test_recent_done_turns_roundtrip(mind_db):
    from app.persona_mind import observe, store

    observe(_record(1))
    observe(_record(2))
    assert store.recent_done_turns("o") == []
    store.mark_done(["k1", "k2"])
    done = store.recent_done_turns("o")
    assert [t["turn_id"] for t in done] == ["k1", "k2"]
    assert store.queued_count() == 0


def test_duplicate_turn_id_ignored(mind_db):
    from app.persona_mind import observe, store

    observe(_record(1))
    observe(_record(1))
    assert store.queued_count() == 1


def test_single_worker_lock_holds(mind_db, tmp_path):
    from app.persona_mind.paths import lock_path
    from app.persona_mind.worker import _release, _try_acquire

    assert str(lock_path()).endswith(".lock")
    first = _try_acquire(lock_path())
    assert first is not None
    second = _try_acquire(lock_path())
    assert second is None  # same process, second acquisition refused
    _release(first)
    third = _try_acquire(lock_path())
    assert third is not None
    _release(third)


def test_worker_thread_singleton(mind_db):
    from app.persona_mind.worker import start_worker, stop_worker, worker_running

    try:
        assert start_worker() is True
        assert worker_running() is True
        assert start_worker() is True  # idempotent, still one thread
    finally:
        stop_worker()
    assert worker_running() is False


def test_worker_refuses_when_disabled(tmp_path, monkeypatch):
    from app.persona_mind.worker import start_worker

    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(tmp_path / "m.sqlite"))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", False)
    assert start_worker() is False


def test_drain_triggers_on_three_routine_turns(mind_db):
    from app.persona_mind import observe
    from app.persona_mind.worker import drain_once

    observe(_record(1))
    observe(_record(2))
    assert drain_once()["reflected"] == 0  # below batch, none salient
    observe(_record(3))
    assert drain_once()["reflected"] == 3


def test_drain_triggers_on_salient_turn(mind_db):
    from app.persona_mind import observe
    from app.persona_mind.worker import drain_once

    observe(_record(1, reaction_to_prev="positive"))
    assert drain_once()["reflected"] == 1


def test_drain_triggers_on_failed_receipt(mind_db):
    from app.persona_mind import observe
    from app.persona_mind.worker import drain_once

    observe(_record(1, receipts=(("pytest", False, "AssertionError"),)))
    assert drain_once()["reflected"] == 1


def test_idle_pass_applies_decay(mind_db):

    from app.persona_mind import observe, store
    from app.persona_mind.worker import drain_once

    now = datetime.now(UTC)
    old = now - timedelta(hours=5)
    for i in range(3):
        observe(_record(i, ts=old))
    drain_once(now=old)  # commits state at the old timestamp
    state = store.get_state("o")["data"]
    state["affect"]["energy"] = 1.0
    store.put_state("o", state)
    store.meta_set("last_turn_at", old.isoformat())
    report = drain_once(now=now)
    assert report["idle"] is True
    decayed = store.get_state("o")["data"]["affect"]["energy"]
    assert decayed < 1.0  # moved toward baseline without any brain call


def test_reflection_count_increments(mind_db):
    from app.persona_mind import observe, store
    from app.persona_mind.worker import drain_once

    assert store.reflection_count() == 0
    for i in range(3):
        observe(_record(i))
    drain_once()
    assert store.reflection_count() == 1


def test_salience_reasons():
    from app.persona_mind.reflect import is_salient

    assert is_salient({"receipts": [["t", False, "E"]], "owner_msg": ""}) == "failed_receipt"
    assert is_salient({"receipts": [], "owner_msg": "you were wrong, it failed"}) == "proven_wrong"
    assert is_salient({"receipts": [], "owner_msg": "I disagree with that"}) == "owner_disagreeing"
    assert is_salient({"receipts": [], "owner_msg": "it works! tests green"}) == "big_win"
    assert is_salient({"receipts": [], "owner_msg": "ok", "reaction_to_prev": "negative"}) == "strong_reaction"
    assert is_salient({"receipts": [], "owner_msg": "run the tests"}) is None
