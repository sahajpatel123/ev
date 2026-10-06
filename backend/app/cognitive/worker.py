"""Graph workers: specialists first, MiMo executor second.

Routing is explicit, not clever. File nodes run the file worker (sandbox ops
directly, confirm-aware). Computer nodes run the computer worker (ground the
screen, then act). A node carrying exactly one other known semantic tool runs
it once through the existing adapter layer. Anything else — open-ended steps,
multi-tool sequences — runs a bounded MiMo tool loop with an executor
personality (narrow, evidence-only, never chatty).

Workers never touch the shared live session: `execute_semantic` persists via
the session store, so each node runs under snapshot/restore isolation and its
own database session. Tier-D nodes never execute here; they return a blocked
receipt so the supervisor asks the owner.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.cognitive.graph import (
    GraphUnavailable,
    TaskNode,
    TaskTier,
    WorkerReceipt,
)
from app.config import settings

logger = logging.getLogger("ev.cognitive.worker")

_EXECUTOR_SYSTEM = (
    "You are the Evie task executor, not the conversationalist. Do the single "
    "assignment below with the listed tools and stop. Rules: use only listed "
    "tools with real arguments; never invent recipients, paths, or message "
    "bodies; never repeat an identical tool call; never narrate or chit-chat "
    "— tool calls first, and when no more calls are needed reply with one "
    "short factual sentence stating what was done or what blocked you. "
    "Destructive, sending, or irreversible actions are forbidden for you: if "
    "the assignment needs one, reply that it needs owner approval instead."
)

_MIMO_WORKER_ROUNDS = 3

_FILE_OPS = frozenset(
    {
        "discover", "index", "status", "search", "read", "list", "open",
        "summarize", "reveal", "finder", "write", "edit", "append", "mkdir",
        "delete", "copy", "move", "rename", "run", "undo",
    }
)


@dataclass
class WorkerCtx:
    actor: str = "master"
    live_session_id: str | None = None
    device_id: str | None = None
    steering_seen: int = 0


def fast_path_eligible(node: TaskNode) -> bool:
    """Single read-only nodes skip the verdict model (evidence check only)."""

    return node.tier is TaskTier.R


def known_semantic_tools() -> set[str]:
    from app.cognitive.capabilities import tool_specs

    return {spec.name for spec in tool_specs()}


def _as_evidence_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)][:40]
    if isinstance(value, dict):
        return [value]
    return []


def _payload_evidence(node: TaskNode, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence is returned substance, never a bare assertion.

    Explicit `evidence` wins. Otherwise the result body's own payload keys
    (ids, rows, counts — everything except the spoken/ok/error envelope)
    count as substance. A body carrying only `ok`/`spoken` yields nothing:
    an assertion without substance must escalate, never accept.
    """

    explicit = _as_evidence_list(body.get("evidence"))
    if explicit:
        return explicit
    skip = {"ok", "spoken", "error", "diagnosis", "tool", "duration_ms"}
    payload_keys = sorted(k for k in body if k not in skip)[:12]
    if not payload_keys:
        return []
    return [{"tool": body.get("tool") or node.tool or "worker", "payload_keys": payload_keys}]


def receipt_from_result(
    node: TaskNode, result: dict[str, Any], *, worker: str, duration_ms: float
) -> WorkerReceipt:
    body = dict(result or {})
    ok = bool(body.get("ok"))
    spoken = str(body.get("spoken") or body.get("error") or "").strip()
    error = body.get("error")
    return WorkerReceipt(
        node_id=node.id,
        ok=ok,
        spoken=spoken[:2000],
        worker=worker,
        artifacts=_as_evidence_list(body.get("artifacts")),
        evidence=_payload_evidence(node, body),
        error=str(error)[:500] if error else None,
        duration_ms=max(0.0, float(duration_ms)),
    )


def _confirmation_receipt(
    node: TaskNode,
    *,
    worker: str,
    spoken: str,
    resumption: dict[str, Any],
    duration_ms: float,
) -> WorkerReceipt:
    """A worker asked to do something gated and stopped for the owner.

    The resumption carries everything the answer door needs to replay the
    exact call with owner approval: the node snapshot plus the approved
    operation and arguments. Nothing here executes.
    """

    return WorkerReceipt(
        node_id=node.id,
        ok=False,
        spoken=spoken[:2000],
        worker=worker,
        error="confirmation_required",
        resumption=resumption,
        duration_ms=max(0.0, float(duration_ms)),
    )


