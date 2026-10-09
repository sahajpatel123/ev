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
import re
import time
from collections.abc import Sequence
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

_SYNTHESIS_SYSTEM = (
    "Write the deliverable NOW from the established results below: one short "
    "line per item (who — gist), nothing else. No opener, no header, no "
    "preamble, no commentary, no tool calls. Your first line must be the "
    "first item — a lone opener or header with nothing after it is a failure."
)


def _worker_effort(attempt: int) -> str:
    """First attempt answers fast; a retry may think harder.

    ``low`` keeps a healthy node's tool picks cheap. When the supervisor
    already rejected attempt one, the same effort would likely repeat the
    same failure, so a retry uses the configured retry effort.
    """

    if attempt <= 1:
        return "low"
    raw = str(getattr(settings, "graph_retry_reasoning_effort", "") or "medium")
    value = raw.strip().lower()
    return value if value in {"low", "medium", "high"} else "medium"


def _worker_budget(node: TaskNode) -> tuple[int, float, float]:
    """Dynamic per-node budget: round cap, wall-clock reserve, and limit."""

    cap = int(getattr(settings, "graph_worker_max_rounds", 4) or 4)
    cap = max(1, min(cap, 8))
    reserve = float(getattr(settings, "graph_worker_reserve_seconds", 8.0) or 0.0)
    reserve = max(0.0, min(reserve, 30.0))
    limit = float(node.timeout_seconds or settings.graph_node_timeout_seconds or 60.0)
    # A reserve larger than the node limit would exhaust before round one
    # (an 8s default reserve against a 5s node budget); clamp it so short
    # budgets still attempt work instead of instantly reporting exhausted.
    reserve = min(reserve, limit / 2.0)
    return cap, reserve, limit

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

    counts = _list_counts(body)
    count_entry = (
        [{"tool": body.get("tool") or node.tool or "worker", "counts": counts}]
        if counts
        else []
    )
    explicit = _as_evidence_list(body.get("evidence"))
    if explicit:
        # Verdicts judge from evidence: a bare {source, timestamp} made the
        # decider hedge partial on perfect digests (live catch). Row counts
        # are the verifiable substance it was missing.
        return [*explicit, *count_entry]
    skip = {"ok", "spoken", "error", "diagnosis", "tool", "duration_ms"}
    payload_keys = sorted(k for k in body if k not in skip)[:12]
    if not payload_keys and not count_entry:
        return []
    entry: dict[str, Any] = {"tool": body.get("tool") or node.tool or "worker"}
    if payload_keys:
        entry["payload_keys"] = payload_keys
    if counts:
        entry["counts"] = counts
    return [entry]


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


_TEXTS_WORDS = frozenset({"text", "texts", "sms", "imessage"})
_MIXED_WORDS = frozenset({"message", "messages", "whatsapp", "chat", "chats"})


