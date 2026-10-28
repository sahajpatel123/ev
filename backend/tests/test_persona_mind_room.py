"""Room: owner baselines, pace/focus rules, ROOM line, attitudes, tunables."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.persona_mind.render import estimate


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


def _at(hour: int, minute: int = 0, day_offset: int = 0):
    base = datetime.now().astimezone().replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return base + timedelta(days=day_offset)


def _record(owner_id, turn_id, ts, owner_msg, receipts=()):
    from app.persona_mind.types import TurnRecord

    return TurnRecord(
        owner_id=owner_id, thread_id="t", turn_id=turn_id, ts=ts,
        owner_msg=owner_msg, evie_reply="ok", channel="text",
        audience="private", tone={}, reaction_to_prev="neutral",
        receipts=tuple(receipts), now_line=None,
    )


def test_baselines_default_until_20(mind_db):
    from app.persona_mind.room import read_room

    reading = read_room("o", "hello there", now=_at(14))
    assert reading.features["baseline_n"] == 0
    assert reading.features["len_z"] == 0.0
    assert reading.pace == "normal"


def test_record_builds_baseline(mind_db):
    from app.persona_mind.room import read_room

    start = _at(14) - timedelta(minutes=60)
    for i in range(25):
        read_room("o", f"message number {i} here", now=start + timedelta(minutes=i),
                  record=True)
    reading = read_room("o", "x", now=start + timedelta(minutes=26))
    assert reading.features["baseline_n"] == 25
    assert reading.features["len_z"] < -1.0  # much shorter than his norm


def test_blitz_on_burst_and_urgency(mind_db):
    from app.persona_mind.room import read_room

    base = _at(14)
    for i in range(3):
        read_room("o", f"ping {i}", now=base - timedelta(seconds=30 * (3 - i)),
                  record=True)
    reading = read_room("o", "quick, just ship it fast asap", now=base)
    assert reading.pace == "blitz"
    assert reading.features["burst"] is True
    assert "moving fast" in reading.room_line


def test_deep_on_why_and_long(mind_db):
    from app.persona_mind.room import read_room

    long_q = "why does the token refresh path behave this way? explain the trade-offs, compare the options, and think through the edge cases in detail please"
    reading = read_room("o", long_q + " " + long_q, now=_at(14))
    assert reading.pace == "deep"
    assert reading.focus == "exploring"


def test_stuck_on_failed_receipts(mind_db):
    from app.persona_mind import observe
    from app.persona_mind.room import read_room

    base = _at(14)
    observe(_record("o", "f1", base - timedelta(minutes=2), "run it",
                    receipts=(("pytest", False, "AssertionError"),)))
    reading = read_room("o", "still red, what now", now=base)
    assert reading.focus == "stuck"
    assert reading.features["failed_last3"] == 1
    assert "own it in a clause" in reading.room_line


def test_stuck_on_repeat(mind_db):
    from app.persona_mind import observe
    from app.persona_mind.room import read_room

    base = _at(14)
    observe(_record("o", "r1", base - timedelta(minutes=2), "run the tests"))
    reading = read_room("o", "run the tests", now=base)
    assert reading.features["repeat"] is True
    assert reading.focus == "stuck"


def test_winding_down_late(mind_db):
    from app.persona_mind.room import read_room

    reading = read_room("o", "one more thing", now=_at(2, 30))
    assert reading.focus == "winding_down"
    assert reading.features["hour"] == 2 or reading.features["hour"] == 3


def test_typo_z_spikes(mind_db):
    from app.persona_mind.room import read_room, typo_rate

    assert typo_rate("run the tests please") == 0.0
    assert typo_rate("teh hte taht yuo waht") == 1.0
    start = _at(14) - timedelta(minutes=60)
    for i in range(25):
        read_room("o", "please run the test suite now", now=start + timedelta(minutes=i),
                  record=True)
    reading = read_room("o", "teh hte taht suite", now=start + timedelta(minutes=26))
    assert reading.features["typo_z"] > 2.0


def test_room_line_always_fits_budget(mind_db):
    from app.persona_mind.room import ROOM_BUDGET_TOKENS, read_room

    assert estimate(read_room("o", "hi", now=_at(14)).room_line) <= ROOM_BUDGET_TOKENS
    assert estimate(read_room("o", "quick fast asap jaldi abhi", now=_at(2)).room_line) <= ROOM_BUDGET_TOKENS


def test_pace_hint_mapping(mind_db):
    from app.persona_mind.room import pace_hint, read_room

    blitz = read_room("o", "quick fast asap ship it", now=_at(14))
    assert blitz.pace == "blitz"
    hint = pace_hint(blitz)
    assert hint["max_sentences"] == 2
    assert hint["ask_back_allowed"] is False

    deep = read_room("o", "why is this so? explain and compare in depth " * 8, now=_at(14))
    assert deep.pace == "deep"
    hint = pace_hint(deep)
    assert hint["max_sentences"] == 8
    assert hint["ask_back_allowed"] is True


def test_pattern_renders_when_relevant(mind_db):
    from app.persona_mind.room import read_room

    patterns = [{"text": "when he says quick he means one line", "turn_ids": ["t1"]}]
    reading = read_room("o", "he says be quick about it", now=_at(14), patterns=patterns)
    assert "Note:" in reading.room_line
    plain = read_room("o", "tell me about databases", now=_at(14), patterns=patterns)
    assert "Note:" not in plain.room_line


def test_attitude_validation():
    from app.persona_mind.state import new_state
    from app.persona_mind.validate import validate_output

    known = {
        "t1": {"owner_msg": "skip the tests", "evie_reply": "ok",
               "ts": "2026-01-01T00:00:00+00:00", "positive_after": False},
        "t2": {"owner_msg": "skip them again", "evie_reply": "fine",
               "ts": "2026-01-02T00:00:00+00:00", "positive_after": False},
    }
    state = new_state("o")
    good = {"attitude_updates": [{
        "action": "add", "topic": "skipped tests",
        "text": "mildly exasperated that tests keep getting skipped",
        "turn_ids": ["t1", "t2"],
    }]}
    result = validate_output(good, known_turns=known, state=state)
    assert result.rejected == []

    one_turn = {"attitude_updates": [{
        "action": "add", "topic": "x", "text": "mildly annoyed at x",
        "turn_ids": ["t1"],
    }]}
    result = validate_output(one_turn, known_turns=known, state=state)
    assert "2 turns of evidence" in result.rejected[0][2]

    identity = {"attitude_updates": [{
        "action": "add", "topic": "lateness",
        "text": "convinced he is a lazy person", "turn_ids": ["t1", "t2"],
    }]}
    result = validate_output(identity, known_turns=known, state=state)
    assert "who he is" in result.rejected[0][2] or "banned marker" in result.rejected[0][2]

    capital = {"attitude_updates": [{
        "action": "add", "topic": "x", "text": "Mildly annoyed at x",
        "turn_ids": ["t1", "t2"],
    }]}
    result = validate_output(capital, known_turns=known, state=state)
    assert "lowercase" in result.rejected[0][2]


def test_attitude_apply_and_decay():
    from datetime import UTC

    from app.persona_mind.reflect import apply_valid
    from app.persona_mind.state import attitude_strength, new_state

    now = datetime.now(UTC)
    state = new_state("o", now)
    apply_valid(state, {"attitude_updates": [{
        "action": "add", "topic": "skipped tests",
        "text": "mildly exasperated that tests keep getting skipped",
        "turn_ids": ["t1", "t2"],
    }]}, now)
    assert len(state["attitudes"]) == 1
    assert attitude_strength(state["attitudes"][0], now) == 0.4
    week_later = now + timedelta(days=7)
    assert abs(attitude_strength(state["attitudes"][0], week_later) - 0.2) < 1e-9


def test_attitude_renders_only_in_play():
    from datetime import UTC

    from app.persona_mind.render import pick_attitude

    now = datetime.now(UTC)
    attitudes = [{
        "topic": "skipped tests",
        "text": "mildly exasperated that tests keep getting skipped",
        "strength": 0.4, "updated_at": now.isoformat(), "evidence": ["t1", "t2"],
    }]
    assert pick_attitude(attitudes, "should I skip the tests", now) is not None
    assert pick_attitude(attitudes, "what is for lunch", now) is None
    assert pick_attitude(attitudes, "should I skip the tests",
                         now + timedelta(days=60)) is None


def test_owner_patterns_apply_capped():
    from datetime import UTC

    from app.persona_mind.reflect import apply_valid
    from app.persona_mind.state import new_state

    now = datetime.now(UTC)
    state = new_state("o", now)
    apply_valid(state, {"owner_patterns": [
        {"text": f"pattern {i}", "turn_ids": ["t1"]} for i in range(15)
    ]}, now)
    assert len(state["owner_patterns"]) == 12


def test_tune_versions_and_rollback(mind_db):
    from app.persona_mind import tune

    assert tune.ensure_v1() == 1
    assert tune.tunables()["burst_n"] == 3.0
    valid, rejected = tune.validate_tunable_updates(
        {"burst_n": 5.0, "burst_window_s": 5.0, "nope": 1.0})
    assert valid == {"burst_n": 5.0}
    assert len(rejected) == 2
    version = tune.apply_version(tunable_updates=valid, diff="test")
    assert version == 2
    assert tune.tunables()["burst_n"] == 5.0
    assert tune.rollback_to(1) is True
    assert tune.tunables()["burst_n"] == 3.0
    assert tune.rollback_to(99) is False
    assert [h["version"] for h in tune.history()] == [1, 2]
