"""Presence event bus: server side of ``docs/contracts/PRESENCE_EVENTS.md``.

Contract ``evie-presence/1``. This module owns the envelope, the replay ring,
the subscriber fan-out, and the translation of kernel/agent/runner events
into wire frames. It imports nothing from ``app`` so producers (kernel,
agent loop, services, panic) can import it without cycles.

Every :meth:`PresenceBus.publish` call validates the frame against the
contract schema and raises ``ValueError`` on violation: an invalid frame is
never emitted, and the producer's tests fail loudly instead of shipping a
frame the app must ignore.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

CONTRACT = "evie-presence/1"

#: Seconds between `:heartbeat` comments on an idle stream (contract: <= 15).
HEARTBEAT_S = 15
#: Ring keeps the newest N frames and prunes anything older than this.
RING_MAX = 2048
RING_KEEP_S = 30 * 60
#: Per-subscriber queue depth; oldest frames drop first (newest survive).
SUBSCRIBER_MAX = 500

#: Event types the backend may send. ``ambient.looking_at`` is app-local and
#: refused here (the contract forbids the backend from sending it).
BACKEND_TYPES = frozenset({
    "stream.hello",
    "turn.started",
    "turn.listening",
    "turn.transcript.partial",
    "turn.transcript.final",
    "turn.thinking",
    "plan.updated",
    "tool.started",
    "tool.finished",
    "turn.needs_confirm",
    "turn.progress",
    "turn.completed",
    "turn.failed",
    "background.update",
    "panic.state",
})

#: Required ``data`` keys per event type (contract §3; minimal-but-honest
#: payloads allowed where the contract says so, e.g. needs_confirm without
#: the rich_confirm capability).
_REQUIRED: dict[str, frozenset[str]] = {
    "stream.hello": frozenset({"contract", "server_time", "cursor", "resumed", "gap", "restarted", "capabilities"}),
    "turn.started": frozenset({"source", "utterance_preview", "label"}),
    "turn.listening": frozenset({"level", "degraded"}),
    "turn.transcript.partial": frozenset({"text", "sequence", "stable", "confidence"}),
    "turn.transcript.final": frozenset({"text", "confidence", "provider"}),
    "turn.thinking": frozenset({"label", "elapsed_ms"}),
    "plan.updated": frozenset({"label", "steps"}),
    "tool.started": frozenset({"call_id", "step_id", "name", "label", "target"}),
    "tool.finished": frozenset({"call_id", "step_id", "name", "label", "ok", "verified", "evidence", "error", "receipt_id", "undo"}),
    "turn.needs_confirm": frozenset({"ticket_id", "risk_class", "channel", "label", "expires_at", "approve", "deny"}),
    "turn.progress": frozenset({"text", "spoken", "sequence"}),
    "turn.completed": frozenset({"summary", "receipt_ids", "verified", "label"}),
    "turn.failed": frozenset({"reason", "category", "failed_step_id", "recoverable", "next_step", "receipt_ids"}),
    "background.update": frozenset({"kind", "label"}),
    "panic.state": frozenset({"panicked", "at", "reason"}),
}

_PLAN_STATUSES = frozenset({"queued", "active", "done", "failed", "skipped"})
_FAIL_CATEGORIES = frozenset({"missing_data", "denied", "timeout", "provider_error", "cancelled", "other"})


def _utc_ms_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_frame(envelope: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless the envelope matches ``evie-presence/1``."""
    for key in ("v", "id", "seq", "ts", "type", "data"):
        if key not in envelope:
            raise ValueError(f"presence frame missing {key!r}")
    if envelope["v"] != CONTRACT:
        raise ValueError(f"presence frame contract {envelope['v']!r} != {CONTRACT!r}")
    etype = envelope["type"]
    if etype == "ambient.looking_at":
        raise ValueError("backend must not send ambient.looking_at")
    if etype not in BACKEND_TYPES:
        raise ValueError(f"unknown presence event {etype!r}")
    data = envelope["data"]
    if not isinstance(data, dict):
        raise ValueError("presence frame data must be an object")
    missing = _REQUIRED[etype] - set(data)
    if missing:
        raise ValueError(f"presence {etype} missing data keys: {sorted(missing)}")
    if etype == "plan.updated":
        steps = data["steps"]
        if not isinstance(steps, list):
            raise ValueError("plan.updated steps must be a list")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("plan.updated step must be an object")
            if set(step) < {"id", "label", "status"}:
                raise ValueError("plan.updated step needs id/label/status")
            if step["status"] not in _PLAN_STATUSES:
                raise ValueError(f"bad plan step status {step['status']!r}")
    if etype == "turn.failed" and data["category"] not in _FAIL_CATEGORIES:
        raise ValueError(f"bad turn.failed category {data['category']!r}")
    if etype == "turn.needs_confirm":
        for side in ("approve", "deny"):
            call = data[side]
            if not isinstance(call, dict) or set(call) < {"method", "path"}:
                raise ValueError(f"turn.needs_confirm {side} needs method/path")


