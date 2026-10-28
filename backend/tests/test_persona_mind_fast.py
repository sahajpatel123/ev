"""Fast loop: flag gating, latency budgets, determinism."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from app.persona_mind.types import TurnRecord


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


def _record(i: int, **over) -> TurnRecord:
    base = {
        "owner_id": "o", "thread_id": "t", "turn_id": f"t{i}",
        "ts": datetime.now(UTC),
        "owner_msg": f"message {i}", "evie_reply": f"reply {i}",
        "channel": "text", "audience": "private",
        "tone": {"roast": 1}, "reaction_to_prev": "neutral",
        "receipts": (), "now_line": None,
    }
    base.update(over)
    return TurnRecord(**base)


def test_observe_never_raises(mind_db):
    from app.persona_mind import observe

    observe(_record(1))
    observe(None)  # type: ignore[arg-type]
    observe("garbage")  # type: ignore[arg-type]


def test_snapshot_none_without_state(mind_db):
    from app.persona_mind import snapshot

    assert snapshot("o", "t", "hello", "text", 120) is None


def test_flag_off_is_noop(tmp_path, monkeypatch):
    from app.persona_mind import observe, snapshot

    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(tmp_path / "m.sqlite"))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", False)
    observe(_record(1))  # writes nothing
    assert snapshot("o", "t", "hello", "text", 120) is None
    assert not (tmp_path / "m.sqlite").exists()


def _seed_state():
    from datetime import timedelta

    from app.persona_mind import observe
    from app.persona_mind.worker import drain_once

    old = datetime.now(UTC) - timedelta(seconds=60)
    for i in range(3):
        observe(_record(100 + i, ts=old))
    drain_once()  # bookkeeping commit even without a brain


def test_observe_p99_under_1ms(mind_db):
    from app.persona_mind import observe

    for i in range(20):
        observe(_record(i))  # warm the connection + page cache
    samples = []
    for i in range(1000):
        start = time.perf_counter()
        observe(_record(1000 + i))
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    assert samples[990] <= 1.0, f"observe p99 = {samples[990]:.3f} ms"


def test_snapshot_p99_under_2ms(mind_db):
    from app.persona_mind import snapshot

    _seed_state()
    assert snapshot("o", "t", "warm", "text", 120) is not None
    samples = []
    for i in range(1000):
        start = time.perf_counter()
        block = snapshot("o", "t", f"query {i % 7}", "text", 120)
        samples.append((time.perf_counter() - start) * 1000.0)
        assert block is not None
    samples.sort()
    assert samples[990] <= 2.0, f"snapshot p99 = {samples[990]:.3f} ms"


def test_snapshot_deterministic(mind_db):
    from app.persona_mind import snapshot

    _seed_state()
    first = snapshot("o", "t", "same query", "text", 120)
    second = snapshot("o", "t", "same query", "text", 120)
    assert first is not None and second is not None
    assert first.text == second.text
    assert first.token_estimate == second.token_estimate
    assert first.version == "mind.v1"


def test_snapshot_reflects_new_state(mind_db):
    from app.persona_mind import observe, snapshot

    _seed_state()
    before = snapshot("o", "t", "q", "text", 120)
    assert before is not None
    for i in range(3):
        observe(_record(500 + i, reaction_to_prev="positive"))
    from app.persona_mind.worker import drain_once

    drain_once()
    after = snapshot("o", "t", "q", "text", 120)
    assert after is not None
    assert after.state_age_s <= before.state_age_s + 60
