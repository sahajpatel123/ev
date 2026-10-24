"""The agent loop: goal in, model-planned tool calls, honest receipts out.

One run = one goal. The brain sees the goal plus a system contract that names
the workspace root, the approval mode, and the honesty rules, then calls tools
one decision at a time. Every call is validated, policy-classified, executed
through the registry, and appended back as a ``tool`` message so the next
decision sees the real result. The loop ends on ``finish``, on a control prompt
(``ask_owner``), on a hard budget, on cancellation, or on a brain failure —
never on a guess and never with a claim a receipt does not support.

Approval model: read-only tools run immediately. Mutating tools park for one
owner decision (this call / the whole run / deny). A denial is handed back to
the brain as a failed observation, so it can adapt instead of ending the run.
Hard DENY verdicts from the lanes (sudo, secret paths, rm -rf /) are final and
never become approval requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.agent import compaction
from app.agent import tools as agent_tools
from app.agent.brain import BrainChoice, build_agent_provider, resolve_agent_brain
from app.agent.tools import AgentTool, ToolContext
from app.config import settings
from app.contracts import ChatMessage
from app.fabric.policy import physical_confirm_required
from app.gateway.validation import validate_arguments

logger = logging.getLogger("ev.agent.loop")


class CancelToken:
    """Cooperative cancellation checked between every step."""

    def __init__(self) -> None:
        self.cancelled = False
        self.reason = ""

    def cancel(self, reason: str = "cancelled by owner") -> None:
        self.cancelled = True
        self.reason = reason


@dataclass
class AgentLimits:
    max_steps: int = 24
    max_tool_calls: int = 40
    max_seconds: int = 900

    @classmethod
    def from_settings(cls) -> AgentLimits:
        return cls(
            max_steps=max(1, min(int(getattr(settings, "agent_max_steps", 24) or 24), 64)),
            max_tool_calls=max(1, min(int(getattr(settings, "agent_max_tool_calls", 40) or 40), 200)),
            max_seconds=max(5, min(int(getattr(settings, "agent_max_seconds", 900) or 900), 3600)),
        )


@dataclass
class ApprovalRequest:
    run_id: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    policy: str
    #: High-risk class from the policy risk floor (send, call, sql_write, ...).
    risk: str | None = None
    #: Only a physical confirmation (HUD card, paired phone, typed reply) may
    #: approve this request. A spoken "yes" is refused upstream.
    physical_required: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "reason": self.reason,
            "policy": self.policy,
            "risk": self.risk,
            "physical_required": self.physical_required,
        }


class ApprovalGate(Protocol):
    async def request(self, request: ApprovalRequest) -> str: ...  # approve|approve_run|deny


class DenyGate:
    """Headless default: refuse mutating tools instead of guessing."""

    async def request(self, request: ApprovalRequest) -> str:
        del request
        return "deny"


class ApproveGate:
    """Explicit owner opt-in (``--yes``): the whole run is approved."""

    async def request(self, request: ApprovalRequest) -> str:
        del request
        return "approve_run"


class CallbackGate:
    """Wrap a sync/async owner callback returning approve / approve_run / deny."""

    def __init__(self, callback: Callable[[ApprovalRequest], Any]) -> None:
        self._callback = callback

    async def request(self, request: ApprovalRequest) -> str:
        result = self._callback(request)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result or "deny")


@dataclass
class QueueGate:
    """Event-driven gate used by the API: approve/deny from another request."""

    _waiters: list[asyncio.Future] = field(default_factory=list)
    last_request: ApprovalRequest | None = None

    async def request(self, request: ApprovalRequest) -> str:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        self.last_request = request
        try:
            return await asyncio.wait_for(
                future, timeout=max(5, int(getattr(settings, "agent_approval_timeout_s", 300)))
            )
        except TimeoutError:
            return "deny"
        finally:
            if future in self._waiters:
                self._waiters.remove(future)
            self.last_request = None

    @property
    def waiting(self) -> bool:
        return bool(self._waiters)

    def decide(self, decision: str) -> bool:
        if not self._waiters:
            return False
        for future in list(self._waiters):
            if not future.done():
                future.set_result(decision)
        return True


@dataclass
class AgentStep:
    index: int
    tool: str
    arguments: dict[str, Any]
    policy: str
    approval: str
    ok: bool
    error: str | None = None
    elapsed_ms: int = 0
    result: Any = None
    verified: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "policy": self.policy,
            "approval": self.approval,
            "ok": self.ok,
            "verified": self.verified,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "result": self.result,
        }


@dataclass
class AgentResult:
    run_id: str
    goal: str
    status: str
    ok: bool
    summary: str
    brain: dict[str, Any]
    root: str
    approved: bool
    steps: list[AgentStep] = field(default_factory=list)
    messages: int = 0
    tool_calls: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    elapsed_ms: int = 0
    error: str | None = None
    degraded: bool = False
    needs_input: bool = False
    question: str = ""
    options: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "brain": self.brain,
            "root": self.root,
            "approved": self.approved,
            "steps": [step.to_dict() for step in self.steps],
            "messages": self.messages,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "degraded": self.degraded,
            "needs_input": self.needs_input,
            "question": self.question,
            "options": self.options,
        }


_SYSTEM = """You are Evie: the owner's personal AI, running on their own Mac.
You are the mind for this turn. You decide what happens; the tools are your
hands. Everything the owner can ask for is reachable through a tool — there
is no fixed list of commands you are limited to.

