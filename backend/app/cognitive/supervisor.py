"""Graph supervisors: one node, one worker, one strict verdict.

The supervisor owns a single node end to end: it already has the worker
receipt and must answer whether the work is done. The brain
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
from app.gateway.roles import DecisionQuestion

logger = logging.getLogger("ev.cognitive.supervisor")

# One brain call asks three finite-choice questions against the same state.
# Answers are categorical: no probability is requested from the model and none
# is invented for it. Evidence grounding stays ours: refs point at receipt
# entries we sent, never at model inventions. The guidance for each option is
# part of the instructions because only the instructions reach the model.
VERDICT_QUESTIONS: dict[str, DecisionQuestion] = {
    "done": DecisionQuestion(
        type="choice",
        instructions=(
            "Does the worker evidence prove the node's objective is fully "
            "accomplished? Judge whether the requested effect happened, not "
            "whether you find the content interesting; treat an asked count "
            "as up-to-N - fewer rows than asked is done when rows were "
            "returned. Answer yes only when the evidence shows the effect."
        ),
        criteria=["yes", "no"],
    ),
    "verdict": DecisionQuestion(
        type="choice",
        instructions=(
            "Which single outcome best describes the worker's attempt on "
            "this node? done = the requested effect happened and the evidence "
            "shows it; partial = real progress happened but part of the "
            "objective is missing; blocked = the worker could not proceed and "
            "needs the owner or another step; failed = the attempt clearly did "
            "not produce the requested effect; unknown = the receipt is too "
            "ambiguous to classify."
        ),
        criteria=["done", "partial", "blocked", "failed", "unknown"],
    ),
    "quality": DecisionQuestion(
        type="choice",
        instructions=(
            "Rate the quality of the work shown in the evidence. broken = "
            "wrong effect or fabricated-looking evidence; weak = partial "
            "effect with major gaps; adequate = the requested effect with thin "
            "evidence; solid = the requested effect with clear evidence; "
            "excellent = exceeds the request with strong evidence."
        ),
        criteria=["broken", "weak", "adequate", "solid", "excellent"],
    ),
}

# Fixed scale for the quality rating. These are labels mapped to numbers by
# us, not model-reported confidence; ACCEPT needs at least "adequate".
_QUALITY_SCORE = {
    "broken": 0.0,
    "weak": 0.25,
    "adequate": 0.6,
    "solid": 0.8,
    "excellent": 1.0,
}


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
    """Deterministic verdict when the judge cannot serve. Never a guess."""

    if receipt.error in {"transport_unavailable", "chat_unresolved"}:
        # Nothing approvable: the route or recipient does not exist. Terminal
        # — a retry would probe the same missing transport again.
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.FAILED,
            ok=False,
            score=0.0,
            reasons=[receipt.spoken or receipt.error or "unreachable"],
            next=VerdictNext.ESCALATE,
            evidence_refs=[],
            judge="local",
        )
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
    if receipt.error in {"missing_recipient_or_body", "ev_app_not_connected"}:
        return SupervisorVerdict(
            node_id=node.id,
            state=NodeState.BLOCKED,
            ok=None,
            score=0.0,
            reasons=[receipt.spoken or "This step needs the owner."],
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
    """Judge state: the node plus the receipt the brain must judge."""

    return {
        "node": {
            "id": node.id,
            "label": node.label,
            "detail": node.detail[:1500],
            "tier": node.tier.value,
        },
        "receipt": _receipt_summary(receipt),
    }


def _answer_choice(answers: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    raw = answers.get(key)
    if isinstance(raw, dict):  # tolerate {"choice": "..."} from an injected judge
        raw = raw.get("choice")
    value = str(raw).strip().lower() if isinstance(raw, str) else ""
    if value not in allowed:
        raise ValueError(f"judge {key} is not one of {allowed}: {raw!r}")
    return value


def answers_to_verdict(
    node: TaskNode, receipt: WorkerReceipt, answers: Any
) -> SupervisorVerdict:
    """Map categorical judge answers onto a verdict. Raises ValueError when the
    answers are incomplete or malformed, so the caller degrades to local."""

    if not isinstance(answers, dict):
        raise ValueError("judge answers are not an object")
    done = _answer_choice(answers, "done", ("yes", "no"))
    state = NodeState(_answer_choice(answers, "verdict", tuple(s.value for s in NodeState)))
    quality = _answer_choice(answers, "quality", tuple(_QUALITY_SCORE))
    if state is NodeState.DONE and done == "yes":
        ok: bool | None = True
    elif state is NodeState.FAILED:
        ok = False
    else:
        # Includes "outcome done but done=no": inconsistent, so not accepted.
        ok = None
    reasons = [f"outcome={state.value}", f"done={done}", f"quality={quality}"]
    refs: list[str] = []
    for index, entry in enumerate(list(receipt.evidence or [])[:20]):
        tag = entry.get("tool") if isinstance(entry, dict) else None
        refs.append(f"{node.id}:evidence:{index}" + (f":{tag}" if tag else ""))
    return SupervisorVerdict(
        node_id=node.id,
        state=state,
        ok=ok,
        score=_QUALITY_SCORE[quality],
        reasons=reasons,
        evidence_refs=refs,
        judge="brain",
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

    if receipt.error in {"transport_unavailable", "chat_unresolved"}:
        # Terminal before any judge: no model verdict can fix a missing route.
        return local_verdict(node, receipt)
    tier_blocked = (
        node.tier is TaskTier.D or receipt.error == "tier_d_requires_approval"
    ) and not approved
    confirm_blocked = receipt.error == "confirmation_required"
    owner_blocked = receipt.error in {"missing_recipient_or_body", "ev_app_not_connected"}
    if fast_path or tier_blocked or confirm_blocked or owner_blocked:
        verdict = local_verdict(node, receipt, approved=approved)
        if tier_blocked or confirm_blocked or owner_blocked:
            verdict.next = VerdictNext.ASK_OWNER
        return verdict
    if decider is None:
        from app.gateway.decider import DeciderProvider, decider_available

        if not decider_available():
            return local_verdict(node, receipt, approved=approved)
        decider = DeciderProvider()
    limit = float(getattr(settings, "verdict_timeout_seconds", 30.0) or 30.0)
    try:
        result = await asyncio.wait_for(
            decider.judge(verdict_state(node, receipt), VERDICT_QUESTIONS),
            timeout=limit,
        )
        verdict = answers_to_verdict(node, receipt, result.answers)
        verdict.model = result.model or getattr(decider, "default_model", None)
    except (TimeoutError, ValueError) as exc:
        logger.warning("judge verdict unusable node=%s err=%s", node.id, exc)
        return local_verdict(node, receipt, approved=approved)
    except Exception as exc:  # noqa: BLE001 - verdict failure degrades, never guesses
        logger.warning(
            "judge call failed node=%s error=%s", node.id, type(exc).__name__
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
    verdict.judge = "brain"
    return verdict
