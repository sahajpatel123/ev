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
import json as _json
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

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "state": {"type": "string", "enum": ["done", "partial", "blocked", "failed", "unknown"]},
        "ok": {"type": ["boolean", "null"]},
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["state", "ok", "score", "reasons", "evidence_refs"],
}

_JUDGE_SYSTEM = (
    "You are the Evie work supervisor. Judge whether the worker finished the "
    "assigned node from its receipt only. Output obeys the schema exactly. "
    "Set ok=true only when the receipt shows the requested effect with "
    "evidence; ok=false when the effect clearly did not happen; ok=null "
    "when the receipt is ambiguous or evidence is missing. state mirrors "
    "that call: done, partial, blocked, failed, or unknown. score is your "
    "confidence 0-1. reasons holds at most 5 short factual strings naming "
    "what the evidence shows. evidence_refs lists the receipt evidence keys "
    "you relied on. Never invent effects the receipt does not show."
)


def decide_next(
    *,
    state: NodeState,
    ok: bool | None,
    score: float,
    has_evidence: bool,
    tier: TaskTier,
    tier_blocked: bool,
    attempt: int,
) -> VerdictNext:
    """Deterministic next-action mapping. Pure: the test matrix pins it."""

    if tier is TaskTier.D or tier_blocked:
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


def local_verdict(node: TaskNode, receipt: WorkerReceipt) -> SupervisorVerdict:
    """Deterministic verdict when the decider cannot serve. Never a guess."""

    if node.tier is TaskTier.D or receipt.error == "tier_d_requires_approval":
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


async def supervise(
    node: TaskNode,
    receipt: WorkerReceipt,
    *,
    decider: Any | None = None,
    attempt: int = 1,
    fast_path: bool = False,
) -> SupervisorVerdict:
    """Verdict one supervised node. Fast path uses the local check only."""

    tier_blocked = node.tier is TaskTier.D or receipt.error == "tier_d_requires_approval"
    if fast_path or tier_blocked:
        verdict = local_verdict(node, receipt)
        if tier_blocked:
            verdict.next = VerdictNext.ASK_OWNER
        return verdict
    model_name: str | None = None
    if decider is None:
        from app.gateway.decider import DeciderProvider, decider_available

        if not decider_available():
            return local_verdict(node, receipt)
        decider = DeciderProvider()
    model_name = getattr(decider, "default_model", None)
    from app.contracts import ChatMessage

    messages = [
        ChatMessage(role="system", content=_JUDGE_SYSTEM),
        ChatMessage(
            role="user",
            content=_json.dumps(
                {
                    "node": {
                        "id": node.id,
                        "label": node.label,
                        "detail": node.detail[:1500],
                        "tier": node.tier.value,
                    },
                    "receipt": _receipt_summary(receipt),
                },
                ensure_ascii=False,
            ),
        ),
    ]
    limit = float(getattr(settings, "decider_timeout_seconds", 30.0) or 30.0)
    try:
        result = await asyncio.wait_for(
            decider.judge(messages, schema=VERDICT_SCHEMA), timeout=limit
        )
        parsed = _json.loads((result.text or "").strip() or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("verdict is not an object")
        missing = {"state", "ok", "score", "reasons", "evidence_refs"} - set(parsed)
        if missing:
            raise ValueError(f"verdict is missing keys: {sorted(missing)}")
        verdict = SupervisorVerdict.model_validate({**parsed, "node_id": node.id})
    except (TimeoutError, ValueError) as exc:
        logger.warning("decider verdict unusable node=%s err=%s", node.id, exc)
        return local_verdict(node, receipt)
    except Exception as exc:  # noqa: BLE001 - verdict failure degrades, never guesses
        logger.warning(
            "decider call failed node=%s error=%s", node.id, type(exc).__name__
        )
        return local_verdict(node, receipt)
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
    )
    verdict.judge = "decider"
    verdict.model = str(model_name) if model_name else None
    return verdict