<plan>
- If you will need more than one tool call, call todo_write FIRST, before
  any other tool, with the steps you plan. Update it as you go. The owner
  sees that list live while you work, and a plan keeps a long task from
  losing its place.
</plan>

<dangerous_actions>
- Before an action that is hard to undo, or that affects shared systems or
  other people, the owner confirms. Sending a message, deleting, publishing,
  or changing shared data are the usual cases. An approval covers only the
  action that was approved.
- Never claim an effect unless a tool receipt shows it. If a tool failed,
  say so plainly.
- Never try to bypass policy (no sudo, no secret paths, no privilege
  escalation attempts).
</dangerous_actions>

<work_policy>
- Complete every explicit requirement. If you cannot complete one, say so.
- If the owner is asking a question, or asking for an explanation, answer it.
  Change things only when the owner asks for a change.
- Read before you write. Verify after you write.
- Prefer the smallest number of steps. Do not call a tool just to look busy.
- Never run an interactive or unbounded command (top, htop, watch, tail -f,
  less, vim, nano, an interactive ssh, or a sleep longer than a minute).
- When the goal is complete (or cannot proceed), call finish once with a short
  summary of what actually happened, citing what was verified.
- If a tool times out or fails twice, change the approach instead of repeating
  it. If five steps fail in a row, stop and tell the owner what is blocked.
- Shell commands run in the workspace root ({root}).
</work_policy>

<tools>
- Your tools are your capabilities. Each one is a real operation on the
  owner's Mac and their own data, and each returns a receipt.
- capability.discover lists what is available in a domain when you need
  something you do not have a tool for. digital.discover and atlas.discover
  do the same for the owner's apps and services. Use them instead of
  guessing a name.
- The owner's personal data lives behind its own tools: memory.search for
  what they remember, life.mail and life.messages for what arrived,
  people.lookup for who, life.send for sending, owner.profile for their
  details. Never answer those questions from memory alone.
- The Mac semantic bus (mail, messages, calendar, contacts, weather, timers,
  PIM) is behind its own tools too. Use them for live personal data.
- Files have their own tools, and you should prefer them over the shell:
  list_dir to see a directory, read_file to read one, write_file to create
  or replace one, edit_file to change part of one, append_file to add to
  one, search_files to find files by name or text, and move_path /
  copy_path / delete_path to move, copy or trash one. files.act is the
  same capability as one call when a single tool fits the job better.
- run_shell is for commands: builds, tests, git, anything else a shell does.
  One command per call, non-interactive, and never as a substitute for the
  file tools above.
- For visible terminal work (Ghostty, CLIs, resuming sessions) use
  terminal_open/terminal_run/terminal_read. Use read_image when the owner
  points at a screenshot, and capture_screen when you need to see the screen.
