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
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.cognitive.capabilities import tool_specs
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
    # Optional per-node override. None means "use the global node budget"
    # (EV_GRAPH_NODE_TIMEOUT_SECONDS); a fixed 60s default made that setting
    # unreachable for every planner node.
    timeout_seconds: float | None = Field(default=None, ge=5.0, le=300.0)

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
    # Multi-shift job status: how many plan->work->judge shifts ran, what the
    # manager report says per step, and which steps are still open.
    shifts: int = Field(default=1, ge=1)
    remaining: list[str] = Field(default_factory=list)
    status_report: str = Field(default="", max_length=8000)


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
                    "timeout_seconds": {"type": ["number", "null"], "minimum": 5, "maximum": 300},
                },
                "required": ["id", "label", "detail", "tier"],
            },
        },
    },
    "required": ["nodes"],
}

def _planner_tool_menu() -> str:
    """Full semantic vocabulary for the planner, generated every import.

    A hand-picked subset is how sends broke: the menu named life.messages
    but not life.send, so the planner bound every messaging node to the
    read tool and every send failed supervision with nothing attempted.
    """
    try:
        names = sorted({spec.name for spec in tool_specs() if spec.name})
    except Exception:
        names = []
    if not names:
        names = [
            "memory.search",
            "life.mail",
            "life.messages",
            "life.send",
            "timer.act",
            "weather.get",
            "capability.discover",
        ]
    return ", ".join(names)


_PLANNER_TOOL_MENU = _planner_tool_menu()


def _read_only_tool_names() -> frozenset[str]:
    """Names the capability graph marks read-only (tier R at most)."""
    try:
        return frozenset(spec.name for spec in tool_specs() if spec.read_only)
    except Exception:
        return frozenset({"memory.search", "life.mail", "life.messages"})


_PLANNER_SYSTEM = (
    "You are the Evie task planner. Decompose the owner's task into a small "
    "directed acyclic graph of narrow nodes. Rules: 1-8 nodes; every node has "
    "a stable kebab-case id, a short label, the exact work in detail, and a "
    "tier (R read-only, W reversible write, D destructive/external/"
    "irreversible). depends_on lists only earlier node ids; leave it empty "
    "for independent nodes. Set tool+arguments only when the node is exactly "
    f"one semantic tool call ({_PLANNER_TOOL_MENU}); otherwise omit "
    "them so a worker figures out the steps. "
    "Outbound sends are life.send with tier D and arguments "
    "{channel, to, text, subject?} carrying the owner's exact words: always "
    "emit the send node — execution parks it for owner approval, and a read "
    "or draft is never a send. Life reads return a short spoken "
    "digest (who plus gist per item), never full bodies or per-item timestamps "
    "— write read-node detail a digest can satisfy, keep synthesis detail to "
    "the digest's who+what, and give synthesis nodes depends_on so they "
    "answer from established results. Write read counts as up-to-N (\"up to 3 "
    "recent texts\"): fewer rows than N is complete when that is all there "
    "is, never a shortfall. timeout_seconds is optional and "
    "bounded 5-300: set it only when a node genuinely needs less or more than "
    "the default; otherwise omit it. A WORK ALREADY ATTEMPTED section may "
    "list steps already done or not done: never plan a DONE step again, and "
    "target only what remains. Prefer fewer nodes: one node "
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
    read_only = _read_only_tool_names()
    for node in nodes:
        hint = (node.tool or "").strip()
        if not hint or node.tier not in (TaskTier.W, TaskTier.D):
            continue
        if hint in {"life.messages", "life.mail"}:
            # A write-tier node on a messaging read tool is always a
            # misplanned send: no message/mail delete tool exists in the
            # vocabulary, so send is the only write intent in this domain
            # (live catch: a WhatsApp send planned as tier-W life.messages
            # executed a read, scored 0.1, and failed with nothing
            # attempted). Rewrite to the send tool at tier D so the node
            # takes the standard park-for-approval path instead.
            args = dict(node.arguments or {})
            to = next(
                (str(args.get(key) or "").strip() for key in (
                    "to", "name", "recipient", "display", "target",
                    "destination", "query", "q",
                ) if str(args.get(key) or "").strip()),
                "",
            )
            text = next(
                (str(args.get(key) or "") for key in (
                    "text", "body", "message", "content",
                ) if str(args.get(key) or "").strip()),
                "",
            )
            rebuilt = {}
            if to:
                rebuilt["to"] = to
            if text:
                rebuilt["text"] = text
            for key in ("channel", "subject"):
                if args.get(key) not in (None, ""):
                    rebuilt[key] = args[key]
            logger.warning(
                "planner bound tier-%s node %r to %r; rewriting to tier-D life.send",
                node.tier.value,
                node.id,
                hint,
            )
            node.tool = "life.send"
            node.tier = TaskTier.D
            node.arguments = rebuilt
        elif hint in read_only:
            # Any other write-tier node on a read-only tool can never
            # satisfy supervision. Drop the hint so the node routes to a
            # worker with the full vocabulary instead of deterministically
            # executing the wrong tool. Arguments stay: the worker reuses
            # whatever the planner already extracted.
            logger.warning(
                "planner bound tier-%s node %r to read-only tool %r; dropping hint",
                node.tier.value,
                node.id,
                hint,
            )
            node.tool = None
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
    context: str | None = None,
) -> list[TaskNode]:
    """Decompose one delegated task into a validated DAG (one MiMo call).

    ``context`` is the manager report from the previous shift when a job is
    being re-planned: it names what is DONE and what is not, so the new plan
    covers only the remaining work instead of repeating finished effects.
    """

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
    content = cleaned[:8000]
    if context:
        content = (
            content
            + "\n\nWORK ALREADY ATTEMPTED (plan only what remains; never "
            "repeat a DONE step):\n"
            + context[:4000]
        )
    messages = [
        ChatMessage(role="system", content=_PLANNER_SYSTEM),
        ChatMessage(role="user", content=content),
    ]
    limit = float(
        timeout
        or getattr(settings, "graph_plan_timeout_seconds", None)
        or settings.cognitive_conversation_timeout_seconds
        or 25.0
    )
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


