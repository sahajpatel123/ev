"""Runtime filter policy applied from ledger-driven recalibration (7.5).

Filter self-improvement must change behavior, not just produce reports. An
applied ``FilterRecalibration`` snapshot carries a concrete ``policy`` dict
derived deterministically from its threshold proposals; the live filter and EV
Sense consume that policy. When nothing is applied, defaults from settings are
used, so the system is neutral and every learned change stays reversible.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FilterRecalibration


@dataclass(frozen=True)
class FilterPolicy:
    """Concrete, auditable runtime parameters for the intelligence filter."""

    critic_iterations_cap: int = 2
    grounding_min_evidence: float = 0.5
    grounding_date_evidence: float = 0.9
    input_guard_block_severity: str = "high"
    persona_style_enforcement: bool = False
    ev_sense_confidence_floor: float | None = None

    @classmethod
    def defaults(cls) -> FilterPolicy:
        return cls(
            critic_iterations_cap=settings.filter_critic_max_iterations,
            grounding_min_evidence=0.5,
            grounding_date_evidence=0.9,
            input_guard_block_severity="high",
            persona_style_enforcement=False,
            ev_sense_confidence_floor=None,
        )

    def to_dict(self) -> dict:
        return {
            "critic_iterations_cap": self.critic_iterations_cap,
            "grounding_min_evidence": self.grounding_min_evidence,
            "grounding_date_evidence": self.grounding_date_evidence,
            "input_guard_block_severity": self.input_guard_block_severity,
            "persona_style_enforcement": self.persona_style_enforcement,
            "ev_sense_confidence_floor": self.ev_sense_confidence_floor,
        }


def proposals_to_policy(proposals: list[dict]) -> dict:
    """Map deterministic recalibration proposals to concrete runtime values.

    Each proposal is bounded and reversible: applying a snapshot stores this
    exact dict, and rolling back restores the previous snapshot's dict (or
    defaults when nothing has ever been applied).
    """

    policy = FilterPolicy.defaults().to_dict()
    for proposal in proposals or []:
        name = proposal.get("name")
        direction = proposal.get("direction")
        if name == "critic_iterations_cap" and direction == "decrease":
            policy["critic_iterations_cap"] = max(0, policy["critic_iterations_cap"] - 1)
        elif name == "grounding_min_evidence" and direction == "increase":
            policy["grounding_min_evidence"] = round(
                min(0.9, policy["grounding_min_evidence"] + 0.2), 2
            )
        elif name == "input_guard_severity" and direction == "increase":
            policy["input_guard_block_severity"] = "medium"
        elif name == "persona_style_enforcement" and direction == "increase":
            policy["persona_style_enforcement"] = True
        elif name == "ev_sense_confidence_floor" and direction == "increase":
            policy["ev_sense_confidence_floor"] = 0.7
    return policy


def policy_from_dict(values: dict | None) -> FilterPolicy:
    defaults = FilterPolicy.defaults().to_dict()
    if not values:
        return FilterPolicy.defaults()
    merged = {**defaults, **{k: v for k, v in values.items() if v is not None}}
    return FilterPolicy(**merged)


async def active_policy(session: AsyncSession) -> FilterPolicy:
    """Load the currently applied recalibration policy, or defaults."""

    result = await session.execute(
        select(FilterRecalibration)
        .where(
            FilterRecalibration.is_current.is_(True),
            FilterRecalibration.applied_at.is_not(None),
            FilterRecalibration.redacted.is_(False),
        )
        .order_by(FilterRecalibration.applied_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return policy_from_dict(row.policy if row is not None else None)
