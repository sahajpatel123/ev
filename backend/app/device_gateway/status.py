"""Phone onboarding/status surface. Server-owned trust language only."""

from __future__ import annotations

from typing import Any

from app.models import Device
from app.runtime_identity import runtime_git_sha

from .sandbox import is_sandbox_device, memory_scope_of


def trust_label(device: Device) -> str:
    if device.revoked_at is not None:
        return "REVOKED"
    if is_sandbox_device(device):
        return "PAIRED_SANDBOX"
    return "TRUSTED_OWNER_DEVICE"


def next_action_for(device: Device) -> str:
    label = trust_label(device)
    if label == "REVOKED":
        return "pair_again"
    if label == "PAIRED_SANDBOX":
        return "promote_on_mac"
    return "ready"


def owner_scope_for(device: Device) -> str:
    if device.revoked_at is not None or is_sandbox_device(device):
        return f"sandbox:{device.id}"
    return "master"


def _profile(device: Device) -> dict[str, Any]:
    raw = getattr(device, "endpoint_profile", None) or {}
    return raw if isinstance(raw, dict) else {}


def _healthkit_public(device: Device) -> dict[str, Any]:
    hk = _profile(device).get("healthkit")
    hk = hk if isinstance(hk, dict) else {}
    available = bool(hk.get("available"))
    return {
        "freshness": str(hk.get("freshness") or ("reported" if available else "unavailable")),
        "sent_to_model": False,
        "available": available,
        "reason": hk.get("reason") or ("no_entitlement" if not available else None),
    }


def _notifications_public(device: Device) -> dict[str, Any]:
    note = _profile(device).get("notifications")
    note = note if isinstance(note, dict) else {}
    has_token = bool(getattr(device, "push_token", None))
    return {
        "push_registered": has_token,
        "push_delivery": "apns" if has_token else "poll",
        "inbox_channel": "in_app_poll",
        "authorization": note.get("authorization") or "undetermined",
    }


def device_status_payload(device: Device, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    trusted = trust_label(device) == "TRUSTED_OWNER_DEVICE"
    payload: dict[str, Any] = {
        "trust_state": trust_label(device),
        "owner_scope": owner_scope_for(device),
        "device_role": device.role or "companion",
        "display_name": device.name,
        "device_id": str(device.id),
        "platform": device.platform,
        "backend_build": runtime_git_sha(),
        "auth_revision": int(getattr(device, "auth_revision", 1) or 1),
        "memory_scope": memory_scope_of(device),
        "next_action": next_action_for(device),
        "product": "Tailscale PWA",
        "turngate_bound": True,
        "trusted_owner": trusted,
        "endpoint_profile": getattr(device, "endpoint_profile", None) or {},
        "healthkit": _healthkit_public(device),
        "notifications": _notifications_public(device),
    }
    if extra:
        payload.update(extra)
    return payload