def _shift_cap() -> int:
    """How many plan -> work -> judge shifts one delegated job may run."""

    raw = int(getattr(settings, "graph_max_shifts", 2) or 2)
    return max(1, min(raw, 3))


def shift_report(
    receipts: list[WorkerReceipt],
    verdicts: list[SupervisorVerdict],
    nodes: list[TaskNode],
) -> str:
    """Manager status for a shift: what is done, what is not, and why.

    This is the supervisor layer's report back to the kernel. It feeds the
    next shift's planner context, so every line names the exact step, state,
    score, evidence count and error — never a bare "failed".
    """

    by_id = {node.id: node for node in nodes}
    lines: list[str] = []
    for receipt, verdict in zip(receipts, verdicts, strict=False):
        node = by_id.get(receipt.node_id)
        label = node.label if node is not None else receipt.node_id
        detail = ((node.detail if node is not None else "") or "")[:240]
        why = "; ".join(verdict.reasons[:2]) or verdict.next.value
        if verdict.next is VerdictNext.ACCEPT:
            status = "DONE"
        elif verdict.next is VerdictNext.ASK_OWNER:
            status = "NEEDS_OWNER"
        else:
            status = "NOT_DONE"
        evidence = len(receipt.evidence) + len(receipt.artifacts)
        error = f", error={receipt.error}" if receipt.error else ""
        lines.append(
            f"- {status}: {label} (state={verdict.state.value}, "
            f"score={float(verdict.score):.2f}, evidence={evidence}{error}) {why}"
        )
        if detail:
            lines.append(f"    detail: {detail}")
    return "\n".join(lines)