class PresenceBus:
    """In-process presence fan-out with a bounded replay ring."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._boot_id = uuid.uuid4().hex[:8]
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING_MAX)
        self._subs: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()
        self._run_turns: dict[str, str] = {}

    # -- publishing ------------------------------------------------------

    def publish(self, etype: str, data: dict[str, Any], turn_id: str | None) -> dict[str, Any]:
        envelope = {
            "v": CONTRACT,
            "id": f"pe-{uuid.uuid4().hex[:16]}",
            "seq": 0,  # filled under the lock
            "ts": _utc_ms_now(),
            "turn_id": turn_id,
            "type": etype,
            "data": data,
        }
        validate_frame(envelope)
        with self._lock:
            self._seq += 1
            envelope["seq"] = self._seq
            self._ring.append(envelope)
            self._prune_locked()
            subs = list(self._subs)
        for queue, loop in subs:
            try:
                loop.call_soon_threadsafe(_deliver, queue, envelope)
            except RuntimeError:
                continue  # loop closed; subscriber reaps itself on disconnect
        return envelope

    def _prune_locked(self) -> None:
        cutoff = time.time() - RING_KEEP_S
        ring = self._ring
        while len(ring) > 1:
            try:
                ts = datetime.fromisoformat(str(ring[0]["ts"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                break
            if ts >= cutoff:
                break
            ring.popleft()

    # -- replay ----------------------------------------------------------

    def latest_id(self) -> str | None:
        with self._lock:
            return self._ring[-1]["id"] if self._ring else None

    def replay_since(self, cursor: str | None) -> tuple[list[dict[str, Any]], bool, bool]:
        """Frames after ``cursor``. Returns (frames, resumed, gap)."""
        with self._lock:
            if not cursor:
                return [], True, False
            ids = [frame["id"] for frame in self._ring]
            if cursor in ids:
                idx = ids.index(cursor)
                return list(self._ring)[idx + 1:], True, False
            return [], False, True

    # -- subscribers -----------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_MAX)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        with self._lock:
            self._subs.add((queue, loop))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {(q, loop) for q, loop in self._subs if q is not queue}

    # -- run/turn binding (agent runs belong to a kernel turn) -----------

    def bind_run_turn(self, run_id: str, turn_id: str) -> None:
        with self._lock:
            self._run_turns[run_id] = turn_id

    def turn_for_run(self, run_id: str) -> str:
        with self._lock:
            bound = self._run_turns.get(run_id)
        if bound:
            return bound
        suffix = run_id.split("-", 1)[1] if "-" in run_id else run_id
        return f"ft-{suffix[:12]}"

    # -- tests -----------------------------------------------------------

    def reset_for_tests(self) -> None:
        with self._lock:
            self._seq = 0
            self._ring.clear()
            self._subs.clear()
            self._run_turns.clear()


def _deliver(queue: asyncio.Queue, envelope: dict[str, Any]) -> None:
    try:
        queue.put_nowait(envelope)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(envelope)


bus = PresenceBus()


def new_turn_id() -> str:
    return f"ft-{uuid.uuid4().hex[:12]}"


# -- typed emitters (one per event; keep payloads schema-valid) ----------

def emit_turn_started(*, turn_id: str, source: str, utterance_preview: str = "", label: str = "Listening…") -> dict[str, Any]:
    return bus.publish("turn.started", {
        "source": source,
        "utterance_preview": utterance_preview[:120],
        "label": label,
    }, turn_id)


def emit_turn_thinking(*, turn_id: str, label: str = "Thinking…", elapsed_ms: int = 0) -> dict[str, Any]:
    return bus.publish("turn.thinking", {"label": label, "elapsed_ms": int(elapsed_ms)}, turn_id)


def emit_turn_progress(*, turn_id: str, text: str, spoken: bool = True, sequence: int = 0) -> dict[str, Any]:
    return bus.publish("turn.progress", {"text": text, "spoken": spoken, "sequence": sequence}, turn_id)


def emit_turn_completed(*, turn_id: str, summary: str, receipt_ids: list[str] | None = None,
                        verified: bool = False, label: str = "Done") -> dict[str, Any]:
    return bus.publish("turn.completed", {
        "summary": summary,
        "receipt_ids": list(receipt_ids or []),
        "verified": verified,
        "label": label,
    }, turn_id)


def emit_turn_failed(*, turn_id: str, reason: str, category: str = "other",
                     failed_step_id: str | None = None, recoverable: bool = True,
                     next_step: str = "", receipt_ids: list[str] | None = None) -> dict[str, Any]:
    if category not in _FAIL_CATEGORIES:
        category = "other"
    return bus.publish("turn.failed", {
        "reason": reason,
        "category": category,
        "failed_step_id": failed_step_id,
        "recoverable": recoverable,
        "next_step": next_step,
        "receipt_ids": list(receipt_ids or []),
    }, turn_id)


def emit_plan_updated(*, turn_id: str, label: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return bus.publish("plan.updated", {"label": label, "steps": steps}, turn_id)


def emit_tool_started(*, turn_id: str, call_id: str, step_id: str, name: str,
                      label: str, target: str = "") -> dict[str, Any]:
    return bus.publish("tool.started", {
        "call_id": call_id, "step_id": step_id, "name": name, "label": label, "target": target,
    }, turn_id)


def emit_tool_finished(*, turn_id: str, call_id: str, step_id: str, name: str, label: str,
                       ok: bool, verified: bool, evidence: dict[str, Any] | None = None,
                       error: str | None = None, receipt_id: str = "",
                       undo: dict[str, Any] | None = None) -> dict[str, Any]:
    return bus.publish("tool.finished", {
        "call_id": call_id, "step_id": step_id, "name": name, "label": label,
        "ok": ok, "verified": verified, "evidence": evidence or {},
        "error": error, "receipt_id": receipt_id, "undo": undo or {"kind": "none"},
    }, turn_id)


def emit_needs_confirm(*, turn_id: str | None, ticket_id: str, risk_class: str, channel: str,
                       label: str, expires_at: str,
                       recipient: dict[str, Any] | None = None, text: str | None = None,
                       diff: dict[str, Any] | None = None,
                       undo: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ticket_id": ticket_id,
        "risk_class": risk_class,
        "channel": channel,
        "label": label,
        "expires_at": expires_at,
        "approve": {"method": "POST", "path": f"/v1/fabric/approvals/{ticket_id}/approve",
                    "needs_reverify": True},
        "deny": {"method": "POST", "path": f"/v1/fabric/approvals/{ticket_id}/deny"},
    }
    if recipient is not None or text is not None or diff is not None or undo is not None:
        data["recipient"] = recipient
        data["text"] = text
        data["diff"] = diff
        data["attachments"] = []
        data["undo"] = undo or {"kind": "none"}
    return bus.publish("turn.needs_confirm", data, turn_id)


def emit_background_update(*, kind: str, label: str, ref: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": kind, "label": label}
    if ref is not None:
        data["ref"] = ref
    return bus.publish("background.update", data, None)


def emit_panic_state(*, panicked: bool, at: str, reason: str = "") -> dict[str, Any]:
    return bus.publish("panic.state", {"panicked": panicked, "at": at, "reason": reason}, None)


def emit_hello(*, cursor: str | None, resumed: bool, gap: bool, restarted: bool,
               capabilities: list[str] | None = None) -> dict[str, Any]:
    return {
        "v": CONTRACT,
        "id": f"pe-{uuid.uuid4().hex[:16]}",
        "seq": 0,
        "ts": _utc_ms_now(),
        "turn_id": None,
        "type": "stream.hello",
        "data": {
            "contract": CONTRACT,
            "server_time": _utc_ms_now(),
            "cursor": cursor,
            "resumed": resumed,
            "gap": gap,
            "restarted": restarted,
            "capabilities": capabilities or ["poll_fallback"],
        },
    }


# -- agent-run event sink --------------------------------------------------

_TODO_TO_PLAN = {"pending": "queued", "in_progress": "active", "completed": "done"}


def agent_sink(event: dict[str, Any]) -> None:
    """Translate one agent-loop ``on_event`` dict into presence frames.

    Best-effort: translation failures are swallowed so telemetry can never
    break a run. (Emission validity itself is enforced by ``publish`` and
    covered by tests with fixed events.)
    """
    with contextlib.suppress(Exception):
        _agent_sink(event)


def _agent_sink(event: dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    run_id = str(event.get("run_id") or "")
    if not run_id:
        return
    turn_id = bus.turn_for_run(run_id)
    if kind == "plan_updated":
        todos = event.get("todos") or []
        steps = [
            {"id": f"s{i + 1}", "label": str(row.get("content") or f"Step {i + 1}")[:200],
             "status": _TODO_TO_PLAN.get(str(row.get("status") or "pending"), "queued")}
            for i, row in enumerate(todos) if isinstance(row, dict)
        ]
        label = str(event.get("goal") or _run_goals.get(run_id) or "Working")
        emit_plan_updated(turn_id=turn_id, label=label[:200], steps=steps)
    elif kind == "run_started":
        _run_goals[run_id] = str(event.get("goal") or "")[:200]
    elif kind == "tool_started":
        step = event.get("step", 0)
        emit_tool_started(turn_id=turn_id, call_id=_call_id(run_id, step),
                           step_id=f"s{step}", name=str(event.get("tool") or "tool"),
                           label=f"Running {event.get('tool') or 'tool'}")
    elif kind == "tool_result":
        step = event.get("step", 0)
        ok = bool(event.get("ok"))
        emit_tool_finished(turn_id=turn_id, call_id=_call_id(run_id, step),
                            step_id=f"s{step}", name=str(event.get("tool") or "tool"),
                            label=str(event.get("error") or ("Done" if ok else "Failed"))[:200],
                            ok=ok, verified=bool(event.get("verified", ok)),
                            error=event.get("error"),
                            undo=_undo_for(event.get("undo")))
    elif kind == "brain_turn":
        if not event.get("tool_calls"):
            text = str(event.get("text") or "").strip()
            emit_turn_thinking(turn_id=turn_id, label=(text[:120] + "…") if text else "Thinking…")


_run_goals: dict[str, str] = {}


def _call_id(run_id: str, step: Any) -> str:
    suffix = run_id.split("-", 1)[1] if "-" in run_id else run_id
    return f"c-{suffix[:8]}-{step}"


def _undo_for(undo: Any) -> dict[str, Any]:
    if isinstance(undo, dict) and undo.get("kind"):
        return {"kind": str(undo["kind"]), "label": str(undo.get("label") or "")}
    if isinstance(undo, str) and undo:
        return {"kind": undo}
    return {"kind": "none"}


_ACTION_CHANNELS = {
    "comms_act": "message.send",
    "files_op": "file.write",
    "terminal_layout": "other",
    "screen_ref": "other",
    "sql_query": "sql.dml",
    "pim_act": "other",
    "builder_job": "other",
    "atlas_discover": "other",
    "mac_act": "other",
    "operator_act": "other",
    "recipe_act": "other",
    "shell_run": "shell.exec",
    "shell_session": "shell.exec",
}


def channel_for_action(action_type: str, payload: dict[str, Any] | None = None) -> str:
    """Contract channel for a parked action (honest fallback: ``other``)."""
    payload = payload or {}
    if action_type == "comms_act":
        op = str(payload.get("op") or payload.get("channel") or "")
        if "mail" in op or "email" in op:
            return "mail.send"
        if "call" in op:
            return "call.place"
        return "message.send"
    if action_type == "files_op":
        op = str(payload.get("op") or "")
        if op in {"delete", "trash", "remove"}:
            return "file.delete"
        return "file.write"
    if action_type == "sql_query":
        sql = str(payload.get("sql") or payload.get("statement") or "").lstrip().upper()
        if sql.startswith(("CREATE", "ALTER", "DROP", "TRUNCATE")):
            return "sql.ddl"
        return "sql.dml"
    return _ACTION_CHANNELS.get(action_type, "other")


def wrap_on_event(on_event: Any) -> Any:
    """Chain ``agent_sink`` after an existing agent ``on_event`` callback."""
    if on_event is None:
        return agent_sink

    def chained(event: dict[str, Any]) -> None:
        try:
            on_event(event)
        finally:
            agent_sink(event)

    return chained


# -- SSE frame source (the /presence/stream body, minus HTTP) -------------

async def stream_frames(resume: str | None, *, is_disconnected: Any = None):
    """Yield SSE chunks: hello first, then replay, then live + heartbeats.

    ``is_disconnected`` is an async callable returning True when the client
    went away (the endpoint passes ``request.is_disconnected``). Extracted
    so the framing behavior is unit-testable: httpx's ASGI transport cannot
    serve an infinite ``StreamingResponse`` (its ``receive()`` waits for
    response completion while Starlette waits for disconnect — a test-only
    deadlock; uvicorn serves the same body fine).
    """
    import json as _json

    frames, resumed, gap = bus.replay_since(resume)
    hello = emit_hello(cursor=bus.latest_id(), resumed=resumed, gap=gap, restarted=False)
    yield f"id: {hello['id']}\nevent: stream.hello\ndata: {_json.dumps(hello['data'])}\n\n"
    for frame in frames:
        yield (f"id: {frame['id']}\nevent: {frame['type']}\n"
               f"data: {_json.dumps(frame['data'])}\n\n")
    queue = bus.subscribe()
    try:
        while True:
            if is_disconnected is not None and await is_disconnected():
                break
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield ":heartbeat\n\n"
                continue
            yield (f"id: {frame['id']}\nevent: {frame['type']}\n"
                   f"data: {_json.dumps(frame['data'])}\n\n")
    finally:
        bus.unsubscribe(queue)
