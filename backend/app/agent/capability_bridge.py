"""Evie's semantic capability graph, exposed to the model as native tools.

Why this module exists
----------------------
The agent lane used to reach every personal capability through one generic
``mac_call`` tool that dispatched by *name string*::

    mac_call(name="life.send", arguments={"to": ..., "text": ...})

That shape had three faults, all of them visible in the working tree:

1. **No typed parameters.** The model only ever saw a one-line description of
   a capability, so it had to guess that ``life.send`` wanted ``to`` and
   ``text`` while ``files.act`` wanted ``op`` and ``path``. Wrong guesses
   surfaced as executor failures, not as schema errors.
2. **A hand-maintained allowlist.** ``mac_call`` carried a hardcoded list of
   ten read-only capability names that it would run without asking. A new
   capability had to be added to that Python list, or it silently started
   asking for approval on a plain read.
3. **The discovery tool could not be discovered.** ``capability.discover``
   — the one tool that answers "what can I do?" — was explicitly refused by
   ``mac_call``.

Both harnesses this workspace reverse-engineered take the opposite approach.
Grok's harness exposes each capability as its own typed schema (26 tools in
``~/.grok/sessions/*/tool_definitions.json``, plus ``search_tool``/``use_tool``
for runtime discovery), and OpenBot carries one row per provider in
``packages/contracts/src/agent-providers.ts`` instead of twenty hand-written
copies. In both, the capability is *data* and the dispatch is the harness's
job.

This module is the same idea for Evie: one ``AgentTool`` per semantic
capability, schemas read straight from ``app.cognitive.capabilities``,
execution straight from ``app.cognitive.executor.execute_semantic``. Adding a
capability is a row in ``SEMANTIC_TOOLS`` — no Python edit here, and no edit
to a call-list anywhere.

The executor's own policy gates (prepare-only, stale-plan, untrusted
provenance) still apply and still win; this bridge adds typed reach, it does
not add authority.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.contracts import ToolSpec

logger = logging.getLogger("ev.agent.capability")

#: Capabilities that own the agent lane itself and must not be re-entered
#: through it: ``agent.run`` spawns the lane that is already running.
SELF_REFERENTIAL = frozenset({"agent.run"})

#: Keys copied out of a semantic result for the model's tool payload. The
#: model needs the substance (``spoken``, ``items``, ``result``) and the
#: truth markers (``ok``, ``error``, ``degraded``, ``grounding``); the rest
#: is lane bookkeeping.
_RESULT_KEYS = frozenset({
    "ok", "spoken", "error", "status", "result", "data", "items",
    "confirm", "needs_confirm", "count", "delivery", "diagnosis",
    "summary", "rows", "matches", "total", "degraded", "grounding",
    "evidence", "intent", "executed", "operation",
})


def semantic_specs() -> list[ToolSpec]:
    """Every semantic capability as a tool spec, in registry order."""
    from app.cognitive.capabilities import tool_specs

    return tool_specs()


def capability_names() -> list[str]:
    return [spec.name for spec in semantic_specs() if spec.name]


#: Read-only operations of the capabilities that are polymorphic: one tool
#: covers a read and a write, and a read must not ask the owner for
#: permission. These are the operation vocabularies the owning lanes already
#: use — the file lane's ``READ_OPS`` and the digital lane's Gmail read set
#: are imported from there rather than copied, so there is exactly one list
#: per lane. The rest are stated here with the lane that owns them noted.
READ_OPERATIONS: dict[str, frozenset[str]] = {
    # app/fabric/dispatch_tools.py — _ShellSession
    "shell.session": frozenset({"stream", "tail", "list", "exit_code", "reattach"}),
    # app/fabric/dispatch_tools.py — _TerminalLayout
    "terminal.layout": frozenset({"plan", "list", "read"}),
    # app/fabric/dispatch_tools.py — _PimAct
    "pim.act": frozenset({
        "reminder_list", "note_list", "calendar_list", "calendar_events",
    }),
    # app/fabric/dispatch_tools.py — _BuilderJob
    "builder.job": frozenset({"status"}),
    # app/fabric/dispatch_tools.py — _CommsAct
    "comms.act": frozenset({"read", "list"}),
    # app/fabric/dispatch_tools.py — _OperatorAct
    "operator.act": frozenset({"observe", "verify"}),
    # app/fabric/dispatch_tools.py — _RecipeAct
    "recipe.act": frozenset({"list", "get"}),
    # app/cognitive/executor.py — _owner_profile
    "owner.profile": frozenset({"get", "read"}),
    # app/cognitive/executor.py — _timer_act
    "timer.act": frozenset({"list"}),
}


def read_operations(capability: str) -> frozenset[str]:
    """The read-only operations of one capability.

    Two vocabularies live in the lanes that own them, and are imported here
    so there is only ever one list: the file lane's ``READ_OPS`` and the
    digital lane's Gmail read set.
    """
    if capability == "files.act":
        try:
            from app.ev.file_sandbox import READ_OPS

            return READ_OPS
        except Exception:  # pragma: no cover - import guard
            return frozenset()
    if capability == "digital.act":
        # The digital capability is service-dependent: a Gmail read is a
        # read, a WhatsApp send is not, and the service decides.
        try:
            from app.digital.tools import _GMAIL_READ_OPS

            return _GMAIL_READ_OPS
        except Exception:  # pragma: no cover - import guard
            return frozenset()
    return READ_OPERATIONS.get(capability, frozenset())


def operation_of(arguments: dict[str, Any]) -> str:
    """The operation a capability call names, if it names one."""
    for key in ("op", "operation"):
        value = str((arguments or {}).get(key) or "").strip().lower()
        if value:
            return value
    return ""


def is_read_call(capability: str, arguments: dict[str, Any]) -> bool:
    """True when this capability call is a read and not an effect.

    A capability that is purely read-only never reaches here: its spec says
    so. This answers the harder question, the one a single boolean cannot:
    ``owner.profile`` with ``op=get`` reads the owner's name, and
    ``owner.profile`` with ``op=set`` writes it. Both are the same tool.
    """
    operations = read_operations(capability)
    if not operations:
        return False
    if capability == "digital.act":
        service = str((arguments or {}).get("service") or "").strip().lower()
        if service not in {"gmail", "mail"}:
            return False
    operation = operation_of(arguments)
    if not operation:
        # No operation named: the capability's own default applies. A default
        # that is not in the read set is an effect.
        return False
    return operation in operations


def _receipt(
    *,
    ok: bool,
    error: str | None = None,
    result: Any = None,
    verified: bool = False,
    evidence: dict[str, Any] | None = None,
    needs_confirm: bool = False,
) -> dict[str, Any]:
    from app.agent.tools import _receipt as _shared

    return _shared(
        ok=ok,
        error=error,
        result=result,
        verified=verified,
        evidence=evidence,
        needs_confirm=needs_confirm,
    )


async def _run_capability(
    spec: ToolSpec,
    arguments: dict[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    """Execute one semantic capability and normalise it to a receipt.

    The session and cognition objects are per-run, not per-call, so a second
    call in the same run sees the same steering version. They are created
    lazily and cached on ``ctx.extra`` for exactly that reason.
    """
    name = spec.name
    if name in SELF_REFERENTIAL:
        return _receipt(
            ok=False,
            error=f"{name} owns the agent lane and is not callable from inside it",
            verified=True,
        )

    try:
        from app.cognitive.executor import execute_semantic
        from app.cognitive.session_store import CognitiveSession
        from app.db import SessionLocal
    except Exception as exc:  # noqa: BLE001 - a missing lane is an honest failure
        logger.exception("capability bridge could not load the executor")
        return _receipt(ok=False, error=f"capability lane unavailable: {exc}", verified=True)

    extra = getattr(ctx, "extra", None) or {}
    cognition = extra.get("cognition")
    if cognition is None:
        cognition = CognitiveSession(
            session_id=f"agent-{getattr(ctx, 'run_id', '') or int(time.time())}",
            live_session_id=str(extra.get("live_session_id") or "") or None,
        )
        extra["cognition"] = cognition

    live_session_id = str(extra.get("live_session_id") or "") or None
    device_id = str(extra.get("device_id") or "") or None

    async with SessionLocal() as session:
        try:
            body = await execute_semantic(
                session,
                name,
                dict(arguments or {}),
                cognition=cognition,
                actor="master",
                live_session_id=live_session_id,
                steering_seen=int(getattr(cognition, "steering_version", 0)),
                device_id=device_id,
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - report the executor's failure
            logger.exception("capability %s failed", name)
            return _receipt(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                verified=True,
                evidence={"capability": name},
            )

    if not isinstance(body, dict):
        return _receipt(ok=False, error="capability returned a non-object result", verified=True)

    ok = bool(body.get("ok", True)) and not body.get("error")
    compact = {key: value for key, value in body.items() if key in _RESULT_KEYS}
    if not compact:
        compact = {"result": body}
    error = str(body.get("error") or "").strip() or None
    if not ok and not error:
        # A failed call with no reason is not something the model can adapt
        # to. Name the reason the lane gave: its diagnosis, its grounding,
        # or what it said.
        reason = (
            str(body.get("diagnosis") or "").strip()
            or str(body.get("grounding") or "").strip()
            or str(body.get("spoken") or "").strip()
        )
        if body.get("degraded"):
            reason = f"degraded: {reason}" if reason else "degraded"
        error = reason[:300] or "the capability did not return a usable result"
    return _receipt(
        ok=ok,
        error=error,
        verified=bool(body.get("verified", ok)),
        result=compact,
        needs_confirm=bool(body.get("needs_confirm") or body.get("confirm")),
        evidence={"capability": name},
    )


def capability_tools() -> list[Any]:
    """One ``AgentTool`` per semantic capability, ready for the registry."""
    from app.agent.tools import AgentTool

    tools: list[Any] = []
    for spec in semantic_specs():
        if not spec.name:
            continue
        if spec.name in SELF_REFERENTIAL:
            # Offered to the model it would be a tool that always refuses.
            # The capability exists for the *outer* lanes that call the agent;
            # inside the lane the agent is already the thing running.
            continue

        async def run(
            arguments: dict[str, Any],
            ctx: Any,
            _spec: ToolSpec = spec,
        ) -> dict[str, Any]:
            return await _run_capability(_spec, arguments, ctx)

        tools.append(AgentTool(spec=spec, run=run))
    return tools


__all__ = [
    "READ_OPERATIONS",
    "SELF_REFERENTIAL",
    "capability_names",
    "capability_tools",
    "is_read_call",
    "operation_of",
    "read_operations",
    "semantic_specs",
]