</tools>

<questions>
- If the goal is genuinely ambiguous or the decision is the owner's, call
  ask_owner with one short question instead of guessing. Offer options when
  the answer is a choice between known paths.
- Ask only when you cannot continue without the answer. If the context is
  enough, act on it.
</questions>

<communication>
- Answer first, then the supporting details. Plain words, active voice.
- The owner has not seen your tool calls, so each message must make sense on
  its own. Say what you did and what you found.
- Say that something is done only when a receipt shows it. If you did not
  verify it, say so.
- Avoid stock phrases such as "delve", "leverage", "it's worth noting", and
  "Bottom line:".
</communication>

<formatting>
- Your output is shown as GitHub-flavored markdown. Use paragraphs for
  explanations and bullet lists for parallel items. Use `inline code` for
  identifiers, paths, and commands.
</formatting>

<environment>
- Today is {today}. Platform: {platform}.
{approval_note}</environment>"""


def _system_prompt(root: Path, approved: bool) -> str:
    import platform
    from datetime import date

    approval_note = (
        "The run is pre-approved: mutating tools may execute without asking again.\n"
        if approved
        else "Mutating tools will ask the owner once; a denial comes back as a failed result.\n"
    )
    return _SYSTEM.format(
        root=root,
        today=date.today().isoformat(),
        platform=platform.platform(),
        approval_note=approval_note,
    )


#: Argument names that name a place on disk. The policy layer's secret guard
#: is about secret *paths*, so only these are judged. A message body that
#: happens to contain the word "token" is not a secret path, and must not
#: silently deny an ordinary send.
_PATH_KEYS = frozenset({
    "path", "paths", "file_path", "target_file", "target_directory",
    "directory", "dir", "cwd", "db_path", "project_dir", "source",
    "destination", "dest", "root",
})


def _policy_target(arguments: dict[str, Any]) -> str:
    """The value the policy layer should judge for one call.

    The fabric guard looks for secret-shaped targets (a keychain path, a
    credential file). A capability hides those behind its own argument names,
    so the target is read from the arguments rather than from the tool name.
    Only path-shaped arguments count: scanning free text would deny a
    legitimate message because it mentioned a secret.
    """
    parts: list[str] = []
    for key, value in (arguments or {}).items():
        if str(key).lower() not in _PATH_KEYS:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if isinstance(item, str))
    return " ".join(parts)[:2000]


def _action_class(tool: AgentTool) -> str:
    """The fabric action class for one tool, read off its own metadata.

    The semantic capabilities already carry a namespaced permission
    (``messaging:send``, ``fabric:shell``) and a risk class. Classifying from
    that row keeps the policy decision on the same data the model was shown,
    instead of a second list that has to be kept in step by hand.
    """
    permission = str(getattr(tool.spec, "permission", "") or "").lower()
    tail = permission.rsplit(":", 1)[-1]
    if tail == "send":
        return "send"
    if tail in {"shell", "session", "exec"}:
        return "shell"
    if tail in {"delete", "bulk"}:
        return tail
    if str(getattr(tool.spec, "name", "")) == "write_file":
        return "write"
    return "run"


def _risk(tool: AgentTool, arguments: dict[str, Any]) -> str | None:
    """The policy risk floor's class for one call (None for ordinary work)."""
    from app.fabric.policy import risk_class

    return risk_class(
        str(tool.spec.name), str(getattr(tool.spec, "permission", "") or ""), arguments
    )