def _file_op_from_node(node: TaskNode) -> tuple[str | None, dict[str, Any]]:
    """Explicit file operation from a tool hint, or (None, {}) for effects."""

    tool = (node.tool or "").strip()
    args = dict(node.arguments or {})
    if tool.startswith("files.") and tool != "files.act":
        op = tool[len("files.") :]
        return (op if op in _FILE_OPS else None), args
    if tool == "files.act" and isinstance(args.get("op"), str):
        op = str(args["op"]).strip().lower()
        return (op if op in _FILE_OPS else None), args
    return None, args


class _SessionGuard:
    """Snapshot/restore the shared cognitive session around worker execution.

    `execute_semantic` persists through the session store (remember/save).
    Without isolation a worker would overwrite the live turn ledger that the
    owner's surfaces share. Both the in-memory handle and the file bytes are
    restored, so a worker run is invisible to concurrent turns.
    """

    def __init__(self) -> None:
        self._active: Any = None
        self._stamp: Any = None
        self._file_bytes: bytes | None = None
        self._had_file = False

    def __enter__(self) -> _SessionGuard:
        from app.cognitive import session_store

        self._active = session_store._ACTIVE
        self._stamp = session_store._ACTIVE_STAMP
        try:
            path = session_store._path()
            if path.exists():
                self._file_bytes = path.read_bytes()
                self._had_file = True
        except OSError:
            self._file_bytes = None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        from app.cognitive import session_store

        session_store._ACTIVE = self._active
        session_store._ACTIVE_STAMP = self._stamp
        try:
            path = session_store._path()
            if self._had_file and self._file_bytes is not None:
                path.write_bytes(self._file_bytes)
            elif not self._had_file and path.exists():
                path.unlink()
        except OSError:
            logger.warning("worker session restore failed; live ledger may be stale")


async def _static_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    execute_fn: Any | None = None,
) -> WorkerReceipt:
    from app.cognitive.session_store import CognitiveSession
    from app.db import SessionLocal

    started = time.perf_counter()
    tool = (node.tool or "").strip()
    if not tool:
        return WorkerReceipt(
            node_id=node.id, ok=False, worker="static", error="no_tool_hint"
        )
    try:
        async with SessionLocal() as session:
            cognition = CognitiveSession(session_id=f"graph-{node.id}")
            call = execute_fn
            if call is None:
                from app.cognitive.executor import execute_semantic

                call = execute_semantic
            result = await call(
                session,
                tool,
                dict(node.arguments or {}),
                cognition=cognition,
                actor=ctx.actor,
                live_session_id=ctx.live_session_id,
                steering_seen=ctx.steering_seen,
                device_id=ctx.device_id,
            )
    except Exception as exc:  # noqa: BLE001 - one node must not kill the job
        logger.warning("static worker failed node=%s error=%s", node.id, type(exc).__name__)
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="static",
            error=f"{type(exc).__name__}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad_tool_result"}
    return receipt_from_result(
        node, result, worker="static", duration_ms=(time.perf_counter() - started) * 1000
    )


