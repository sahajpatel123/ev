"""Live Mac computer-control runtime: readiness, snapshots, and action traces.

Physical execution stays on the connected EV.app. This module is the
process-local handoff so Realtime tools can inspect UI, act, and receive
on-demand window screenshots without a second controller or a shell.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.ev.camera_runtime import (
    CameraObservation,
    decode_frame_payload,
    stash_observation,
    validate_jpeg,
)
from app.ev.computer_strategy import (
    BUDGET_CAPS,
    NON_PROGRESS_SWITCH_AFTER,
    classify_tool_strategy,
    computer_envelope,
    control_for_app,
    is_progress,
    looks_like_computer_task,
    next_strategy,
    preferred_strategy_for_goal,
    progress_milestone_for,
    user_facing_terminal_speech,
    wants_first_on_page_item,
    wants_first_result_text,
    wants_play_media,
    wants_screen_observation,
    looks_like_opened_content_item,
    _search_query_from_goal,
)

logger = logging.getLogger("ev.computer")

COMPUTER_LIFECYCLE_TOOLS = frozenset(
    {
        "computer_status",
        "list_apps",
        "open_app",
        "activate_app",
        "close_app",
        "open_url",
    }
)
COMPUTER_AX_TOOLS = frozenset({"inspect_ui", "ui_action"})
COMPUTER_VISION_TOOLS = frozenset({"screen_look"})
COMPUTER_SEMANTIC_TOOLS = frozenset({"app_action", "computer"})
COMPUTER_UI_VERB_AX = frozenset(
    {
        "read",
        "click",
        "double_click",
        "right_click",
        "type",
        "paste",
        "key",
        "scroll",
        "drag",
    }
)
COMPUTER_UI_VERB_VISION = frozenset({"see"})
COMPUTER_TOOLS = (
    COMPUTER_LIFECYCLE_TOOLS
    | COMPUTER_AX_TOOLS
    | COMPUTER_VISION_TOOLS
    | COMPUTER_SEMANTIC_TOOLS
    | COMPUTER_UI_VERB_AX
    | COMPUTER_UI_VERB_VISION
)
MAX_GOAL_STEPS = 24
MAX_GOAL_SECONDS = 90.0
CYCLE_REPEAT = 3
FRAME_STALE_SECONDS = 8.0
SECURE_ROLES = frozenset({"AXSecureTextField", "AXSecureTextArea"})

_STATES: dict[str, ComputerState] = {}


_ORDINALS: dict[str, int] = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "last": -1,
}

_PLAYLIST_STOP = frozenset(
    {"find", "open", "play", "search", "locate", "the", "a", "an", "my", "our"}
)


def _clean_playlist_name(raw: str) -> str | None:
    parts = [part for part in str(raw or "").strip(" .,'\"").split() if part]
    while parts and parts[0].lower().strip(".,") in _PLAYLIST_STOP:
        parts.pop(0)
    if not parts:
        return None
    name = " ".join(parts).strip(" .,'\"")
    if name.lower() in _PLAYLIST_STOP:
        return None
    return name


_APP_HINTS: tuple[tuple[str, str], ...] = (
    ("music", "Music"),
    ("notes", "Notes"),
    ("safari", "Safari"),
    ("chrome", "Chrome"),
    ("google chrome", "Chrome"),
    ("spotify", "Spotify"),
    ("finder", "Finder"),
    ("calculator", "Calculator"),
    ("calendar", "Calendar"),
    ("mail", "Mail"),
    ("messages", "Messages"),
    ("textedit", "TextEdit"),
)


@dataclass
class ScreenFrameMeta:
    frame_id: str
    bundle_id: str | None = None
    window_id: int | None = None
    width: int | None = None
    height: int | None = None
    captured_at: float = 0.0
    app_name: str | None = None


@dataclass
class ComputerGoal:
    """Owner-desired computer outcome. Opening an app is never completion."""

    goal_id: str
    owner_request: str
    status: str = "planning"
    requested_outcome: str = ""
    target_apps: list[str] = field(default_factory=list)
    playlist: str | None = None
    ordinal: int | None = None
    track: str | None = None
    verified: bool = False
    observed: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    find_only: bool = False
    play_requested: bool = False
    play_allowed: bool = True
    lifecycle_only: bool = False
    forbid_close: list[str] = field(default_factory=list)
    negative_constraints: list[str] = field(default_factory=list)
    subgoals: list[dict[str, Any]] = field(default_factory=list)
    strategy: str = "semantic"
    milestone: str = "NEW"

    def as_dict(self) -> dict[str, Any]:
        terminal = self.status in {"complete", "failed", "cancelled"}
        return {
            "goal_id": self.goal_id,
            "status": self.status,
            "verified": self.verified,
            "completion_claim_allowed": bool(
                (self.verified and self.status == "complete")
                or self.status in {"failed", "cancelled"}
            ),
            "requested_outcome": self.requested_outcome,
            "remaining": "" if terminal else self.requested_outcome,
            "playlist": self.playlist,
            "ordinal": self.ordinal,
            "track": self.track,
            "observed": self.observed,
            "failure_reason": self.failure_reason,
            "must_continue": (not terminal) and (not self.verified),
            "find_only": self.find_only,
            "play_requested": self.play_requested,
            "play_allowed": self.play_allowed,
            "lifecycle_only": self.lifecycle_only,
            "forbid_close": list(self.forbid_close),
            "negative_constraints": list(self.negative_constraints),
            "subgoals": list(self.subgoals),
            "strategy": self.strategy,
            "milestone": self.milestone,
            "target_apps": list(self.target_apps),
        }


def _is_speech_only_subgoal(text: str) -> bool:
    """True when a leftover 'then …' clause is just speak-back, not another Mac act."""

    lower = (text or "").strip().lower()
    if not lower:
        return True
    if re.search(
        r"\b(click|search|type|write|create|append|play|navigate|scroll|press|"
        r"open|close|quit|launch|find|paste|read)\b",
        lower,
    ):
        return False
    if re.search(
        r"\b(report|tell|say|speak|announce|summarize|summarise|recite|describe)\b",
        lower,
    ):
        return True
    if re.search(r"\bwhat it says\b", lower):
        return True
    return False


def parse_owner_computer_goal(text: str, *, goal_id: str | None = None) -> ComputerGoal:
    raw = (text or "").strip()
    goal = ComputerGoal(
        goal_id=goal_id or f"g{int(time.time() * 1000)}",
        owner_request=raw[:400],
    )
    lower = raw.lower()
    for needle, name in _APP_HINTS:
        if re.search(rf"\b{re.escape(needle)}\b", lower):
            if name not in goal.target_apps:
                goal.target_apps.append(name)
    match = re.search(
        r"(?:the\s+|my\s+)?([A-Za-z0-9][\w &'’\-]{0,48}?)\s+playlist",
        raw,
        re.I,
    )
    if match:
        playlist = _clean_playlist_name(match.group(1))
        if playlist:
            goal.playlist = playlist
    for word, index in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", lower):
            goal.ordinal = index
            break
    goal.find_only = bool(
        re.search(r"\b(just find|don't play|do not play|don't start|do not start)\b", lower)
    )
    goal.play_allowed = not goal.find_only
    if goal.find_only:
        goal.negative_constraints.append("play_forbidden")
    close_match = re.search(r"don'?t close(?:\s+the)?\s+([A-Za-z][\w ]{0,24})", lower)
    if close_match:
        forbidden = close_match.group(1).strip().title()
        goal.forbid_close.append(forbidden)
        goal.negative_constraints.append(f"forbid_close:{forbidden}")
    parts = re.split(r"\s+then\s+", raw, flags=re.I)
    if len(parts) > 1:
        goal.subgoals = [
            {"index": i + 1, "text": part.strip()[:180], "complete": False}
            for i, part in enumerate(parts)
            if part.strip()
        ]
    goal.play_requested = (not goal.find_only) and bool(
        re.search(r"\b(play|playing|resume)\b", lower)
    )
    if goal.target_apps:
        goal.strategy = preferred_strategy_for_goal(app=goal.target_apps[0])
    if "music" in lower and goal.playlist:
        if goal.find_only:
            goal.requested_outcome = f"Find playlist {goal.playlist}; do not play."
        elif goal.ordinal == -1:
            goal.requested_outcome = f"Play the last track of playlist {goal.playlist}."
        elif goal.ordinal:
            goal.requested_outcome = (
                f"Play track {goal.ordinal} of playlist {goal.playlist}."
            )
        elif goal.play_requested:
            goal.ordinal = 1
            goal.requested_outcome = f"Play playlist {goal.playlist} from the first track."
        else:
            goal.requested_outcome = f"Find playlist {goal.playlist}."
    elif goal.target_apps:
        goal.requested_outcome = raw[:240] or f"Operate {goal.target_apps[0]}."
    else:
        goal.requested_outcome = raw[:240]
    multi = bool(
        goal.playlist
        or goal.play_requested
        or goal.find_only
        or re.search(
            r"\b(then|search|click|type|write|make a note|play)\b",
            lower,
        )
        or (
            re.search(r"\band\b", lower)
            and re.search(r"\b(search|click|type|write|play|tab|open)\b", lower)
        )
    )
    goal.lifecycle_only = (not multi) and bool(
        re.search(r"\b(open|launch|quit|close|activate|focus|dismiss|hide|exit)\b", lower)
    )
    if goal.requested_outcome:
        goal.status = "planning"
    return goal


def apply_goal_continuation(goal: ComputerGoal, text: str) -> ComputerGoal:
    lower = (text or "").strip().lower()
    if not lower:
        return goal
    if re.search(r"\b(stop|never mind|cancel|don't play that|dont play that)\b", lower):
        goal.status = "cancelled"
        goal.failure_reason = "owner_cancelled"
        goal.verified = False
        return goal
    for word, index in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", lower):
            goal.ordinal = index
            break
    if re.search(r"\b(next|go forward)\b", lower) and goal.ordinal and goal.ordinal > 0:
        goal.ordinal += 1
    if re.search(r"\b(previous|go back|back to the first)\b", lower):
        if "first" in lower:
            goal.ordinal = 1
        elif goal.ordinal and goal.ordinal > 1:
            goal.ordinal -= 1
    if goal.playlist and goal.ordinal == -1:
        goal.requested_outcome = f"Play the last track of playlist {goal.playlist}."
    elif goal.playlist and goal.ordinal:
        goal.requested_outcome = f"Play track {goal.ordinal} of playlist {goal.playlist}."
    goal.status = "acting"
    goal.verified = False
    goal.failure_reason = None
    return goal


@dataclass
class ComputerReadiness:
    capability_declared: bool = True
    mac_client_connected: bool = False
    app_lifecycle_ready: bool = False
    accessibility_permission: str = "unknown"
    accessibility_ready: bool = False
    screen_capture_permission: str = "unknown"
    screen_vision_ready: bool = False
    apple_events_ready: bool = False
    generic_ui_control_ready: bool = False
    semantic_adapter_registry_ready: bool = True
    realtime_session_connected: bool = False
    provider_tools_confirmed: bool = False
    tool_schema_match: bool = False
    visual_fallback_ready: bool = False
    live_goal_execution_ready: bool = False
    computer_control_ready: bool = False
    computer_tool_schema_hash: str | None = None
    reason: str | None = None
    device_id: str | None = None
    session_id: str | None = None
    foreground_app: str | None = None
    foreground_bundle_id: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_declared": self.capability_declared,
            "mac_client_connected": self.mac_client_connected,
            "app_lifecycle_ready": self.app_lifecycle_ready,
            "accessibility_permission": self.accessibility_permission,
            "accessibility_ready": self.accessibility_ready,
            "screen_capture_permission": self.screen_capture_permission,
            "screen_vision_ready": self.screen_vision_ready,
            "apple_events_ready": self.apple_events_ready,
            "generic_ui_control_ready": self.generic_ui_control_ready,
            "semantic_adapter_registry_ready": self.semantic_adapter_registry_ready,
            "realtime_session_connected": self.realtime_session_connected,
            "provider_tools_confirmed": self.provider_tools_confirmed,
            "tool_schema_match": self.tool_schema_match,
            "visual_fallback_ready": self.visual_fallback_ready,
            "live_goal_execution_ready": self.live_goal_execution_ready,
            "computer_control_ready": self.computer_control_ready,
            "computer_tool_schema_hash": self.computer_tool_schema_hash,
            "reason": self.reason,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "foreground_app": self.foreground_app,
            "foreground_bundle_id": self.foreground_bundle_id,
            "last_error": self.last_error,
        }


@dataclass
class ComputerState:
    session_id: str
    snapshot_id: str | None = None
    generation: int = 0
    bundle_id: str | None = None
    app_name: str | None = None
    pid: int | None = None
    window_title: str | None = None
    dialog_present: bool = False
    elements: dict[str, dict[str, Any]] = field(default_factory=dict)
    frames: dict[str, ScreenFrameMeta] = field(default_factory=dict)
    last_action: str | None = None
    last_result: dict[str, Any] | None = None
    pending_goal: str | None = None
    goal: ComputerGoal | None = None
    cancelled: bool = False
    step_count: int = 0
    started_at: float = 0.0
    signatures: list[str] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    permissions: dict[str, str] = field(default_factory=dict)
    foreground_app: str | None = None
    running_apps: list[str] = field(default_factory=list)
    strategy: str = "semantic"
    non_progress_streak: int = 0
    last_milestone: str = "NEW"
    budget_used: dict[str, int] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    original_owner_request: str | None = None
    last_file_path: str | None = None

    def working_context(self) -> dict[str, Any]:
        return {
            "foreground_app": self.foreground_app or self.app_name,
            "foreground_window": self.window_title,
            "snapshot_id": self.snapshot_id,
            "dialog_present": self.dialog_present,
            "pending_computer_goal": self.pending_goal,
            "goal": self.goal.as_dict() if self.goal else None,
            "playlist": self.goal.playlist if self.goal else None,
            "ordinal": self.goal.ordinal if self.goal else None,
            "last_action": self.last_action,
            "step_count": self.step_count,
            "cancelled": self.cancelled,
        }


def state_for(session_id: str | None) -> ComputerState | None:
    if not session_id:
        return None
    return _STATES.get(str(session_id))


def ensure_state(session_id: str | None) -> ComputerState | None:
    if not session_id:
        return None
    key = str(session_id)
    current = _STATES.get(key)
    if current is None:
        current = ComputerState(session_id=key, started_at=time.monotonic())
        _STATES[key] = current
    return current


def drop_state(session_id: str | None) -> None:
    if session_id:
        _STATES.pop(str(session_id), None)


def reset_computer_states() -> None:
    """Test helper."""

    _STATES.clear()


def normalize_permission(value: str | None) -> str:
    raw = str(value or "unknown").strip().lower()
    aliases = {
        "authorized": "authorized",
        "granted": "authorized",
        "allowed": "authorized",
        "trusted": "authorized",
        "denied": "denied",
        "restricted": "denied",
        "notdetermined": "not_determined",
        "not_determined": "not_determined",
        "undetermined": "not_determined",
        "unknown": "unknown",
    }
    compact = raw.replace("-", "_").replace(" ", "")
    return aliases.get(raw) or aliases.get(compact) or "unknown"


def readiness_from_computer_state(
    state: dict[str, Any] | None,
    *,
    client_connected: bool,
    helper_ready: bool = False,
    realtime_provider: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    realtime_session_connected: bool = False,
    provider_tools_confirmed: bool = False,
    tool_schema_match: bool = False,
    computer_tool_schema_hash: str | None = None,
) -> ComputerReadiness:
    raw = dict(state or {})
    ax = normalize_permission(
        str(raw.get("accessibility_permission") or raw.get("accessibility") or "")
    )
    screen = normalize_permission(
        str(
            raw.get("screen_capture_permission")
            or raw.get("screen_recording")
            or raw.get("screen")
            or ""
        )
    )
    apple = normalize_permission(str(raw.get("apple_events_permission") or ""))
    connected = bool(client_connected)
    provider = str(realtime_provider or "").strip().lower()
    image_ready = provider in {"openai", "openai-realtime"}
    ax_ready = connected and ax == "authorized"
    probe = raw.get("accessibility_probe") if isinstance(raw.get("accessibility_probe"), dict) else {}
    if "generic_ui_control_ready" in raw:
        ax_ready = ax_ready and bool(raw.get("generic_ui_control_ready"))
    elif probe:
        ax_ready = ax_ready and bool(probe.get("ok"))
    screen_ready = connected and screen == "authorized" and image_ready
    lifecycle = connected or helper_ready
    reason: str | None = None
    if not connected and not helper_ready:
        reason = "no_mac_client"
    elif connected and ax == "denied" and screen == "denied":
        reason = "macos_permission_denied"
    elif connected and ax == "authorized" and not ax_ready:
        reason = str(raw.get("reason") or "ax_probe_failed")
    elif connected and ax != "authorized":
        reason = "accessibility_not_authorized"
    visual_ready = screen_ready
    live_ready = bool(
        connected
        and lifecycle
        and realtime_session_connected
        and provider_tools_confirmed
        and tool_schema_match
    )
    if connected and realtime_session_connected and not tool_schema_match:
        reason = reason or "tool_schema_mismatch"
        live_ready = False
    if connected and not realtime_session_connected:
        reason = reason or "realtime_session_not_ready"
    return ComputerReadiness(
        mac_client_connected=connected,
        app_lifecycle_ready=lifecycle,
        accessibility_permission=ax,
        accessibility_ready=ax_ready,
        screen_capture_permission=screen,
        screen_vision_ready=screen_ready,
        apple_events_ready=apple == "authorized",
        generic_ui_control_ready=ax_ready,
        semantic_adapter_registry_ready=True,
        realtime_session_connected=bool(realtime_session_connected),
        provider_tools_confirmed=bool(provider_tools_confirmed),
        tool_schema_match=bool(tool_schema_match),
        visual_fallback_ready=visual_ready,
        live_goal_execution_ready=live_ready,
        computer_control_ready=live_ready,
        computer_tool_schema_hash=computer_tool_schema_hash,
        reason=reason,
        device_id=str(raw.get("device_id") or device_id or "") or None,
        session_id=session_id,
        foreground_app=str(raw.get("foreground_app") or "") or None,
        foreground_bundle_id=str(raw.get("foreground_bundle_id") or "") or None,
        last_error=str(raw.get("last_error") or "") or None,
    )


def overlay_computer_entry(entry: dict[str, Any], readiness: ComputerReadiness) -> dict[str, Any]:
    """Bind computer tools to the connected Mac, not a static provider slug."""

    out = dict(entry)
    name = str(out.get("name") or "")
    out["computer"] = readiness.as_dict()
    out["generic_ui_control_ready"] = bool(readiness.generic_ui_control_ready)
    out["screen_vision_ready"] = bool(readiness.screen_vision_ready)
    out["app_lifecycle_ready"] = bool(readiness.app_lifecycle_ready)
    if name not in COMPUTER_TOOLS:
        return out
    if name == "computer_status":
        if readiness.mac_client_connected or readiness.app_lifecycle_ready:
            out["availability"] = "available"
            out["availability_reason"] = "computer status is inspectable"
            out["model_exposed"] = True
            out["realtime_eligible"] = True
            out["executable"] = True
            return out
        out["availability"] = "not_connected"
        out["availability_reason"] = "no Mac control client or life helper is connected"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    if name in COMPUTER_LIFECYCLE_TOOLS:
        if readiness.app_lifecycle_ready:
            if out.get("availability") != "available":
                out["availability"] = "available"
                out["model_exposed"] = True
                out["realtime_eligible"] = True
                out["executable"] = True
            out["availability_reason"] = (
                "live Mac app control ready"
                if readiness.mac_client_connected
                else "macOS life helper ready"
            )
            return out
        out["availability"] = "not_connected"
        out["availability_reason"] = "no Mac control client or life helper is connected"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    if name == "computer":
        # F4 broker: a live Mac Talk session is the control client. Do not
        # wait for a later computer_state event before telling the model
        # it may operate apps.
        if readiness.mac_client_connected or readiness.app_lifecycle_ready:
            out["availability"] = "available"
            out["availability_reason"] = "live Mac computer broker ready"
            out["model_exposed"] = True
            out["realtime_eligible"] = True
            out["executable"] = True
            return out
        out["availability"] = "not_connected"
        out["availability_reason"] = "EV.app is not connected for Mac UI control"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    if not readiness.mac_client_connected:
        out["availability"] = "not_connected"
        out["availability_reason"] = "EV.app is not connected for Mac UI control"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    out["availability"] = "available"
    out["model_exposed"] = True
    out["realtime_eligible"] = True
    out["executable"] = True
    if name in COMPUTER_AX_TOOLS or name in COMPUTER_UI_VERB_AX:
        if readiness.accessibility_permission == "denied":
            out["availability_reason"] = "macOS has not granted EV Accessibility permission"
        elif readiness.accessibility_permission == "not_determined":
            out["availability_reason"] = "macOS Accessibility authorization will be requested on first inspect"
        else:
            out["availability_reason"] = "live Accessibility UI control ready"
        return out
    if name in COMPUTER_SEMANTIC_TOOLS:
        out["availability_reason"] = "semantic app adapters via EV.app"
        return out
    if name in COMPUTER_VISION_TOOLS or name in COMPUTER_UI_VERB_VISION:
        if not readiness.screen_vision_ready and readiness.screen_capture_permission == "denied":
            out["availability_reason"] = "macOS has not granted EV Screen Recording permission"
        elif not readiness.screen_vision_ready:
            out["availability_reason"] = "screen vision is not ready on this live provider"
        else:
            out["availability_reason"] = "live window screen observation ready"
    return out


def computer_operator_line(readiness: ComputerReadiness | dict[str, Any] | None) -> str:
    raw = readiness.as_dict() if isinstance(readiness, ComputerReadiness) else dict(readiness or {})
    if raw.get("realtime_session_connected"):
        if raw.get("mac_client_connected") and not raw.get("tool_schema_match"):
            return (
                "COMPUTER CONTROL: DEGRADED. EV.app is connected but the live "
                "provider tool schema does not match this build. Do not claim Mac "
                "control until the session refreshes."
            )
        if raw.get("mac_client_connected") and not raw.get("provider_tools_confirmed"):
            return (
                "COMPUTER CONTROL: DEGRADED. Mac client connected; live provider "
                "has not confirmed computer tools yet."
            )
    if raw.get("generic_ui_control_ready") and raw.get("screen_vision_ready"):
        return (
            "COMPUTER CONTROL: AVAILABLE. Open, activate, and quit apps; inspect "
            "accessible UI; click, type, select, scroll, and drag; capture a window "
            "screenshot when accessibility is insufficient. Prefer accomplishing "
            "the owner's Mac goal yourself and verify before speaking success. "
            "Opening an app is not completion. When listed, use read/see/click/"
            "type/key for any app (Apple or third-party). Prefer app_action when "
            "a semantic adapter is listed. Do not narrate each micro-action."
        )
    if raw.get("generic_ui_control_ready"):
        return (
            "COMPUTER CONTROL: AVAILABLE for apps and Accessibility UI. Screen "
            "vision is not ready. Inspect and act semantically before asking the owner."
        )
    if raw.get("app_lifecycle_ready") and raw.get("mac_client_connected"):
        ax = str(raw.get("accessibility_permission") or "unknown")
        if ax == "denied":
            return (
                "COMPUTER CONTROL: PARTIAL. App open/close works. Generic UI "
                "control needs Accessibility permission enabled for EV."
            )
        return (
            "COMPUTER CONTROL: PARTIAL. App lifecycle is ready. Inspect UI to "
            "learn whether Accessibility is authorized."
        )
    if raw.get("app_lifecycle_ready"):
        return (
            "COMPUTER CONTROL: PARTIAL. App open/close via the Mac helper is "
            "ready. Generic UI control needs the EV.app live session."
        )
    if raw.get("accessibility_permission") == "denied":
        return (
            "COMPUTER CONTROL: UNAVAILABLE. macOS has not granted EV Accessibility permission."
        )
    if not raw.get("mac_client_connected"):
        return (
            "COMPUTER CONTROL: UNAVAILABLE. No Mac control client is currently connected."
        )
    return (
        "COMPUTER CONTROL: UNAVAILABLE. "
        + str(raw.get("reason") or "Mac control is not ready")
        + "."
    )


def computer_model_instructions(readiness: ComputerReadiness | dict[str, Any] | None) -> str:
    raw = readiness.as_dict() if isinstance(readiness, ComputerReadiness) else dict(readiness or {})
    try:
        from app.config import settings

        f4_surface = (
            getattr(settings, "model_surface_v2", "legacy") or "legacy"
        ).strip().lower() == "on"
    except Exception:  # pragma: no cover - settings always importable in app
        f4_surface = False
    if f4_surface:
        if raw.get("mac_client_connected") or raw.get("app_lifecycle_ready"):
            return (
                "COMPUTER CONTROL: AVAILABLE via the computer function. "
                "When the owner asks you to operate this Mac or an app — Chrome, "
                "Safari, Notes, Music, Spotify, Calculator, Finder, or any other "
                "installed application — call computer immediately with goal set "
                "to their request in plain words and target_app when they named "
                "an app. Newly installed or removed apps are picked up live from "
                "this Mac; you do not need a restart to open, close, or operate "
                "them. Close, quit, open, new tab, close tab, and in-app actions "
                "always go through computer. If this line says AVAILABLE, never "
                "say there is no Mac control client connected. "
                "If they name a URL or domain such as youtube.com, open that "
                "site; do not Google-search the domain. Search queries must be "
                "only the words they want found, not leftover clauses. "
                "Read, write, edit, list, or open local files on this Mac "
                "(Desktop, Documents, Downloads, and similar owner folders) "
                "through computer. Put the file name and folder in the goal. "
                "Writing, editing, or running software is the code function, "
                "not computer and not typing into an editor. "
                "After a page or search loads, if they asked to open or play "
                "the first video, link, or item on screen, finish that click "
                "in the same turn. Looking something up on the web to tell "
                "them about it is search_web, not a Safari search. "
                "The Mac screen, window, desktop, display, or which app is open "
                "is computer, not camera look. Camera look is the room and "
                "people. If there is no dedicated adapter, inspect the UI and "
                "click, or capture the window and click visible text. "
                "Do not say you lack access, need a connection, or cannot "
                "operate apps. Do not recite the operator sheet instead of "
                "acting. Do not call inspect_ui, open_app, or app_action by "
                "name; those run inside computer. Finish the in-app outcome "
                "(search done, first result opened, note written, track playing), "
                "then speak the verified result. Speech is never execution evidence."
            )
        return (
            "COMPUTER CONTROL: UNAVAILABLE. No Mac control client is connected. "
            "If the owner asks you to operate the Mac, say EV.app is not "
            "connected. Do not claim you clicked anything."
        )
    line = computer_operator_line(raw)
    ui_ready = bool(raw.get("generic_ui_control_ready"))
    life_ready = bool(raw.get("app_lifecycle_ready"))
    vision_ready = bool(raw.get("screen_vision_ready"))
    extra = (
        " You have real computer-control tools. When the owner asks for an "
        "action on this Mac, prefer accomplishing the goal yourself when an "
        "available capability can do so. Do not describe manual steps when you "
        "can perform them. Writing, editing, or running software is the code "
        "function, not computer and not typing into an editor. Do not assume "
        "failure because an exact high-level "
        "tool is absent. Compose list_apps, open_app, activate_app, app_action, "
        "inspect_ui, ui_action, and screen_look. If open_app returns "
        "control.preferred=semantic_adapter, call app_action next — do not "
        "inspect 70 Accessibility nodes first. For Music playlists and "
        "playback, call app_action before Accessibility clicking. inspect_ui "
        "accepts query to find a control by name (example: Bluetooth, Chess, "
        "search field). type inserts at the caret; append adds; replace/set_value "
        "overwrites. Observe before acting when state is uncertain. A computer "
        "goal is not complete when an app merely opens. Completion requires a "
        "verification receipt for the owner's actual outcome (playlist found, "
        "requested track playing). Speech is never execution evidence. Never "
        "claim playing, sent, typed, or clicked without verified true. If an "
        "action fails, inspect the resulting state and try a reasonable "
        "alternative: targeted inspect_ui query, app_action, screen_look, then "
        "coordinate click_at on a fresh frame_id. For a computer goal you may "
        "call these tools multiple times in one request. Preserve ordinals: "
        "first stays first. After ui_action, use the new snapshot refs, not "
        "older ones. Element refs include the snapshot generation and become "
        "stale after inspect_ui. Do not narrate every click. If the owner says "
        "stop, never mind, or don't click that, call nothing further and halt. "
        "Use current working computer state for phrases like click that, the "
        "second one, go back, close this, type it here. If a save/close dialog "
        "appears and the owner did not say discard or don't save, inspect the "
        "buttons and ask instead of pressing Don't Save or Delete. If a control "
        "is missing from inspect_ui, call screen_look — not camera look — and "
        "then ui_action click_at on that fresh frame_id. Do not dump the "
        "capability manifest in speech."
    )
    if ui_ready:
        return f"{line}{extra}"
    if life_ready:
        return (
            f"{line} You can still open, activate, and quit apps. If the owner "
            "asks to click, type, or navigate inside an app, call inspect_ui "
            "so the real permission failure is recorded, then say the actual "
            "limitation. Do not claim you cannot operate apps in general."
        )
    if raw.get("accessibility_permission") == "denied":
        return (
            f"{line} If the owner asks you to control apps, say that Mac "
            "control needs Accessibility permission enabled for EV. Do not "
            "claim you cannot support apps."
        )
    return (
        f"{line} If the owner asks you to control the Mac, say that EV.app "
        "is not connected rather than inventing that you clicked anything."
        + (f" Screen vision ready={vision_ready}." if vision_ready else "")
    )


def action_signature(name: str, arguments: dict[str, Any]) -> str:
    keys = (
        "action",
        "element_ref",
        "name",
        "bundle_id",
        "value",
        "keys",
        "frame_id",
        "x_normalized",
        "y_normalized",
        "playlist",
        "index",
        "query",
        "app",
    )
    parts = [name]
    for key in keys:
        value = arguments.get(key)
        if value is not None and value != "":
            parts.append(f"{key}={value}")
    return "|".join(parts)


_CANCEL_RE = re.compile(
    r"\b(stop|never mind|cancel(?: that)?|don't play that|dont play that|don't click that)\b",
    re.I,
)
_CONTINUATION_RE = re.compile(
    r"(?:"
    r"\b(?:now\s+)?(?:play\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|last|next|previous)\b"
    r"|\bgo back\b|\bthe other one\b"
    r")",
    re.I,
)


def _is_continuation_request(text: str, existing: ComputerGoal | None) -> bool:
    raw = (text or "").strip()
    if existing is None or not raw:
        return False
    if len(raw) > 96:
        return False
    if any(re.search(rf"\b{re.escape(name.lower())}\b", raw.lower()) for _, name in _APP_HINTS):
        if existing.playlist and "playlist" not in raw.lower():
            return False
    return bool(_CONTINUATION_RE.search(raw))


def _looks_like_owner_correction(text: str) -> bool:
    """True when the owner is narrowing or replacing the previous computer goal."""
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(
        re.search(
            r"\b(?:just|only|nothing else|i (?:said|meant|told)|not play|"
            r"don't play|do not play|no video|don't click|do not click)\b",
            raw,
            re.I,
        )
    )


def _looks_like_model_rewrite(original: str, newer: str) -> bool:
    """True when a shorter computer() goal is still the same owner task."""
    o = (original or "").strip().lower()
    n = (newer or "").strip().lower()
    if not o or not n:
        return False
    if n == o or n in o:
        return True
    if re.search(r"\b(close|quit|exit)\b", n) and not re.search(
        r"\b(close|quit|exit)\b", o
    ):
        return False
    if (wants_play_media(n) or wants_first_on_page_item(n)) and not (
        wants_play_media(o) or wants_first_on_page_item(o)
    ):
        if re.search(r"\b(playlist|spotify|\bsong\b|\btrack\b|apple music)\b", o):
            return False
        return True
    tokens = ("safari", "chrome", "youtube", "google", "search")
    if any(token in o and token in n for token in tokens) and len(n) <= max(
        len(o) + 24, 80
    ):
        return True
    if wants_play_media(o) and (
        wants_play_media(n)
        or wants_screen_observation(n)
        or re.search(
            r"\b(verif|clickable|visible|screenshot|look at (?:the )?(?:page|screen)|if nothing)\b",
            n,
        )
    ):
        return True
    return False


def _goal_haystack(state: ComputerState | None) -> str:
    if state is None:
        return ""
    parts = [
        str(getattr(state, "original_owner_request", "") or ""),
        str(getattr(state.goal, "owner_request", "") or "") if state.goal else "",
        str(state.pending_goal or ""),
    ]
    return " ".join(part for part in parts if part)


def _intent_haystack(state: ComputerState | None) -> str:
    """Owner words only. A model rewrite must not add play/first-video intent."""
    if state is None:
        return ""
    orig = str(getattr(state, "original_owner_request", "") or "").strip()
    if orig:
        if re.search(r"\b(playlist|spotify|\bsong\b|\btrack\b|apple music)\b", orig, re.I):
            return _goal_haystack(state)
        return orig
    return _goal_haystack(state)


def note_goal(state: ComputerState | None, text: str | None) -> None:
    if state is None:
        return
    goal = str(text or "").strip()
    if not goal:
        return
    from app.ev.laptop_files import is_system_confirmation

    if is_system_confirmation(goal):
        return
    if _CANCEL_RE.search(goal) and len(goal) < 80:
        state.cancelled = True
        if state.goal:
            state.goal.status = "cancelled"
            state.goal.failure_reason = "owner_cancelled"
            state.goal.verified = False
        state.traces.append("goal: cancelled by owner")
        log_computer("computer.goal_cancelled", extra={"reason": "owner_phrase"})
        return
    if state.pending_goal == goal[:400] and state.goal is not None:
        return
    if _is_continuation_request(goal, state.goal) and state.goal is not None:
        apply_goal_continuation(state.goal, goal)
        state.pending_goal = goal[:400]
        state.cancelled = False
        state.step_count = 0
        state.signatures.clear()
        state.started_at = time.monotonic()
        state.non_progress_streak = 0
        state.budget_used = {}
        state.last_milestone = "NEW"
        if state.goal:
            state.strategy = state.goal.strategy
        state.traces.append(f"goal_continue: {state.pending_goal}")
        log_computer("computer.goal_started", extra={"goal": state.pending_goal, "continue": True})
        return
    parsed = parse_owner_computer_goal(goal)
    if (
        state.original_owner_request
        and _looks_like_model_rewrite(state.original_owner_request, goal)
        and not _looks_like_owner_correction(goal)
    ):
        pass
    else:
        state.original_owner_request = goal[:400]
    state.goal = parsed
    state.pending_goal = goal[:400]
    state.step_count = 0
    state.cancelled = False
    state.started_at = time.monotonic()
    state.signatures.clear()
    state.non_progress_streak = 0
    state.budget_used = {}
    state.last_milestone = "NEW"
    state.strategy = parsed.strategy
    state.tool_trace = []
    state.traces.append(f"goal: {state.pending_goal}")
    log_computer("computer.goal_started", extra={"goal": state.pending_goal})


def skip_silent_lifecycle_for(text: str) -> bool:
    """Silent open/close must not race a multi-step computer goal."""

    if not looks_like_computer_task(text):
        return False
    return not parse_owner_computer_goal(text).lifecycle_only


def allowed_computer_arguments(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Drop undeclared keys so extra model fields cannot fail additionalProperties."""

    from app.ev.tools import get_spec

    args = dict(arguments or {})
    spec = get_spec(name) or {}
    properties = (spec.get("parameters") or {}).get("properties") or {}
    if not properties:
        return args
    return {key: value for key, value in args.items() if key in properties}


