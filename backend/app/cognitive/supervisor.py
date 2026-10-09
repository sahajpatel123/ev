"""Graph supervisors: one node, one worker, one strict verdict.

The supervisor owns a single node end to end: it already has the worker
receipt and must answer whether the work is done. The decider model
interprets; this module enforces. Three rules are non-negotiable:

1. No evidence refs means not done — a bare `ok: true` never accepts.
2. Tier-D receipts always resolve to ask_owner, never accept.
3. An undecodable or missing verdict degrades to the deterministic local
   verdict, never to a guess.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.cognitive.graph import (
    NodeState,
    SupervisorVerdict,
    TaskNode,
    TaskTier,
    VerdictNext,
    WorkerReceipt,
)
from app.config import settings

logger = logging.getLogger("ev.cognitive.supervisor")

# One Decisions API call asks all three questions against the same state:
# a noul (is it done?), a choice (which outcome?), and a score (how good?).
# The model returns calibrated probabilities, not prose, so there is nothing
# to parse — only numbers to validate. Evidence grounding stays ours: refs
# point at receipt entries we sent, never at model inventions.
VERDICT_QUESTIONS: dict[str, Any] = {
    "done": {
        "type": "noul",
        "instructions": (
            "The worker evidence proves the node's objective is fully "
            "accomplished. Judge whether the requested effect happened, not "
            "whether you find the content interesting; treat an asked count "
            "as up-to-N — fewer rows than asked is done when rows were "
            "returned."
        ),
    },
    "verdict": {
        "type": "choice",
        "instructions": (
            "Which single outcome best describes the worker's attempt on "
            "this node?"
        ),
        "criteria": {
            "done": "The requested effect happened and the evidence shows it.",
            "partial": "Real progress happened but part of the objective is missing.",
            "blocked": "The worker could not proceed and needs the owner or another step.",
            "failed": "The attempt clearly did not produce the requested effect.",
            "unknown": "The receipt is too ambiguous to classify.",
        },
    },
    "quality": {
        "type": "score",
        "instructions": "Rate the quality of the work shown in the evidence.",
        "criteria": [
            "broken: wrong effect or fabricated-looking evidence",
            "weak: partial effect with major gaps",
            "adequate: the requested effect with thin evidence",
            "solid: the requested effect with clear evidence",
            "excellent: exceeds the request with strong evidence",
        ],
    },
}

# Live wire key first (observed 2026-10-05): {"type": "noul", "noul": 0.89}.
_NOUL_PROBABILITY_KEYS = ("noul", "probability", "p_yes", "p")


def decide_next(
    *,
    state: NodeState,
    ok: bool | None,
    score: float,
    has_evidence: bool,
    tier: TaskTier,
    tier_blocked: bool,
    attempt: int,
    approved: bool = False,
) -> VerdictNext:
    """Deterministic next-action mapping. Pure: the test matrix pins it."""

    if (tier is TaskTier.D or tier_blocked) and not approved:
        return VerdictNext.ASK_OWNER
    if not has_evidence:
        if state is NodeState.BLOCKED:
            return VerdictNext.ASK_OWNER
        return VerdictNext.ESCALATE
    if state is NodeState.BLOCKED:
        return VerdictNext.ASK_OWNER
    if ok is True and state is NodeState.DONE and score >= 0.5:
        return VerdictNext.ACCEPT
    if ok is True and state is NodeState.DONE:
        return VerdictNext.ESCALATE
    if ok is False or state is NodeState.FAILED:
        return VerdictNext.RETRY if attempt < 2 else VerdictNext.ESCALATE
    if state is NodeState.PARTIAL:
        return VerdictNext.RETRY if attempt < 2 else VerdictNext.ESCALATE
    if ok is None or state is NodeState.UNKNOWN or score < 0.5:
        return VerdictNext.ESCALATE
    return VerdictNext.ESCALATE


def _tool_is_read_only(tool: str | None) -> bool:
    """True only when the tool is a known read-only semantic tool.

    Unknown or missing tools return False: the tier guard must reject
    proven readers, never tools it cannot classify.
    """

    if not tool:
        return False
    try:
        from app.cognitive.capabilities import tool_specs
    except Exception:
        return False
    for spec in tool_specs():
        if spec.name == tool:
            return bool(spec.read_only)
    return False


def local_verdict(node: TaskNode, receipt: WorkerReceipt, *, approved: bool = False) -> SupervisorVerdict:
    """Deterministic verdict when the decider cannot serve. Never a guess."""

    if (node.tier is TaskTier.D or receipt.error == "tier_d_requires_approval") and not approved:
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.BLOCKED,
            ok=None,
            score=0.0,
            reasons=[receipt.spoken or "Tier-D step needs owner approval."],
            next=VerdictNext.ASK_OWNER,
            evidence_refs=[],
            judge="local",
        )
    if receipt.error == "confirmation_required":
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.BLOCKED,
            ok=None,
            score=0.0,
            reasons=[receipt.spoken or "This step needs owner approval."],
            next=VerdictNext.ASK_OWNER,
            evidence_refs=[],
            judge="local",
        )
    if receipt.error == "node_timeout":
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.FAILED,
            ok=False,
            score=0.6,
            reasons=["Worker exceeded its time budget."],
            next=VerdictNext.RETRY,
            evidence_refs=[],
            judge="local",
        )
    if (
        node.tier is TaskTier.W
        and receipt.ok
        and _tool_is_read_only(getattr(node, "tool", None))
    ):
        # A write step answered by a read-only tool is planner error, not
        # completion (live catch: life.messages "verified" a WhatsApp send).
        # Unknown tools skip this guard; only known readers are rejected.
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.UNKNOWN,
            ok=None,
            score=0.2,
            reasons=[
                f"Write step was answered by read-only tool {getattr(node, 'tool', None) or 'unknown'}; "
                "a read cannot complete a write."
            ],
            next=VerdictNext.ESCALATE,
            evidence_refs=[],
            judge="local",
        )
    if receipt.ok and receipt.has_evidence:
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.DONE,
            ok=True,
            score=0.5,
            reasons=["Worker reported success with evidence (local check)."],
            next=VerdictNext.ACCEPT,
            evidence_refs=[f"{node.id}:receipt"],
            judge="local",
        )
    if receipt.ok:
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.UNKNOWN,
            ok=None,
            score=0.2,
            reasons=["Worker reported success without evidence."],
            next=VerdictNext.ESCALATE,
            evidence_refs=[],
            judge="local",
        )
    return SupervisorVerdict(
        node_id=node.id,
        state=NodeState.FAILED,
        ok=False,
        score=0.5,
        reasons=[receipt.error or "Worker reported failure."],
        next=VerdictNext.RETRY,
        evidence_refs=[],
        judge="local",
    )


def _receipt_summary(receipt: WorkerReceipt) -> dict[str, Any]:
    return {
        "ok": receipt.ok,
        "worker": receipt.worker,
        "spoken": receipt.spoken[:500],
        "error": receipt.error,
        "artifacts": receipt.artifacts[:10],
        "evidence": receipt.evidence[:10],
        "duration_ms": round(receipt.duration_ms, 1),
    }


def verdict_state(node: TaskNode, receipt: WorkerReceipt) -> dict[str, Any]:
    """Decisions API state: the node plus the receipt the model must judge."""

    return {
        "node": {
            "id": node.id,
            "label": node.label,
            "detail": node.detail[:1500],
            "tier": node.tier.value,
        },
        "receipt": _receipt_summary(receipt),
    }


def _as_probability(value: Any, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"decider {what} is not a number: {value!r}")
    prob = float(value)
    if not 0.0 <= prob <= 1.0:
        raise ValueError(f"decider {what} is outside 0-1: {value!r}")
    return prob


def _choice_probability(answer: dict[str, Any], choice: str) -> float | None:
    dist = answer.get("probabilities")
    if not isinstance(dist, dict):
        return None
    raw = dist.get(choice)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    prob = float(raw)
    return prob if 0.0 <= prob <= 1.0 else None


def answers_to_verdict(
    node: TaskNode, receipt: WorkerReceipt, answers: Any
) -> SupervisorVerdict:
    """Map typed decider answers onto a verdict. Raises ValueError when the
    answers are incomplete or malformed, so the caller degrades to local."""

    if not isinstance(answers, dict):
        raise ValueError("decider answers are not an object")
    done_answer = answers.get("done")
    verdict_answer = answers.get("verdict")
    quality_answer = answers.get("quality")
    if not all(isinstance(a, dict) for a in (done_answer, verdict_answer, quality_answer)):
        raise ValueError("decider answers are missing done/verdict/quality")
    assert isinstance(verdict_answer, dict) and isinstance(done_answer, dict)
    raw_choice = verdict_answer.get("choice", verdict_answer.get("value"))
    try:
        state = NodeState(str(raw_choice).strip().lower())
    except ValueError as exc:
        raise ValueError(f"decider choice is not a node state: {raw_choice!r}") from exc
    p_done: float | None = None
    for key in _NOUL_PROBABILITY_KEYS:
        if done_answer.get(key) is not None:
            p_done = _as_probability(done_answer[key], what="done probability")
            break
    if p_done is None:
        raise ValueError("decider noul answer has no probability")
    if state is NodeState.DONE and p_done >= 0.5:
        ok: bool | None = True
    elif state is NodeState.FAILED:
        ok = False
    else:
        ok = None
    choice_p = _choice_probability(verdict_answer, state.value)
    reasons = [
        f"outcome={state.value}"
        + (f" (p={choice_p:.2f})" if choice_p is not None else ""),
        f"done-noul p(yes)={p_done:.2f}",
    ]
    if isinstance(quality_answer, dict):
        expected = quality_answer.get("expected", quality_answer.get("score"))
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            reasons.append(f"quality expected={float(expected):.2f}")
    refs: list[str] = []
    for index, entry in enumerate(list(receipt.evidence or [])[:20]):
        tag = entry.get("tool") if isinstance(entry, dict) else None
        refs.append(f"{node.id}:evidence:{index}" + (f":{tag}" if tag else ""))
    return SupervisorVerdict(
        node_id=node.id,
        state=state,
        ok=ok,
        score=p_done,
        reasons=reasons[:5],
        evidence_refs=refs,
        judge="decider",
    )


async def supervise(
    node: TaskNode,
    receipt: WorkerReceipt,
    *,
    decider: Any | None = None,
    attempt: int = 1,
    fast_path: bool = False,
    approved: bool = False,
) -> SupervisorVerdict:
    """Verdict one supervised node. Fast path uses the local check only.

    ``approved`` marks a receipt produced by an owner-approved execution:
    the tier-D block is spent, so the verdict judges the evidence instead
    of re-parking. Only the answer door passes it, once per approval.
    """

    tier_blocked = (
        node.tier is TaskTier.D or receipt.error == "tier_d_requires_approval"
    ) and not approved
    confirm_blocked = receipt.error == "confirmation_required"
    if fast_path or tier_blocked or confirm_blocked:
        verdict = local_verdict(node, receipt, approved=approved)
        if tier_blocked or confirm_blocked:
            verdict.next = VerdictNext.ASK_OWNER
        return verdict
    if decider is None:
        from app.gateway.decider import DeciderProvider, decider_available

        if not decider_available():
            return local_verdict(node, receipt, approved=approved)
        decider = DeciderProvider()
    limit = float(getattr(settings, "decider_timeout_seconds", 30.0) or 30.0)
    try:
        result = await asyncio.wait_for(
            decider.judge(verdict_state(node, receipt), VERDICT_QUESTIONS),
            timeout=limit,
        )
        verdict = answers_to_verdict(node, receipt, result.answers)
        verdict.model = result.model or getattr(decider, "default_model", None)
    except (TimeoutError, ValueError) as exc:
        logger.warning("decider verdict unusable node=%s err=%s", node.id, exc)
        return local_verdict(node, receipt, approved=approved)
    except Exception as exc:  # noqa: BLE001 - verdict failure degrades, never guesses
        logger.warning(
            "decider call failed node=%s error=%s", node.id, type(exc).__name__
        )
        return local_verdict(node, receipt, approved=approved)
    has_evidence = bool(verdict.evidence_refs) and receipt.has_evidence
    if not has_evidence and verdict.ok is True:
        verdict.ok = None
        verdict.state = NodeState.UNKNOWN
        verdict.score = min(float(verdict.score), 0.2)
        verdict.reasons = [*verdict.reasons, "No receipt evidence to accept."][:5]
    verdict.next = decide_next(
        state=verdict.state,
        ok=verdict.ok,
        score=float(verdict.score),
        has_evidence=receipt.has_evidence,
        tier=node.tier,
        tier_blocked=tier_blocked,
        attempt=attempt,
        approved=approved,
    )
    verdict.judge = "decider"
    return verdict
