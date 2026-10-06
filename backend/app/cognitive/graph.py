"""Delegate graph: MiMo planner -> supervisors -> workers under delegate_task.

A delegated owner task becomes a small DAG of TaskNodes. Single-node,
read-only plans take the fast path (one static tool call plus a
deterministic evidence check, no verdict model). Anything bigger fans out to
supervisors that each own one node: pick a worker, run it, collect the
receipt, and ask the decider for a strict verdict. The aggregator joins
verdicts into one honest job outcome.

Evidence decides; models interpret. A node without evidence refs is never
accepted, and tier-D (destructive / external / irreversible) nodes never
execute in graph v1 — they resolve to ask_owner with a plan summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.config import settings

logger = logging.getLogger("ev.cognitive.graph")


def delegate_graph_active() -> bool:
    """True when the owner opted into the graph runner (EV_DELEGATE_GRAPH=on)."""

    return (getattr(settings, "delegate_graph", "off") or "off").strip().lower() == "on"


class GraphUnavailable(RuntimeError):
    """Raised pre-execution when the graph path cannot serve.

    The caller must fall back to the legacy single-turn path. Never raised
    after a worker has executed: partial effects plus a legacy rerun could
    execute the owner's action twice.
    """


class GraphPlanError(ValueError):
    """The planner returned a DAG that failed validation."""


class TaskTier(StrEnum):
    R = "R"  # read-only: observe, fetch, search, read
    W = "W"  # reversible write: create, edit, organize (snapshot/undo)
    D = "D"  # destructive / external / irreversible: delete, send, run


class NodeState(StrEnum):
    DONE = "done"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VerdictNext(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    ESCALATE = "escalate"
    ASK_OWNER = "ask_owner"


class TaskNode(BaseModel):
    """One DAG node: a narrow unit of work with an explicit blast radius."""

    model_config = {"extra": "ignore"}

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    label: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=2000)
    tier: TaskTier = TaskTier.R
    depends_on: list[str] = Field(default_factory=list)
    tool: str | None = Field(default=None, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)

    @field_validator("depends_on")
    @classmethod
    def _no_self_dep(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("depends_on must not repeat")
        return value

    def idempotency_key(self, job_id: str) -> str:
        digest = hashlib.sha256(
            f"{job_id}:{self.id}:{self.label}:{self.detail}".encode()
        ).hexdigest()
        return f"graph-{digest[:32]}"


class WorkerReceipt(BaseModel):
    """What a worker did, with pointers to verifiable effects."""

    model_config = {"extra": "ignore"}

    node_id: str = Field(min_length=1, max_length=64)
    ok: bool = False
    spoken: str = Field(default="", max_length=2000)
    worker: str = Field(default="static", max_length=32)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=500)
    duration_ms: float = Field(default=0.0, ge=0.0)
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    resumption: dict[str, Any] | None = Field(default=None)

    @property
    def has_evidence(self) -> bool:
        return bool(self.artifacts or self.evidence)


class SupervisorVerdict(BaseModel):
    """Strict verdict shape. Parsed fail-closed; prose booleans never count."""

    model_config = {"extra": "ignore"}

    node_id: str = Field(min_length=1, max_length=64)
    state: NodeState = NodeState.UNKNOWN
    ok: bool | None = None
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    next: VerdictNext = VerdictNext.ESCALATE
    evidence_refs: list[str] = Field(default_factory=list)
    judge: str = Field(default="local", max_length=32)
    model: str | None = Field(default=None, max_length=160)


class StatusEvent(BaseModel):
    """One graph progress event. Spoken milestones are throttled; all persist."""

    model_config = {"extra": "ignore"}

    job_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=32)  # plan|started|milestone|verdict|done|blocked|failed
    text: str = Field(min_length=1, max_length=500)
    node_id: str | None = Field(default=None, max_length=64)
    important: bool = False
    at: float = Field(default_factory=time.time)


class GraphOutcome(BaseModel):
    """Joined job result in the existing delegation vocabulary."""

    model_config = {"extra": "ignore"}

    status: str = "failed"  # answered | waiting | failed
    spoken: str = Field(min_length=1, max_length=2000)
    nodes_total: int = Field(ge=0)
    nodes_accepted: int = Field(ge=0)
    receipts: list[WorkerReceipt] = Field(default_factory=list)
    verdicts: list[SupervisorVerdict] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


ProgressCallback = Callable[[StatusEvent], Awaitable[None]]


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": 300},
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "maxLength": 32},
                    "label": {"type": "string", "maxLength": 160},
                    "detail": {"type": "string", "maxLength": 2000},
                    "tier": {"type": "string", "enum": ["R", "W", "D"]},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "tool": {"type": ["string", "null"], "maxLength": 80},
                    "arguments": {"type": "object"},
                },
                "required": ["id", "label", "detail", "tier"],
            },
        },
    },
    "required": ["nodes"],
}

_PLANNER_SYSTEM = (
    "You are the Evie task planner. Decompose the owner's task into a small "
    "directed acyclic graph of narrow nodes. Rules: 1-8 nodes; every node has "
    "a stable kebab-case id, a short label, the exact work in detail, and a "
    "tier (R read-only, W reversible write, D destructive/external/"
    "irreversible). depends_on lists only earlier node ids; leave it empty "
    "for independent nodes. Set tool+arguments only when the node is exactly "
    "one semantic tool call (memory.search, life.mail, life.messages, "
    "timer.act, weather.get, capability.discover, and kin); otherwise omit "
    "them so a worker figures out the steps. Prefer fewer nodes: one node "
    "for one action. Never invent recipient names, file paths, or message "
    "bodies the owner did not give. File work is files.act: set tool to "
    "files.act with explicit arguments {op, path, ...} (op is discover, "
    "search, read, write, edit, append, mkdir, delete, copy, move, rename, "
    "run, summarize, reveal, or undo) instead of a free-text effect whenever "
    "the operation is clear. Screen and app control is computer.perform_effect "
    "with the concrete goal; coding work is code.act with the concrete goal."
)


def validate_dag(raw_nodes: list[dict[str, Any]], *, max_nodes: int) -> list[TaskNode]:
    """Parse planner output into an acyclic, dependency-closed node list."""

    try:
        nodes = [TaskNode.model_validate(raw) for raw in raw_nodes]
    except Exception as exc:
        raise GraphPlanError(f"planner nodes failed validation: {exc}") from exc
    if not nodes:
        raise GraphPlanError("planner returned no nodes")
    if len(nodes) > max_nodes:
        raise GraphPlanError(f"planner returned {len(nodes)} nodes, cap is {max_nodes}")
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise GraphPlanError("planner node ids must be unique")
    for node in nodes:
        for dep in node.depends_on:
            if dep == node.id:
                raise GraphPlanError(f"node {node.id!r} depends on itself")
            if dep not in by_id:
                raise GraphPlanError(f"node {node.id!r} depends on unknown {dep!r}")
    ordered: list[TaskNode] = []
    resolved: set[str] = set()
    remaining = list(nodes)
    while remaining:
        progressed = False
        for node in list(remaining):
            if all(dep in resolved for dep in node.depends_on):
                ordered.append(node)
                resolved.add(node.id)
                remaining.remove(node)
                progressed = True
        if not progressed:
            raise GraphPlanError("planner nodes contain a dependency cycle")
    return ordered


def execution_waves(nodes: list[TaskNode]) -> list[list[TaskNode]]:
    """Group validated nodes into parallel waves (Kahn levels)."""

    done: set[str] = set()
    remaining = list(nodes)
    waves: list[list[TaskNode]] = []
    while remaining:
        wave = [n for n in remaining if all(d in done for d in n.depends_on)]
        if not wave:
            raise GraphPlanError("planner nodes contain a dependency cycle")
        waves.append(wave)
        for node in wave:
            done.add(node.id)
            remaining.remove(node)
    return waves


async def plan_task(
    task: str,
    *,
    provider: Any | None = None,
    timeout: float | None = None,
) -> list[TaskNode]:
    """Decompose one delegated task into a validated DAG (one MiMo call)."""

    from app.contracts import ChatMessage
    from app.gateway.roles import text_role_available

    cleaned = (task or "").strip()
    if not cleaned:
        raise GraphPlanError("cannot plan an empty task")
    if provider is None:
        if not text_role_available():
            raise GraphUnavailable("planner unavailable: MiMo key is not set")
        from app.gateway.roles import require_text_provider

        provider = require_text_provider()
        if hasattr(provider, "reasoning_effort"):
            provider.reasoning_effort = "low"
    if not hasattr(provider, "chat_structured"):
        raise GraphUnavailable("planner unavailable: provider lacks structured output")
    messages = [
        ChatMessage(role="system", content=_PLANNER_SYSTEM),
        ChatMessage(role="user", content=cleaned[:8000]),
    ]
    limit = float(timeout or settings.cognitive_conversation_timeout_seconds or 25.0)
    try:
        result = await asyncio.wait_for(
            provider.chat_structured(messages, schema=PLAN_SCHEMA, schema_name="task_plan"),
            timeout=limit,
        )
    except TimeoutError as exc:
        raise GraphUnavailable("planner timed out") from exc
    import json as _json

    try:
        parsed = _json.loads((result.text or "").strip() or "{}")
    except ValueError as exc:
        raise GraphPlanError(f"planner returned invalid JSON: {exc}") from exc
    raw_nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(raw_nodes, list):
        raise GraphPlanError("planner returned no node list")
    return validate_dag(
        raw_nodes, max_nodes=int(getattr(settings, "graph_max_nodes", 8) or 8)
    )


class StatusThrottler:
    """Important events always pass; milestones pass at most every N seconds."""

    def __init__(self, *, min_interval_seconds: float | None = None) -> None:
        self.min_interval = float(
            min_interval_seconds
            if min_interval_seconds is not None
            else settings.graph_status_min_interval_seconds
        )
        self._last_spoken_at = 0.0

    def speakable(self, event: StatusEvent, *, now: float | None = None) -> bool:
        if event.important:
            return True
        if event.kind not in {"milestone", "started"}:
            return False
        at = now if now is not None else time.time()
        if at - self._last_spoken_at < self.min_interval:
            return False
        self._last_spoken_at = at
        return True


async def run_graph(
    task: str,
    *,
    job_id: str,
    actor: str = "master",
    live_session_id: str | None = None,
    device_id: str | None = None,
    progress: ProgressCallback | None = None,
    plan_provider: Any | None = None,
    decider: Any | None = None,
    nodes: list[TaskNode] | None = None,
) -> GraphOutcome:
    """Plan, execute in waves, supervise, and join one honest outcome.

    Pass ``nodes`` to skip planning and run a fixed node list (the resume
    path replays the remaining plan instead of re-planning the task).
    """

    from app.cognitive.supervisor import supervise
    from app.cognitive.worker import WorkerCtx, fast_path_eligible, run_node

    if nodes is None:
        nodes = await plan_task(task, provider=plan_provider)
    labels = ", ".join(node.label for node in nodes[:4])
    if progress is not None:
        await progress(
            StatusEvent(
                job_id=job_id,
                kind="plan",
                text=f"Planned {len(nodes)} step{'s' if len(nodes) != 1 else ''}: {labels}",
                important=True,
            )
        )
    ctx = WorkerCtx(actor=actor, live_session_id=live_session_id, device_id=device_id)
    receipts: list[WorkerReceipt] = []
    verdicts: list[SupervisorVerdict] = []
    waves = (
        [nodes]
        if len(nodes) == 1 and fast_path_eligible(nodes[0])
        else execution_waves(nodes)
    )
    fast = len(waves) == 1 and len(nodes) == 1 and fast_path_eligible(nodes[0])
    parallel = max(1, min(int(getattr(settings, "graph_max_parallel", 3) or 3), 6))
    gate = asyncio.Semaphore(parallel)

    async def _run_supervised(node: TaskNode) -> tuple[WorkerReceipt, SupervisorVerdict]:
        async with gate:
            if progress is not None:
                await progress(
                    StatusEvent(
                        job_id=job_id, kind="started", text=node.label, node_id=node.id
                    )
                )
            receipt = await run_node(node, ctx, job_id=job_id)
            verdict = await supervise(node, receipt, decider=decider)
            if verdict.next is VerdictNext.RETRY:
                retry_receipt = await run_node(node, ctx, job_id=job_id, attempt=2)
                retry_verdict = await supervise(
                    node, retry_receipt, decider=decider, attempt=2
                )
                if retry_verdict.next is not VerdictNext.RETRY:
                    return retry_receipt, retry_verdict
                retry_verdict.next = VerdictNext.ESCALATE
                return retry_receipt, retry_verdict
            return receipt, verdict

    if fast:
        node = nodes[0]
        if progress is not None:
            await progress(
                StatusEvent(job_id=job_id, kind="started", text=node.label, node_id=node.id)
            )
        receipt = await run_node(node, ctx, job_id=job_id)
        verdict = await supervise(node, receipt, decider=decider, fast_path=True)
        if verdict.next is VerdictNext.RETRY:
            receipt = await run_node(node, ctx, job_id=job_id, attempt=2)
            verdict = await supervise(
                node, receipt, decider=decider, fast_path=True, attempt=2
            )
            if verdict.next is VerdictNext.RETRY:
                verdict.next = VerdictNext.ESCALATE
        logger.info(
            "graph verdict job=%s node=%s worker=%s ok=%s state=%s next=%s err=%s ms=%.0f",
            job_id,
            verdict.node_id,
            receipt.worker,
            receipt.ok,
            verdict.state.value,
            verdict.next.value,
            receipt.error or "-",
            receipt.duration_ms,
        )
        receipts.append(receipt)
        verdicts.append(verdict)
    else:
        for wave in waves:
            if any(v.next is VerdictNext.ASK_OWNER for v in verdicts):
                break
            results = await asyncio.gather(*(_run_supervised(n) for n in wave))
            for receipt, verdict in results:
                receipts.append(receipt)
                verdicts.append(verdict)
                logger.info(
                    "graph verdict job=%s node=%s worker=%s ok=%s state=%s next=%s err=%s ms=%.0f",
                    job_id,
                    verdict.node_id,
                    receipt.worker,
                    receipt.ok,
                    verdict.state.value,
                    verdict.next.value,
                    receipt.error or "-",
                    receipt.duration_ms,
                )
                if progress is not None:
                    await progress(
                        StatusEvent(
                            job_id=job_id,
                            kind="verdict",
                            text=f"{verdict.node_id}: {verdict.state.value}",
                            node_id=verdict.node_id,
                            important=verdict.next
                            in {VerdictNext.ASK_OWNER, VerdictNext.ESCALATE},
                        )
                    )
    return join_outcome(
        job_id=job_id,
        receipts=receipts,
        verdicts=verdicts,
        nodes_total=len(nodes),
        nodes=nodes,
    )


def join_outcome(
    *,
    job_id: str,
    receipts: list[WorkerReceipt],
    verdicts: list[SupervisorVerdict],
    nodes_total: int,
    nodes: list[TaskNode] | None = None,
) -> GraphOutcome:
    """Join verdicts using the existing delegation vocabulary.

    answered = every node accepted. waiting = the owner must answer or
    approve something. failed = anything else, with partial progress named.
    The plan persists in evidence so the answer door can resume the exact
    remaining nodes instead of re-planning.
    """

    del job_id
    accepted = sum(1 for v in verdicts if v.next is VerdictNext.ACCEPT)
    asked = [v for v in verdicts if v.next is VerdictNext.ASK_OWNER]
    failed = [v for v in verdicts if v.state is NodeState.FAILED]
    evidence: dict[str, Any] = {
        "nodes_total": nodes_total,
        "nodes_accepted": accepted,
        "receipts": [r.model_dump() for r in receipts],
        "verdicts": [v.model_dump() for v in verdicts],
    }
    if nodes is not None:
        evidence["plan"] = [n.model_dump() for n in nodes]
    if verdicts and accepted == len(verdicts) == nodes_total:
        spoken_bits = [r.spoken for r in receipts if r.spoken.strip()]
        spoken = spoken_bits[-1] if spoken_bits else "Done."
        return GraphOutcome(
            status="answered",
            spoken=spoken[:2000],
            nodes_total=nodes_total,
            nodes_accepted=accepted,
            receipts=receipts,
            verdicts=verdicts,
            evidence=evidence,
        )
    if asked:
        questions = "; ".join(
            (v.reasons[0] if v.reasons else f"{v.node_id} needs input") for v in asked
        )
        return GraphOutcome(
            status="waiting",
            spoken=f"I need your input before continuing: {questions}"[:2000],
            nodes_total=nodes_total,
            nodes_accepted=accepted,
            receipts=receipts,
            verdicts=verdicts,
            evidence=evidence,
        )
    done_labels = [
        r.node_id for r, v in zip(receipts, verdicts, strict=False) if v.next is VerdictNext.ACCEPT
    ]
    bad_labels = [v.node_id for v in failed] or [
        v.node_id for v in verdicts if v.next is not VerdictNext.ACCEPT
    ]
    spoken = "I couldn't finish that request."
    if done_labels:
        spoken += f" Finished: {', '.join(done_labels)}."
    if bad_labels:
        spoken += f" Not finished: {', '.join(bad_labels)}."
    return GraphOutcome(
        status="failed",
        spoken=spoken[:2000],
        nodes_total=nodes_total,
        nodes_accepted=accepted,
        receipts=receipts,
        verdicts=verdicts,
        evidence=evidence,
    )


def new_job_id() -> str:
    return uuid4().hex
