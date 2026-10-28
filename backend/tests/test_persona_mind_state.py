"""State math: decay, clamps, seeding, expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.persona_mind.reflect import seed_opinions
from app.persona_mind.state import (
    apply_decay,
    blank_affect,
    clamp01,
    coerce_ts,
    decayed_vector,
    domain_rate,
    expire_items,
    has_positive_since,
    new_state,
    record_reaction,
)
from app.persona_mind.temperament import BASELINE


def test_clamp01_bounds():
    assert clamp01(-0.5) == 0.0
    assert clamp01(1.5) == 1.0
    assert clamp01(0.4) == 0.4


def test_decay_zero_elapsed_is_identity():
    vector = {d: 0.9 for d in BASELINE}
    assert decayed_vector(vector, BASELINE, 0.0) == vector


def test_decay_half_life_6h():
    vector = {d: 1.0 for d in BASELINE}
    half = decayed_vector(vector, BASELINE, 6 * 3600)
    for dim, base in BASELINE.items():
        assert half[dim] == abs(base + (1.0 - base) * 0.5)


def test_decay_converges_to_baseline():
    vector = {d: 0.0 for d in BASELINE}
    late = decayed_vector(vector, BASELINE, 72 * 3600)
    for dim, base in BASELINE.items():
        assert abs(late[dim] - base) < 0.01


def test_decay_never_leaves_unit_interval():
    vector = {d: 2.0 for d in BASELINE}
    out = decayed_vector(vector, BASELINE, 3600.0)
    assert all(0.0 <= v <= 1.0 for v in out.values())


def test_apply_decay_uses_timestamp():
    now = datetime.now(UTC)
    state = new_state("o", now - timedelta(hours=6))
    state["affect"] = {d: 1.0 for d in BASELINE}
    out = apply_decay(state, now)
    for dim, base in BASELINE.items():
        assert abs(out[dim] - (base + (1.0 - base) * 0.5)) < 1e-9


def test_seed_opinions_from_legacy_tables():
    from app.ev.dynamic_personality import STABLE_TASTE, STANCES

    state = new_state("o")
    assert seed_opinions(state) is True
    assert seed_opinions(state) is False  # idempotent
    assert len(state["opinions"]) == len(STANCES) + len(STABLE_TASTE)
    assert all(op["confidence"] == 0.6 for op in state["opinions"])
    topics = {op["topic"] for op in state["opinions"]}
    assert "sleep" in topics and "directness" in topics


def test_expire_items_drops_stale():
    now = datetime.now(UTC)
    state = new_state("o", now)
    state["intentions"] = [
        {"text": "old", "expires_at": (now - timedelta(hours=73)).isoformat()},
        {"text": "fresh", "expires_at": (now + timedelta(hours=1)).isoformat()},
    ]
    state["threads"] = [
        {"text": "old", "expires_at": (now - timedelta(days=8)).isoformat()},
        {"text": "fresh", "expires_at": (now + timedelta(days=1)).isoformat()},
    ]
    dropped = expire_items(state, now)
    assert dropped == {"intentions": 1, "threads": 1, "attitudes": 0}
    assert [i["text"] for i in state["intentions"]] == ["fresh"]
    assert [t["text"] for t in state["threads"]] == ["fresh"]


def test_reaction_log_positive_since():
    now = datetime.now(UTC)
    state = new_state("o", now)
    early = (now - timedelta(hours=1)).isoformat()
    late = now.isoformat()
    record_reaction(state, "t1", "neutral", early)
    assert has_positive_since(state, "t0", early) is False
    record_reaction(state, "t2", "positive", late)
    assert has_positive_since(state, "t0", early) is True
    assert has_positive_since(state, "t9", (now + timedelta(hours=1)).isoformat()) is False
    record_reaction(state, "t3", "bogus", late)  # ignored
    assert len(state["reaction_log"]) == 2


def test_domain_rate_none_until_data():
    state = new_state("o")
    assert domain_rate(state, "testing") is None
    state["domains"] = {"testing": {"hit": 3, "total": 4}}
    assert domain_rate(state, "testing") == 0.75


def test_blank_affect_matches_baseline():
    assert blank_affect() == BASELINE


def test_coerce_ts_handles_naive_and_garbage():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert coerce_ts(naive).tzinfo is not None
    assert coerce_ts("not-a-time").tzinfo is not None  # falls back to now
