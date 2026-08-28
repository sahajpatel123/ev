"""F2 Computer Executor — Evie's internal hands over existing transports.

PRINCIPLE (F2 law): the primitives are Evie's hands, not her brain. They
perform structured operations; they never decide what the owner meant, whether
an action is safe, or whether a plan is correct. Planning/routing/policy stay
above (tool_select / TurnGate / policy.evaluate_policy).

ONE EXECUTOR, NO NEW REGISTRY: this module binds to the EXISTING capability
names (inspect_ui, ui_action, open_app, ..., sandbox.read_file,
operations.NAMED_OPERATIONS) and the EXISTING transports
(LiveSession.request_computer → MacControlService; app.tools.sandbox jail).
No new tool catalog, no second policy system.

Families:
  observe  — read-only perception (AX first, vision only when structured UI
             cannot answer). Normally R0. Must not mutate UI.
  act      — press/focus/select/type/paste/key/scroll/drag/increment/
             decrement/expand/collapse/confirm/cancel through ui_action.
             NOT new model-facing tool names.
  navigate — open/close/activate app, open URL via native NSWorkspace paths.
  fs       — jailed sandbox file operations (read/write today; destructive
             fs ops stay unavailable until their jail policy exists).
  exec     — named, allowlisted structured operations only (R4; never
             available to the realtime catalog).

VERIFICATION CONTRACT: for meaningful mutations the executor performs
OBSERVE BEFORE → ACT → OBSERVE AFTER → VERIFY EXPECTED EFFECT. ``ok`` only
means the primitive was attempted/succeeded at the API level (EXECUTED);
``verified`` means the expected semantic effect was subsequently observed.
The report layer may claim semantic success only when verified is true (the
pre-existing false-success gate), unless a capability contract defines a
different authoritative success signal.

Feature flag ``EV_COMPUTER_EXECUTOR_V2``: off | shadow | on.
  off    — existing computer paths only.
  shadow — mutations stay on the old path (never double-click); the executor
           records planning/validation/risk + may dual-run READ-ONLY observes.
  on     — existing tool names route through this executor; on any executor
           failure the adapter falls back to the old path and records it.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import settings


class SideEffectState(StrEnum):
    """Execution fence: has a mutating side effect possibly been attempted?

    PERMANENT LAW (F2/F3): once a state in MUTATION_RISK_STATES is reached,
    automatic legacy fallback is FORBIDDEN — the action must never be repeated
    blindly. Ambiguity fails truthfully.
    """

    NOT_ATTEMPTED = "not_attempted"
    PRECONDITION_FAILED = "precondition_failed"  # refused before any dispatch
    POLICY_DENIED = "policy_denied"
    DISPATCH_NOT_STARTED = "dispatch_not_started"  # nothing was sent
    ATTEMPTED_NO_EFFECT_KNOWN = "attempted_no_effect_known"
    EFFECT_OBSERVED = "effect_observed"  # dispatched; effect seen but unverified
    VERIFIED = "verified"
    AMBIGUOUS_AFTER_ATTEMPT = "ambiguous_after_attempt"  # sent; outcome unknown


# States from which a retry/fallback could duplicate a real-world mutation.
MUTATION_RISK_STATES = frozenset(
    {
        SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN,
        SideEffectState.EFFECT_OBSERVED,
        SideEffectState.VERIFIED,
        SideEffectState.AMBIGUOUS_AFTER_ATTEMPT,
    }
)
# States from which legacy fallback is legal (§2 A/B: read-only or
# failure BEFORE mutation dispatch began).
FALLBACK_SAFE_STATES = frozenset(
    {
        SideEffectState.NOT_ATTEMPTED,
        SideEffectState.PRECONDITION_FAILED,
        SideEffectState.POLICY_DENIED,
        SideEffectState.DISPATCH_NOT_STARTED,
    }
)

# Client-level refusals: the command reached the client and was refused
# BEFORE any UI mutation — re-routing is safe.
PRE_DISPATCH_REFUSALS = frozenset(
    {
        "stale_element",
        "element_not_found",
        "sensitive_field",
        "missing_element",
        "protected",
        "invalid_url",
        "find_only",
        "unsupported",
        "unknown_command",
    }
)

EXECUTOR_TOOLS = frozenset(
    {
        "computer_status",
        "list_apps",
        "inspect_ui",
        "screen_look",
        "ui_action",
        "open_app",
        "close_app",
        "activate_app",
        "open_url",
    }
)

# ui_action verbs that change UI state and therefore deserve the verify
# contract. Read-only ui verbs (e.g. value getters) stay single-observe.
MUTATING_UI_ACTIONS = frozenset(
    {
        "press",
        "click_at",
        "focus",
        "select",
        "type",
        "paste",
        "key",
        "scroll",
        "drag",
        "increment",
        "decrement",
        "expand",
        "collapse",
        "confirm",
        "cancel",
        "append",
        "replace",
        "set",
        "raise",
    }
)

NAVIGATE_TOOLS = frozenset({"open_app", "close_app", "activate_app", "open_url"})
OBSERVE_TOOLS = frozenset({"computer_status", "list_apps", "inspect_ui", "screen_look"})

# (family, operation) → existing capability name (policy truth stays there)
CAPABILITY_FOR_OPERATION: dict[tuple[str, str], str] = {
    ("observe", "inspect_ui"): "inspect_ui",
    ("observe", "screen_look"): "screen_look",
    ("observe", "list_apps"): "list_apps",
    ("observe", "computer_status"): "computer_status",
    ("act", "ui_action"): "ui_action",
    ("navigate", "open_app"): "open_app",
    ("navigate", "close_app"): "close_app",
    ("navigate", "activate_app"): "activate_app",
    ("navigate", "open_url"): "open_url",
    ("fs", "read"): "tool.file_read",
    ("fs", "write"): "tool.file_write",
    ("exec", "run"): "execute_command",
}


def executor_mode() -> str:
    return (getattr(settings, "computer_executor_v2", "off") or "off").strip().lower()


def family_for_tool(name: str) -> str | None:
    if name in OBSERVE_TOOLS:
        return "observe"
    if name == "ui_action":
        return "act"
    if name in NAVIGATE_TOOLS:
        return "navigate"
    return None


def is_mutating(name: str, arguments: dict[str, Any] | None) -> bool:
    if name == "ui_action":
        return str((arguments or {}).get("action") or "").lower() in MUTATING_UI_ACTIONS
    return name in NAVIGATE_TOOLS


@dataclass
class ComputerExecutionRequest:
    """One structured executor job. Internal — never a model-facing schema."""

    primitive: str  # family: observe | act | navigate | fs | exec
    operation: str  # executor-level operation (tool name / fs op / named op)
    target: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    execution_id: str = ""
    owner_turn_id: str | None = None
    device_id: str | None = None
    goal: str | None = None
    expected_effect: dict[str, Any] | None = None
    risk_context: dict[str, Any] = field(default_factory=dict)
    snapshot_ref: str | None = None
    idempotency_key: str | None = None

    def capability_name(self) -> str:
        return CAPABILITY_FOR_OPERATION.get((self.primitive, self.operation), self.operation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "primitive": self.primitive,
            "operation": self.operation,
            "target": self.target,
            "goal": self.goal,
            "expected_effect": self.expected_effect,
            "capability": self.capability_name(),
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class ComputerExecutionResult:
    """EXECUTED (primitive ran) vs VERIFIED (expected effect observed)."""

    execution_id: str
    ok: bool = False
    executed: bool = False
    verified: bool = False
    side_effect: SideEffectState = SideEffectState.NOT_ATTEMPTED
    observation_before: dict[str, Any] | None = None
    observation_after: dict[str, Any] | None = None
    error_code: str | None = None
    retryable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    family: str = ""
    operation: str = ""
    policy_risk: str | None = None
    latency_ms: float | None = None
    fallback_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fallback_allowed(self) -> bool:
        """Legacy fallback legality per the F2 execution-fence law (§2)."""

        return self.side_effect in FALLBACK_SAFE_STATES

    def to_tool_payload(self) -> dict[str, Any]:
        """Shape compatible with the existing computer tool receipts."""

        payload = dict(self.raw or {})
        payload.setdefault("ok", self.ok)
        payload["executed"] = self.executed
        payload["verified"] = self.verified
        if self.error_code:
            payload.setdefault("error", self.error_code)
        if self.evidence:
            payload["executor_evidence"] = {
                key: value
                for key, value in self.evidence.items()
                if key in {"before", "after", "effect", "strategy", "transport_ms"}
            }
        return payload


def _new_execution_id() -> str:
    from uuid import uuid4

    return f"exec-{uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Execution fence: exactly-once mutation guarantee across retries
# ---------------------------------------------------------------------------

_FENCE: dict[str, SideEffectState] = {}
_FENCE_MAX = 512


def _fence_key(request: ComputerExecutionRequest) -> str:
    return str(request.idempotency_key or request.execution_id)


def check_fence(request: ComputerExecutionRequest) -> SideEffectState | None:
    """Prior fenced state for this execution identity, if any."""

    return _FENCE.get(_fence_key(request))


def mark_fence(request: ComputerExecutionRequest, state: SideEffectState) -> None:
    """Record the furthest-known side-effect state (monotonic upgrade)."""

    key = _fence_key(request)
    prior = _FENCE.get(key)
    if prior is None or state in MUTATION_RISK_STATES or prior not in MUTATION_RISK_STATES:
        _FENCE[key] = state
    if len(_FENCE) > _FENCE_MAX:
        oldest = next(iter(_FENCE))
        _FENCE.pop(oldest, None)


def fence_snapshot() -> dict[str, Any]:
    risky = sum(1 for state in _FENCE.values() if state in MUTATION_RISK_STATES)
    return {"entries": len(_FENCE), "mutation_risk_entries": risky}


def reset_fence() -> None:
    """Test/diagnostic reset. Never called on the production hot path."""

    _FENCE.clear()


def _note(event: str, request: ComputerExecutionRequest, result: ComputerExecutionResult) -> None:
    """Bounded executor diagnostics. No typed content, no secrets, no file text."""

    try:
        from app.ev.computer_runtime import log_computer

        log_computer(
            event,
            extra={
                "execution_id": request.execution_id,
                "family": request.primitive,
                "operation": request.operation,
                "capability": request.capability_name(),
                "risk": result.policy_risk,
                "attempted": result.executed,
                "verified": result.verified,
                "latency_ms": result.latency_ms,
                "error": result.error_code,
                "fallback": result.fallback_path,
            },
        )
    except Exception:  # noqa: BLE001 - diagnostics never break execution
        contextlib.suppress(Exception)


# ---------------------------------------------------------------------------
# Verification predicates (expected_effect)
# ---------------------------------------------------------------------------


def _verify_effect(
    expected: dict[str, Any] | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    """Generic expected-effect verification. Unknown effect types verify False."""

    if not expected:
        # No expectation declared → cannot claim semantic verification.
        return False
    effect = str(expected.get("type") or "").strip().lower()
    if effect == "app_foreground":
        want = str(expected.get("app") or "").lower()
        got = str((after or {}).get("app") or (after or {}).get("app_name") or "").lower()
        return bool(want and got == want)
    if effect == "app_closed":
        want = str(expected.get("app") or "").lower()
        got = str((after or {}).get("app") or "").lower()
        apps = [str(a).lower() for a in ((after or {}).get("apps") or [])]
        return bool(want) and got != want and want not in apps
    if effect == "url_open":
        got = str((after or {}).get("url") or "").lower()
        want = str(expected.get("url") or "").lower()
        return bool(want) and (not got or got == want)
    if effect == "state_changed":
        # Generic flip/change check over the observed field.
        field_name = str(expected.get("field") or "")
        if not field_name:
            return False
        return (before or {}).get(field_name) != (after or {}).get(field_name)
    if effect == "state_equals":
        field_name = str(expected.get("field") or "")
        return bool(field_name) and (after or {}).get(field_name) == expected.get("value")
    return False


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class ComputerExecutor:
    """Internal general computer execution substrate over existing transports."""

    def __init__(self, *, live: Any = None, session: Any = None, actor: str = "master"):
        self.live = live
        self.session = session
        self.actor = actor

    # -- transport ---------------------------------------------------------

    async def _transport(self, command: str, arguments: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
        if self.live is None or not hasattr(self.live, "request_computer"):
            return {
                "ok": False,
                "error": "computer_not_connected",
                "spoken": "I need the EV app live on this Mac for that.",
            }
        return await self.live.request_computer(command, arguments, timeout=timeout)

    async def _observe_state(self) -> dict[str, Any]:
        """Cheap structured observation (AX-first; no pixels)."""

        result = await self._transport("inspect_ui", {"query": "frontmost"}, timeout=6.0)
        if not result.get("ok"):
            return {"ok": False}
        app = result.get("app") or result.get("app_name")
        return {
            "ok": True,
            "app": app,
            "window": result.get("window_title"),
            "elements": len(result.get("elements") or []),
        }

    # -- policy ------------------------------------------------------------

    def _risk_for(self, request: ComputerExecutionRequest) -> str:
        from app.ev.policy import evaluate_policy

        capability = request.capability_name()
        decision = evaluate_policy(
            capability,
            actor=self.actor,
            channel="voice",
            provider_connected=self.live is not None,
        )
        risk = getattr(decision, "risk_class", None) or "R1"
        if request.primitive == "act":
            from app.ev.computer import classify_ui_risk

            # SEMANTIC EFFECT LAW: risk follows target semantics, not the
            # primitive. A press() is low-risk for "Play" and confirmation-
            # worthy for destructive labels.
            semantic = classify_ui_risk(request.args, request.risk_context.get("element"))
            if semantic == "high":
                risk = f"{risk}+semantic-high"
        return risk

    # -- families ----------------------------------------------------------

    async def _run_observe(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        result = ComputerExecutionResult(execution_id=request.execution_id, family="observe", operation=request.operation)
        started = time.monotonic()
        raw = await self._transport(request.operation, dict(request.args))
        result.raw = raw
        result.ok = bool(raw.get("ok"))
        result.executed = result.ok
        # Observe is read-only: no mutation state beyond dispatch bookkeeping.
        result.side_effect = SideEffectState.NOT_ATTEMPTED
        result.verified = result.ok and not raw.get("error")
        if result.ok:
            result.error_code = None
        else:
            error = str(raw.get("error") or "observe_failed")
            result.error_code = error
            result.side_effect = (
                SideEffectState.DISPATCH_NOT_STARTED
                if error == "computer_not_connected"
                else SideEffectState.PRECONDITION_FAILED
            )
        result.retryable = result.error_code in {"timeout", "computer_bridge_failed"}
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def _run_act(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        result = ComputerExecutionResult(execution_id=request.execution_id, family="act", operation="ui_action")
        started = time.monotonic()
        # EPHEMERAL UI REFERENCE LAW (§23): an element ref must be bound to the
        # current snapshot generation. Unknown/stale refs FAIL CLOSED — re-observe,
        # never guess a replacement target.
        element_ref = str((request.args or {}).get("element_ref") or "").strip()
        if element_ref:
            from app.ev.computer_runtime import state_for, validate_element_ref

            state = state_for(getattr(self.live, "session_id", None))
            if validate_element_ref(state, element_ref) is None:
                result.raw = {"ok": False, "error": "stale_element"}
                result.error_code = "stale_element"
                result.executed = False
                result.verified = False
                result.side_effect = SideEffectState.PRECONDITION_FAILED
                result.retryable = True
                result.latency_ms = round((time.monotonic() - started) * 1000, 2)
                mark_fence(request, result.side_effect)
                _note("computer.executor_block", request, result)
                return result
        mutating = is_mutating("ui_action", request.args)
        if mutating:
            result.observation_before = await self._observe_state()
        # FENCE: from here the primitive may reach the UI — mark before await.
        if mutating:
            mark_fence(request, SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN)
            result.side_effect = SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN
        raw = await self._transport("ui_action", dict(request.args))
        result.raw = raw
        result.ok = bool(raw.get("ok"))
        # EXECUTED ≠ VERIFIED (§27): an AX press that returns success is only
        # the primitive dispatch; the claim gate is the after-observation.
        error_code = str(raw.get("error") or "") if not raw.get("ok") else None
        result.executed = bool(raw.get("ok")) or error_code in {"stale_element", "element_not_found"}
        result.error_code = error_code
        if raw.get("ok") and mutating:
            # Fallback is now forbidden regardless of what verification finds.
            result.side_effect = SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN
            try:
                result.observation_after = await self._observe_state()
            except Exception:  # noqa: BLE001 - observation loss = ambiguity
                result.observation_after = None
            result.verified = _verify_effect(request.expected_effect, result.observation_before, result.observation_after)
            if result.verified:
                result.side_effect = SideEffectState.VERIFIED
            elif result.observation_after and result.observation_after.get("ok") is False:
                # Verification itself could not be performed → ambiguity (§3).
                result.side_effect = SideEffectState.AMBIGUOUS_AFTER_ATTEMPT
                result.error_code = "ambiguous_effect"
            else:
                result.side_effect = SideEffectState.EFFECT_OBSERVED
                result.error_code = "verification_failed"
            result.evidence = {
                "before": result.observation_before,
                "after": result.observation_after,
                "effect": request.expected_effect,
            }
        elif raw.get("ok"):
            result.side_effect = SideEffectState.NOT_ATTEMPTED  # read-style act
            result.verified = True
        else:
            result.verified = False
            result.evidence = {"error": result.error_code}
            if error_code in PRE_DISPATCH_REFUSALS:
                result.side_effect = SideEffectState.PRECONDITION_FAILED
            elif error_code == "computer_not_connected":
                result.side_effect = SideEffectState.DISPATCH_NOT_STARTED
            elif mutating:
                # Sent but refused/unknown at the client: never assume safety.
                result.side_effect = SideEffectState.AMBIGUOUS_AFTER_ATTEMPT
                result.error_code = result.error_code or "ambiguous_effect"
            else:
                result.side_effect = SideEffectState.PRECONDITION_FAILED
        if mutating:
            mark_fence(request, result.side_effect)
        result.retryable = result.error_code in {"timeout", "computer_bridge_failed", "stale_element"}
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def _run_navigate(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        result = ComputerExecutionResult(execution_id=request.execution_id, family="navigate", operation=request.operation)
        started = time.monotonic()
        before = await self._observe_state() if request.operation != "open_url" else None
        # FENCE: navigation mutations may reach the Mac — mark before await.
        mark_fence(request, SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN)
        result.side_effect = SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN
        raw = await self._transport(request.operation, dict(request.args))
        result.raw = raw
        result.ok = bool(raw.get("ok"))
        result.executed = result.ok
        error_code = str(raw.get("error") or "") if not result.ok else None
        result.error_code = error_code
        if result.ok:
            try:
                after = await self._observe_state() if request.operation != "open_url" else {"url": request.args.get("url")}
            except Exception:  # noqa: BLE001 - observation loss = ambiguity
                after = None
            result.observation_before = before
            result.observation_after = after
            default_effect: dict[str, Any] | None
            if request.operation in {"open_app", "activate_app"}:
                default_effect = {"type": "app_foreground", "app": request.target or request.args.get("name") or request.args.get("app")}
            elif request.operation == "close_app":
                default_effect = {"type": "app_closed", "app": request.target or request.args.get("name") or request.args.get("app")}
            else:
                default_effect = {"type": "url_open", "url": request.args.get("url")}
            effect = request.expected_effect or default_effect
            if after is None:
                result.verified = False
                result.side_effect = SideEffectState.AMBIGUOUS_AFTER_ATTEMPT
                result.error_code = "ambiguous_effect"
            else:
                result.verified = _verify_effect(effect, before, after)
                result.side_effect = SideEffectState.VERIFIED if result.verified else SideEffectState.EFFECT_OBSERVED
                if not result.verified:
                    result.error_code = "verification_failed"
            result.evidence = {"before": before, "after": after, "effect": effect}
        else:
            result.verified = False
            result.evidence = {"error": error_code}
            if error_code in PRE_DISPATCH_REFUSALS:
                result.side_effect = SideEffectState.PRECONDITION_FAILED
            elif error_code == "computer_not_connected":
                result.side_effect = SideEffectState.DISPATCH_NOT_STARTED
            else:
                result.side_effect = SideEffectState.AMBIGUOUS_AFTER_ATTEMPT
                result.error_code = result.error_code or "ambiguous_effect"
        mark_fence(request, result.side_effect)
        result.retryable = result.error_code in {"timeout", "computer_bridge_failed"}
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def _run_fs(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        from app.tools import sandbox

        result = ComputerExecutionResult(execution_id=request.execution_id, family="fs", operation=request.operation)
        started = time.monotonic()
        path = str(request.args.get("path") or "")
        try:
            if request.operation == "read":
                raw = sandbox.read_file(path)
                raw = {**raw, "ok": True}
            elif request.operation == "write":
                raw = {**sandbox.write_file(path, str(request.args.get("content") or "")), "ok": True}
            else:
                # Destructive/other fs ops have no jail policy yet: fail closed.
                raw = {
                    "ok": False,
                    "error": "fs_operation_unavailable",
                    "spoken": "That filesystem operation is not available yet.",
                }
        except sandbox.SandboxError as exc:
            raw = {"ok": False, "error": "sandbox_denied", "detail": str(exc)[:80]}
        result.raw = raw
        result.ok = bool(raw.get("ok"))
        result.executed = result.ok
        result.error_code = None if result.ok else str(raw.get("error") or "fs_failed")
        result.verified = result.ok
        if request.operation == "write":
            # FENCE: a write may have reached the jail before any failure surfaced.
            mark_fence(request, SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN)
            result.side_effect = SideEffectState.ATTEMPTED_NO_EFFECT_KNOWN
            if result.ok:
                # Verify by re-reading inside the jail.
                check = sandbox.read_file(path)
                result.verified = isinstance(check, dict) and "content" in check
                result.side_effect = SideEffectState.VERIFIED if result.verified else SideEffectState.AMBIGUOUS_AFTER_ATTEMPT
                result.evidence = {"recheck": result.verified}
            else:
                result.side_effect = SideEffectState.PRECONDITION_FAILED
        else:
            result.side_effect = SideEffectState.NOT_ATTEMPTED
        result.retryable = result.error_code in {"timeout"}
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def _run_exec(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        from app.tools.operations import resolve_operation
        from app.tools.sandbox import SandboxError, run_command

        result = ComputerExecutionResult(execution_id=request.execution_id, family="exec", operation=request.operation)
        started = time.monotonic()
        # EXEC ALLOWLIST LAW: only named, structured operations. Raw strings
        # and shell-shaped arguments are rejected before anything runs.
        operation = resolve_operation(str(request.args.get("operation") or ""))
        if operation is None or request.args.get("command"):
            result.error_code = "operation_not_allowlisted"
            result.raw = {"ok": False, "error": result.error_code}
            _note("computer.executor_block", request, result)
            return result
        try:
            raw = dict(run_command(operation.command, timeout_seconds=30))
            raw.setdefault("ok", raw.get("exit_code") == 0)
            result.raw = raw
            result.ok = bool(raw.get("ok"))
            mark_fence(request, SideEffectState.EFFECT_OBSERVED)
            result.side_effect = SideEffectState.EFFECT_OBSERVED
        except SandboxError as exc:
            result.raw = {"ok": False, "error": "sandbox_denied", "detail": str(exc)[:80]}
            result.side_effect = SideEffectState.PRECONDITION_FAILED
        result.executed = result.ok
        result.error_code = None if result.ok else str(result.raw.get("error") or "exec_failed")
        result.verified = result.ok  # structured op output IS the evidence
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    # -- entry -------------------------------------------------------------

    async def execute(self, request: ComputerExecutionRequest) -> ComputerExecutionResult:
        if not request.execution_id:
            request.execution_id = _new_execution_id()
        # FENCE (§4): a retried execution identity that may already have mutated
        # the world is refused — no automatic second mutation, ever.
        prior = check_fence(request)
        if prior in MUTATION_RISK_STATES:
            result = ComputerExecutionResult(
                execution_id=request.execution_id,
                ok=False,
                executed=False,
                verified=False,
                side_effect=prior,
                error_code="fence_blocked_mutation_retry",
                family=request.primitive,
                operation=request.operation,
                evidence={"prior_state": prior.value},
            )
            _note("computer.executor_fence_block", request, result)
            return result
        runners = {
            "observe": self._run_observe,
            "act": self._run_act,
            "navigate": self._run_navigate,
            "fs": self._run_fs,
            "exec": self._run_exec,
        }
        runner = runners.get(request.primitive)
        if runner is None:
            return ComputerExecutionResult(
                execution_id=request.execution_id,
                error_code="unknown_primitive",
                family=request.primitive,
                operation=request.operation,
            )
        try:
            result = await runner(request)
        except Exception as exc:  # noqa: BLE001 - executor failures fall back
            result = ComputerExecutionResult(
                execution_id=request.execution_id,
                family=request.primitive,
                operation=request.operation,
                error_code=f"executor_error:{type(exc).__name__}",
            )
        try:
            result.policy_risk = self._risk_for(request)
        except Exception:  # noqa: BLE001 - risk labeling is advisory
            result.policy_risk = None
        _note("computer.executor_executed", request, result)
        return result


# ---------------------------------------------------------------------------
# Adapter for existing computer tools (flag: off | shadow | on)
# ---------------------------------------------------------------------------


async def execute_tool_via_executor(
    name: str,
    arguments: dict[str, Any],
    *,
    live: Any,
    actor: str = "master",
    expected_effect: dict[str, Any] | None = None,
) -> ComputerExecutionResult | None:
    """Route one existing tool call through the executor (F2 'on' path).

    Returns None when the family cannot serve the tool, so the adapter falls
    back to the legacy path (recorded in diagnostics).
    """

    family = family_for_tool(name)
    if family is None:
        return None
    executor = ComputerExecutor(live=live, actor=actor)
    request = ComputerExecutionRequest(
        primitive=family,
        operation=name,
        args=dict(arguments or {}),
        expected_effect=expected_effect,
    )
    result = await executor.execute(request)
    result.fallback_path = None
    return result


async def shadow_validate_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    live: Any,
    actor: str = "master",
) -> dict[str, Any] | None:
    """F2 'shadow' path: plan + validate + risk, WITHOUT executing mutations.

    Read-only observe tools may dual-run for parity comparison. Mutations are
    NEVER executed here (§29: no double click/type).
    """

    family = family_for_tool(name)
    if family is None:
        return None
    plan = ComputerExecutionRequest(
        primitive=family,
        operation=name,
        args=dict(arguments or {}),
    )
    plan.execution_id = _new_execution_id()
    executor = ComputerExecutor(live=live, actor=actor)
    risk = None
    with contextlib.suppress(Exception):
        risk = executor._risk_for(plan)
    mutating = is_mutating(name, arguments)
    observation = None
    if not mutating and live is not None and name in OBSERVE_TOOLS:
        # Dual-run allowed for read-only observation only.
        result = await executor.execute(plan)
        observation = {"ok": result.ok, "verified": result.verified, "latency_ms": result.latency_ms}
    record = {
        "execution_id": plan.execution_id,
        "family": family,
        "operation": name,
        "mutating": mutating,
        "risk": risk,
        "expected_effect": plan.expected_effect,
        "executed": False if mutating else None,
        "dual_run": observation,
    }
    _note("computer.executor_shadow", plan, ComputerExecutionResult(execution_id=plan.execution_id, family=family, operation=name))
    return record