def _classify(tool: AgentTool, arguments: dict[str, Any], *, approved: bool) -> str:
    """AUTO | CONFIRM | DENY for one validated call (lanes keep final say).

    The risk floor sits under the autonomy setting: a send, call, SQL write,
    permanent delete, publish or egress asks even in a pre-approved run, unless
    a live standing grant covers its class.
    """
    from app.fabric.policy import decide as fabric_decide
    from app.fabric.policy import grant_lifts
    from app.fabric.shell_sessions import classify_command

    risk = _risk(tool, arguments)
    floor = bool(risk) and not grant_lifts(risk)

    if tool.spec.name == "run_shell":
        cls = classify_command(str(arguments.get("cmd") or ""))
        if cls == "DENY":
            return "DENY"
        if floor:
            return "CONFIRM"
        if cls == "CONFIRM" and not (
            approved or bool(getattr(settings, "agent_allow_destructive", False))
        ):
            return "CONFIRM"
        return "AUTO"

    action = _action_class(tool)
    target = _policy_target(arguments) or str(tool.spec.name)
    gate = fabric_decide(action, target, "owner_turn")
    is_capability = "." in str(tool.spec.name)

    if is_capability and gate == "DENY":
        # A capability's read operation can still reach a secret path, and
        # the semantic layer does not guard paths as precisely as the agent's
        # own file tools do. A DENY is final and is checked before anything
        # that would shortcut to AUTO.
        return "DENY"

    if floor:
        return "CONFIRM"

    if tool.spec.read_only:
        return "AUTO"

    # A polymorphic capability can be a read or a write depending on the
    # operation it names: owner.profile with op=get reads the owner's name,
    # owner.profile with op=set writes it. The capability's own lanes own
    # those operation vocabularies; the bridge imports them.
    try:
        from app.agent.capability_bridge import is_read_call

        if is_read_call(str(tool.spec.name), arguments):
            return "AUTO"
    except Exception:  # noqa: BLE001 - a missing vocabulary must not fail the call
        logger.debug("read-call vocabulary unavailable", exc_info=True)

    if gate == "DENY":
        return "DENY"
    return "CONFIRM" if not approved else "AUTO"


