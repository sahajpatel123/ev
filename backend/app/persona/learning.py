"""Learning what lands (spec §6): Beta weights + Thompson sampling.

Per-owner Beta(α, β) weights per technique and per topic, plus an adaptive
joke-rate target bounded by settings.joke_rate_bounds. All functions are
pure; persistence goes through the CallbackStore interface. RNG is seeded
from (thread_id, turn_index) so every turn is reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.persona import techniques
from app.persona.settings import joke_rate_bounds

if TYPE_CHECKING:
    from app.persona.context import ToneDecision


@dataclass(frozen=True)
class Beta:
    """Beta(α, β) belief. Neutral observations change nothing."""

    alpha: float = 1.0
    beta: float = 1.0

    def mean(self) -> float:
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    def sample(self, rng: random.Random) -> float:
        try:
            return rng.betavariate(max(self.alpha, 1e-6), max(self.beta, 1e-6))
        except ValueError:
            return 0.5

    def updated(self, reaction: str) -> Beta:
        if reaction == "positive":
            return Beta(self.alpha + 1.0, self.beta)
        if reaction == "negative":
            return Beta(self.alpha, self.beta + 1.0)
        return self


@dataclass(frozen=True)
class LearningWeights:
    """The full per-owner belief state (immutable; update() copies)."""

    techniques: dict[str, Beta] = field(default_factory=dict)
    topics: dict[str, Beta] = field(default_factory=dict)
    joke_rate: Beta = field(default_factory=Beta)

    @classmethod
    def default(cls) -> LearningWeights:
        return cls(
            techniques={tech.id: Beta() for tech in techniques.TECHNIQUES},
            topics={},
            joke_rate=Beta(),
        )


def seeded_rng(thread_id: str | None, turn_index: int) -> random.Random:
    """Deterministic RNG for one turn."""
    return random.Random(f"{thread_id or 'none'}:{int(turn_index)}")


def technique_beta(weights: LearningWeights, technique_id: str) -> Beta:
    return weights.techniques.get(technique_id) or Beta()


def topic_beta(weights: LearningWeights, topic: str) -> Beta:
    return weights.topics.get(topic) or Beta()


def joke_rate_target(weights: LearningWeights) -> float:
    """Adaptive joke rate: Beta mean clamped to the configured bounds."""
    low, high = joke_rate_bounds
    return min(high, max(low, weights.joke_rate.mean()))


def update(
    weights: LearningWeights,
    technique: str | None,
    topic: str | None,
    reaction: str,
) -> LearningWeights:
    """Return new weights after one classified reaction (pure)."""
    if reaction not in ("positive", "neutral", "negative"):
        reaction = "neutral"
    new_techniques = dict(weights.techniques)
    if technique:
        current = new_techniques.get(technique) or Beta()
        new_techniques[technique] = current.updated(reaction)
    new_topics = dict(weights.topics)
    if topic:
        current = new_topics.get(topic) or Beta()
        new_topics[topic] = current.updated(reaction)
    return LearningWeights(
        techniques=new_techniques,
        topics=new_topics,
        joke_rate=weights.joke_rate.updated(reaction),
    )


def hint_for(
    decision: ToneDecision,
    weights: LearningWeights,
    rng: random.Random,
) -> str | None:
    """Pick a technique id by Thompson sampling, or None to stay straight.

    None when humour is off, ROAST is 0, nothing is eligible, or the
    joke-rate draw says skip this turn.
    """
    if not decision.humour_allowed or decision.roast <= 0:
        return None
    if rng.random() > joke_rate_target(weights):
        return None
    candidates = techniques.eligible(roast=decision.roast, audience=decision.audience)
    if not candidates:
        return None
    scored = [
        (technique_beta(weights, tech.id).sample(rng), tech.id) for tech in candidates
    ]
    best_score = max(score for score, _ in scored)
    for score, technique_id in scored:
        if score == best_score:
            return technique_id
    return None


def describe(weights: LearningWeights) -> dict[str, Any]:
    """JSON-safe snapshot for reports and evals (no persistence here)."""
    return {
        "techniques": {
            key: {"alpha": value.alpha, "beta": value.beta, "mean": value.mean()}
            for key, value in sorted(weights.techniques.items())
        },
        "topics": {
            key: {"alpha": value.alpha, "beta": value.beta, "mean": value.mean()}
            for key, value in sorted(weights.topics.items())
        },
        "joke_rate": {
            "alpha": weights.joke_rate.alpha,
            "beta": weights.joke_rate.beta,
            "target": joke_rate_target(weights),
        },
    }