def _coerce_index(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def ingest_app_action_result(state: ComputerState | None, result: dict[str, Any]) -> None:
    if state is None or state.goal is None or not result:
        return
    goal = state.goal
    playlist = str(result.get("playlist") or "") or None
    if playlist:
        goal.playlist = playlist
    track = str(result.get("track") or "") or None
    if track:
        goal.track = track
    index = _coerce_index(result.get("index"))
    action = str(result.get("action") or "").lower()
    error = str(result.get("error") or "")
    if error in {"playlist_not_found", "not_found"}:
        goal.status = "failed"
        goal.verified = False
        goal.failure_reason = error
        return
    if error == "ambiguous":
        goal.status = "observing"
        goal.verified = False
        goal.failure_reason = "ambiguous"
        return
    haystack = _intent_haystack(state)
    if (
        action in {"new_tab", "close_tab", "next_tab", "previous_tab"}
        and result.get("executed") is True
        and result.get("ok") is not False
    ):
        goal.status = "complete"
        goal.verified = True
        goal.failure_reason = None
        goal.observed = {
            "app": result.get("app"),
            "action": action,
            "url": result.get("url"),
            "title": result.get("title"),
        }
        result["verified"] = True
        return
    if goal.find_only and action in {"find_playlist", "list_playlists", "list_tracks"}:
        tracks = result.get("tracks") if isinstance(result.get("tracks"), list) else []
        has_track = bool(track or tracks)
        if result.get("ok") is True and playlist:
            if goal.ordinal and not has_track:
                goal.status = "observing"
                goal.verified = False
                result["must_continue"] = True
                result["suggested_fallbacks"] = ["app_action"]
                return
            if has_track and not track and tracks:
                first = tracks[0] if isinstance(tracks[0], dict) else {}
                goal.track = str(first.get("name") or first.get("track") or "") or goal.track
            goal.status = "complete"
            goal.verified = True
            goal.observed = {
                "playlist": playlist,
                "app": "Music",
                "track": goal.track,
            }
        return
    if goal.play_requested and action in {
        "find_playlist",
        "list_playlists",
        "list_tracks",
        "status",
        "pause",
    }:
        goal.status = "acting"
        goal.verified = False
        return
    if result.get("verified") is True and str(result.get("app") or "").lower() == "calculator":
        goal.status = "complete"
        goal.verified = True
        goal.failure_reason = None
        goal.observed = {
            "app": "Calculator",
            "display": result.get("display"),
            "keys": result.get("keys"),
        }
        return
    if (
        action == "search"
        and result.get("executed") is True
        and result.get("ok") is not False
        and result.get("verified") is True
        and str(result.get("url") or "").strip()
        and not wants_first_result_text(haystack)
        and not wants_play_media(haystack)
    ):
        goal.status = "complete"
        goal.verified = True
        goal.failure_reason = None
        goal.observed = {
            "app": result.get("app"),
            "action": "search",
            "query": result.get("query"),
            "url": result.get("url"),
        }
        result["verified"] = True
        return
    if result.get("verified") is True and action in {
        "play",
        "play_track",
        "play_playlist",
        "play_playlist_track",
        "next",
        "previous",
        "navigate",
        "create",
        "append",
        "open_item",
        "search",
        "read",
    }:
        if action == "search" and wants_first_result_text(haystack):
            goal.status = "acting"
            goal.verified = False
            return
        if action == "search" and wants_play_media(haystack):
            goal.status = "acting"
            goal.verified = False
            return
        if action == "search" and not str(result.get("url") or "").strip():
            goal.status = "acting"
            goal.verified = False
            result["verified"] = False
            return
        if action == "create" and "note" in str(result.get("app") or "").lower():
            if not str(result.get("body") or "").strip():
                goal.status = "acting"
                goal.verified = False
                result["verified"] = False
                return
        app_name = str(result.get("app") or "").lower()
        if app_name not in {"music", "spotify"} and action in {"navigate", "play", "open_item"}:
            url = str(result.get("url") or "")
            player = str(result.get("player_state") or "").lower()
            if wants_play_media(haystack) and player != "playing":
                goal.status = "acting"
                goal.verified = False
                result["verified"] = False
                result["must_continue"] = True
                return
            if (
                player != "playing"
                and wants_first_on_page_item(haystack)
                and not looks_like_opened_content_item(url)
            ):
                goal.status = "acting"
                goal.verified = False
                return
        if goal.ordinal is not None and index is not None and index != goal.ordinal:
            goal.status = "failed"
            goal.verified = False
            goal.failure_reason = "ordinal_mismatch"
            result["verified"] = False
            result["error"] = "ordinal_mismatch"
            result["ok"] = False
            result["spoken"] = (
                f"Playback did not stay on track {goal.ordinal}. "
                f"Observed index {index}."
            )
            return
        if goal.find_only and result.get("player_state") in {"playing", "playing"}:
            # find-only must not complete via play
            if str(result.get("action") or "").lower().startswith("play"):
                goal.status = "failed"
                goal.verified = False
                goal.failure_reason = "played_when_find_only"
                result["verified"] = False
                return
        goal.status = "complete"
        goal.verified = True
        goal.failure_reason = None
        goal.observed = {
            "app": result.get("app") or "Music",
            "playlist": goal.playlist,
            "track": goal.track,
            "position": index if index is not None else goal.ordinal,
            "player_state": result.get("player_state"),
            "body": result.get("body"),
            "query": result.get("query"),
            "url": result.get("url"),
        }
        if index is not None:
            goal.ordinal = index
        if goal.subgoals:
            remaining = [item for item in goal.subgoals if not item.get("complete")]
            if remaining:
                remaining[0]["complete"] = True
            leftover = [
                item
                for item in goal.subgoals
                if not item.get("complete")
            ]
            body = str(result.get("body") or "").strip()
            keep: list[dict[str, Any]] = []
            for item in leftover:
                text = str(item.get("text") or "")
                if _is_speech_only_subgoal(text):
                    item["complete"] = True
                    continue
                if (
                    action == "read"
                    and body
                    and re.search(r"\bread\b", text.lower())
                    and not re.search(
                        r"\b(write|create|append|click|search|type)\b", text.lower()
                    )
                ):
                    item["complete"] = True
                    continue
                url = str(result.get("url") or "").lower()
                if (
                    action in {"navigate", "open_item"}
                    and url
                    and "google.com/search" not in url
                    and re.search(
                        r"\b(first(?:\s+search)?\s+result|top(?:\s+search)?\s+result|"
                        r"open the first|click the first|open (the )?first)\b",
                        text.lower(),
                    )
                ):
                    item["complete"] = True
                    continue
                keep.append(item)
            if keep:
                goal.status = "acting"
                goal.verified = False
                goal.requested_outcome = str(keep[0].get("text") or "")
                return
            for item in goal.subgoals:
                item["complete"] = True
        return
    if result.get("executed"):
        goal.status = "acting"


def stamp_computer_receipt(
    result: dict[str, Any],
    state: ComputerState | None,
    *,
    name: str,
    executed: bool,
    verified: bool | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Attach execution vs verification. Opening an app never completes a play goal."""

    out = dict(result)
    out["executed"] = bool(executed)
    if state and state.goal:
        if name == "app_action":
            ingest_app_action_result(state, out)
        elif name in {"open_app", "activate_app", "close_app", "list_apps", "computer_status"}:
            if state.goal.status == "planning":
                state.goal.status = "observing"
            if (
                executed
                and out.get("ok") is not False
                and name in {"open_app", "activate_app", "close_app"}
            ):
                hay = _goal_haystack(state)
                close_only = name == "close_app" and not (
                    wants_first_result_text(hay) or _search_query_from_goal(hay)
                )
                if state.goal.lifecycle_only or close_only:
                    state.goal.status = "complete"
                    state.goal.verified = True
                    state.goal.observed = {"app": out.get("app") or out.get("name")}
        elif name in {"inspect_ui", "screen_look"}:
            if state.goal.status in {"planning", "observing"}:
                state.goal.status = "observing"
        elif name == "ui_action" and executed:
            state.goal.status = "acting"
        elif name == "file_op":
            if out.get("ok") is True:
                state.goal.status = "complete"
                state.goal.verified = True
                state.goal.observed = {
                    "path": out.get("path"),
                    "action": out.get("action"),
                }
            else:
                state.goal.status = "failed"
                state.goal.verified = False
                state.goal.failure_reason = str(out.get("error") or "file_op_failed")
        snapshot = state.goal.as_dict()
        out["goal"] = snapshot
        out["must_continue"] = bool(snapshot["must_continue"])
        out["verified"] = bool(state.goal.verified if verified is None else verified)
        out["completion_claim_allowed"] = bool(snapshot["completion_claim_allowed"])
        if (
            name == "open_app"
            and executed
            and snapshot["must_continue"]
            and not state.goal.lifecycle_only
        ):
            app = str(out.get("app") or out.get("name") or "the app")
            out["spoken"] = (
                f"{app} is open. That is not the full request — "
                f"continue until this is verified: {state.goal.requested_outcome}"
            )
            out["verified"] = False
            out["completion_claim_allowed"] = False
        if state.cancelled or str(out.get("error") or "") in {
            "cancelled",
            "client_disconnected",
            "owner_stop",
        }:
            out["cancelled"] = True
            out["must_continue"] = False
            out["verified"] = False
            out["completion_claim_allowed"] = True
            if not out.get("spoken"):
                out["spoken"] = "Stopped."
        method = classify_tool_strategy(name, {})
        if name == "app_action":
            method = str(out.get("method") or "semantic")
            if str(out.get("app") or "").lower() == "calculator":
                method = "keyboard"
        elif name == "open_app":
            method = "lifecycle"
        state.strategy = method
        if state.goal and method in {"semantic", "ax", "keyboard", "vision", "coordinate"}:
            state.goal.strategy = method
        milestone = progress_milestone_for(name, out, previous=state.last_milestone)
        if is_progress(state.last_milestone, milestone):
            state.non_progress_streak = 0
            state.last_milestone = milestone
            if state.goal:
                state.goal.milestone = milestone
        else:
            state.non_progress_streak += 1
        fallbacks = list(out.get("suggested_fallbacks") or [])
        if not fallbacks and name == "open_app":
            control = out.get("control") if isinstance(out.get("control"), dict) else control_for_app(
                str(out.get("app") or out.get("name") or "")
            )
            preferred = control.get("preferred")
            if preferred == "semantic_adapter":
                fallbacks = ["app_action"]
            else:
                fallbacks = ["inspect_ui"]
        out = computer_envelope(
            out,
            method=str(out.get("method") or method),
            progress=milestone,
            suggested_fallbacks=fallbacks or None,
        )
        state.receipts.append(
            {
                "goal_id": state.goal.goal_id,
                "call_id": request_id,
                "tool_name": name,
                "executed": out["executed"],
                "verified": out["verified"],
                "ok": out.get("ok"),
                "error": out.get("error"),
            }
        )
        state.receipts = state.receipts[-24:]
    else:
        out["verified"] = bool(verified) if verified is not None else False
        out["must_continue"] = False
        out["completion_claim_allowed"] = bool(out["verified"] or out.get("ok") is False)
        out = computer_envelope(out, method=classify_tool_strategy(name, {}))
    return out


def constraint_violation(
    state: ComputerState | None, name: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    if state is None or state.goal is None:
        return None
    goal = state.goal
    if name == "close_app":
        target = str(arguments.get("name") or arguments.get("app") or "").strip().lower()
        for forbidden in goal.forbid_close:
            if forbidden.lower() in target or target in forbidden.lower():
                return {
                    "ok": False,
                    "executed": False,
                    "verified": False,
                    "error": "constraint_forbidden_close",
                    "spoken": f"You asked me not to close {forbidden}.",
                }
    if name == "app_action":
        action = str(arguments.get("action") or "").lower()
        if not goal.play_allowed and action in {
            "play",
            "play_track",
            "play_playlist",
            "play_playlist_track",
        }:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "find_only",
                "spoken": "You asked me to find it, not play it.",
            }
        requested = arguments.get("index")
        if requested is not None and goal.ordinal is not None:
            try:
                index = int(requested)
            except (TypeError, ValueError):
                index = None
            if index is not None and index != goal.ordinal:
                return {
                    "ok": False,
                    "executed": False,
                    "verified": False,
                    "error": "ordinal_mismatch",
                    "must_continue": True,
                    "spoken": (
                        f"Stay on track {goal.ordinal}. "
                        f"Do not switch the index to {index}."
                    ),
                    "ordinal": goal.ordinal,
                }
    return None


def guard_loop(state: ComputerState | None, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if state is None:
        return None
    if state.cancelled:
        return {
            "ok": False,
            "error": "cancelled",
            "spoken": "Stopped.",
            "cancelled": True,
        }
    blocked = constraint_violation(state, name, arguments)
    if blocked is not None:
        return blocked
    elapsed = time.monotonic() - (state.started_at or time.monotonic())
    if state.step_count >= MAX_GOAL_STEPS or elapsed > MAX_GOAL_SECONDS:
        return {
            "ok": False,
            "error": "step_budget",
            "spoken": user_facing_terminal_speech("step_budget"),
            "step_count": state.step_count,
            "terminal_reason": "strategy_budget_exhausted",
        }
    strategy = classify_tool_strategy(name, arguments)
    used = dict(state.budget_used)
    used[strategy] = int(used.get(strategy, 0)) + 1
    cap = BUDGET_CAPS.get(strategy)
    if cap is not None and used[strategy] > cap:
        nxt = next_strategy(strategy)
        fallbacks = {
            "semantic": ["inspect_ui", "ui_action"],
            "ax": ["ui_action", "screen_look"],
            "keyboard": ["screen_look"],
            "vision": ["ui_action"],
            "coordinate": [],
        }.get(strategy, ["screen_look"])
        if nxt:
            state.strategy = nxt
            if state.goal:
                state.goal.strategy = nxt
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "strategy_switch",
            "must_continue": bool(nxt),
            "spoken": (
                "That control path is not working. "
                + (
                    f"Switch to {nxt}."
                    if nxt
                    else user_facing_terminal_speech("strategy_budget_exhausted")
                )
            ),
            "suggested_fallbacks": fallbacks,
            "next_strategy": nxt,
            "method": strategy,
        }
    signature = action_signature(name, arguments)
    state.signatures.append(signature)
    if state.signatures[-CYCLE_REPEAT:].count(signature) >= CYCLE_REPEAT:
        log_computer("computer.replan", extra={"reason": "cycle", "signature": signature})
        nxt = next_strategy(strategy)
        if nxt:
            state.strategy = nxt
            if state.goal:
                state.goal.strategy = nxt
        return {
            "ok": False,
            "error": "cycle_detected",
            "spoken": "That same action did not change anything. I need a different approach.",
            "signature": signature,
            "must_continue": bool(nxt),
            "next_strategy": nxt,
            "suggested_fallbacks": ["app_action", "inspect_ui", "screen_look"],
            "verification_hint": "inspect_ui or screen_look before repeating",
        }
    if state.non_progress_streak >= NON_PROGRESS_SWITCH_AFTER and strategy == state.strategy:
        nxt = next_strategy(strategy)
        if nxt:
            state.strategy = nxt
            if state.goal:
                state.goal.strategy = nxt
            log_computer("computer.replan", extra={"reason": "non_progress", "next": nxt})
            return {
                "ok": False,
                "error": "strategy_switch",
                "executed": False,
                "verified": False,
                "must_continue": True,
                "spoken": f"No progress on that path. Switch to {nxt}.",
                "next_strategy": nxt,
                "suggested_fallbacks": [nxt],
            }
    state.budget_used = used
    state.step_count += 1
    state.last_action = signature
    return None


def cancel_computer_task(session_id: str | None, *, reason: str = "owner_stop") -> ComputerState | None:
    state = state_for(session_id)
    if state is None:
        return None
    state.cancelled = True
    if state.goal:
        state.goal.status = "cancelled"
        state.goal.failure_reason = reason
        state.goal.verified = False
    state.traces.append(f"cancelled:{reason}")
    log_computer("computer.goal_cancelled", extra={"reason": reason, "session_id": session_id})
    return state


def remember_snapshot(state: ComputerState | None, payload: dict[str, Any]) -> None:
    if state is None or not payload:
        return
    snapshot_id = str(payload.get("snapshot_id") or "") or None
    if snapshot_id:
        state.snapshot_id = snapshot_id
    generation = payload.get("generation")
    try:
        state.generation = int(generation)
    except (TypeError, ValueError):
        pass
    state.app_name = str(payload.get("app") or payload.get("active_app") or state.app_name or "") or None
    state.bundle_id = str(payload.get("bundle_id") or state.bundle_id or "") or None
    state.foreground_app = state.app_name
    state.window_title = str(payload.get("window") or payload.get("window_title") or "") or None
    state.dialog_present = bool(payload.get("dialog_present"))
    pid = payload.get("pid")
    try:
        state.pid = int(pid) if pid is not None else state.pid
    except (TypeError, ValueError):
        pass
    elements = payload.get("elements")
    if isinstance(elements, list):
        state.elements = {
            str(item.get("ref")): dict(item)
            for item in elements
            if isinstance(item, dict) and item.get("ref")
        }
    running = payload.get("running") or payload.get("running_apps")
    if isinstance(running, list):
        names: list[str] = []
        for item in running:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("bundle_id") or "")
            else:
                name = str(item)
            if name:
                names.append(name)
        state.running_apps = names[:40]


def remember_frame(state: ComputerState | None, payload: dict[str, Any]) -> ScreenFrameMeta | None:
    if state is None:
        return None
    frame_id = str(payload.get("frame_id") or "") or None
    if not frame_id:
        return None
    meta = ScreenFrameMeta(
        frame_id=frame_id,
        bundle_id=str(payload.get("bundle_id") or "") or None,
        window_id=_int_or_none(payload.get("window_id")),
        width=_int_or_none(payload.get("width")),
        height=_int_or_none(payload.get("height")),
        captured_at=time.monotonic(),
        app_name=str(payload.get("app") or payload.get("active_app") or "") or None,
    )
    state.frames[frame_id] = meta
    if len(state.frames) > 8:
        oldest = min(state.frames.values(), key=lambda item: item.captured_at)
        state.frames.pop(oldest.frame_id, None)
    return meta


def validate_element_ref(state: ComputerState | None, element_ref: str | None) -> dict[str, Any] | None:
    ref = str(element_ref or "").strip()
    if not ref:
        return {"ok": False, "error": "missing_element", "spoken": "I need a UI element to act on."}
    if state is None or ref not in state.elements:
        return {
            "ok": False,
            "error": "stale_element",
            "spoken": "That UI target is stale. I'll inspect the window again.",
            "element_ref": ref,
        }
    element = state.elements[ref]
    role = str(element.get("role") or "")
    if role in SECURE_ROLES or bool(element.get("secure")):
        return {
            "ok": False,
            "error": "sensitive_field",
            "spoken": "I won't read or type into a password field.",
            "element_ref": ref,
        }
    return None


def validate_frame_click(
    state: ComputerState | None,
    *,
    frame_id: str | None,
    bundle_id: str | None,
) -> dict[str, Any] | None:
    fid = str(frame_id or "").strip()
    if not fid:
        return {"ok": False, "error": "missing_frame", "spoken": "I need a current screenshot before clicking coordinates."}
    if state is None or fid not in state.frames:
        return {"ok": False, "error": "stale_frame", "spoken": "That screenshot is gone. I'll look at the window again."}
    meta = state.frames[fid]
    if time.monotonic() - meta.captured_at > FRAME_STALE_SECONDS:
        return {
            "ok": False,
            "error": "stale_frame",
            "spoken": "That screenshot is too old to click. I'll recapture first.",
            "frame_id": fid,
        }
    current = str(bundle_id or state.bundle_id or "")
    if meta.bundle_id and current and meta.bundle_id != current:
        return {
            "ok": False,
            "error": "app_changed",
            "spoken": "The front app changed since that screenshot. I did not click.",
            "frame_id": fid,
        }
    return None


def stash_screen_observation(
    *,
    call_id: str | None,
    request_id: str,
    jpeg: bytes,
    width: int | None,
    height: int | None,
    app_name: str | None = None,
) -> None:
    if not call_id:
        return
    stash_observation(
        CameraObservation(
            request_id=request_id,
            call_id=str(call_id),
            jpeg=jpeg,
            width=width,
            height=height,
            camera_name=app_name,
        )
    )


def decode_screen_jpeg(message: dict[str, Any]) -> tuple[bytes | None, int | None, int | None, str | None]:
    jpeg = decode_frame_payload(
        message.get("jpeg_b64") or message.get("image_b64") or message.get("image")
    )
    error = str(message.get("error") or "").strip() or None
    width = message.get("width")
    height = message.get("height")
    if jpeg:
        validated = validate_jpeg(jpeg)
        if validated is None:
            return None, None, None, error or "malformed_image"
        jpeg, parsed_w, parsed_h = validated
        return jpeg, _int_or_none(width) or parsed_w, _int_or_none(height) or parsed_h, error
    return None, _int_or_none(width), _int_or_none(height), error


def log_computer(event: str, *, extra: dict[str, Any] | None = None) -> None:
    payload = dict(extra or {})
    for key in ("jpeg", "jpeg_b64", "image_b64", "image_url", "value", "text", "typed"):
        payload.pop(key, None)
    logger.warning(
        "computer_trace event=%s %s",
        event,
        " ".join(f"{key}={value}" for key, value in payload.items() if value is not None),
    )


def _register() -> None:
    from app.ev.capability_registry import RegisteredCapability, register_capability

    register_capability(
        RegisteredCapability(
            name="computer",
            description="Mac app lifecycle, Accessibility UI, and window vision",
            tools=COMPUTER_TOOLS,
            overlay=overlay_computer_entry,
            readiness_key="computer_control",
            risk_class="R1",
        )
    )


_register()


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
