"""Honest protocol sheet: enabled / needs_setup / locked / refused."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FeatureGate, Integration, IntegrationCredential
from app.utils.text import utcnow

REFUSED_PROTOCOLS: tuple[tuple[str, str, str], ...] = (
    (
        "instant_kill",
        "Instant Kill",
        "A weapons-grade kill switch is not implemented and will not be.",
    ),
    (
        "telecom_wiretap",
        "telecom wiretaps",
        "Intercepting third-party telecom is refused.",
    ),
    (
        "city_facial_hunt",
        "city facial hunt",
        "City-scale facial search is refused.",
    ),
    (
        "satellite_drone_weapons",
        "satellite/drone weapons",
        "Satellite or drone weapons are refused.",
    ),
    (
        "become_vision",
        "becoming Vision",
        "EV does not become a synthetic person or upload into a body.",
    ),
    (
        "stranger_baby_monitor",
        "stranger Baby Monitor",
        "Watching strangers without consent is refused.",
    ),
)

CAPABILITY_RE = re.compile(
    r"\b(?:what can you do|what protocols(?: do i have)?|who are you|"
    r"what are you|your capabilities|what do you (?:do|know)|"
    r"introduce yourself|list (?:my )?protocols)\b",
    re.IGNORECASE,
)
REFUSED_ASK_RE = re.compile(
    r"\b(?:refused|banned|what can you not|what (?:won't|will not) you|"
    r"instant kill|wiretap|facial hunt|drone weapons|become vision|"
    r"baby monitor)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Protocol:
    key: str
    title: str
    status: str
    detail: str


def is_capability_intent(message: str) -> bool:
    return bool(CAPABILITY_RE.search(message or ""))


def is_refused_ask(message: str) -> bool:
    return bool(REFUSED_ASK_RE.search(message or ""))


def _refused() -> list[Protocol]:
    return [
        Protocol(key, title, "refused", detail)
        for key, title, detail in REFUSED_PROTOCOLS
    ]


async def _gate(session: AsyncSession, key: str) -> FeatureGate | None:
    return (
        await session.execute(select(FeatureGate).where(FeatureGate.key == key))
    ).scalar_one_or_none()


async def set_gate(
    session: AsyncSession,
    key: str,
    status: str,
    *,
    reason: str | None = None,
    setup_hint: str | None = None,
) -> FeatureGate:
    row = await _gate(session, key)
    if row is None:
        row = FeatureGate(key=key, status=status, reason=reason, setup_hint=setup_hint)
        session.add(row)
    else:
        row.status = status
        row.reason = reason
        row.setup_hint = setup_hint
        row.updated_at = utcnow()
    await session.flush()
    return row


async def _integration_active(session: AsyncSession, adapter: str) -> bool:
    row = (
        await session.execute(
            select(Integration.id).where(
                Integration.adapter == adapter,
                Integration.status == "active",
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _integration_ready(
    session: AsyncSession,
    adapter: str,
    *,
    required_scopes: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Report provider readiness, including credentials and granted scopes."""

    row = (
        await session.execute(
            select(Integration)
            .where(
                Integration.adapter == adapter,
                Integration.status == "active",
            )
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return False, f"{adapter} adapter is not installed."
    scopes = {str(scope) for scope in (row.scopes or [])}
    aliases = {
        "calendar:read": {"calendar:read"},
        "messaging:read": {"messaging:read", "message:read"},
    }
    missing = [
        scope for scope in required_scopes
        if not scopes.intersection(aliases.get(scope, {scope}))
    ]
    if missing:
        return False, "Missing provider scope: " + ", ".join(missing) + "."
    config = row.config if isinstance(row.config, dict) else {}
    provider = str(config.get("provider") or "").strip().lower()
    credential_required = adapter in {"calendar", "messaging", "phone", "mail", "contacts"} and provider not in {
        "local",
        "macos_life",
        "device_proxy",
    }
    if credential_required:
        credential = (
            await session.execute(
                select(IntegrationCredential.id)
                .where(
                    IntegrationCredential.integration_id == row.id,
                    IntegrationCredential.kind == "oauth",
                    IntegrationCredential.revoked_at.is_(None),
                    IntegrationCredential.encrypted_access.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if credential is None:
            return False, "Provider credential is not configured; authorize the integration."
    return True, "Provider adapter and required credentials are ready."


async def protocol_sheet(session: AsyncSession) -> list[Protocol]:
    """Live capability list. Refused is always present even if gates are empty."""

    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    items: list[Protocol] = []

    items.append(
        Protocol(
            "voice_companion",
            "Day-long voice companion",
            "enabled",
            "One live thread across Mac, iOS, and web.",
        )
    )
    items.append(
        Protocol("memory", "Personal memory", "enabled", "Facts, decisions, goals, timeline.")
    )

    search = (settings.search_provider or "none").lower()
    if search in {"none", ""}:
        items.append(
            Protocol(
                "web_search",
                "Web search",
                "needs_setup",
                "EV_SEARCH_PROVIDER unset (set live or brave).",
            )
        )
    else:
        items.append(Protocol("web_search", "Web search", "enabled", f"provider={search}"))
    # Weather is an executable Open-Meteo capability, not a web-search
    # capability. Keep the human protocol sheet aligned with the runtime
    # projection when EV_SEARCH_PROVIDER is intentionally disabled.
    items.append(Protocol("weather", "Live weather", "enabled", "Open-Meteo."))

    calendar_ready, calendar_detail = await _integration_ready(
        session,
        "calendar",
        required_scopes=("calendar:read",),
    )
    if calendar_ready:
        items.append(Protocol("calendar", "Calendar / leave-by", "enabled", calendar_detail))
    else:
        items.append(
            Protocol(
                "calendar",
                "Calendar / leave-by",
                "needs_setup",
                (
                    calendar_detail
                    + " macOS Calendar permission alone does not connect the backend calendar provider."
                ),
            )
        )

    messaging_ready, messaging_detail = await _integration_ready(
        session,
        "messaging",
        required_scopes=("messaging:read",),
    )
    if messaging_ready:
        items.append(Protocol("messages", "Messages via life bridge", "enabled", messaging_detail))
    else:
        items.append(
            Protocol(
                "messages",
                "Messages via life bridge",
                "needs_setup",
                messaging_detail,
            )
        )

    from app.ev.apps import find_macos_life_integration
    from app.ev.computer_runtime import computer_operator_line, readiness_from_computer_state
    from app.voice.live.layer import active_lives

    apps_row = await find_macos_life_integration(session)
    lives = active_lives()
    live = lives[0] if lives else None
    provider = getattr(getattr(live, "grok_voice", None), "_provider", None) if live else None
    computer_ready = readiness_from_computer_state(
        getattr(live, "_computer_state", {}) if live is not None else {},
        client_connected=live is not None,
        helper_ready=apps_row is not None,
        realtime_provider=str(provider or ""),
        device_id=getattr(live, "device_id", None) if live is not None else None,
        session_id=getattr(live, "session_id", None) if live is not None else None,
    )
    if apps_row is not None or computer_ready.app_lifecycle_ready:
        items.append(
            Protocol(
                "macos_apps",
                "Open and close Mac apps",
                "enabled",
                computer_operator_line(computer_ready)
                if computer_ready.mac_client_connected
                else "macos_life helper: apps.activate, apps.quit, open.url.",
            )
        )
    else:
        items.append(
            Protocol(
                "macos_apps",
                "Open and close Mac apps",
                "needs_setup",
                "Connect EV.app or the macos_life helper.",
            )
        )
    if computer_ready.generic_ui_control_ready:
        computer_status = "enabled"
        computer_detail = computer_operator_line(computer_ready)
    elif computer_ready.accessibility_permission == "denied":
        computer_status = "needs_permission"
        computer_detail = "Mac control needs Accessibility permission enabled for EV."
    elif computer_ready.app_lifecycle_ready:
        computer_status = "needs_setup"
        computer_detail = computer_operator_line(computer_ready)
    else:
        computer_status = "needs_setup"
        computer_detail = computer_operator_line(computer_ready)
    items.append(Protocol("computer_control", "Mac computer control", computer_status, computer_detail))

    octo = (settings.octoprint_url or "").strip()
    if octo:
        items.append(Protocol("octoprint", "Workshop printer", "enabled", "OctoPrint URL set."))
    else:
        items.append(
            Protocol(
                "octoprint",
                "Workshop printer",
                "needs_setup",
                "OctoPrint URL unset.",
            )
        )

    items.append(Protocol("hud", "HUD / lookout", "enabled", "Native glass via present."))
    from app.ev.camera_runtime import camera_operator_line, readiness_from_camera_state
    from app.voice.live.layer import active_lives

    lives = active_lives()
    live = lives[0] if lives else None
    provider = getattr(getattr(live, "grok_voice", None), "_provider", None) if live else None
    camera_ready = readiness_from_camera_state(
        getattr(live, "_camera_state", {}) if live is not None else {},
        client_connected=live is not None,
        realtime_provider=str(provider or ""),
        device_id=getattr(live, "device_id", None) if live is not None else None,
        session_id=getattr(live, "session_id", None) if live is not None else None,
    )
    if live is not None:
        camera_ready.last_capture_status = getattr(live, "_last_capture_status", None)
    if camera_ready.capture_ready and camera_ready.realtime_image_input_ready:
        sight_status = "enabled"
        sight_detail = camera_operator_line(camera_ready)
    elif camera_ready.permission == "denied":
        sight_status = "needs_permission"
        sight_detail = "macOS has not granted EV camera access."
    else:
        sight_status = "needs_setup"
        sight_detail = camera_operator_line(camera_ready)
    items.append(Protocol("sight", "Camera", sight_status, sight_detail))

    wheels_gate = await _gate(session, "training_wheels")
    if profile.training_wheels_completed_at is not None:
        wheels_status = "enabled"
        wheels_detail = "Training wheels completed."
    elif profile.training_wheels_started_at is not None:
        wheels_status = "enabled"
        wheels_detail = "Training wheels in progress. Say complete training wheels when done."
    elif wheels_gate is not None:
        wheels_status = wheels_gate.status
        wheels_detail = wheels_gate.reason or "Training wheels locked."
    else:
        wheels_status = "locked"
        wheels_detail = "Say start training wheels."
    items.append(Protocol("training_wheels", "Training wheels", wheels_status, wheels_detail))

    items.extend(_refused())
    return items


def protocols_to_dicts(items: list[Protocol]) -> list[dict]:
    return [
        {"key": p.key, "title": p.title, "status": p.status, "detail": p.detail}
        for p in items
    ]


def enabled_tour(items: list[Protocol], *, limit: int = 8) -> list[Protocol]:
    enabled = [p for p in items if p.status == "enabled"]
    return enabled[:limit]


def protocols_hud(items: list[Protocol], *, include_refused: bool = False) -> dict:
    shown = items if include_refused else [p for p in items if p.status != "refused"]
    lines = [f"{p.title} — {p.status}" + (f" ({p.detail})" if p.detail else "") for p in shown]
    return {
        "schema_version": "ev.hud.card.v1",
        "generated_at": utcnow().isoformat(),
        "title": "Protocols",
        "body": "\n".join(lines) if lines else "No unlocked protocols.",
        "items": [p.title for p in shown[:12]],
    }


def speak_enabled(items: list[Protocol], *, limit: int = 8) -> str:
    bullets = enabled_tour(items, limit=limit)
    if not bullets:
        return (
            "No protocols are unlocked yet. Say start training wheels when you want a tour."
        )
    names = "; ".join(p.title for p in bullets)
    return (
        f"You have these protocols: {names}. "
        "Say start training wheels when you want the first-run tour."
    )


# This is deliberately a speech vocabulary, not a second capability registry.
# The runtime projection supplies the names and states; these labels only keep
# those names out of partner speech.
_SPOKEN_CAPABILITY_LABELS = {
    "get_weather": "weather",
    "heading_out": "heading out",
    "start_timer": "timers",
    "set_reminder": "timers",
    "list_timers": "timers",
    "cancel_timer": "timers",
    "search_memory": "memory",
    "search_decisions": "memory",
    "search_timeline": "memory",
    "recall_history": "memory",
    "recall": "memory",
    "get_person": "memory",
    "present": "HUD",
    "calibrate": "diagnostics",
    "search_web": "web search",
    "web_search": "web search",
    "calculate": "safe math",
    "get_health_trends": "health trends",
    "get_gear_status": "gear status",
    "brief_me": "briefings",
    "calendar_read": "calendar",
    "calendar_add": "calendar",
    "list_messages": "messages",
    "send_message": "messages",
    "list_mail": "mail",
    "resolve_contact": "contacts",
    "place_call": "calls",
    "open_url": "open apps",
    "open_app": "open apps",
    "close_app": "close apps",
    "activate_app": "open apps",
    "list_apps": "open apps",
    "inspect_ui": "Mac control",
    "ui_action": "Mac control",
    "screen_look": "Mac control",
    "app_action": "Mac control",
    "computer": "Mac control",
    "computer_status": "Mac control",
    "code": "coding",
    "read": "Mac control",
    "see": "Mac control",
    "click": "Mac control",
    "double_click": "Mac control",
    "right_click": "Mac control",
    "type": "Mac control",
    "paste": "Mac control",
    "key": "Mac control",
    "scroll": "Mac control",
    "drag": "Mac control",
    "home_status": "home status",
    "home_act": "home actions",
    "look": "camera",
    "observe_camera": "camera",
    "capture_photo": "camera",
    "record_video": "camera",
    "camera_replay": "camera replay",
}

_SPOKEN_CAPABILITY_ORDER = (
    "weather",
    "heading out",
    "timers",
    "memory",
    "HUD",
    "diagnostics",
    "web search",
    "safe math",
    "health trends",
    "gear status",
    "briefings",
    "calendar",
    "messages",
    "mail",
    "contacts",
    "open apps",
    "close apps",
    "Mac control",
    "coding",
    "home status",
    "calls",
    "home actions",
    "camera",
)


def _manifest_lists(manifest: dict, *keys: str) -> tuple[list[dict], bool]:
    """Read the first authoritative list without widening an empty projection."""

    containers = [manifest]
    runtime = manifest.get("runtime_manifest")
    if isinstance(runtime, dict):
        containers.append(runtime)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], True
    return [], False


def _spoken_projection_entries(manifest: dict) -> list[dict]:
    entries, present = _manifest_lists(manifest, "live_tool_projection", "live_tools")
    if present:
        return entries
    entries, present = _manifest_lists(manifest, "capabilities", "runtime_capabilities")
    if present:
        return entries
    entries, _ = _manifest_lists(manifest, "realtime_tools", "tools")
    return entries


def _spoken_all_entries(manifest: dict) -> list[dict]:
    entries, present = _manifest_lists(manifest, "capabilities", "runtime_capabilities")
    if present:
        return entries
    return _spoken_projection_entries(manifest)


def _manifest_names(manifest: dict, key: str) -> tuple[set[str], bool]:
    containers = [manifest]
    runtime = manifest.get("runtime_manifest")
    if isinstance(runtime, dict):
        containers.append(runtime)
    for container in containers:
        value = container.get(key)
        if isinstance(value, list):
            names = {
                _spoken_name(item) if isinstance(item, dict) else str(item).strip()
                for item in value
            }
            return {name for name in names if name}, True
    return set(), False


def _spoken_name(entry: dict) -> str:
    return str(entry.get("name") or "").strip()


def _is_spoken_ready(entry: dict) -> bool:
    """Require the state fields when present; flat function payloads are accepted."""

    if entry.get("availability") is not None and entry.get("availability") != "available":
        return False
    if entry.get("risk_class") in {"R4", "forbidden"}:
        return False
    for field in ("model_exposed", "realtime_eligible", "executable"):
        if field in entry and entry.get(field) is not True:
            return False
    if entry.get("name") in {"look", "observe_camera", "capture_photo", "record_video"} and entry.get("capture_ready") is False:
        return False
    if entry.get("name") in {"inspect_ui", "ui_action", "read", "click", "double_click", "right_click", "type", "paste", "key", "scroll", "drag"} and entry.get("generic_ui_control_ready") is False:
        return False
    if entry.get("name") in {"screen_look", "see"} and entry.get("screen_vision_ready") is False:
        return False
    return bool(_spoken_name(entry))


def _spoken_label(entry: dict, *, setup: bool = False) -> str | None:
    name = _spoken_name(entry)
    label = _SPOKEN_CAPABILITY_LABELS.get(name)
    if label is None:
        return None
    if setup and label == "calendar":
        return "calendar (Google)"
    if setup and label == "messages":
        return "messages (life helper)"
    if setup and label in {"open apps", "close apps"}:
        return f"{label} (life helper)"
    return label


def _spoken_labels(entries: list[dict], *, predicate, setup: bool = False) -> list[str]:
    labels: set[str] = set()
    try:
        from app.ev.tool_select import LIVE_VOICE_TOOLS

        allowed = LIVE_VOICE_TOOLS
    except ImportError:  # pragma: no cover - keeps this pure for minimal tooling
        allowed = frozenset(_SPOKEN_CAPABILITY_LABELS)
    for entry in entries:
        if _spoken_name(entry) not in allowed or not predicate(entry):
            continue
        label = _spoken_label(entry, setup=setup)
        if label:
            labels.add(label)
    ordered: list[str] = []
    for label in _SPOKEN_CAPABILITY_ORDER:
        for candidate in (label, f"{label} (Google)", f"{label} (life helper)"):
            if candidate in labels:
                ordered.append(candidate)
                break
    return ordered


def _ready_spoken_labels(manifest: dict) -> list[str]:
    projected = _spoken_projection_entries(manifest)
    executable_set, executable_names_present = _manifest_names(manifest, "executable_tools")
    if executable_names_present:
        projected = [item for item in projected if _spoken_name(item) in executable_set]
    return _spoken_labels(projected, predicate=_is_spoken_ready)


def spoken_ready_capability_line(manifest: dict | None) -> str:
    current = manifest if isinstance(manifest, dict) else {}
    ready = _ready_spoken_labels(current)
    extra = (
        " That already includes the owner's people and chats."
        if "memory" in ready
        else ""
    )
    return "I can do now: " + (", ".join(ready) if ready else "nothing is verified yet") + "." + extra


def spoken_operator_sheet(
    manifest: dict | None,
    *,
    include_refused: bool = False,
    refused: list[Protocol] | None = None,
) -> str:
    """Render the current runtime projection as concise partner speech.

    The sheet is intentionally derived from ``live_tool_projection`` for the
    ready line. A registry entry with no current provider, device, or policy
    state can therefore appear only as setup/confirmation context, never as a
    callable capability.
    """

    current = manifest if isinstance(manifest, dict) else {}
    all_entries = _spoken_all_entries(current)
    ready = _ready_spoken_labels(current)
    ready_line = (
        "I can do now: " + (", ".join(ready) if ready else "nothing is verified yet") + "."
        + (" That already includes the owner's people and chats." if "memory" in ready else "")
    )
    setup = _spoken_labels(
        all_entries,
        setup=True,
        predicate=lambda entry: entry.get("availability") in {"not_connected", "unavailable"},
    )
    confirmation = _spoken_labels(
        all_entries,
        predicate=lambda entry: (
            entry.get("availability") == "available"
            and (
                entry.get("confirmation_required") is True
                or entry.get("risk_class") in {"R3", "R4"}
            )
        ),
    )
    # A label already on the ready line is not also a missing connection.
    # F4 projects `computer` while supervised inspect_ui/open_app stay
    # not_connected internally — that must not become "Needs a connection:
    # Mac control" after Mac control is already ready.
    setup = [label for label in setup if label not in ready]
    projected_names = {_spoken_name(item) for item in _spoken_projection_entries(current)}
    if "computer" in projected_names and "Mac control" in ready:
        setup = [
            label
            for label in setup
            if label
            not in {
                "Mac control",
                "open apps",
                "close apps",
                "open apps (life helper)",
                "close apps (life helper)",
            }
        ]

    lines = [
        ready_line,
    ]
    if setup:
        lines.append("Needs a connection: " + ", ".join(setup) + ".")
    if confirmation:
        lines.append("Needs a tap on your phone: " + ", ".join(confirmation) + ".")
    if include_refused:
        refused_items = refused if refused is not None else _refused()
        names = ", ".join(item.title for item in refused_items)
        if names:
            lines.append("I will not do: " + names + ".")
    return " ".join(lines)


async def capability_reply(
    session: AsyncSession,
    *,
    include_refused: bool = False,
    actor: str | None = None,
    device_id=None,
    realtime_provider: str | None = None,
    channel: str | None = None,
    session_id: str | None = None,
) -> dict:
    items = await protocol_sheet(session)
    from app.ev.policy import capability_manifest as build_manifest

    runtime = await build_manifest(
        session,
        actor=actor or "",
        device_id=device_id,
        realtime_provider=realtime_provider,
        channel=channel,
        session_id=session_id,
    )
    runtime_entries = [
        item for item in runtime.get("capabilities", []) if isinstance(item, dict)
    ]
    runtime_confirmation = [
        item
        for item in runtime_entries
        if item.get("availability") == "available"
        and (
            item.get("confirmation") not in {None, "none"}
            or item.get("confirmation_required") is True
            or item.get("risk_class") in {"R3", "R4"}
        )
    ]
    runtime_setup = [
        item
        for item in runtime_entries
        if item.get("availability") in {"not_connected", "unavailable"}
    ]
    approved_tools = list(runtime.get("approved_tools") or [])
    executable_tools = list(runtime.get("executable_tools") or [])
    # Speech must only call a capability "ready" when the current actor,
    # device, provider, and policy state say it is executable. Provider
    # availability alone is not enough for confirmation-gated or unscoped
    # capabilities.
    if include_refused:
        refused = [p for p in items if p.status == "refused"]
        text = spoken_operator_sheet(runtime, include_refused=True, refused=refused)
    else:
        text = spoken_operator_sheet(runtime)
        capability_error = str(runtime.get("capability_error") or "").strip()
        if capability_error:
            text += (
                " Live capability projection failed. I won't claim an action is "
                "ready until it recovers."
            )
    projected_value = runtime.get("live_tool_projection")
    if not isinstance(projected_value, list):
        projected_value = runtime.get("tools")
    if not isinstance(projected_value, list):
        projected_value = []
    realtime_value = runtime.get("realtime_tools")
    if not isinstance(realtime_value, list):
        realtime_value = runtime.get("tools")
    if not isinstance(realtime_value, list):
        realtime_value = []
    hud = protocols_hud(items, include_refused=include_refused)
    return {
        "reply": text,
        "hud": hud,
        "protocols": protocols_to_dicts(items),
        "enabled": [p.title for p in enabled_tour(items)],
        "runtime_manifest": runtime,
        "runtime_capabilities": runtime_entries,
        "live_tool_projection": list(projected_value),
        "realtime_tools": list(realtime_value),
        "realtime_tool_names": [
            str(item.get("name"))
            for item in realtime_value
            if isinstance(item, dict) and item.get("name")
        ],
        "realtime_tool_choice": (runtime.get("realtime") or {}).get("tool_choice", "auto"),
        "approved_tools": approved_tools,
        "executable_tools": executable_tools,
        "confirmation_tools": list(runtime.get("confirmation_tools") or []),
        "current_device": runtime.get("current_device") or runtime.get("device"),
        "current_provider": runtime.get("current_provider"),
        "missing_setup": runtime_setup,
        "requires_confirmation": runtime_confirmation,
        "capability_error": runtime.get("capability_error"),
        "capability_diagnostics": runtime.get("diagnostics") or {},
        "session_id": runtime.get("session_id"),
        "projection_timestamp": runtime.get("projection_timestamp"),
        "camera": runtime.get("camera") or {},
    }


async def start_training_wheels(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    now = utcnow()
    if profile.training_wheels_started_at is None:
        profile.training_wheels_started_at = now
    profile.updated_at = now
    await set_gate(session, "training_wheels", "enabled", reason="started")
    await session.flush()
    return {
        "started": True,
        "started_at": profile.training_wheels_started_at.isoformat(),
        "reply": "Training wheels started. When you're done, say complete training wheels.",
    }


async def complete_training_wheels(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile, play_dedication
    from app.ev.training_wheels import remaining_steps, unlock_after_training

    remaining = await remaining_steps(session)
    if remaining:
        return {
            "completed": False,
            "error": "training_wheels_incomplete",
            "remaining": remaining,
            "reply": "Finish Training Wheels first: " + ", ".join(remaining) + ".",
        }

    profile = await get_profile(session)
    now = utcnow()
    first_complete = profile.training_wheels_completed_at is None
    profile.training_wheels_completed_at = profile.training_wheels_completed_at or now
    profile.onboarding_completed_at = profile.onboarding_completed_at or now
    profile.updated_at = now
    await set_gate(session, "training_wheels", "enabled", reason="completed")
    await unlock_after_training(session)
    await session.flush()
    dedication = await play_dedication(session, auto=True) if first_complete else {
        "played": False,
        "reason": "already_played",
        "text": profile.dedication_text,
        "blob_id": profile.dedication_blob_id,
    }
    return {
        "completed": True,
        "completed_at": profile.training_wheels_completed_at.isoformat(),
        "dedication": dedication,
        "reply": (
            dedication.get("text")
            if dedication.get("played")
            else "Training wheels complete."
        ),
    }


def mark_onboarding(profile, when: datetime | None = None) -> None:
    if profile.onboarding_completed_at is None:
        profile.onboarding_completed_at = when or utcnow()
        profile.updated_at = profile.onboarding_completed_at
