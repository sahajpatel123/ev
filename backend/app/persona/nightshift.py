"""Night Shift (spec §8): conversation and plan only.

Execution belongs to the agent lane (handoff: run_night_shift). This module
detects fatigue, decides nudges, builds validated plans, and shapes the
morning brief. The forbidden list is a module constant no config can change;
any plan naming a forbidden capability fails validation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.persona.context import PersonaContext
from app.persona.settings import (
    checkpoint_every_min as default_checkpoint_min,
)
from app.persona.settings import (
    max_nudges,
    night_end,
    night_scope,
    night_start,
    nudge_gap_min,
)

# Fixed overnight allow-list (spec §8).
ALLOWED = frozenset(
    {
        "read",
        "edit_in_workspace",
        "run_tests",
        "local_build",
        "research",
        "write_docs",
        "stage_pr",
    }
)

# Fixed overnight deny-list. A constant: no config, caller, or plan field
# can weaken it — validate() enforces the superset.
FORBIDDEN = frozenset(
    {
        "push_to_main",
        "merge",
        "deploy",
        "send",
        "delete_outside_workspace",
        "spend_money",
        "use_credentials",
        "first_contact",
    }
)

STOP_CONDITIONS = (
    "goal_met",
    "time_budget",
    "wake_minus_30_min",
    "three_consecutive_failures",
)

_WORKSPACE_RE = re.compile(r"^evie/night-\d{8}$")
_TIRED_RE = re.compile(r"\btired\b|\bone more\b|\blast thing\b", re.IGNORECASE)
_REPEAT_LETTER_RE = re.compile(r"(\w)\1\1")
_WORD_RE = re.compile(r"[a-z']+")

# Common typos (lowercase). A small, high-precision set by design.
_KNOWN_TYPOS = frozenset(
    {
        "teh", "adn", "hte", "taht", "waht", "wiht", "nad", "thier",
        "recieve", "seperate", "occured", "begining", "writting",
        "runing", "mkae", "amke", "becuase", "jsut", "tahts", "thsi",
    }
)


@dataclass(frozen=True)
class NightState:
    """Per-thread, per-night nudge state (persisted by the thread owner)."""

    date: str = ""
    nudges: int = 0
    last_nudge_ts: datetime | None = None
    declined: bool = False
    last_signals: tuple[str, ...] = ()


def _typo_count(text: str) -> int:
    words = _WORD_RE.findall((text or "").lower())
    count = sum(1 for word in words if word in _KNOWN_TYPOS)
    count += sum(1 for word in words if _REPEAT_LETTER_RE.search(word))
    return count


def _failure_signature(receipt: Any) -> str | None:
    if isinstance(receipt, Mapping):
        if receipt.get("ok") is not False:
            return None
        error = receipt.get("error") or receipt.get("name") or "tool"
        return " ".join(str(error).lower().split())[:60]
    if getattr(receipt, "ok", None) is not False:
        return None
    error = getattr(receipt, "error", None) or getattr(receipt, "name", None) or "tool"
    return " ".join(str(error).lower().split())[:60]


def fatigue_signals(
    recent_owner_msgs: list[str] | tuple[str, ...],
    receipts: tuple[Any, ...] | list[Any],
) -> list[str]:
    """Deterministic fatigue detection. Returns signal ids (possibly empty)."""
    signals: list[str] = []
    msgs = [msg or "" for msg in (recent_owner_msgs or [])]
    if any(_TIRED_RE.search(msg) for msg in msgs):
        signals.append("tired_phrase")
    signatures: dict[str, int] = {}
    for receipt in receipts or ():
        signature = _failure_signature(receipt)
        if signature:
            signatures[signature] = signatures.get(signature, 0) + 1
    if any(count >= 3 for count in signatures.values()):
        signals.append("repeated_failure")
    if len(msgs) >= 2:
        half = len(msgs) // 2
        first, second = msgs[:half], msgs[half:]
        count_first = sum(_typo_count(msg) for msg in first)
        count_second = sum(_typo_count(msg) for msg in second)
        rate_first = count_first / max(1, len(first))
        rate_second = count_second / max(1, len(second))
        if rate_second > rate_first and count_second >= 2:
            signals.append("typo_rate_up")
    return signals


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def _in_night_window(now: datetime) -> bool:
    stamp = _hhmm(now)
    return night_start <= stamp < night_end


def _session_minutes(ctx: PersonaContext) -> float | None:
    if not isinstance(ctx.local_time, datetime):
        return None
    if not isinstance(ctx.session_started, datetime):
        return None
    return max(0.0, (ctx.local_time - ctx.session_started).total_seconds() / 60.0)


def should_nudge(
    ctx: PersonaContext,
    night_state: NightState | None,
    *,
    recent_owner_msgs: list[str] | tuple[str, ...] = (),
) -> bool:
    """Whether Evie may nudge about Night Shift this turn (spec §8)."""
    state = night_state or NightState()
    if not isinstance(ctx.local_time, datetime):
        return False
    if not _in_night_window(ctx.local_time):
        return False
    if state.nudges >= max_nudges:
        return False
    signals = fatigue_signals(recent_owner_msgs, ctx.receipts)
    new_signals = [signal for signal in signals if signal not in state.last_signals]
    if state.declined and not new_signals:
        return False
    if state.last_nudge_ts is not None:
        gap = (ctx.local_time - state.last_nudge_ts).total_seconds() / 60.0
        if gap < nudge_gap_min:
            return False
    if state.nudges >= 1 and not new_signals:
        return False
    minutes = _session_minutes(ctx)
    long_session = minutes is not None and minutes >= 120
    return bool(long_session or signals)


def nudge_recorded(
    state: NightState | None,
    now: datetime,
    signals: list[str] | tuple[str, ...],
    *,
    declined: bool = False,
) -> NightState:
    """Return the next NightState after a nudge (helper for the owner)."""
    state = state or NightState()
    return NightState(
        date=now.strftime("%Y-%m-%d"),
        nudges=state.nudges + 1,
        last_nudge_ts=now,
        declined=bool(declined or state.declined),
        last_signals=tuple(signals),
    )


@dataclass(frozen=True)
class NightShiftPlan:
    """Serializable overnight plan. Execution lives in the agent lane."""

    goal: str
    task_ref: str
    workspace: str
    allowed: tuple[str, ...] = tuple(sorted(ALLOWED))
    forbidden: tuple[str, ...] = tuple(sorted(FORBIDDEN))
    scope: str = night_scope
    new_features: bool = False
    stop_conditions: tuple[str, ...] = STOP_CONDITIONS
    checkpoint_every_min: int = default_checkpoint_min
    confirmation_queue: tuple[dict[str, Any], ...] = ()
    brief_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "task_ref": self.task_ref,
            "workspace": self.workspace,
            "allowed": list(self.allowed),
            "forbidden": list(self.forbidden),
            "scope": self.scope,
            "new_features": self.new_features,
            "stop_conditions": list(self.stop_conditions),
            "checkpoint_every_min": self.checkpoint_every_min,
            "confirmation_queue": list(self.confirmation_queue),
            "brief_at": self.brief_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> NightShiftPlan:
        return cls(
            goal=str(raw.get("goal", "")),
            task_ref=str(raw.get("task_ref", "")),
            workspace=str(raw.get("workspace", "")),
            allowed=tuple(str(item) for item in raw.get("allowed", [])),
            forbidden=tuple(str(item) for item in raw.get("forbidden", [])),
            scope=str(raw.get("scope", night_scope)),
            new_features=bool(raw.get("new_features", False)),
            stop_conditions=tuple(str(item) for item in raw.get("stop_conditions", [])),
            checkpoint_every_min=int(raw.get("checkpoint_every_min", 0)),
            confirmation_queue=tuple(
                dict(item) for item in raw.get("confirmation_queue", [])
            ),
            brief_at=raw.get("brief_at"),
        )


def validate(plan: NightShiftPlan) -> tuple[str, ...]:
    """Validate a plan. Empty tuple means valid; otherwise error strings."""
    errors: list[str] = []
    if not plan.goal.strip():
        errors.append("goal is empty")
    if not plan.task_ref.strip():
        errors.append("task_ref is empty")
    if not _WORKSPACE_RE.match(plan.workspace or ""):
        errors.append(f"workspace must match evie/night-YYYYMMDD, got {plan.workspace!r}")
    allowed = set(plan.allowed or ())
    forbidden_named = allowed & FORBIDDEN
    if forbidden_named:
        errors.append(f"allowed names forbidden capabilities: {sorted(forbidden_named)}")
    unknown = allowed - ALLOWED - FORBIDDEN
    if unknown:
        errors.append(f"allowed names unknown capabilities: {sorted(unknown)}")
    if not FORBIDDEN.issubset(set(plan.forbidden or ())):
        errors.append("forbidden list weakened: must contain every fixed capability")
    if plan.scope != night_scope:
        errors.append(f"scope {plan.scope!r} exceeds {night_scope!r}")
    if plan.new_features:
        errors.append("new features are outside the default night scope")
    if set(plan.stop_conditions or ()) != set(STOP_CONDITIONS):
        errors.append("stop_conditions must be exactly the four fixed conditions")
    if int(plan.checkpoint_every_min or 0) < 5:
        errors.append("checkpoint_every_min must be at least 5")
    return tuple(errors)


def takeover_allowed(*, allow_execution: bool = False) -> bool:
    """Whether Evie may offer to take over work (honesty gate, §3.6).

    False until the agent lane can actually execute nights. The lab passes
    allow_execution=True to run the full flow there only.
    """
    from app.persona.settings import NIGHT_SHIFT_EXECUTION_AVAILABLE

    return bool(allow_execution or NIGHT_SHIFT_EXECUTION_AVAILABLE)


def plan_from_context(
    ctx: PersonaContext, *, allow_execution: bool = False
) -> NightShiftPlan | None:
    """Build a plan from the current task and work state.

    None when the task is unclear (Evie asks one question instead of
    guessing) or when the honesty gate is closed (she must not offer a
    capability that doesn't exist).
    """
    if not takeover_allowed(allow_execution=allow_execution):
        return None
    work = ctx.work_state
    task = (work.task if work is not None else None) or ""
    task = " ".join(task.split())
    if not task:
        return None
    moment = ctx.local_time if isinstance(ctx.local_time, datetime) else None
    if moment is None and isinstance(ctx.session_started, datetime):
        moment = ctx.session_started
    stamp = moment.strftime("%Y%m%d") if moment is not None else "00000000"
    return NightShiftPlan(
        goal=task,
        task_ref=task,
        workspace=f"evie/night-{stamp}",
    )


def render_offer(plan: NightShiftPlan) -> dict[str, Any]:
    """Structured slots for the takeover offer (the model writes the words)."""
    return {
        "task": plan.task_ref,
        "workspace": plan.workspace,
        "brief_at": plan.brief_at,
        "wont": sorted(plan.forbidden),
    }


def render_plan_summary(plan: NightShiftPlan) -> dict[str, Any]:
    """Structured slots for the accepted-plan summary (facts, not wording)."""
    return {
        "goal": plan.goal,
        "workspace": plan.workspace,
        "allowed": sorted(plan.allowed),
        "wont": sorted(plan.forbidden),
        "stop_conditions": list(plan.stop_conditions),
        "scope": plan.scope,
    }


@dataclass(frozen=True)
class DoneItem:
    """One completed overnight item. A receipt reference is mandatory."""

    summary: str
    ref: str

    def __post_init__(self) -> None:
        if not (self.ref or "").strip():
            raise ValueError("DoneItem requires a receipt reference")


@dataclass(frozen=True)
class MorningBrief:
    """The morning brief shape (spec §8). done[] items enforce receipts."""

    done: tuple[DoneItem, ...] = ()
    tests: str | None = None
    waiting_for_you: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    night_line_slot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": [
                {"summary": item.summary, "ref": item.ref} for item in self.done
            ],
            "tests": self.tests,
            "waiting_for_you": list(self.waiting_for_you),
            "next_steps": list(self.next_steps),
            "night_line_slot": self.night_line_slot,
        }
