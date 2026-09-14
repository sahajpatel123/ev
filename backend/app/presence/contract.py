"""Presence OS V1 — durable intent contract (pure, no DB, no model).

ONE Evie owns intent across three bodies (Home Station, Pocket, Satellite).
A GoalContract is orchestration over canonical Core truth (Project/Goal rows),
Memory recall, and the device plane — never a second store.

- GoalState: durable lifecycle incl. WAITING_* / PARKED / STALLED.
- CompletionConfidence: VERIFIED vs PARTIAL vs BLOCKED/FAILED/UNKNOWN.
- InterruptionPolicy / AutonomyPolicy: owner-stated bounds; policy code wins.
- NodeKind / NodeTarget: bounded execution graph vocabulary.
- ConditionClass: durable wait conditions (event-driven, never model loops).
- AttentionVerdict: SILENT → ACTIVE_CONVERSATION_INTERRUPT.
- ShortCommand: contextual one-word intents resolved against SituationModel.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


class GoalState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    WAITING_FOR_CONDITION = "WAITING_FOR_CONDITION"
    WAITING_FOR_DEVICE = "WAITING_FOR_DEVICE"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PARKED = "PARKED"
    STALLED = "STALLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = frozenset(
    {GoalState.COMPLETED, GoalState.FAILED, GoalState.CANCELLED, GoalState.EXPIRED}
)

WAITING_STATES = frozenset(
    {
        GoalState.WAITING,
        GoalState.WAITING_FOR_CONDITION,
        GoalState.WAITING_FOR_DEVICE,
        GoalState.WAITING_FOR_APPROVAL,
        GoalState.STALLED,
    }
)

# Allowed edges. Terminal states have no outgoing edges.
TRANSITIONS: dict[GoalState, frozenset[GoalState]] = {
    GoalState.DRAFT: frozenset({GoalState.ACTIVE, GoalState.CANCELLED, GoalState.EXPIRED}),
    GoalState.ACTIVE: frozenset(
        {
            GoalState.WAITING,
            GoalState.WAITING_FOR_CONDITION,
            GoalState.WAITING_FOR_DEVICE,
            GoalState.WAITING_FOR_APPROVAL,
            GoalState.PARKED,
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
        }
    ),
    GoalState.WAITING: frozenset(
        {
            GoalState.ACTIVE,
            GoalState.WAITING_FOR_CONDITION,
            GoalState.WAITING_FOR_DEVICE,
            GoalState.WAITING_FOR_APPROVAL,
            GoalState.PARKED,
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
            GoalState.STALLED,
        }
    ),
    GoalState.WAITING_FOR_CONDITION: frozenset(
        {
            GoalState.ACTIVE,
            GoalState.WAITING,
            GoalState.PARKED,
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
            GoalState.STALLED,
        }
    ),
    GoalState.WAITING_FOR_DEVICE: frozenset(
        {
            GoalState.ACTIVE,
            GoalState.WAITING,
            GoalState.PARKED,
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
            GoalState.STALLED,
        }
    ),
    GoalState.WAITING_FOR_APPROVAL: frozenset(
        {
            GoalState.ACTIVE,
            GoalState.WAITING,
            GoalState.PARKED,
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
        }
    ),
    GoalState.PARKED: frozenset({GoalState.ACTIVE, GoalState.CANCELLED, GoalState.EXPIRED}),
    GoalState.STALLED: frozenset(
        {
            GoalState.ACTIVE,
            GoalState.WAITING,
            GoalState.PARKED,
            GoalState.FAILED,
            GoalState.CANCELLED,
            GoalState.EXPIRED,
        }
    ),
}


def can_transition(frm: str, to: str) -> bool:
    try:
        return GoalState(to) in TRANSITIONS.get(GoalState(frm), frozenset())
    except ValueError:
        return False


class CompletionConfidence(StrEnum):
    COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
    COMPLETED_PARTIAL = "COMPLETED_PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class InterruptionPolicy(StrEnum):
    NORMAL = "NORMAL"
    MINIMAL = "MINIMAL"
    ONLY_IF_BLOCKED = "ONLY_IF_BLOCKED"
    URGENT = "URGENT"
    SILENT_UNTIL_COMPLETE = "SILENT_UNTIL_COMPLETE"


class AutonomyPolicy(StrEnum):
    READ_ONLY = "READ_ONLY"
    SAFE_DIGITAL = "SAFE_DIGITAL"
    CONFIRM_EXTERNAL_WRITE = "CONFIRM_EXTERNAL_WRITE"
    CONFIRM_R3_R4 = "CONFIRM_R3_R4"
    CUSTOM = "CUSTOM"


class NodeKind(StrEnum):
    CORE_READ = "CORE_READ"
    CORE_WRITE = "CORE_WRITE"
    SPARK_REASONING = "SPARK_REASONING"
    RESEARCH = "RESEARCH"
    CLOUD_JOB = "CLOUD_JOB"
    HOME_STATION_ACTION = "HOME_STATION_ACTION"
    PHONE_LOCAL_ACTION = "PHONE_LOCAL_ACTION"
    WAIT_CONDITION = "WAIT_CONDITION"
    WAIT_DEVICE = "WAIT_DEVICE"
    WAIT_APPROVAL = "WAIT_APPROVAL"
    VERIFY = "VERIFY"
    NOTIFY = "NOTIFY"
    HANDOFF = "HANDOFF"
    ARTIFACT_OPERATION = "ARTIFACT_OPERATION"
    EMAIL_READ = "EMAIL_READ"
    EMAIL_SEND = "EMAIL_SEND"
    WHATSAPP_READ = "WHATSAPP_READ"
    WHATSAPP_SEND = "WHATSAPP_SEND"
    CONTACT_RESOLVE = "CONTACT_RESOLVE"
    CALENDAR_CREATE = "CALENDAR_CREATE"
    BROWSER_ACTION = "BROWSER_ACTION"
    ARTIFACT_DOWNLOAD = "ARTIFACT_DOWNLOAD"
    COMMUNICATION_WAIT = "COMMUNICATION_WAIT"


class NodeTarget(StrEnum):
    CORE = "CORE"
    CLOUD = "CLOUD"
    HOME_STATION = "HOME_STATION"
    POCKET_NODE = "POCKET_NODE"
    SATELLITE_NODE = "SATELLITE_NODE"


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ConditionClass(StrEnum):
    TIME = "TIME"
    DEVICE_ONLINE = "DEVICE_ONLINE"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    TASK_COMPLETED = "TASK_COMPLETED"
    FILE_APPEARED = "FILE_APPEARED"
    FILE_CHANGED = "FILE_CHANGED"
    RESEARCH_RESULT = "RESEARCH_RESULT"
    EXTERNAL_DATA_CHANGE = "EXTERNAL_DATA_CHANGE"
    LOCATION_REGION = "LOCATION_REGION"
    OWNER_STATE = "OWNER_STATE"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    CUSTOM_PREDICATE = "CUSTOM_PREDICATE"
    EMAIL_RECEIVED_MATCH = "EMAIL_RECEIVED_MATCH"
    WHATSAPP_REPLY_MATCH = "WHATSAPP_REPLY_MATCH"
    DOCUMENT_RECEIVED = "DOCUMENT_RECEIVED"
    PERSON_RESPONDED = "PERSON_RESPONDED"
    DEADLINE_APPROACHING = "DEADLINE_APPROACHING"
    CALENDAR_EVENT_CHANGED = "CALENDAR_EVENT_CHANGED"


class ConditionState(StrEnum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AttentionVerdict(StrEnum):
    SILENT = "SILENT"
    INBOX_ONLY = "INBOX_ONLY"
    DIGEST = "DIGEST"
    NORMAL_PUSH = "NORMAL_PUSH"
    URGENT_PUSH = "URGENT_PUSH"
    ACTIVE_CONVERSATION_INTERRUPT = "ACTIVE_CONVERSATION_INTERRUPT"


class ShortCommand(StrEnum):
    CONTINUE = "CONTINUE"
    STOP = "STOP"
    PARK = "PARK"
    RESUME = "RESUME"
    MOVE_TO_MAC = "MOVE_TO_MAC"
    BRING_HERE = "BRING_HERE"
    FINISH = "FINISH"
    DO_IT = "DO_IT"
    WHAT_CHANGED = "WHAT_CHANGED"
    WHY_WAITING = "WHY_WAITING"
    WHAT_NEEDS_ME = "WHAT_NEEDS_ME"
    UNKNOWN = "UNKNOWN"


# Durable event vocabulary. `goal.*` prefix keeps every transition inside the
# everywhere-sync visible set; no new emission path needed.
GOAL_EVENTS = (
    "goal.contract_created",
    "goal.activated",
    "goal.waiting",
    "goal.condition_satisfied",
    "goal.resumed",
    "goal.approval_required",
    "goal.task_teleported",
    "goal.checkpoint",
    "goal.completed",
    "goal.cancelled",
    "goal.parked",
    "goal.expired",
    "goal.failed",
    "goal.stalled",
)


_SHORT_PATTERNS: tuple[tuple[ShortCommand, re.Pattern[str]], ...] = (
    (ShortCommand.STOP, re.compile(r"^\s*stop\.?\s*$", re.I)),
    (ShortCommand.PARK, re.compile(r"\bpark (this|it|that)\b", re.I)),
    (
        ShortCommand.RESUME,
        re.compile(r"\b(resume|continue (what we were|the task|where we))\b", re.I),
    ),
    (
        ShortCommand.MOVE_TO_MAC,
        re.compile(r"\b(move|continue) (this|that|it) (to|on) (my |the )?mac\b", re.I),
    ),
    (
        ShortCommand.BRING_HERE,
        re.compile(r"\bbring (this|that|it|me the result)( here| to my phone| back)?\b", re.I),
    ),
    (
        ShortCommand.WHAT_CHANGED,
        re.compile(r"\bwhat (has |')?changed\b", re.I),
    ),
    (
        ShortCommand.WHY_WAITING,
        re.compile(r"\bwhy (are we|am i) waiting\b", re.I),
    ),
    (
        ShortCommand.WHAT_NEEDS_ME,
        re.compile(r"\bwhat (needs me|do you need from me)\b", re.I),
    ),
    (
        ShortCommand.FINISH,
        re.compile(r"\bfinish this\b", re.I),
    ),
    (
        ShortCommand.DO_IT,
        re.compile(r"^\s*do it\.?\s*$", re.I),
    ),
    (
        ShortCommand.CONTINUE,
        re.compile(r"^\s*continue\.?\s*$", re.I),
    ),
)


def classify_short_command(text: str) -> ShortCommand:
    raw = (text or "").strip()
    for command, pattern in _SHORT_PATTERNS:
        if pattern.search(raw):
            return command
    return ShortCommand.UNKNOWN


_ONLY_IF_BLOCKED_RE = re.compile(
    r"\b(don't bother me|only if (you (actually )?need|blocked)|tell me only if)\b", re.I
)
_SILENT_RE = re.compile(r"\b(don't interrupt|silent until (complete|done))\b", re.I)
_URGENT_RE = re.compile(r"\b(urgent|asap|right away|immediately)\b", re.I)


def interruption_from_text(text: str) -> InterruptionPolicy:
    raw = text or ""
    if _SILENT_RE.search(raw):
        return InterruptionPolicy.SILENT_UNTIL_COMPLETE
    if _ONLY_IF_BLOCKED_RE.search(raw):
        return InterruptionPolicy.ONLY_IF_BLOCKED
    if _URGENT_RE.search(raw):
        return InterruptionPolicy.URGENT
    return InterruptionPolicy.NORMAL


_READ_ONLY_RE = re.compile(r"\b(don't (change|touch|do) anything|read.only|just (look|watch))\b", re.I)
_NO_SEND_RE = re.compile(
    r"\b(don't send anything|do not send anything|without (asking|me))\b", re.I
)


def autonomy_from_text(text: str) -> AutonomyPolicy:
    raw = text or ""
    if _READ_ONLY_RE.search(raw):
        return AutonomyPolicy.READ_ONLY
    if _NO_SEND_RE.search(raw):
        return AutonomyPolicy.CONFIRM_EXTERNAL_WRITE
    return AutonomyPolicy.SAFE_DIGITAL


def owner_line_for(state: GoalState, *, title: str = "") -> str:
    name = f" “{title}”" if title else ""
    return {
        GoalState.WAITING_FOR_DEVICE: (
            f"I've queued{name} for your Mac. It will resume when the Mac is back — nothing is lost."
        ),
        GoalState.WAITING_FOR_CONDITION: (
            f"I'm watching for the right moment{name}. I'll continue on my own when it arrives."
        ),
        GoalState.WAITING_FOR_APPROVAL: (
            f"{name or 'That'} needs your approval before I continue. I've sent it to your phone."
        ),
        GoalState.PARKED: f"Parked{name}. Nothing will run until you say resume.",
        GoalState.COMPLETED: "Done — verified.",
        GoalState.FAILED: f"I couldn't complete{name}. Here's exactly where it stopped.",
        GoalState.CANCELLED: f"Cancelled{name}.",
        GoalState.EXPIRED: f"{name or 'That'} expired before it could finish.",
        GoalState.STALLED: f"I'm stuck on{name} with no good path forward — I need you.",
    }.get(state, "Working on it — I'll report when there's something worth your attention.")


def public_contract(row: Any) -> dict[str, Any]:
    """Privacy-safe projection of a PresenceContract row."""
    def get(name: str, default: Any = None) -> Any:
        return getattr(row, name, default)
    return {
        "goal_id": str(get("id") or ""),
        "linked_goal_id": str(get("linked_goal_id") or "") if get("linked_goal_id") else None,
        "objective": str(get("objective") or ""),
        "state": str(get("state") or ""),
        "confidence": str(get("confidence") or ""),
        "priority": str(get("priority") or ""),
        "interruption_policy": str(get("interruption_policy") or ""),
        "autonomy_policy": str(get("autonomy_policy") or ""),
        "blocked_reason": get("blocked_reason"),
        "next_condition": dict(get("next_condition") or {}),
        "origin_device_id": str(get("origin_device_id") or "") if get("origin_device_id") else None,
        "deadline_at": get("deadline_at").isoformat() if get("deadline_at") else None,
        "version": int(get("version") or 0),
        "updated_at": get("updated_at").isoformat() if get("updated_at") else None,
    }
