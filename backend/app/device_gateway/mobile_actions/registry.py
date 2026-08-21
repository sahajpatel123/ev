"""Curated mobile capabilities. Quality over a huge flaky catalog.

Product actuator is the Native Capability Broker. The Shortcuts Bridge
remains in-tree as regression reference and is not the intended path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .trust import ConfirmPolicy, RiskClass, risk_for_operation

ClassLevel = Literal[0, 1, 2, 3]
Method = Literal["native_broker", "web_handoff", "shortcuts_bridge", "app_url", "unsupported"]
Verification = Literal["strong", "medium", "weak"]
Confirm = Literal["none", "voice_ok", "required", "system_ui", "block"]

CLASS_NAVIGATE = 0
CLASS_REVERSIBLE = 1
CLASS_COMMUNICATION = 2
CLASS_BLOCKED = 3


@dataclass(frozen=True)
class MobileCapability:
    operation: str
    title: str
    class_level: ClassLevel
    methods: tuple[Method, ...]
    verification: Verification
    confirmation: Confirm
    needs_native: bool
    needs_contacts: bool = False
    permission: str | None = None
    v1: bool = True
    notes: str = ""
    risk_class: RiskClass = "T1"
    confirmation_policy: ConfirmPolicy = "none"
    system_confirmation_required: bool = False
    reversibility: str = "easy"
    needs_bridge: bool = False  # legacy alias; product path ignores Shortcuts


def _cap(
    operation: str,
    title: str,
    class_level: ClassLevel,
    methods: tuple[Method, ...],
    verification: Verification,
    *,
    needs_native: bool = False,
    needs_contacts: bool = False,
    permission: str | None = None,
    notes: str = "",
    reversibility: str = "easy",
    can_send_directly: bool = False,
) -> MobileCapability:
    risk, policy = risk_for_operation(operation, can_send_directly=can_send_directly)
    confirm: Confirm = "none"
    if policy == "voice":
        confirm = "required"
    elif policy == "system_ui":
        confirm = "system_ui"
    elif policy == "block":
        confirm = "block"
    return MobileCapability(
        operation=operation,
        title=title,
        class_level=class_level,
        methods=methods,
        verification=verification,
        confirmation=confirm,
        needs_native=needs_native,
        needs_contacts=needs_contacts,
        permission=permission,
        notes=notes,
        risk_class=risk,
        confirmation_policy=policy,
        system_confirmation_required=policy == "system_ui",
        reversibility=reversibility,
        needs_bridge=needs_native,
    )


CAPABILITIES: dict[str, MobileCapability] = {
    "create_timer": _cap(
        "create_timer",
        "Timer",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "strong",
        needs_native=True,
        permission="alarmkit_or_notifications",
        notes="AlarmKit when available; otherwise an Evie notification timer, never claimed as Clock.",
    ),
    "create_reminder": _cap(
        "create_reminder",
        "Reminder",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "strong",
        needs_native=True,
        permission="reminders",
    ),
    "create_alarm": _cap(
        "create_alarm",
        "Alarm",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "medium",
        needs_native=True,
        permission="alarmkit_or_notifications",
    ),
    "call_contact": _cap(
        "call_contact",
        "Call",
        CLASS_COMMUNICATION,
        ("native_broker", "web_handoff"),
        "weak",
        needs_native=True,
        needs_contacts=True,
        permission="contacts",
        notes="System Phone UI is the final control. Opening the dialer is not a connected call.",
        reversibility="hard",
    ),
    "facetime_contact": _cap(
        "facetime_contact",
        "FaceTime",
        CLASS_COMMUNICATION,
        ("native_broker", "web_handoff"),
        "weak",
        needs_native=True,
        needs_contacts=True,
        permission="contacts",
        reversibility="hard",
    ),
    "message_contact": _cap(
        "message_contact",
        "Message",
        CLASS_COMMUNICATION,
        ("native_broker", "web_handoff"),
        "medium",
        needs_native=True,
        needs_contacts=True,
        permission="contacts",
        notes="MessageUI composer is the one confirmation. Do not add an Evie Are you sure.",
        reversibility="hard",
    ),
    "start_directions": _cap(
        "start_directions",
        "Directions",
        CLASS_NAVIGATE,
        ("native_broker", "web_handoff"),
        "medium",
    ),
    "open_maps": _cap(
        "open_maps",
        "Maps",
        CLASS_NAVIGATE,
        ("native_broker", "web_handoff"),
        "medium",
    ),
    "create_calendar_event": _cap(
        "create_calendar_event",
        "Calendar",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "strong",
        needs_native=True,
        permission="calendar",
        notes="Personal events execute on explicit command. Invitations would require voice confirm (not v1).",
    ),
    "open_app": _cap(
        "open_app",
        "Open app",
        CLASS_NAVIGATE,
        ("native_broker", "web_handoff"),
        "medium",
        notes="Curated AppLaunchRegistry only. No installed-app enumeration.",
    ),
    "current_location": _cap(
        "current_location",
        "Location",
        CLASS_NAVIGATE,
        ("native_broker",),
        "medium",
        needs_native=True,
        permission="location",
    ),
    "share_content": _cap(
        "share_content",
        "Share",
        CLASS_NAVIGATE,
        ("native_broker", "web_handoff"),
        "medium",
    ),
    "copy_to_clipboard": _cap(
        "copy_to_clipboard",
        "Copy",
        CLASS_NAVIGATE,
        ("native_broker", "web_handoff"),
        "strong",
    ),
    "schedule_notification": _cap(
        "schedule_notification",
        "Notification",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "medium",
        needs_native=True,
        permission="notifications",
        notes="Evie notification, not an iOS Reminder item.",
    ),
    "haptic": _cap(
        "haptic",
        "Haptic",
        CLASS_NAVIGATE,
        ("native_broker",),
        "strong",
        needs_native=True,
    ),
    "set_focus": _cap(
        "set_focus",
        "Focus",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "medium",
        needs_native=True,
        notes="Only if the native broker reports Focus support.",
    ),
    "media_play_pause": _cap(
        "media_play_pause",
        "Media",
        CLASS_REVERSIBLE,
        ("native_broker",),
        "weak",
        needs_native=True,
        notes="Only with a documented integration. Opening Spotify is open_app, not playback control.",
    ),
    "direct_message": _cap(
        "direct_message",
        "Direct send",
        CLASS_COMMUNICATION,
        ("native_broker",),
        "strong",
        needs_native=True,
        notes="Test/mock direct-send adapter. Not Apple SMS. Voice confirmation is the one authorization.",
        reversibility="hard",
        can_send_directly=True,
    ),
    "self_test": _cap(
        "self_test",
        "Native self-test",
        CLASS_NAVIGATE,
        ("native_broker",),
        "strong",
        needs_native=True,
    ),
}

BLOCKED_OPERATIONS = frozenset(
    {
        "run_shortcut",
        "pay",
        "payment",
        "purchase",
        "transfer",
        "upi",
        "bank_transfer",
        "set_passcode",
        "change_password",
        "delete_contact",
        "delete_photos",
        "delete_conversation",
        "delete_files",
        "webhook",
        "post_url",
        "find_my_erase",
        "set_wifi",
        "set_bluetooth",
        "arbitrary_url",
    }
)

CORE_V1_OPERATIONS = (
    "create_timer",
    "create_reminder",
    "call_contact",
    "message_contact",
    "start_directions",
    "open_maps",
    "facetime_contact",
    "create_alarm",
    "create_calendar_event",
    "open_app",
    "current_location",
    "share_content",
    "copy_to_clipboard",
)

HANDSHAKE_ONLY_OPERATIONS = frozenset({"set_focus", "media_play_pause", "direct_message", "haptic"})

CANARY_OPERATIONS = (
    "create_timer",
    "create_reminder",
    "open_app",
    "call_contact",
    "message_contact",
    "start_directions",
)

EMERGENCY_NUMBERS = frozenset(
    {"911", "112", "999", "000", "110", "119", "101", "102", "108"}
)
HOME_QUERIES = frozenset({"home", "my home", "the house", "my house", "home address"})


def get_capability(operation: str) -> MobileCapability | None:
    return CAPABILITIES.get((operation or "").strip())


def is_blocked(operation: str) -> bool:
    name = (operation or "").strip()
    return name in BLOCKED_OPERATIONS or name.startswith("delete_")


def advertised_operations(*, handshake: dict[str, Any] | None) -> tuple[str, ...]:
    ops = list(CORE_V1_OPERATIONS)
    caps = set((handshake or {}).get("capabilities") or ())
    for extra in HANDSHAKE_ONLY_OPERATIONS:
        if extra in caps:
            ops.append(extra)
    return tuple(ops)


def public_capability_row(cap: MobileCapability, *, available: bool, reason: str | None) -> dict[str, Any]:
    return {
        "operation": cap.operation,
        "title": cap.title,
        "class": cap.class_level,
        "risk_class": cap.risk_class,
        "methods": list(cap.methods),
        "verification": cap.verification,
        "confirmation": cap.confirmation,
        "confirmation_policy": cap.confirmation_policy,
        "system_confirmation_required": cap.system_confirmation_required,
        "needs_native": cap.needs_native,
        "needs_bridge": False,
        "available": available,
        "reason": reason,
    }