async def _file_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    execute_fn: Any | None = None,
) -> WorkerReceipt:
    """Run file nodes against the sandbox directly, confirm-aware.

    Explicit ops (`files.<op>` or `files.act` with `{op, ...}`) execute
    here without the computer-narration detour. A gated op returns a
    confirmation receipt — never a blind failure — carrying the exact
    resumption the owner can approve. Free-text effects fall through to
    the semantic path, whose result is mapped the same way.
    """

    from app.ev.file_sandbox import execute_op

    started = time.perf_counter()
    op, args = _file_op_from_node(node)
    params = {k: v for k, v in args.items() if k != "confirm"}
    if op is None:
        return await _file_effect_worker(node, ctx=ctx, execute_fn=execute_fn)
    try:
        result = execute_op(op, params, origin="graph", confirm=False)
    except Exception as exc:  # noqa: BLE001 - one node must not kill the job
        logger.warning("file worker failed node=%s error=%s", node.id, type(exc).__name__)
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="file",
            error=f"{type(exc).__name__}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad_tool_result"}
    if result.get("error") == "confirmation_required" or result.get("needs_confirm"):
        return _confirmation_receipt(
            node,
            worker="file",
            spoken=str(result.get("spoken") or f"Confirm {op} and I'll do it."),
            resumption={"node": node.model_dump(), "op": op, "params": params},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    return receipt_from_result(
        node, result, worker="file", duration_ms=(time.perf_counter() - started) * 1000
    )


async def _file_effect_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    execute_fn: Any | None = None,
) -> WorkerReceipt:
    """Free-text file effects run the semantic path with confirm mapping."""

    from app.cognitive.session_store import CognitiveSession
    from app.db import SessionLocal

    started = time.perf_counter()
    try:
        async with SessionLocal() as session:
            cognition = CognitiveSession(session_id=f"graph-{node.id}")
            call = execute_fn
            if call is None:
                from app.cognitive.executor import execute_semantic

                call = execute_semantic
            result = await call(
                session,
                "files.act",
                dict(node.arguments or {}),
                cognition=cognition,
                actor=ctx.actor,
                live_session_id=ctx.live_session_id,
                steering_seen=ctx.steering_seen,
                device_id=ctx.device_id,
            )
    except Exception as exc:  # noqa: BLE001 - one node must not kill the job
        logger.warning("file worker failed node=%s error=%s", node.id, type(exc).__name__)
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="file",
            error=f"{type(exc).__name__}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad_tool_result"}
    if result.get("error") == "confirmation_required" or result.get("needs_confirm"):
        return _confirmation_receipt(
            node,
            worker="file",
            spoken=str(result.get("spoken") or "Confirm that file step and I'll do it."),
            resumption={"node": node.model_dump()},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    return receipt_from_result(
        node, result, worker="file", duration_ms=(time.perf_counter() - started) * 1000
    )


async def _computer_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    execute_fn: Any | None = None,
) -> WorkerReceipt:
    """Ground the screen first, then act with the observation attached.

    Vague UI goals fail when the actor works blind. The grounding call is
    read-only; when it fails the worker still attempts the act with the
    raw goal rather than failing on grounding alone.
    """

    from app.cognitive.session_store import CognitiveSession
    from app.db import SessionLocal

    started = time.perf_counter()
    steps: list[dict[str, Any]] = []
    args = dict(node.arguments or {})
    goal = str(args.get("goal") or args.get("effect") or node.detail or node.label)
    try:
        async with SessionLocal() as session:
            cognition = CognitiveSession(session_id=f"graph-{node.id}")
            call = execute_fn
            if call is None:
                from app.cognitive.executor import execute_semantic

                call = execute_semantic
            if (node.tool or "").strip() != "computer.observe":
                ground = await call(
                    session,
                    "look.capture",
                    {"prompt": f"Describe the screen for this goal: {goal}"[:1000]},
                    cognition=cognition,
                    actor=ctx.actor,
                    live_session_id=ctx.live_session_id,
                    steering_seen=ctx.steering_seen,
                    device_id=ctx.device_id,
                )
                if not isinstance(ground, dict):
                    ground = {"ok": False, "error": "bad_tool_result"}
                steps.append(
                    {"phase": "ground", "ok": bool(ground.get("ok")),
                     "error": ground.get("error")}
                )
                seen = str(ground.get("spoken") or "").strip()
                if seen:
                    goal = f"{goal}\nScreen now shows: {seen[:1500]}"
            if (node.tool or "").strip() == "computer.observe":
                result = await call(
                    session,
                    "computer.observe",
                    {"goal": goal},
                    cognition=cognition,
                    actor=ctx.actor,
                    live_session_id=ctx.live_session_id,
                    steering_seen=ctx.steering_seen,
                    device_id=ctx.device_id,
                )
            else:
                result = await call(
                    session,
                    "computer.perform_effect",
                    {"effect": goal},
                    cognition=cognition,
                    actor=ctx.actor,
                    live_session_id=ctx.live_session_id,
                    steering_seen=ctx.steering_seen,
                    device_id=ctx.device_id,
                )
    except Exception as exc:  # noqa: BLE001 - one node must not kill the job
        logger.warning("computer worker failed node=%s error=%s", node.id, type(exc).__name__)
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="computer",
            error=f"{type(exc).__name__}",
            rounds=steps,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad_tool_result"}
    steps.append(
        {"phase": "act", "ok": bool(result.get("ok")), "error": result.get("error")}
    )
    if result.get("error") == "confirmation_required" or result.get("needs_confirm"):
        return _confirmation_receipt(
            node,
            worker="computer",
            spoken=str(result.get("spoken") or "Confirm that computer step and I'll do it."),
            resumption={"node": node.model_dump()},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    receipt = receipt_from_result(
        node, result, worker="computer", duration_ms=(time.perf_counter() - started) * 1000
    )
    receipt.rounds = steps
    return receipt


async def _mimo_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    provider: Any | None = None,
    execute_fn: Any | None = None,
    partial_box: dict[str, Any] | None = None,
) -> WorkerReceipt:
    from app.cognitive.session_store import CognitiveSession
    from app.cognitive.speed import tool_specs_for_turn
    from app.contracts import ChatMessage
    from app.db import SessionLocal

    started = time.perf_counter()
    if provider is None:
        from app.gateway.roles import require_text_provider, text_role_available

        if not text_role_available():
            raise GraphUnavailable("worker unavailable: MiMo key is not set")
        provider = require_text_provider()
        if hasattr(provider, "reasoning_effort"):
            provider.reasoning_effort = "low"
    # Delegated workers get the full surface: compact omits files.act /
    # computer / code, so file nodes could never execute from this loop.
    specs = tool_specs_for_turn(compact=False)
    allowed = {spec.name for spec in specs}
    messages = [
        ChatMessage(role="system", content=_EXECUTOR_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                f"Assignment: {node.label}\n{node.detail}\n"
                f"Tier: {node.tier.value} (R read-only, W reversible, "
                "D approval-required — you must not perform D actions)."
            )[:4000],
        ),
    ]
    ran = 0
    failures = 0
    last_spoken = ""
    tool_evidence: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    try:
        async with SessionLocal() as session:
            cognition = CognitiveSession(session_id=f"graph-{node.id}")
            call = execute_fn
            if call is None:
                from app.cognitive.executor import execute_semantic

                call = execute_semantic
            for _ in range(_MIMO_WORKER_ROUNDS):
                result = await provider.chat_with_tools(messages, specs)
                calls = [c for c in (result.tool_calls or []) if c.name in allowed]
                if not calls:
                    text = (result.text or "").strip()
                    if text and not last_spoken:
                        last_spoken = text[:500]
                    break
                for tool_call in calls:
                    signature = f"{tool_call.name}:{sorted((tool_call.arguments or {}).items())}"
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    try:
                        out = await call(
                            session,
                            str(tool_call.name),
                            dict(tool_call.arguments or {}),
                            cognition=cognition,
                            actor=ctx.actor,
                            live_session_id=ctx.live_session_id,
                            steering_seen=ctx.steering_seen,
                            device_id=ctx.device_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - tool failure is data
                        out = {"ok": False, "error": type(exc).__name__}
                    if not isinstance(out, dict):
                        out = {"ok": False, "error": "bad_tool_result"}
                    ran += 1
                    if not out.get("ok"):
                        failures += 1
                    rounds.append(
                        {
                            "tool": str(tool_call.name),
                            "ok": bool(out.get("ok")),
                            "error": out.get("error"),
                            "ms": round((time.perf_counter() - started) * 1000, 1),
                        }
                    )
                    tool_evidence.append(
                        {
                            "tool": str(tool_call.name),
                            "ok": bool(out.get("ok")),
                            "spoken": str(out.get("spoken") or "")[:200],
                        }
                    )
                    spoken = str(out.get("spoken") or "").strip()
                    if spoken:
                        last_spoken = spoken[:500]
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=str(out)[:4000],
                            name=str(tool_call.name),
                            tool_call_id=str(tool_call.id),
                        )
                    )
    except GraphUnavailable:
        raise
    except asyncio.CancelledError:
        # Node budget fired mid-loop: stash what ran so the router can
        # return partial rounds instead of a bare timeout, then re-raise —
        # swallowing cancellation would break wait_for accounting and eat
        # genuine external cancels.
        if partial_box is not None:
            partial_box["receipt"] = WorkerReceipt(
                node_id=node.id,
                ok=False,
                spoken=last_spoken,
                worker="mimo",
                rounds=rounds,
                error="node_timeout",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        raise
    except Exception as exc:  # noqa: BLE001 - one node must not kill the job
        logger.warning("mimo worker failed node=%s error=%s", node.id, type(exc).__name__)
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="mimo",
            rounds=rounds,
            error=f"{type(exc).__name__}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    duration_ms = (time.perf_counter() - started) * 1000
    if ran == 0:
        if last_spoken and node.tier is TaskTier.R:
            # A read-only node whose answer is prose (draft, summarize,
            # explain): the text is the deliverable, captured as an
            # artifact with honest provenance instead of a failure.
            return WorkerReceipt(
                node_id=node.id,
                ok=True,
                spoken=last_spoken,
                worker="mimo",
                artifacts=[{"kind": "text", "text": last_spoken[:4000]}],
                rounds=rounds,
                duration_ms=duration_ms,
            )
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            spoken=last_spoken,
            worker="mimo",
            rounds=rounds,
            error="no_action_taken",
            duration_ms=duration_ms,
        )
    return WorkerReceipt(
        node_id=node.id,
        ok=failures == 0,
        spoken=last_spoken,
        worker="mimo",
        evidence=tool_evidence[:20],
        rounds=rounds,
        error=None if failures == 0 else "tool_failures",
        duration_ms=duration_ms,
    )


