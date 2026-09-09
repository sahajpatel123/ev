"""Mobile V2 portable-operator contract.

ONE Evie, two bodies: Home Station (Mac) executes heavy work, iPhones are
portable capability bridges. This module is pure contract + deterministic
routing policy — no model, no second brain.

- RouteTarget: where the work belongs (phone / core / cloud / home / multi).
- ActionResult: honest outcome vocabulary; never fake success.
- Interference: phone-originated Mac work defaults NON_DISRUPTIVE; visible
  foreground UI only when the owner explicitly asked or no background path
  exists (Mac non-interference law, permanent).
- Notification policy: deterministic owner-attention gate (no model chatter).
- Muse enforcement: normal phone path allows Muse Voice / Muse Spark /
  deterministic Core only; OpenAI Realtime / gpt-4o-transcribe / Grok / Luna /
  DeepSeek are never automatic brains (explicit rollback ticket only).

DB-touching helpers reuse the existing stores (broker claim, offline queue,
phone_action_records); this file adds no tables and edits no migrations.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


class RouteTarget(StrEnum):
    PHONE_LOCAL = "PHONE_LOCAL"
    CORE = "CORE"
    CLOUD = "CLOUD"
    HOME_STATION = "HOME_STATION"
    MULTI_DEVICE = "MULTI_DEVICE"


class ActionResult(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    QUEUED = "QUEUED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class Interference(StrEnum):
    NON_DISRUPTIVE = "NON_DISRUPTIVE"
    FOREGROUND_REQUIRED = "FOREGROUND_REQUIRED"


# Bounded expiry for Home Station queueing (24h default, matches offline engine).
HOME_QUEUE_TTL_SECONDS = 86400
HOME_QUEUE_MAX_TTL_SECONDS = 7 * 86400

# --- Route classification (capability reasoning, not a phrase-book) ---

_PHONE_LOCAL_RE = re.compile(
    r"\b(where am i|nearby|near me|how far|take (a |this )?photo|scan this|"
    r"read this (qr|barcode)|call |text |facetime|message |copy that|"
    r"use (what'?s on )?my clipboard|remind me when i (reach|leave|arrive|am near))",
    re.IGNORECASE,
)
_CORE_RE = re.compile(
    r"\b(what are my commitments|what'?s (on |for )?(today|tomorrow)|my focus|"
    r"my reminders|my inbox|remember this|what did i decide|my next|"
    r"what should i focus on|what'?s next)",
    re.IGNORECASE,
)
_CLOUD_RE = re.compile(
    r"\b(research|search the web|quantum|brief me on|what is quantum|"
    r"summarize the news|weather in )",
    re.IGNORECASE,
)
_HOME_RE = re.compile(
    r"\b(on my mac|on the mac|home station|find .* (file|invoice|report)|"
    r"open .* (project|app)|organize .* files|create .* pdf|check my mail|"
    r"what'?s (running|happening) on my mac|continue .* on my mac|"
    r"save .* on my mac|put this .* on my mac|fix that file|calculator)",
    re.IGNORECASE,
)
_MULTI_RE = re.compile(
    r"\b(take a photo .* (mac|note)|put .* into .* (mac|note)|"
    r"send .* to (my )?mac|continue .* on my mac)",
    re.IGNORECASE,
)

# Capabilities that can never run invisibly (explicit screen observation).
_FOREGROUND_CAPABILITIES = frozenset(
    {
        "screen_look",
        "mac.screen_look",
        "computer.screen_look",
        "show_mac",
    }
)

_EXPLICIT_FOREGROUND_RE = re.compile(
    r"\b(show me|open .* and show|bring .* to front|display on my mac|"
    r"look at what'?s on my mac|show me my mac)\b",
    re.IGNORECASE,
)

_STOP_RE = re.compile(
    r"^\s*(stop( evie)?|cancel( (that|this|the task|it))?|abort)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def resolve_route_target(text: str) -> RouteTarget:
    """Decide where phone work belongs. Owner never chooses."""
    raw = (text or "").strip()
    if not raw:
        return RouteTarget.CORE
    if _MULTI_RE.search(raw):
        return RouteTarget.MULTI_DEVICE
    if _HOME_RE.search(raw):
        return RouteTarget.HOME_STATION
    if _PHONE_LOCAL_RE.search(raw):
        # "Remind me when I ..." needs both phone geofence + core reminder.
        if re.search(r"\bremind me when i\b", raw, re.IGNORECASE):
            return RouteTarget.MULTI_DEVICE
        return RouteTarget.PHONE_LOCAL
    if _CORE_RE.search(raw):
        return RouteTarget.CORE
    if _CLOUD_RE.search(raw):
        return RouteTarget.CLOUD
    # Default: conversational/core truth first; never assume Home Station.
    return RouteTarget.CORE


def classify_interference(
    text: str,
    *,
    capability: str = "",
    explicit_foreground_requested: bool | None = None,
) -> Interference:
    """Default NON_DISRUPTIVE. Foreground only when genuinely necessary."""
    cap = (capability or "").strip().lower()
    if cap in _FOREGROUND_CAPABILITIES:
        # Even screen-look should be task-scoped + explicit; the caller
        # passes explicit_foreground_requested=True only for an owner ask
        # such as "show me my Mac".
        if explicit_foreground_requested is False:
            return Interference.NON_DISRUPTIVE
        return Interference.FOREGROUND_REQUIRED
    if explicit_foreground_requested is True:
        return Interference.FOREGROUND_REQUIRED
    if _EXPLICIT_FOREGROUND_RE.search(text or ""):
        return Interference.FOREGROUND_REQUIRED
    return Interference.NON_DISRUPTIVE


def is_stop_request(text: str) -> bool:
    """Obvious stop/cancel needs no model reasoning."""
    return bool(_STOP_RE.match(text or ""))


# --- Honest result mapping (legacy states -> contract) ---

_BROKER_TO_RESULT: dict[str, ActionResult] = {
    "SUCCEEDED": ActionResult.COMPLETED,
    "EXECUTING": ActionResult.PARTIAL,
    "ROUTED": ActionResult.PARTIAL,
    "REQUESTED": ActionResult.PARTIAL,
    "QUEUED": ActionResult.QUEUED,
    "CANCELLED": ActionResult.BLOCKED,
    "EXPIRED": ActionResult.FAILED,
    "FAILED": ActionResult.FAILED,
}

_PHONE_MAC_TO_RESULT: dict[str, ActionResult] = {
    "COMPLETED": ActionResult.COMPLETED,
    "ACCEPTED": ActionResult.PARTIAL,
    "QUEUED": ActionResult.QUEUED,
    "FAILED": ActionResult.FAILED,
}

_OFFLINE_TO_RESULT: dict[str, ActionResult] = {
    "pending": ActionResult.QUEUED,
    "accepted": ActionResult.QUEUED,
    "executed": ActionResult.COMPLETED,
    "failed": ActionResult.FAILED,
    "expired": ActionResult.FAILED,
    "rejected": ActionResult.BLOCKED,
}


def map_broker_status(status: str) -> ActionResult:
    return _BROKER_TO_RESULT.get((status or "").upper(), ActionResult.FAILED)


def map_phone_mac_status(status: str) -> ActionResult:
    return _PHONE_MAC_TO_RESULT.get((status or "").upper(), ActionResult.FAILED)


def map_offline_state(state: str) -> ActionResult:
    return _OFFLINE_TO_RESULT.get((state or "").lower(), ActionResult.FAILED)


def owner_reply_for(
    result: ActionResult,
    *,
    capability: str = "",
    target: RouteTarget | str = RouteTarget.CORE,
) -> str:
    """Natural owner-facing line. QUEUED/OFFLINE never claim completion."""
    cap = f" ({capability})" if capability else ""
    tgt = str(target)
    if result == ActionResult.COMPLETED:
        return "Done."
    if result == ActionResult.PARTIAL:
        return "That's underway — I'll confirm when it finishes."
    if result == ActionResult.QUEUED:
        return "Queued for Home Station. I'll notify you when it's done — not done yet."
    if result == ActionResult.DEVICE_OFFLINE:
        return "Your Mac is offline right now. I did not pretend it ran."
    if result == ActionResult.NEEDS_CONFIRMATION:
        return f"That needs your approval before I run it{cap}."
    if result == ActionResult.BLOCKED:
        return f"I can't complete that{cap} — a boundary blocks it. I'll prepare everything up to that boundary."
    return f"That didn't complete{cap} on {tgt}."


# --- Notification intelligence (deterministic, no model chatter) ---

_NOTIFY_YES = frozenset(
    {
        "task_finished",
        "research_finished",
        "mac_task_finished",
        "approval_required",
        "deadline",
        "reminder_due",
        "important_failure",
        "queued_task_completed",
        "handoff_ready",
    }
)

_NOTIFY_NO = frozenset(
    {
        "internal_retry",
        "sync_ok",
        "background_index",
        "telemetry",
        "routine_sync",
    }
)


def should_notify(kind: str) -> bool:
    """Owner attention has value -> True. Internal events -> False."""
    key = (kind or "").strip().lower()
    if key in _NOTIFY_YES:
        return True
    if key in _NOTIFY_NO:
        return False
    # Unknown kinds default to quiet; explicit request paths set their own kind.
    return False


# --- Muse enforcement (no silent legacy brain) ---

_MUSE_VOICE = frozenset({"meta_muse_voice", "muse_voice"})
_MUSE_SPARK = frozenset({"meta_muse_spark", "muse", "muse_spark"})
_DETERMINISTIC_CORE = frozenset({"core", "turn_gate", "deterministic", "echo_debug"})

_LEGACY_BRAINS = frozenset(
    {
        "openai_realtime",
        "gpt-4o-transcribe",
        "gpt-realtime",
        "grok",
        "xai",
        "luna",
        "gpt-5.6-luna",
        "deepseek",
        "deepseek-v4-flash",
    }
)


def phone_brain_allowed(provider: str) -> bool:
    """True for Muse Voice / Muse Spark / deterministic Core. Legacy never auto."""
    name = (provider or "").strip().lower()
    return name in _MUSE_VOICE | _MUSE_SPARK | _DETERMINISTIC_CORE


def phone_brain_is_legacy(provider: str) -> bool:
    name = (provider or "").strip().lower()
    return any(legacy in name for legacy in _LEGACY_BRAINS)


def legacy_brain_error(provider: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "LEGACY_BRAIN_BLOCKED",
        "message": (
            f"Phone brain '{provider}' is not the normal path. "
            "Normal phone intelligence is Muse Voice Transcribe → OwnerTurn/Core "
            "→ Muse Spark. Legacy path needs an explicit rollback ticket."
        ),
        "retryable": False,
    }
