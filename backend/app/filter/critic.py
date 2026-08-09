"""Provider-backed critic for the output filter (same-model judge, bounded).

The deterministic stages always run. When a critic provider is available, it
judges the draft against EV's identity/grounding/persona/safety/contract rules
and returns a revised draft plus scores. The critic goes through the same
neutral gateway as the main brain, so swapping the provider never changes the
critic's job description or EV's guarantees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.contracts import ChatMessage, RequestEnvelope
from app.filter.envelope import Claim, GroundingMaterial
from app.schemas import InteractionStrategy

CRITIC_SYSTEM = (
    "You are EV's quality critic. You review a draft answer against EV's identity, "
    "memory grounding, persona, safety, and contract rules. Return ONLY JSON: "
    '{"scores": {"grounding": 0-1, "persona": 0-1, "safety": 0-1, '
    '"contract": 0-1, "actionability": 0-1, "honesty": 0-1, "overall": 0-1}, '
    '"revised_text": "...", "issues": ["..."]}. '
    "Never invent personal memories; if a personal claim is unsupported, remove or "
    "soften it. Keep EV's voice: warm, precise, honest, and concise per the mode."
)


@dataclass
class CriticRevision:
    revised_text: str
    scores: dict
    issues: list[str] = field(default_factory=list)
    costs: dict = field(default_factory=dict)
    used_provider: bool = True


class CriticProvider(Protocol):
    async def revise(
        self,
        *,
        draft: str,
        strategy: InteractionStrategy,
        grounding: list[GroundingMaterial],
        claims: list[Claim],
        deterministic_scores: dict,
        iteration: int,
    ) -> CriticRevision: ...


def _critic_prompt(
    *,
    draft: str,
    strategy: InteractionStrategy,
    grounding: list[GroundingMaterial],
    claims: list[Claim],
    deterministic_scores: dict,
    iteration: int,
) -> str:
    memory_lines = "\n".join(f"- {m.text}" for m in grounding[:20])
    claim_lines = "\n".join(
        f"- {c.text} (supported={c.supported}, action={c.action})" for c in claims[:20]
    )
    scores = ", ".join(f"{k}={v}" for k, v in deterministic_scores.items())
    return (
        f"Mode: {strategy.mode}. Iteration: {iteration + 1}.\n"
        f"Strategy: {strategy.rationale}\n"
        f"Deterministic scores: {scores}\n\n"
        f"Memory available to ground personal claims:\n{memory_lines or '(none)'}\n\n"
        f"Claims extracted from the draft:\n{claim_lines or '(none)'}\n\n"
        f"Draft:\n{draft}\n\n"
        "Fix only what the rubric says is broken. Do not add new facts or memories."
    )


def _parse_revision(raw: str) -> dict | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("scores"), dict):
        return None
    return parsed


class GatewayCritic:
    """Critic that reuses the same neutral gateway as the main brain."""

    def __init__(
        self,
        gateway,
        *,
        request_id: str,
        envelope: RequestEnvelope | None = None,
    ) -> None:
        self.gateway = gateway
        self.request_id = request_id
        self.envelope = envelope

    async def revise(
        self,
        *,
        draft: str,
        strategy: InteractionStrategy,
        grounding: list[GroundingMaterial],
        claims: list[Claim],
        deterministic_scores: dict,
        iteration: int,
    ) -> CriticRevision:
        prompt = _critic_prompt(
            draft=draft,
            strategy=strategy,
            grounding=grounding,
            claims=claims,
            deterministic_scores=deterministic_scores,
            iteration=iteration,
        )
        call = await self.gateway.chat(
            [
                ChatMessage(role="system", content=CRITIC_SYSTEM),
                ChatMessage(role="user", content=prompt),
            ],
            envelope=self.envelope,
            model=None,
            temperature=0.0,
        )
        if call.status != "ok" or not call.result.text.strip():
            return CriticRevision(
                revised_text=draft,
                scores=deterministic_scores,
                issues=["critic unavailable"],
                used_provider=False,
            )
        parsed = _parse_revision(call.result.text)
        if parsed is None:
            return CriticRevision(
                revised_text=draft,
                scores=deterministic_scores,
                issues=["critic output unparseable"],
                used_provider=False,
            )
        revised = parsed.get("revised_text") or draft
        merged_scores = dict(deterministic_scores)
        for key, value in parsed.get("scores", {}).items():
            if isinstance(value, (int, float)):
                merged_scores[key] = round(float(value), 3)
        return CriticRevision(
            revised_text=revised,
            scores=merged_scores,
            issues=list(parsed.get("issues") or []),
            costs=call.usage(),
        )