async def _execute_plan(
    nodes: list[TaskNode],
    *,
    ctx: Any,
    job_id: str,
    progress: ProgressCallback | None,
    decider: Any | None,
    prior_shift: Sequence[WorkerReceipt] = (),
    allow_fast: bool = True,
) -> tuple[list[WorkerReceipt], list[SupervisorVerdict]]:
    """Run one shift's DAG: waves, one supervisor per node, one retry."""

    from app.cognitive.supervisor import supervise
    from app.cognitive.worker import fast_path_eligible, run_node

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
            # Waves run in order, so earlier waves' receipts are final here.
            # A synthesis node builds on its depends_on results instead of
            # re-fetching blind (live catch: it never saw them at all).
            # Earlier shifts' receipts ride along too: a shift-2 synthesis
            # node names shift-1 results it cannot depend on.
            wanted = set(node.depends_on or [])
            combined = [*prior_shift, *(r for r in receipts if r.node_id in wanted)]
            deduped: dict[str, WorkerReceipt] = {}
            for receipt in combined:
                deduped[receipt.node_id] = receipt
            prior = list(deduped.values())
            receipt = await run_node(node, ctx, job_id=job_id, prior=prior)
            verdict = await supervise(node, receipt, decider=decider)
            if verdict.next is VerdictNext.RETRY:
                # The supervisor's reasons are the retry's brief: the worker
                # must fix the named failure, not repeat it. Quote the previous
                # answer too — a stub ("Here's your combined summary:" with
                # nothing after) is only fixable when the model sees its stub.
                hint = "; ".join(verdict.reasons[:3]) or verdict.state.value
                if (receipt.spoken or "").strip():
                    hint = f"Your previous answer was: {receipt.spoken[:400]}. " + hint
                retry_receipt = await run_node(
                    node, ctx, job_id=job_id, attempt=2, hint=hint, prior=prior
                )
                retry_verdict = await supervise(
                    node, retry_receipt, decider=decider, attempt=2
                )
                if retry_verdict.next is not VerdictNext.RETRY:
                    return retry_receipt, retry_verdict
                retry_verdict.next = VerdictNext.ESCALATE
                return retry_receipt, retry_verdict
            return receipt, verdict

    # Replans never fast-path: a shift-2 single node would otherwise skip the
    # verdict model, so a cop-out replan (one narrow thread for a six-item
    # summary ask — live catch) auto-accepts and the job "answers" short.
    if fast and allow_fast:
        node = nodes[0]
        if progress is not None:
            await progress(
                StatusEvent(job_id=job_id, kind="started", text=node.label, node_id=node.id)
            )
        receipt = await run_node(node, ctx, job_id=job_id)
        verdict = await supervise(node, receipt, decider=decider, fast_path=True)
        if verdict.next is VerdictNext.RETRY:
            hint = "; ".join(verdict.reasons[:3]) or verdict.state.value
            if (receipt.spoken or "").strip():
                hint = f"Your previous answer was: {receipt.spoken[:400]}. " + hint
            receipt = await run_node(node, ctx, job_id=job_id, attempt=2, hint=hint)
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
        return receipts, verdicts
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
    return receipts, verdicts


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
    """Plan, execute, supervise, re-plan what remains, join an honest outcome.

    A job is a sequence of shifts (plan -> work -> judge) capped by
    ``graph_max_shifts``. After each shift the manager report names what is
    done and what is not; the next shift plans only the remaining work with
    that report as context. Owner questions and tier-D stops end the loop
    immediately — a re-plan never overrides the owner.

    Pass ``nodes`` to skip planning and run a fixed node list in one shift
    (the resume path replays the remaining plan instead of re-planning).
    """

    from app.cognitive.worker import WorkerCtx

    ctx = WorkerCtx(actor=actor, live_session_id=live_session_id, device_id=device_id)
    if nodes is not None:
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
        fixed_receipts, fixed_verdicts = await _execute_plan(
            nodes, ctx=ctx, job_id=job_id, progress=progress, decider=decider
        )
        by_id = {node.id: node for node in nodes}
        fixed_remaining = [
            (by_id[v.node_id].label if v.node_id in by_id else v.node_id)
            for v in fixed_verdicts
            if v.next is not VerdictNext.ACCEPT
        ]
        return join_outcome(
            job_id=job_id,
            receipts=fixed_receipts,
            verdicts=fixed_verdicts,
            nodes_total=len(nodes),
            nodes=nodes,
            shifts=1,
            status_report=shift_report(fixed_receipts, fixed_verdicts, nodes),
            remaining=fixed_remaining,
        )
    receipts: list[WorkerReceipt] = []
    verdicts: list[SupervisorVerdict] = []
    planned: list[TaskNode] = []
    context: str | None = None
    remaining: list[str] = []
    shifts = 0
    max_shifts = _shift_cap()
    for shift in range(1, max_shifts + 1):
        try:
            nodes = await plan_task(task, provider=plan_provider, context=context)
        except (GraphPlanError, GraphUnavailable):
            # Only the FIRST plan may fail pre-execution and bubble up to the
            # legacy single-turn fallback. A follow-up plan fails after
            # side effects already ran: end the job honestly with the work
            # that happened, because a legacy rerun could execute it twice.
            if shift == 1:
                raise
            break
        shifts = shift
        planned.extend(nodes)
        labels = ", ".join(node.label for node in nodes[:4])
        if progress is not None:
            await progress(
                StatusEvent(
                    job_id=job_id,
                    kind="plan",
                    text=(
                        f"Planned {len(nodes)} step{'s' if len(nodes) != 1 else ''}"
                        f"{' (shift ' + str(shift) + ')' if shift > 1 else ''}: {labels}"
                    ),
                    important=True,
                )
            )
        shift_receipts, shift_verdicts = await _execute_plan(
            nodes, ctx=ctx, job_id=job_id, progress=progress, decider=decider,
            prior_shift=receipts, allow_fast=(shift == 1),
        )
        receipts.extend(shift_receipts)
        verdicts.extend(shift_verdicts)
        by_id = {node.id: node for node in nodes}
        remaining = [
            (by_id[v.node_id].label if v.node_id in by_id else v.node_id)
            for v in shift_verdicts
            if v.next is not VerdictNext.ACCEPT
        ]
        if any(v.next is VerdictNext.ASK_OWNER for v in shift_verdicts):
            break
        if not remaining or shift >= max_shifts:
            break
        context = shift_report(shift_receipts, shift_verdicts, nodes)
    report = shift_report(receipts, verdicts, planned)
    return join_outcome(
        job_id=job_id,
        receipts=receipts,
        verdicts=verdicts,
        nodes_total=len(planned),
        nodes=planned,
        shifts=shifts,
        status_report=report,
        remaining=remaining,
    )