def _audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments safe to persist: no file bodies, secrets redacted."""
    from app.fabric.failures import redact

    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if key == "content":
            out[key] = f"<{len(str(value))} chars>"
        elif isinstance(value, str):
            out[key] = redact(value)[:2000]
        else:
            out[key] = value
    return out


def _audit_result(result: Any) -> Any:
    """Tool result safe to persist: bodies become length + preview, secrets redacted."""
    from app.fabric.failures import redact

    if not isinstance(result, dict):
        return result
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, str):
            if key in {"content", "text", "body"}:
                out[key] = {
                    "chars": len(value),
                    "preview": redact(value[:120]),
                }
            else:
                out[key] = redact(value[:2000])
        elif isinstance(value, list) and len(value) > 50:
            out[key] = list(value[:50]) + [f"... {len(value) - 50} more"]
        else:
            out[key] = value
    return out


#: Agent tools with a direct fabric-lane equivalent. A span is recorded so
#: model-first turns stay traceable through the same evidence ring as the
#: conversational lanes. Tools without an equivalent (app/GUI acts, Mac bus,
#: web fetch) leave no span — the loop's own event stream is their record —
#: rather than inventing lane names.
_TOOL_SPANS: dict[str, tuple[str, str]] = {
    "run_shell": ("shell", "run"),
    "read_file": ("files", "read"),
    "list_dir": ("files", "list"),
    "write_file": ("files", "create"),
    "append_file": ("files", "append"),
    "edit_file": ("files", "overwrite"),
    "move_path": ("files", "move"),
    "copy_path": ("files", "copy"),
    "delete_path": ("files", "delete"),
    "search_files": ("files", "search"),
    "sql_query": ("sql", "query"),
    "capture_screen": ("screen", "capture"),
    "read_image": ("screen", "read"),
    "terminal_open": ("terminal", "open"),
    "terminal_run": ("terminal", "run"),
    "terminal_read": ("terminal", "read"),
    "terminal_list": ("terminal", "list"),
}


async def _execute_tool(
    tool: AgentTool, arguments: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    try:
        mapped = _TOOL_SPANS.get(tool.spec.name)
        if mapped is None:
            return await tool.run(arguments, ctx)
        from app.fabric.trace import span as _trace_span

        with _trace_span(mapped[0], mapped[1]):
            return await tool.run(arguments, ctx)
    except Exception as exc:  # noqa: BLE001 - a tool crash is a failed call
        logger.exception("agent tool %s crashed", tool.spec.name)
        return agent_tools._receipt(ok=False, error=f"tool crashed: {type(exc).__name__}: {exc}")


async def run_agent(
    goal: str,
    *,
    root: str | Path | None = None,
    approved: bool | None = None,
    brain: BrainChoice | None = None,
    provider: Any = None,
    limits: AgentLimits | None = None,
    gate: ApprovalGate | None = None,
    cancel: CancelToken | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    run_id: str | None = None,
    context: str = "",
) -> AgentResult:
    """Run one goal to completion, a control stop, or an honest failure."""
    limits = limits or AgentLimits.from_settings()
    # The owner's autonomy setting decides whether a mutating step parks for a
    # second decision. It never relaxes the hard DENY rules: those are checked
    # in _classify and inside each lane, and stay final. An explicit
    # ``approved=False`` (tests, a confirm-mode surface) is still honoured.
    if approved is None:
        approved = (
            str(getattr(settings, "agent_autonomy", "trusted") or "trusted").lower()
            == "trusted"
        )
    approved = bool(approved)
    choice = brain or resolve_agent_brain()
    rid = run_id or f"run-{uuid.uuid4().hex[:12]}"
    root_path = agent_tools._norm_root(root)
    started = time.perf_counter()
    steps: list[AgentStep] = []
    executed_signatures: set[str] = set()
    consecutive_failures = 0
    messages: list[ChatMessage] = []
    usage: dict[str, Any] = {}
    tool_call_count = 0
    cancelled = cancel or CancelToken()

    def emit(kind: str, **fields: Any) -> None:
        if on_event is None:
            return
        try:
            on_event({"kind": kind, "run_id": rid, **fields})
        except Exception:  # noqa: BLE001 - telemetry must not kill a run
            logger.debug("agent event sink failed", exc_info=True)

    def result(status: str, summary: str, **extra: Any) -> AgentResult:
        return AgentResult(
            run_id=rid,
            goal=goal,
            status=status,
            ok=status in {"completed", "needs_input"} and not extra.get("force_not_ok", False),
            summary=summary,
            brain=choice.to_dict(),
            root=str(root_path),
            approved=bool(approved),
            steps=steps,
            messages=len(messages),
            tool_calls=tool_call_count,
            usage=usage,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            degraded=choice.degraded,
            **{k: v for k, v in extra.items() if k != "force_not_ok"},
        )

    started_at = time.time()
    emit("run_started", goal=goal, root=str(root_path), brain=choice.to_dict())

    if not (goal or "").strip():
        return result("failed", "I need a goal to work on.", error="empty_goal")
    if not root_path.is_dir():
        return result(
            "failed", f"The workspace root does not exist: {root_path}", error="bad_root"
        )

    if provider is None:
        if choice.degraded:
            return result(
                "failed",
                (
                    "No agent brain is configured, so I won't pretend to think. "
                    "Set EV_OPENROUTER_API_KEY (DeepSeek main brain), "
                    "EV_XAI_API_KEY, EV_DEEPSEEK_API_KEY, or "
                    "EV_AGENT_OPENAI_API_KEY (or EV_AGENT_PROVIDER with a local "
                    "endpoint), then run again."
                ),
                error="brain_unavailable",
            )
        if not choice.available:
            return result(
                "failed",
                (
                    f"No usable brain: {choice.reason}. Set EV_OPENROUTER_API_KEY "
                    "(DeepSeek main brain), EV_XAI_API_KEY, EV_DEEPSEEK_API_KEY, "
                    "or EV_AGENT_OPENAI_API_KEY, or pick a provider with "
                    "EV_AGENT_PROVIDER."
                ),
                error="brain_unavailable",
            )
        provider = build_agent_provider(choice)

    # Tool-calling runs want coherent multi-step reasoning, not the high-effort
    # chain that makes a single file edit take a minute. The kernel's mouth
    # provider sets its own per-turn effort; the agent gets one explicit level.
    if hasattr(provider, "reasoning_effort") and not getattr(provider, "reasoning_effort", None):
        effort = str(getattr(settings, "agent_reasoning_effort", "medium") or "medium").lower()
        if effort in {"low", "medium", "high"}:
            provider.reasoning_effort = effort

    messages = [
        ChatMessage(role="system", content=_system_prompt(root_path, approved)),
        ChatMessage(role="user", content=goal + (f"\n\nContext:\n{context}" if context else "")),
    ]
    specs = agent_tools.agent_tool_specs()
    ctx = ToolContext(root=root_path, approved=approved, run_id=rid)
    # The plan tool publishes on the run's event stream, and the capability
    # bridge keeps one cognition per run. Both live on the context, so the
    # context has to be the one object every tool call sees.
    ctx.extra["emit"] = emit
    deadline = time.monotonic() + limits.max_seconds

    # Context budget. The tool surface is sent on every request, so a wide
    # surface spends most of the window before the conversation starts.
    context_tokens = int(getattr(settings, "agent_context_tokens", 0) or 0)
    compact_percent = int(getattr(settings, "agent_compact_threshold_percent", 85) or 85)
    keep_recent = int(getattr(settings, "agent_compact_keep_recent", 12) or 12)
    schema_tokens = compaction.tool_schema_tokens(specs)

    async def _fit_context() -> None:
        """Compact the older transcript when it would overflow the window."""
        if not context_tokens:
            return
        if not compaction.context_budget_exceeded(
            messages,
            context_tokens=context_tokens,
            threshold_percent=compact_percent,
            tool_schema_tokens=schema_tokens,
        ):
            return
        compacted, report = compaction.compact_messages(
            messages, keep_recent=keep_recent
        )
        if not report.compacted:
            return
        messages[:] = compacted
        emit("context_compacted", **report.to_dict())

    async def ask(req: ApprovalRequest) -> str:
        emit("approval_requested", **req.to_dict())
        decision = "deny"
        if gate is not None:
            try:
                decision = await gate.request(req)
            except Exception:  # noqa: BLE001
                logger.exception("approval gate failed")
                decision = "deny"
        emit("approval_decision", tool=req.tool, decision=decision)
        return decision

    model_kwargs: dict[str, Any] = {}
    for step_index in range(1, limits.max_steps + 1):
        if cancelled.cancelled:
            return result("cancelled", f"Stopped: {cancelled.reason}.", error="cancelled")
        if time.monotonic() > deadline:
            return result(
                "capped",
                f"I stopped after the {limits.max_seconds}s budget without finishing.",
                error="time_budget",
            )
        try:
            from app.security.boundary import ModelBoundaryViolation, guard_model_payload

            await _fit_context()
            try:
                safe_messages = guard_model_payload(messages, None)
            except ModelBoundaryViolation as exc:
                return result(
                    "failed",
                    f"I can't send that to the model: {exc}",
                    error="privacy_boundary",
                )
            reply = await provider.chat_with_tools(
                safe_messages, specs, temperature=0.2, **model_kwargs
            )
        except Exception as exc:  # noqa: BLE001 - map brain failures honestly
            from app.gateway.cloud_providers import (
                CloudChatEgressDenied,
                CloudChatUnavailable,
            )
            from app.gateway.openrouter_mimo import MimoEgressDenied, MimoUnavailable
            from app.gateway.reliability import CircuitOpenError

            if isinstance(exc, (MimoEgressDenied, CloudChatEgressDenied)):
                return result("failed", f"Remote chat is blocked: {exc}", error="egress_denied")
            if isinstance(exc, (MimoUnavailable, CloudChatUnavailable)):
                return result("failed", f"The brain is unavailable: {exc}", error="brain_unavailable")
            if isinstance(exc, CircuitOpenError):
                return result("failed", f"The brain circuit is open: {exc}", error="brain_unavailable")
            if isinstance(exc, TimeoutError):
                return result("failed", "The brain timed out before deciding.", error="brain_timeout")
            logger.exception("agent brain call failed")
            return result(
                "failed", f"The brain call failed: {type(exc).__name__}: {exc}", error="brain_error"
            )

        usage = dict(getattr(reply, "usage", None) or usage)
        calls = list(getattr(reply, "tool_calls", None) or [])
        messages.append(ChatMessage(role="assistant", content=reply.text or "", tool_calls=calls or None))
        emit("brain_turn", step=step_index, tool_calls=len(calls), text=(reply.text or "")[:400])

        if not calls:
            summary = (reply.text or "").strip() or "The brain returned no answer."
            if any(not step.ok for step in steps):
                return result(
                    "completed", summary, error="steps_failed", force_not_ok=True
                )
            return result("completed", summary, error=None)

        for call in calls:
            if cancelled.cancelled:
                return result("cancelled", f"Stopped: {cancelled.reason}.", error="cancelled")
            tool_call_count += 1
            if tool_call_count > limits.max_tool_calls:
                return result(
                    "capped",
                    f"I stopped after {limits.max_tool_calls} tool calls without finishing.",
                    error="tool_budget",
                )
            name = str(getattr(call, "name", "") or "")
            raw_args = dict(getattr(call, "arguments", None) or {})
            call_id = str(getattr(call, "id", None) or f"call-{step_index}-{tool_call_count}")

            if name == "finish":
                summary = str(raw_args.get("summary") or "").strip() or "Done."
                claimed_ok = raw_args.get("ok")
                failed = [step for step in steps if not step.ok]
                if claimed_ok is False:
                    return result("failed", summary, error="brain reported failure")
                if failed:
                    return result(
                        "completed",
                        summary,
                        error="claimed success but steps failed",
                        force_not_ok=True,
                    )
                return result("completed", summary, error=None)
            if name == "ask_owner":
                question = str(raw_args.get("question") or "").strip() or "What should I do?"
                options: list[dict[str, str]] = []
                raw_options = raw_args.get("options")
                if isinstance(raw_options, list):
                    for entry in raw_options:
                        if not isinstance(entry, dict):
                            continue
                        label = str(entry.get("label") or "").strip()
                        if not label:
                            continue
                        options.append({
                            "label": label[:120],
                            "description": str(entry.get("description") or "").strip()[:240],
                        })
                out = result(
                    "needs_input",
                    question,
                    needs_input=True,
                    question=question,
                    options=options,
                )
                return out

            tool = agent_tools.REGISTRY.get(name)
            if tool is None:
                receipt = agent_tools._receipt(ok=False, error=f"unknown tool {name!r}")
                messages.append(ChatMessage(
                    role="tool", content=agent_tools.tool_json(receipt),
                    name=name or "unknown", tool_call_id=call_id,
                ))
                steps.append(AgentStep(
                    index=step_index, tool=name or "unknown", arguments=raw_args,
                    policy="AUTO", approval="n/a", ok=False, error=receipt["error"],
                ))
                continue

            effective, issues = validate_arguments(raw_args, tool.spec.parameters)
            if issues:
                receipt = agent_tools._receipt(
                    ok=False, error="invalid arguments: " + "; ".join(issues)
                )
                messages.append(ChatMessage(
                    role="tool", content=agent_tools.tool_json(receipt),
                    name=name, tool_call_id=call_id,
                ))
                steps.append(AgentStep(
                    index=step_index, tool=name, arguments=raw_args, policy="AUTO",
                    approval="n/a", ok=False, error=receipt["error"],
                ))
                continue

            policy = _classify(tool, effective, approved=ctx.approved)
            risk = _risk(tool, effective) if policy == "CONFIRM" else None
            high_risk = physical_confirm_required(risk)
            approval = "n/a"
            # An approved call still shares the run's context: the capability
            # bridge keeps one cognition per run, and the plan tool keeps one
            # plan. A fresh context here would silently start a second run's
            # state mid-run.
            approved_ctx = ToolContext(
                root=ctx.root, approved=True, run_id=rid, extra=ctx.extra
            )

            signature = f"{name}:{json.dumps(effective, sort_keys=True, default=str)}"
            if signature in executed_signatures:
                # The same call already ran in this run. Re-running a mutating
                # command or retrying a timed-out one is how an agent burns ten
                # minutes without progress; tell the brain to change course.
                receipt = agent_tools._receipt(
                    ok=False,
                    error=(
                        "duplicate call: this exact tool and arguments already ran; "
                        "change the approach or finish with what you have"
                    ),
                    verified=True,
                )
                steps.append(AgentStep(
                    index=step_index, tool=name, arguments=_audit_arguments(effective),
                    policy=policy, approval="n/a", ok=False, error=receipt["error"],
                ))
                messages.append(ChatMessage(
                    role="tool", content=agent_tools.tool_json(receipt),
                    name=name, tool_call_id=call_id,
                ))
                emit(
                    "tool_result", step=step_index, tool=name, ok=False,
                    policy=policy, approval="n/a", error=receipt["error"],
                )
                continue
            executed_signatures.add(signature)

            async def run_now(
                target_ctx: ToolContext,
                *,
                decision: str = "n/a",
                _step: int = step_index,
                _tool: AgentTool = tool,
                _args: dict[str, Any] = effective,  # noqa: B006 - bound per iteration
                _name: str = name,
                _policy: str = policy,
            ):
                emit(
                    "tool_started", step=_step, tool=_name,
                    policy=_policy, approval=decision,
                )
                return await _execute_tool(_tool, _args, target_ctx)
            if policy == "DENY":
                deny_error = "denied by policy"
                if name == "run_shell":
                    deny_error = agent_tools._deny_hint(
                        str(effective.get("cmd") or ""), None
                    )
                receipt = agent_tools._receipt(
                    ok=False, error=deny_error, verified=True,
                    evidence={"tool": name},
                )
            elif policy == "CONFIRM" and (high_risk or not ctx.approved):
                decision = await ask(ApprovalRequest(
                    run_id=rid, tool=name, arguments=effective,
                    reason=(
                        f"{name} is a high-risk action ({risk})" if high_risk
                        else f"{name} is a mutating action"
                    ),
                    policy=policy, risk=risk, physical_required=high_risk,
                ))
                approval = decision
                if high_risk and decision == "approve_run":
                    # An approval covers only the action that was approved.
                    decision = "approve"
                    approval = "approve"
                if decision == "approve_run":
                    ctx.approved = True
                    approved = True
                    receipt = await run_now(ctx, decision=decision)
                elif decision == "approve":
                    receipt = await run_now(approved_ctx, decision=decision)
                else:
                    receipt = agent_tools._receipt(
                        ok=False, error="owner denied this action", verified=True
                    )
            else:
                receipt = await run_now(ctx)
                if receipt.get("needs_confirm") and not ctx.approved:
                    decision = await ask(ApprovalRequest(
                        run_id=rid, tool=name, arguments=effective,
                        reason=f"{name} touches a path outside the workspace root",
                        policy="CONFIRM",
                    ))
                    approval = decision
                    if decision == "approve_run":
                        ctx.approved = True
                        approved = True
                        receipt = await run_now(ctx, decision=decision)
                    elif decision == "approve":
                        receipt = await run_now(approved_ctx, decision=decision)
                    else:
                        receipt = agent_tools._receipt(
                            ok=False, error="owner denied this action", verified=True
                        )

            step = AgentStep(
                index=step_index,
                tool=name,
                arguments=_audit_arguments(effective),
                policy=policy,
                approval=approval,
                ok=bool(receipt.get("ok")),
                verified=bool(receipt.get("verified")),
                error=receipt.get("error"),
                result=_audit_result(receipt.get("result")),
                elapsed_ms=int(receipt.get("evidence", {}).get("elapsed_ms") or 0),
            )
            steps.append(step)
            emit("tool_result", step=step_index, tool=name, ok=step.ok,
                 policy=policy, approval=approval, error=step.error)
            messages.append(ChatMessage(
                role="tool", content=agent_tools.tool_json(receipt),
                name=name, tool_call_id=call_id,
            ))
            if step.ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    return result(
                        "failed",
                        (
                            f"I stopped after {consecutive_failures} failed steps in a row. "
                            f"Last error: {step.error or 'unknown error'}. "
                            "Tell me a different way to do it, or give me the missing detail."
                        ),
                        error="no_progress",
                    )

    return result(
        "capped",
        f"I stopped after {limits.max_steps} steps without a final answer.",
        error="step_budget",
    )
