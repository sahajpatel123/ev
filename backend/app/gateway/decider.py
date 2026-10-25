"""Supervisor verdict judge, run on the single brain.

The decider is a role, not a model: it reads a ``state`` plus finite
``questions`` and returns the chosen option for each. It runs on the same
brain as everything else (structured output through
:func:`app.gateway.roles.decide_via_role`, which validates every choice
against the declared options and audits the call).

Answers are categorical strings. The brain is never asked for a
probability and none is ever invented for it; callers map the choices onto
their own fixed scales.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ev.gateway.decider")


class DeciderUnavailable(RuntimeError):
    """The judge is unconfigured or returned an unusable response."""


@dataclass
class DeciderResult:
    """Chosen option per question id from one judge call."""

    answers: dict[str, Any]
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float | None:
        cost = self.usage.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return None
        return float(cost)


class DeciderProvider:
    """Finite-choice judge on the single brain."""

    def __init__(self, *, default_model: str | None = None) -> None:
        from app.gateway.openrouter_muse import default_brain_model

        self.default_model = default_model or default_brain_model()

    async def judge(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
    ) -> DeciderResult:
        """One verdict call; an answer outside the declared options is an error."""

        from app.gateway.roles import decide_via_role

        if not isinstance(questions, dict) or not questions:
            raise DeciderUnavailable("Decider called without questions")
        call = await decide_via_role(state, questions, actor="supervisor")
        if call.status != "ok":
            raise DeciderUnavailable(call.error or "verdict call returned no valid choices")
        result = DeciderResult(
            answers={
                qid: answer.choice for qid, answer in (call.decision_answers or {}).items()
            },
            model=getattr(call.result, "model", None),
            usage=dict(getattr(call.result, "usage", None) or {}),
        )
        if result.cost_usd is not None:
            logger.debug(
                "decider verdict model=%s cost_usd=%.6f", result.model, result.cost_usd
            )
        return result


def decider_available() -> bool:
    """Cheap, network-free pre-check that the brain can serve a verdict.

    Without a brain key the supervisor falls back to the deterministic local
    verdict instead of treating a guess as a judgment.
    """

    from app.gateway.roles import text_role_available

    return text_role_available()