def join_outcome(
    *,
    job_id: str,
    receipts: list[WorkerReceipt],
    verdicts: list[SupervisorVerdict],
    nodes_total: int,
    nodes: list[TaskNode] | None = None,
    shifts: int = 1,
    status_report: str = "",
    remaining: list[str] | None = None,
) -> GraphOutcome:
    """Join verdicts using the existing delegation vocabulary.

    answered = every node accepted, including a follow-up shift that finished
    the first shift's remainder. waiting = the owner must answer or approve
    something. failed = anything else, with partial progress named. The plan
    persists in evidence so the answer door can resume the exact remaining
    nodes instead of re-planning.
    """

    del job_id
    accepted = sum(1 for v in verdicts if v.next is VerdictNext.ACCEPT)
    asked = [v for v in verdicts if v.next is VerdictNext.ASK_OWNER]
    # The manager report is bounded here, at the contract's edge: a 2-shift
    # job with retries must never fail validation after real work has run.
    status_report = (status_report or "")[:8000]
    open_labels = (
        list(remaining)
        if remaining is not None
        else [v.node_id for v in verdicts if v.next is not VerdictNext.ACCEPT]
    )
    evidence: dict[str, Any] = {
        "nodes_total": nodes_total,
        "nodes_accepted": accepted,
        "shifts": shifts,
        "remaining": open_labels,
        "status_report": status_report,
        "receipts": [r.model_dump() for r in receipts],
        "verdicts": [v.model_dump() for v in verdicts],
    }
    if nodes is not None:
        evidence["plan"] = [n.model_dump() for n in nodes]
    if verdicts and not open_labels:
        # One spoken per node, retries and refetches superseded: without the
        # dedup a retried node would speak twice.
        by_node: dict[str, str] = {}
        for receipt in receipts:
            if receipt.spoken.strip():
                by_node[receipt.node_id] = receipt.spoken
        spoken_bits = list(by_node.values())
        last = receipts[-1] if receipts else None
        synthesized = (
            last is not None
            and last.worker == "mimo"
            and any(
                isinstance(entry, dict) and entry.get("source") == "prior_step"
                for entry in (last.evidence or [])
            )
        )
        if synthesized or len(spoken_bits) <= 1:
            spoken = spoken_bits[-1] if spoken_bits else "Done."
        else:
            # No synthesis node: a multi-fetch plan would otherwise answer
            # with only the last digest (live catch: the mail digest never
            # reached the owner). Join every accepted node's spoken instead.
            spoken = "\n\n".join(spoken_bits)
        return GraphOutcome(
            status="answered",
            spoken=spoken[:2000],
            nodes_total=nodes_total,
            nodes_accepted=accepted,
            receipts=receipts,
            verdicts=verdicts,
            evidence=evidence,
            shifts=shifts,
            remaining=[],
            status_report=status_report,
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
            shifts=shifts,
            remaining=open_labels,
            status_report=status_report,
        )
    done_labels = [
        r.node_id for r, v in zip(receipts, verdicts, strict=False) if v.next is VerdictNext.ACCEPT
    ]
    spoken = "I couldn't finish that request."
    if done_labels:
        spoken += f" Finished: {', '.join(done_labels)}."
    if open_labels:
        spoken += f" Not finished: {', '.join(open_labels[:4])}."
    return GraphOutcome(
        status="failed",
        spoken=spoken[:2000],
        nodes_total=nodes_total,
        nodes_accepted=accepted,
        receipts=receipts,
        verdicts=verdicts,
        evidence=evidence,
        shifts=shifts,
        remaining=open_labels,
        status_report=status_report,
    )


def new_job_id() -> str:
    return uuid4().hex
