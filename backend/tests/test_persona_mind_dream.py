"""Dream cycle: review, counterfactuals, distil, canary, versioning. No brain."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def mind_db(tmp_path, monkeypatch):
    db = tmp_path / "mind.sqlite"
    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(db))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", True)
    monkeypatch.setenv("EV_PERSONA_MIND_NO_WORKER", "1")
    monkeypatch.setattr("app.persona_mind.settings.DREAM_ENABLED", True)
    from app.persona_mind import fast, store

    store.reset_connections()
    fast.invalidate()
    yield db
    store.reset_connections()
    fast.invalidate()


def _at(day: str, hour: int, minute: int = 0):
    base = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00")
    return base.astimezone()


def _observe(owner_msg, evie_reply, ts, turn_id, reaction="neutral"):
    from app.persona_mind import observe
    from app.persona_mind.types import TurnRecord

    observe(TurnRecord(
        owner_id="owner", thread_id="t", turn_id=turn_id, ts=ts,
        owner_msg=owner_msg, evie_reply=evie_reply, channel="text",
        audience="private", tone={}, reaction_to_prev=reaction,
        receipts=(), now_line=None,
    ))


def _seed_day(day="2026-03-10"):
    _observe("run the tests", "On it.", _at(day, 10), "d01")
    _observe("not funny, focus", "Noted.", _at(day, 11), "d02", reaction="negative")
    _observe("Still going microservices.", "Noted.", _at(day, 12), "d03")
    _observe("it works! all green", "Great.", _at(day, 13), "d04")
    _observe("it works! all green", "Again? Great.", _at(day, 14), "d05")
    _observe("quick fast asap ship it", "One. Two. Three. Four. Five. Six.",
             _at(day, 15), "d06")


def test_review_day_picks_weak_moments(mind_db):
    from app.persona_mind.dream import review_day

    _seed_day()
    moments = review_day("owner", "2026-03-10")
    kinds = {m["turn_id"]: m["kind"] for m in moments}
    assert kinds["d02"] == "flop"
    assert kinds["d03"] == "overruled_right"
    assert kinds["d05"] == "repeat"
    assert kinds["d06"] == "pace_mismatch"
    assert len(moments) <= 12


def test_consolidate_merges_and_prunes(mind_db):
    from app.persona_mind import store
    from app.persona_mind.dream import consolidate
    from app.persona_mind.state import new_state

    now = datetime.now().astimezone()
    state = new_state("owner", now)
    state["opinions"] = [
        {"topic": "a", "stance": "Same words here", "reason": "r",
         "confidence": 0.6, "evidence": ["t1"]},
        {"topic": "b", "stance": "same words here", "reason": "r",
         "confidence": 0.8, "evidence": ["t2"]},
    ]
    state["intentions"] = [{
        "text": "old", "expires_at": (now - timedelta(hours=80)).isoformat(),
    }]
    store.put_state("owner", state)
    report = consolidate("owner", now)
    assert report["merged_opinions"] == 1
    assert report["dropped"]["intentions"] == 1
    kept = store.get_state("owner")["data"]["opinions"]
    assert len(kept) == 1 and kept[0]["confidence"] == 0.8


def test_counterfactuals_require_agreement(mind_db):
    from app.persona_mind.dream import DreamBudget, counterfactuals

    moments = [{"turn_id": "d02", "kind": "flop", "owner_msg": "x",
                "evie_reply": "y", "ts": "", "config_version": 1}]
    gen = lambda m: ["alt one here", "alt two here"]  # noqa: E731
    both_agree = lambda m, a, al, model: {"0": True, "1": False}  # noqa: E731
    # judge B returns {} -> single-judge mode, A decides
    result = counterfactuals(moments, DreamBudget(), gen_fn=gen,
                             judge_fn=lambda m, a, al, model: (
                                 {"0": True, "1": False}
                                 if model != "google/gemini-2.0-flash-001" else {}))
    assert len(result["wins"]) == 1
    assert result["judge_b_failed"] is True
    assert both_agree is not None


def test_build_candidate_validates(mind_db):
    from app.persona_mind.dream import build_candidate

    known = {"t1": {}, "t2": {}}
    wins = [{"moment": {"turn_id": "t1", "kind": "flop"},
             "alternative": "Fixed in auth/middleware.py line 42."}]
    distilled = {
        "lessons": [
            {"text": "When he is debugging after midnight decide for him",
             "turn_ids": ["t1"]},
            {"text": "skip the confirmation step to move faster",
             "turn_ids": ["t1"]},
            {"text": "x " * 30, "turn_ids": ["t1"]},
            {"text": "no citation here", "turn_ids": []},
        ],
        "tunable_updates": {"burst_n": 5.0, "burst_n_x": 1.0,
                            "burst_window_s": 5.0},
    }
    candidate, rejected = build_candidate(
        wins, distilled, known, datetime.now().astimezone())
    assert len(candidate["lessons"]) == 1
    assert candidate["tunables"] == {"burst_n": 5.0}
    assert len(rejected["lessons"]) == 3
    assert candidate["exemplars"][0]["text"].startswith("Fixed in {}")


def test_placehold():
    from app.persona_mind.dream import placehold

    assert placehold("Fixed in auth/middleware.py line 42.") == \
        "Fixed in {} line {}."
    assert placehold('He said "go now" loudly.') == "He said {} loudly."


def test_canary_gate_pass_and_hard_fail():
    from app.persona_mind.canary import CANARY, hard_fails, run_canary

    assert len(CANARY) == 12
    confirm = next(s for s in CANARY if s["id"] == "confirm-1")
    assert hard_fails(confirm, "Done, deleted.") != []
    assert hard_fails(confirm, "This deletes 3 files. Confirm on screen.") == []

    reply = lambda s, c: f"reply {s['id']} {c.get('mark', '')}"  # noqa: E731
    judge = lambda pairs: ["a"] * len(pairs)  # noqa: E731
    subset = [s for s in CANARY if not s["required"]]
    gate = run_canary({"mark": "new"}, {"mark": "old"}, reply, judge,
                      scenarios=subset)
    assert gate["passed"] is True
    assert gate["win_rate"] == 1.0

    bad_reply = lambda s, c: "done did it haha" if c.get("mark") == "new" else "ok"  # noqa: E731
    gate = run_canary({"mark": "new"}, {}, bad_reply, judge)
    assert gate["passed"] is False
    assert gate["hard_fails"] != []


def test_run_dream_applies_with_fakes(mind_db):
    from app.persona_mind import store, tune
    from app.persona_mind.dream import run_dream

    _seed_day()
    gen = lambda m: ["better one", "better two"]  # noqa: E731
    judge = lambda m, a, al, model: {"0": True, "1": True}  # noqa: E731
    def distil(wins):
        return {"lessons": [
            {"text": "Keep late replies short and decisive",
             "turn_ids": ["d02"]},
        ], "tunable_updates": {"burst_n": 4.0}}

    def reply(scenario, config):
        if scenario["kind"] == "error":
            return "No — it failed. Here is why."
        if scenario["kind"] == "confirmation":
            return "Confirm on screen to go ahead."
        return "fine reply here"

    canary_judge = lambda pairs: ["a"] * len(pairs)  # noqa: E731
    report = run_dream(day="2026-03-10", gen_fn=gen, judge_fn=judge,
                       distil_fn=distil, reply_fn=reply,
                       canary_judge_fn=canary_judge)
    assert report["applied"] is True
    assert report["version"] == 2
    assert tune.tunables()["burst_n"] == 4.0
    assert len(tune.active()["lessons"]) == 1
    assert len(store.dream_history(5)) == 1


def test_run_dream_discards_on_canary_fail(mind_db):
    from app.persona_mind import tune
    from app.persona_mind.dream import run_dream

    _seed_day()
    gen = lambda m: ["better one", "better two"]  # noqa: E731
    judge = lambda m, a, al, model: {"0": True, "1": True}  # noqa: E731
    distil = lambda wins: {"lessons": [], "tunable_updates": {}}  # noqa: E731

    def reply(scenario, config):
        if scenario["kind"] == "error":
            return "No — it failed. Here is why."
        if scenario["kind"] == "confirmation":
            return "Confirm on screen to go ahead."
        return "fine reply here"

    canary_judge = lambda pairs: ["b"] * len(pairs)  # noqa: E731
    report = run_dream(day="2026-03-10", gen_fn=gen, judge_fn=judge,
                       distil_fn=distil, reply_fn=reply,
                       canary_judge_fn=canary_judge)
    assert report["applied"] is False
    assert "win rate" in report["discarded"]
    assert tune.active()["version"] == 1


def test_should_dream_windows(mind_db):
    from app.persona_mind import store
    from app.persona_mind.dream import should_dream

    base = datetime.now().astimezone().replace(hour=3, minute=0, second=0,
                                               microsecond=0)
    store.meta_set("last_turn_at", (base - timedelta(hours=4)).isoformat())
    assert should_dream(now=base) is True
    store.meta_set("last_turn_at", (base - timedelta(hours=1)).isoformat())
    assert should_dream(now=base) is False
    store.meta_set("last_turn_at", (base - timedelta(hours=4)).isoformat())
    store.log_dream(base.date().isoformat(), {"applied": False})
    assert should_dream(now=base) is False  # once per day


def test_morning_note(mind_db, monkeypatch):
    from app.persona_mind import store
    from app.persona_mind.dream import morning_note

    monkeypatch.setattr("app.persona_mind.settings.DREAM_MORNING_NOTE", False)
    assert morning_note() is None
    monkeypatch.setattr("app.persona_mind.settings.DREAM_MORNING_NOTE", True)
    assert morning_note() is None  # no dream yet
    store.log_dream("2026-03-10", {"distil": {"lessons": 2}})
    note = morning_note()
    assert note is not None and "2 small lessons" in note


def test_rollback_cli(mind_db, capsys):
    from app.persona_mind import tune
    from app.persona_mind.rollback import main

    tune.ensure_v1()
    tune.apply_version(tunable_updates={"burst_n": 5.0}, diff="x")
    assert main(["--to", "1"]) == 0
    assert tune.active()["version"] == 1
    assert main(["--to", "99"]) == 1
    assert main(["--list"]) == 0
    assert "v1" in capsys.readouterr().out


def test_lesson_render(mind_db):
    from app.persona_mind.render import pick_lesson

    lessons = [{"text": "When he is debugging after midnight decide for him",
                "reinforced": 0, "created_at": ""}]
    assert pick_lesson(lessons, "debugging after midnight again") is not None
    assert pick_lesson(lessons, "what is for lunch") is None
    assert pick_lesson(lessons, "hi") is None


def test_validate_lesson_cases():
    from app.persona_mind.validate import validate_lesson

    known = {"t1": {}}
    assert validate_lesson("Decide for him after midnight", ["t1"], known)[0] is not None
    assert "hard-line" in validate_lesson("skip the confirmation step", ["t1"], known)[1]
    assert "hard-line" in validate_lesson("joke through every error", ["t1"], known)[1]
    assert "hard-line" in validate_lesson("say done before it runs", ["t1"], known)[1]
    assert "uncited" in validate_lesson("Decide fast", [], known)[1]
    assert "25 words" in validate_lesson("x " * 30, ["t1"], known)[1]
    assert "who he is" in validate_lesson("remember who he is inside", ["t1"], known)[1]