def _scope_messages_aisle(tool: str, node: TaskNode, arguments: dict[str, Any]) -> None:
    """Scope a texts node to the imessage aisle without touching query words.

    A node labeled "Fetch recent texts" but carrying no query text reads the
    mixed inbox (SMS + WhatsApp), and the verdict rightly wonders why a texts
    ask came back with WhatsApp. Seeding the label AS the query flips the
    speak manner on verbs ("read"→particular) — a live catch — so this passes
    channel=imessage instead: same digest-stable default query, one aisle.
    Only when the label names the aisle explicitly (bare "messages" stays
    mixed), names no person (a person ask must keep its query path, never be
    narrowed to an aisle), and no explicit query or channel already rules.
    Verbs are harmless here because the query words never change.
    """

    if tool != "life.messages":
        return
    if str(arguments.get("query") or arguments.get("q") or "").strip():
        return
    if str(arguments.get("channel") or "").strip():
        return
    label = node.label or ""
    words = set(re.findall(r"[a-z]+", label.lower()))
    if not (words & _TEXTS_WORDS) or (words & _MIXED_WORDS):
        return
    try:
        from app.memory.life_archive.locate import _chat_person_query_token

        person = _chat_person_query_token(label)
    except Exception:
        return
    if (person or "").strip():
        return
    arguments["channel"] = "imessage"


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
            arguments = dict(node.arguments or {})
            _scope_messages_aisle(tool, node, arguments)
            result = await call(
                session,
                tool,
                arguments,
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


_LIST_RESULT_KEYS = ("messages", "items", "rows", "results", "contacts", "matches")


def _list_counts(out: dict[str, Any]) -> dict[str, int]:
    """Row counts for every list-shaped payload key (empty when not a list tool)."""

    counts: dict[str, int] = {}
    for key in _LIST_RESULT_KEYS:
        value = out.get(key)
        if isinstance(value, list):
            counts[key] = len(value)
    return counts


def _spoken_supersedes(out: dict[str, Any], prior: str, prior_rows: int | None) -> bool:
    """New tool spoken wins unless it is emptiness over substance.

    Live catch: a synthesis node re-fetched messages, got rows, then got an
    honest empty on a narrower second call — and the receipt spoke the empty
    line because recency always won. Failures still overwrite (they must stay
    visible); non-list tools keep current behavior.
    """

    if not prior:
        return True
    if not out.get("ok"):
        return True
    counts = _list_counts(out)
    if not counts:
        return True
    if any(value > 0 for value in counts.values()):
        return True
    return prior_rows == 0


def _count_rows(out: dict[str, Any]) -> int | None:
    """Total list rows, or None when the result is not list-shaped."""

    counts = _list_counts(out)
    if not counts:
        return None
    return sum(counts.values())


def _prior_results_block(prior: Sequence[WorkerReceipt]) -> str:
    """Established-results context for a dependent worker (capped)."""

    lines: list[str] = []
    total = 0
    for receipt in prior:
        spoken = (receipt.spoken or "").strip()
        if not spoken:
            spoken = f"(failed: {receipt.error})" if receipt.error else "(no summary)"
        line = f"- {receipt.node_id} ({'done' if receipt.ok else 'failed'}): {spoken[:600]}"
        if total + len(line) > 2400:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "Results already established by earlier steps — use these; only call "
        "tools for what is still missing:\n" + "\n".join(lines)
    )


def _content_lines(text: str) -> list[str]:
    """Lines carrying item substance: a who-gist separator, not a bare label.

    "**Emails**" and "# Texts" are headers, not items; "Here's your combined
    summary:" is an opener. Real synthesis lines join a who to a gist with
    an em dash, a colon, or a bullet — exactly the shape the synthesis prompt
    demands ("one short line per item").
    """

    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bare = line.strip("*").strip()
        if not bare:
            continue
        if (
            "—" in bare
            or ": " in bare
            or re.match(r"^([-*•]|\d+[.)])\s+\S", bare)
        ):
            lines.append(line)
    return lines


def _is_stub(text: str, prior: Sequence[WorkerReceipt]) -> bool:
    """A synthesis that says nothing: lone opener/header for a multi-part ask.

    Live catch: "Emails" (header-only) graded accept at 0.73, and "Here's
    your combined summary:" (lead-in-only) twice. A multi-dep synthesis owes
    at least two content lines; fewer is a stub. The stub text stays on the
    receipt so the retry hint quotes it back for a second chance with tools.
    """

    if len(prior) < 2:
        return False
    return len(_content_lines(text)) < 2


