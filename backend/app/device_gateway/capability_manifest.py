"""Phone capability manifest: one server-computed answer to "what can THIS
iPhone do right now?".

Owner problem this solves (2026-09-08): a freshly paired iPhone sits in
PAIRED_SANDBOX where the spoken surface is deliberately chat-only, so the
owner experiences "Evie on iPhone just talks" with no sign that tools,
memory, camera look, and Mac actions exist one promotion away. This module
turns the trust lifecycle into an explicit, honest manifest the PWA renders,
so the gap and the unlock are visible on the device itself.

Read-only by design: no endpoint here mutates trust, memory, or tools. The
manifest is derived from the SAME policy code the voice/text paths enforce
(`is_sandbox_device`, the phone-core read surface, the phone-mac action
surface), so it cannot drift from what turns actually do.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.device_gateway.sandbox import is_sandbox_device
from app.models import Device

# The realtime function surface minted in webrtc_live.phone_webrtc_session for
# a TRUSTED_OWNER_DEVICE. Sandbox sessions get the sandbox double instead
# (strip_production_memory_from_manifest), so these names are owner-only.
TRUSTED_REALTIME_TOOLS = (
    "evie_state_query",
    "phone_action",
    "evie_look",
    "evie_home_action",
)

# Deterministic phone-core reads (device_gateway/phone_core.py): served from
# the Home Station without the model, durable receipts on top.
PHONE_CORE_READS = (
    "clock",
    "identity",
    "capabilities",
    "memory_history",
    "weather",
    "calendar",
    "contacts",
    "inbox",
)

# Home-Station action surface (device_gateway/phone_mac.py + routed actions):
# what a trusted phone voice turn may DO, not just read.
PHONE_TRUSTED_ACTIONS = (
    "start_timer",
    "set_reminder",
    "calendar_read",
    "get_weather",
    "list_mail",
    "list_messages",
    "open_app",
    "open_calculator",
    "computer_action",
    "search_web",
)


def _trust_state(device: Device) -> str:
    if device.revoked_at is not None:
        return "REVOKED"
    return "TRUSTED_OWNER_DEVICE" if not is_sandbox_device(device) else "PAIRED_SANDBOX"


def capability_manifest(device: Device) -> dict[str, Any]:
    """Build the per-device capability manifest. Pure function of device state."""

    trust = _trust_state(device)
    trusted = trust == "TRUSTED_OWNER_DEVICE"
    revoked = trust == "REVOKED"

    tools: dict[str, bool] = {}
    for name in PHONE_TRUSTED_ACTIONS:
        tools[name] = trusted
    reads: dict[str, bool] = {}
    for name in PHONE_CORE_READS:
        reads[name] = trusted

    limits: list[str] = []
    if not trusted:
        limits.extend(
            [
                "Spoken tools are off: no timers, reminders, weather, calendar, or Mac actions.",
                "Memory is off: Evie cannot recall your history on this device.",
                "Camera look is off: no photo understanding from this phone.",
            ]
        )

    manifest: dict[str, Any] = {
        "trust_state": trust,
        "environment": "OWNER" if trusted else "SANDBOX",
        "voice": {
            "backend": (settings.phone_audio_backend or "webrtc_strict"),
            "realtime": True,
            "tools": TRUSTED_REALTIME_TOOLS if trusted else (),
            "interrupt": "client_confirmed",
        },
        "tts": {
            # Cycle 58 — the phone speaks with the SAME voice identity as the
            # desk: engine + voice + rate come straight from the canonical
            # EV_VOICE_TTS_* settings. Display-only; changing the timbre is a
            # settings decision, never a per-surface one.
            "engine": str(settings.voice_tts_engine or "auto"),
            "voice": str(settings.voice_tts_voice or ""),
            "provider": str(settings.voice_tts_provider or ""),
            "same_as_desk": True,
        },
        "tools": tools,
        "reads": reads,
        "memory": {
            "scope": "owner" if trusted else "sandbox",
            "history_k": 3 if trusted else 0,
            "shadow_injection": trusted,
        },
        "camera_look": trusted,
        "limits": limits,
        "upgrade_hint": (
            None
            if trusted
            else (
                "Ask Evie on your MacBook: 'promote this iPhone' — tools, memory, "
                "and camera unlock the moment trust is granted."
            )
        ),
    }
    if revoked:
        manifest["limits"] = ["This device's trust was revoked. Re-pair to restore Evie."]
        manifest["upgrade_hint"] = None
    return manifest
