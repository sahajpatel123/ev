"""Eval scoring tests: dual-judge vote aggregation (no brain).

Covers score_samples majority/tie math, per-judge rates, hard-fail
collection, the grounding fields in judge_user_block, and the
TUNE/REPORT split (REPORT includes the sealed held-out file).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.personality.judge import judge_user_block  # noqa: E402
from eval.personality.run import (  # noqa: E402
    _side,
    load_scenarios,
    score_samples,
    split_scenarios,
)


def _vote(side: str, *, judge: str = "muse-default",
          criterion: str = "tie", fails: list | None = None,
          v2_first: bool = True) -> dict:
    label = {"v2": "a", "baseline": "b", "tie": "tie"}[side] \
        if v2_first else {"v2": "b", "baseline": "a", "tie": "tie"}[side]
    judgment = {
        "verdict": label,
        "truth": label if criterion == "truth" else "tie",
        "usefulness": "tie", "human": "tie", "funny": "tie",
        "specificity": "tie", "appropriateness": "tie",
        "hard_fails_a": list(fails or []) if v2_first else [],
        "hard_fails_b": [] if v2_first else list(fails or []),
        "judge_model": judge,
    }
    return {"v2_first": v2_first, "judgment": judgment}


def _sample(votes: list[dict]) -> dict:
    return {"votes": votes}


def test_side_mapping_both_orders() -> None:
    assert _side("a", True) == "v2"
    assert _side("b", True) == "baseline"
    assert _side("a", False) == "baseline"
    assert _side("b", False) == "v2"
    assert _side("tie", True) == "tie"


def test_unanimous_v2_scores_1() -> None:
    samples = [_sample([_vote("v2"), _vote("v2"), _vote("v2"), _vote("v2")]),
               _sample([_vote("v2"), _vote("v2"), _vote("v2"), _vote("v2")])]
    agg = score_samples(samples)
    assert agg["score"] == 1.0
    assert agg["verdict"] == "v2"
    assert agg["hard_fails"] == []


def test_split_2_2_is_tie_half() -> None:
    votes = [_vote("v2"), _vote("v2"), _vote("baseline"), _vote("baseline")]
    agg = score_samples([_sample(votes), _sample(votes)])
    assert agg["score"] == 0.5
    assert agg["verdict"] == "tie"


def test_majority_3_1_wins_sample() -> None:
    votes = [_vote("v2"), _vote("v2"), _vote("v2"), _vote("baseline")]
    agg = score_samples([_sample(votes), _sample(votes)])
    assert agg["sample_scores"] == [0.75, 0.75]
    assert agg["verdict"] == "v2"


def test_per_judge_rates_and_hard_fails() -> None:
    votes = [_vote("v2", judge="muse-default"),
             _vote("v2", judge="muse-default"),
             _vote("baseline", judge="opus", fails=["invented_referent"]),
             _vote("tie", judge="opus")]
    agg = score_samples([_sample(votes), _sample(votes)])
    assert agg["per_judge"]["muse-default"] == 1.0
    assert agg["per_judge"]["opus"] == 0.25
    assert agg["hard_fails"] == ["invented_referent"]


def test_truth_useful_veto() -> None:
    votes = [_vote("v2", criterion="truth"), _vote("v2"),
             _vote("v2"), _vote("v2")]
    agg = score_samples([_sample(votes), _sample(votes)])
    assert agg["truth_useful_ok"] is True
    bad = [_vote("v2"), _vote("baseline", criterion="truth"),
           _vote("v2"), _vote("v2")]
    assert score_samples([_sample(bad), _sample(bad)])["truth_useful_ok"] is False


def test_judge_block_carries_grounding_context() -> None:
    scenario = {
        "owner_msg": "roast me like last Diwali",
        "family": "joke needing invention",
        "expected_ceiling": 2,
        "signals": {"local_time": "2026-10-28T20:30:00", "day": "Wednesday"},
        "history": [{"role": "owner", "text": "hi"}],
        "memory": "none",
    }
    block = judge_user_block(scenario, "A reply", "B reply")
    assert "2026-10-28T20:30:00" in block
    assert "Wednesday" in block
    assert "History" in block
    assert "Memory note" in block


def test_split_tune_excludes_all_held_out() -> None:
    scenarios = load_scenarios()
    tune = split_scenarios(scenarios, "tune")
    assert tune
    assert all(not s.get("held_out") for s in tune)
    assert len(tune) == 48


def test_split_report_includes_sealed_file() -> None:
    scenarios = load_scenarios()
    report = split_scenarios(scenarios, "report")
    assert len(report) == 20
    assert all(s.get("held_out") for s in report)