def _synthesis_receipt(
    node: TaskNode, prior: Sequence[WorkerReceipt], synth: Any, started: float
) -> WorkerReceipt:
    """Receipt for a no-tool synthesis answer over established results."""

    text = (((synth.text if synth else "") or "").strip())[:2000]
    if not text or _is_stub(text, prior):
        return WorkerReceipt(
            node_id=node.id, ok=False, spoken=text, worker="mimo", rounds=[],
            error="empty_synthesis",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    return WorkerReceipt(
        node_id=node.id, ok=True, spoken=text, worker="mimo",
        artifacts=[{"kind": "text", "text": text[:4000]}],
        evidence=[
            {
                "source": "prior_step",
                "node_id": r.node_id,
                "spoken": (r.spoken or "")[:500],
            }
            for r in prior
        ][:20],
        rounds=[],
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def _mimo_worker(
    node: TaskNode,
    *,
    ctx: WorkerCtx,
    provider: Any | None = None,
    execute_fn: Any | None = None,
    attempt: int = 1,
    hint: str | None = None,
    partial_box: dict[str, Any] | None = None,
    prior: Sequence[WorkerReceipt] = (),
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
    # Delegated workers have no live latency budget: full tool surface, same as
    # the kernel's delegated_worker_active policy. Compact omits files.act /
    # computer / code, so file nodes could never execute.
    if hasattr(provider, "reasoning_effort"):
        provider.reasoning_effort = _worker_effort(attempt)
    specs = tool_specs_for_turn(compact=False)
    max_rounds, reserve, limit = _worker_budget(node)
    deadline = started + limit
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
    prior_block = _prior_results_block(prior or ())
    if prior_block:
        # depends_on results the planner said to build on. Without them the
        # worker re-fetches blind (live catch: six redundant calls, then a
        # cherry-picked half answer) because it never saw the earlier steps.
        messages.append(ChatMessage(role="user", content=prior_block[:2600]))
    if hint:
        # The supervisor already rejected attempt one. Repeating the same
        # action would repeat the same failure; name what went wrong and
        # require a different approach.
        messages.append(
            ChatMessage(
                role="user",
                content=(
                    f"Previous attempt failed: {hint[:600]}. Do not repeat the "
                    "same failed action; choose a different approach, or say "
                    "precisely what is blocking completion."
                ),
            )
        )
    ran = 0
    failures = 0
    last_spoken = ""
    last_rows: int | None = None
    tool_evidence: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    budget_exhausted = False
    try:
        if (
            not (node.tool or "").strip()
            and node.tier is TaskTier.R
            and attempt <= 1
            and prior
            and all(r.ok for r in prior)
            and all((r.spoken or "").strip() for r in prior)
        ):
            # Synthesis: every dependency delivered. Answer from the
            # established results with no tools — offering the tool loop here
            # caused live thrash (8 re-fetch calls, then a cherry-picked half
            # answer). A retry, a failed/missing dependency, or a write tier
            # keeps the loop, as does a model that still demands tools: its
            # calls name a genuine gap. Synthesis gets its own system prompt:
            # the executor's one-sentence rule produced bare lead-ins with
            # nothing after them.
            from app.contracts import ChatMessage as _ChatMessage

            synth_messages = [
                _ChatMessage(role="system", content=_SYNTHESIS_SYSTEM),
                *messages[1:],
            ]
            synth = await provider.chat_with_tools(synth_messages, [])
            if synth is not None and getattr(synth, "tool_calls", None):
                pass  # Fall through to the tool loop below.
            else:
                return _synthesis_receipt(node, prior, synth, started)
        async with SessionLocal() as session:
            cognition = CognitiveSession(session_id=f"graph-{node.id}")
            call = execute_fn
            if call is None:
                from app.cognitive.executor import execute_semantic

                call = execute_semantic
            for _ in range(max_rounds):
                # Never start a round the node budget cannot afford: the
                # outer wait_for is the hard stop, but stopping between
                # rounds keeps the receipt honest instead of turning a
                # nearly-finished job into node_timeout.
                if time.perf_counter() >= deadline - reserve:
                    budget_exhausted = True
                    break
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
                    # Evidence must be returned substance, never a bare
                    # assertion: the same rule the static worker enforces
                    # through `_payload_evidence`. A tool result carrying
                    # only ok/spoken yields nothing and the supervisor
                    # escalates instead of accepting an unproven success.
                    for entry in _payload_evidence(node, out):
                        entry["tool"] = str(tool_call.name)
                        entry["ok"] = bool(out.get("ok"))
                        tool_evidence.append(entry)
                    spoken = str(out.get("spoken") or "").strip()
                    if spoken and _spoken_supersedes(out, last_spoken, last_rows):
                        last_spoken = spoken[:500]
                        last_rows = _count_rows(out)
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
        logger.warning(
            "mimo worker failed node=%s error=%s", node.id, type(exc).__name__, exc_info=True
        )
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
        # First-attempt prose on a read-only node (draft, summarize, explain)
        # is the deliverable itself. On a retry the supervisor already named
        # a failure and demanded a different approach, so prose without any
        # tool action is honestly no action taken. A lone opener/header is no
        # deliverable at all (stub check), whatever the attempt.
        if (
            last_spoken
            and node.tier is TaskTier.R
            and attempt <= 1
            and not _is_stub(last_spoken, prior)
        ):
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
            error="node_budget_exhausted" if budget_exhausted else "no_action_taken",
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


def _tool_is_write_capable(tool: str) -> bool:
    """True when the tool is a known semantic tool that may mutate."""

    try:
        from app.cognitive.capabilities import tool_specs
    except Exception:
        return False
    for spec in tool_specs():
        if spec.name == tool:
            return not bool(spec.read_only)
    return False


def _tier_d_question(tool: str, arguments: dict[str, Any], label: str) -> str:
    """Deterministic owner question for a parked tier-D node. No model call."""

    args = dict(arguments or {})
    text = str(args.get("text") or args.get("body") or args.get("message") or "").strip()
    who = str(
        args.get("display") or args.get("to") or args.get("recipient")
        or args.get("target") or args.get("destination") or ""
    ).strip()
    if tool in {"life.send", "digital.act"} and text:
        return f'Should I send "{text[:300]}" to {who or "them"}?'
    if tool == "phone.call" and who:
        return f"Should I call {who[:120]}?"
    if tool == "computer.perform_effect":
        effect = str(args.get("effect") or "").strip()
        return f"Should I do this on your Mac: {(effect or label)[:300]}?"
    if tool == "files.act":
        op = str(args.get("op") or "").strip()
        path = str(args.get("path") or "").strip()
        detail = f"{op} {path}".strip()
        return f"Should I run this file step: {(detail or label)[:300]}?"
    if text and who:
        return f'Should I send "{text[:300]}" to {who or "them"}?'
    if text:
        return f"Should I proceed: {text[:300]}?"
    return f"{label[:160]} — should I proceed?"


async def _park_tier_d_node(node: TaskNode, ctx: WorkerCtx) -> WorkerReceipt | None:
    """Park a single-tool tier-D node on the approval ledger.

    Returns the confirmation receipt, or None when the node cannot be
    parked (no single write-capable tool, bad arguments, store failure) —
    the caller then falls back to the bare blocked receipt, still honest.
    """

    from app.db import SessionLocal

    tool = (node.tool or "").strip()
    arguments = node.arguments if isinstance(node.arguments, dict) else None
    if not tool or arguments is None or not _tool_is_write_capable(tool):
        return None
    try:
        from app.ev.messaging.approval import park_graph_action

        question = _tier_d_question(tool, arguments, node.label)
        async with SessionLocal() as session:
            action = await park_graph_action(
                session,
                node_id=node.id,
                label=node.label,
                tool=tool,
                arguments=arguments,
                question=question,
                actor=ctx.actor or "graph",
                device_id=ctx.device_id,
                live_session_id=ctx.live_session_id,
                address=str(
                    arguments.get("to") or arguments.get("target")
                    or arguments.get("destination") or ""
                ),
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - parking must never kill the job
        logger.warning("tier-D park failed node=%s error=%s", node.id, type(exc).__name__)
        return None
    return _confirmation_receipt(
        node,
        worker="router",
        spoken=question,
        resumption={
            "node": node.model_dump(),
            "op": "approved_action",
            "params": {"action_id": str(action.id)},
        },
        duration_ms=0.0,
    )


async def run_node(
    node: TaskNode,
    ctx: WorkerCtx,
    *,
    job_id: str,
    attempt: int = 1,
    provider: Any | None = None,
    execute_fn: Any | None = None,
    hint: str | None = None,
    prior: Sequence[WorkerReceipt] = (),
) -> WorkerReceipt:
    """Route one node to a worker and return its receipt (never raises)."""

    del job_id
    if node.tier is TaskTier.D:
        parked = await _park_tier_d_node(node, ctx)
        if parked is not None:
            return parked
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
                _mimo_worker(
                    node,
                    ctx=ctx,
                    provider=provider,
                    execute_fn=execute_fn,
                    attempt=attempt,
                    hint=hint,
                    partial_box=partial,
                    prior=prior,
                ),
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