def _worker_family(node: TaskNode) -> str:
    """Explicit routing: families first, single tools static, else MiMo."""

    tool = (node.tool or "").strip()
    if tool == "files.act" or tool.startswith("files."):
        return "file"
    if tool in {"computer.observe", "computer.perform_effect"}:
        return "computer"
    if tool and tool in known_semantic_tools():
        return "static"
    return "mimo"


async def run_node(
    node: TaskNode,
    ctx: WorkerCtx,
    *,
    job_id: str,
    attempt: int = 1,
    provider: Any | None = None,
    execute_fn: Any | None = None,
) -> WorkerReceipt:
    """Route one node to a worker and return its receipt (never raises)."""

    del job_id, attempt
    if node.tier is TaskTier.D:
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            spoken=(
                f"This step needs your approval before I touch anything: {node.label}. "
                "Say the word and I will proceed."
            )[:2000],
            worker="router",
            error="tier_d_requires_approval",
        )
    limit = float(node.timeout_seconds or settings.graph_node_timeout_seconds or 60.0)
    tool = (node.tool or "").strip()
    family = _worker_family(node)
    partial: dict[str, Any] = {}
    try:
        with _SessionGuard():
            if family == "file":
                return await asyncio.wait_for(
                    _file_worker(node, ctx=ctx, execute_fn=execute_fn), timeout=limit
                )
            if family == "computer":
                return await asyncio.wait_for(
                    _computer_worker(node, ctx=ctx, execute_fn=execute_fn), timeout=limit
                )
            if family == "static":
                return await asyncio.wait_for(
                    _static_worker(node, ctx=ctx, execute_fn=execute_fn), timeout=limit
                )
            return await asyncio.wait_for(
                _mimo_worker(node, ctx=ctx, provider=provider,
                             execute_fn=execute_fn, partial_box=partial),
                timeout=limit,
            )
    except TimeoutError:
        # The MiMo loop stashes partial rounds on cancellation; prefer them
        # over static attribution so the supervisor retries with evidence.
        kept = partial.get("receipt")
        if isinstance(kept, WorkerReceipt):
            return kept
        # The worker itself was cancelled before it could report (setup,
        # guard, or a shielded await): attribute the phase statically.
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker=family,
            rounds=[{"phase": "worker_call", "tool": tool or None}],
            error="node_timeout",
            duration_ms=limit * 1000,
        )
    except GraphUnavailable as exc:
        return WorkerReceipt(
            node_id=node.id,
            ok=False,
            worker="mimo",
            error="worker_unavailable",
            spoken=str(exc)[:500],
        )
