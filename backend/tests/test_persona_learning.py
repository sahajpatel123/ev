"""Learning tests: Beta updates, Thompson hints, and the 30-turn simulation."""

from __future__ import annotations

from app.persona.context import PersonaContext, ToneDecision
from app.persona.learning import (
    Beta,
    LearningWeights,
    describe,
    hint_for,
    joke_rate_target,
    seeded_rng,
    update,
)
from app.persona.settings import joke_rate_bounds
from app.persona.techniques import TECHNIQUES


def _decision(roast: int = 2, audience: str = "private") -> ToneDecision:
    return ToneDecision(
        roast=roast,
        humour_allowed=roast >= 1,
        technique=None,
        profanity=False,
        audience=audience,
        reasons=(),
    )


def test_beta_update_rules() -> None:
    base = Beta()
    assert base.updated("positive") == Beta(2.0, 1.0)
    assert base.updated("negative") == Beta(1.0, 2.0)
    assert base.updated("neutral") == base
    assert base.updated("mystery") == base


def test_update_is_pure_and_additive() -> None:
    before = LearningWeights.default()
    after = update(before, "understatement", "code", "positive")
    assert before.techniques["understatement"] == Beta(1.0, 1.0)
    assert after.techniques["understatement"] == Beta(2.0, 1.0)
    assert after.topics["code"] == Beta(2.0, 1.0)
    assert after.joke_rate == Beta(2.0, 1.0)


def test_joke_rate_stays_in_bounds() -> None:
    weights = LearningWeights.default()
    for _ in range(50):
        weights = update(weights, "escalation", "code", "negative")
    low, high = joke_rate_bounds
    assert low <= joke_rate_target(weights) <= high
    assert joke_rate_target(weights) == low


def test_hint_returns_none_when_humour_off() -> None:
    weights = LearningWeights.default()
    rng = seeded_rng("t", 0)
    assert hint_for(_decision(roast=0), weights, rng) is None
    off = ToneDecision(0, False, None, False, "private", ())
    assert hint_for(off, weights, rng) is None


def test_hint_respects_min_roast_and_audience() -> None:
    weights = LearningWeights.default()
    for turn in range(30):
        hint = hint_for(_decision(roast=1), weights, seeded_rng("t", turn))
        assert hint in (None, "understatement", "absurd_literalism",
                        "twist_ending", "callback", "brutal_honest")
    for turn in range(30):
        hint = hint_for(_decision(roast=2, audience="company"), weights,
                        seeded_rng("t", turn))
        assert hint in (None, "understatement", "absurd_literalism",
                        "twist_ending", "callback")


def test_hint_is_deterministic_for_same_seed() -> None:
    weights = LearningWeights.default()
    first = hint_for(_decision(), weights, seeded_rng("thread-9", 4))
    second = hint_for(_decision(), weights, seeded_rng("thread-9", 4))
    assert first == second


def test_seeded_rng_differs_across_turns() -> None:
    assert seeded_rng("t", 0).random() != seeded_rng("t", 1).random()


def test_simulation_rewards_understatement_punishes_escalation() -> None:
    """A scripted owner loves understatement, hates escalation: within 30
    turns the technique means must move the right way."""
    weights = LearningWeights.default()
    for turn in range(30):
        hint = hint_for(_decision(), weights, seeded_rng("sim", turn))
        if hint == "understatement":
            weights = update(weights, hint, "code", "positive")
        elif hint == "escalation":
            weights = update(weights, hint, "code", "negative")
        else:
            weights = update(weights, hint, "code", "neutral")
    under = weights.techniques["understatement"].mean()
    escal = weights.techniques["escalation"].mean()
    assert under > 0.5 >= escal, (under, escal)


def test_joke_rate_falls_under_repeated_negatives() -> None:
    weights = LearningWeights.default()
    start = joke_rate_target(weights)
    for _ in range(10):
        weights = update(weights, "escalation", "code", "negative")
    assert joke_rate_target(weights) < start


def test_describe_snapshot_is_json_safe() -> None:
    import json

    snapshot = describe(LearningWeights.default())
    json.dumps(snapshot)
    assert len(snapshot["techniques"]) == len(TECHNIQUES)
    assert snapshot["joke_rate"]["target"] == 0.5


def test_default_weights_cover_all_techniques() -> None:
    weights = LearningWeights.default()
    assert {tech.id for tech in TECHNIQUES} <= set(weights.techniques)


def test_context_unused_import_guard() -> None:
    # PersonaContext stays importable from learning-adjacent tests without cycles.
    assert PersonaContext(channel="text").channel == "text"
