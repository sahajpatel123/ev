"""Server-owned hardware and permission evidence. Never infer a 16 Pro from a name."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.utils.text import utcnow

from .devices import presence_state

# Known machine identifiers. Rank 0 is preferred camera. Display names are ignored.
_PRO_MACHINES = frozenset(
    {
        "iphone17,1",  # iPhone 16 Pro
        "iphone17,2",  # iPhone 16 Pro Max
        "iphone16,1",  # iPhone 15 Pro
        "iphone16,2",  # iPhone 15 Pro Max
    }
)
_SE_MACHINES = frozenset(
    {
        "iphone14,6",  # iPhone SE (3rd generation)
        "iphone12,8",  # iPhone SE (2nd generation)
    }
)

_THIS_PHONE = re.compile(r"\b(this phone|the device i(?:'m| am) using|on this iphone)\b", re.I)
_LOOK = re.compile(
    r"\b(look at this|look once|take a photo|take a picture|observe|what do you see|"
    r"record a (?:short |bounded )?clip|ocr|read (?:this|the text))\b",
    re.I,
)


def camera_quality_for_machine(machine: str | None) -> tuple[str, int]:
    key = (machine or "").strip().lower()
    if key in _PRO_MACHINES:
        return "pro", 0
    if key in _SE_MACHINES:
        return "standard", 10
    if key.startswith("iphone"):
        return "standard", 20
    return "unknown", 50


def merge_endpoint_profile(device: Device, *, hardware: dict | None, permissions: dict | None) -> dict[str, Any]:
    current = dict(getattr(device, "endpoint_profile", None) or {})
    hw_in = hardware if isinstance(hardware, dict) else {}
    perm_in = permissions if isinstance(permissions, dict) else {}
    machine = str(hw_in.get("model") or hw_in.get("machine") or current.get("hardware", {}).get("model") or "")[:64]
    quality, rank = camera_quality_for_machine(machine)
    if "camera_quality" in hw_in and str(hw_in.get("camera_quality")) in {"pro", "standard", "unknown"}:
        quality = str(hw_in["camera_quality"])
    if "camera_preference_rank" in hw_in:
        try:
            rank = int(hw_in["camera_preference_rank"])
        except (TypeError, ValueError):
            pass
    hardware_row = {
        **(current.get("hardware") if isinstance(current.get("hardware"), dict) else {}),
        **{k: hw_in[k] for k in hw_in if k in {"model", "machine", "chip"}},
        "model": machine or None,
        "camera_quality": quality,
        "camera_preference_rank": rank,
    }
    permissions_row = {
        **(current.get("permissions") if isinstance(current.get("permissions"), dict) else {}),
        **{str(k)[:32]: str(v)[:32] for k, v in perm_in.items()},
    }
    profile = {
        "hardware": hardware_row,
        "permissions": permissions_row,
        "reported_at": utcnow().isoformat(),
    }
    device.endpoint_profile = profile
    return profile


def _camera_rank(device: Device) -> int:
    profile = getattr(device, "endpoint_profile", None) or {}
    hardware = profile.get("hardware") if isinstance(profile, dict) else {}
    if isinstance(hardware, dict) and hardware.get("camera_preference_rank") is not None:
        try:
            return int(hardware["camera_preference_rank"])
        except (TypeError, ValueError):
            pass
    machine = ""
    if isinstance(hardware, dict):
        machine = str(hardware.get("model") or hardware.get("machine") or "")
    return camera_quality_for_machine(machine)[1]


def _camera_permission(device: Device) -> str:
    profile = getattr(device, "endpoint_profile", None) or {}
    perms = profile.get("permissions") if isinstance(profile, dict) else {}
    if not isinstance(perms, dict):
        return "unknown"
    raw = str(perms.get("camera") or perms.get("NSCamera") or "").lower()
    if raw in {"granted", "authorized", "allowed"}:
        return "granted"
    if raw in {"denied", "restricted"}:
        return "denied"
    if raw in {"undetermined", "not_determined", "prompt"}:
        return "undetermined"
    return "unknown"


def wants_perception(text: str) -> bool:
    return bool(_LOOK.search(text or ""))


def perception_action(text: str) -> str:
    low = (text or "").lower()
    if "ocr" in low or "read this" in low or "read the text" in low:
        return "ocr"
    if "record" in low and "clip" in low:
        return "record_clip"
    if "photo" in low or "picture" in low:
        return "capture_photo"
    if "observe" in low:
        return "observe"
    return "look_once"


def explicit_this_phone(text: str) -> bool:
    return bool(_THIS_PHONE.search(text or ""))


async def resolve_camera_target(
    session: AsyncSession,
    *,
    origin: Device,
    text: str = "",
) -> dict[str, Any]:
    """Pick a camera with hardware evidence. Origin only when explicitly requested."""

    if explicit_this_phone(text):
        return {
            "ok": True,
            "device": origin,
            "device_id": str(origin.id),
            "display_name": origin.name,
            "reason": "explicit_this_phone",
            "permission": _camera_permission(origin),
            "freshness": presence_state(origin),
            "provenance": "owner_utterance",
        }

    rows = list((await session.execute(select(Device).where(Device.revoked_at.is_(None)))).scalars().all())
    phones = [
        d
        for d in rows
        if (d.device_type or "").lower() == "phone"
        or (d.role or "").endswith("companion")
        or "camera" in (d.capabilities or [])
    ]
    if not phones:
        phones = [origin]

    def _eligible(device: Device) -> bool:
        if str(getattr(device, "memory_scope", "") or "").lower() == "sandbox":
            return False
        perm = _camera_permission(device)
        if perm == "denied":
            return False
        return True

    trusted = [d for d in phones if _eligible(d)] or phones
    online = [d for d in trusted if presence_state(d) == "ONLINE"]
    pool = online or trusted
    pool.sort(key=lambda d: (_camera_rank(d), presence_state(d) != "ONLINE", str(d.id)))
    chosen = pool[0]
    reason = "preferred_hardware" if _camera_rank(chosen) == 0 else "fallback_hardware"
    if chosen.id == origin.id and _camera_rank(chosen) != 0:
        reason = "origin_available"
    return {
        "ok": True,
        "device": chosen,
        "device_id": str(chosen.id),
        "display_name": chosen.name,
        "reason": reason,
        "permission": _camera_permission(chosen),
        "freshness": presence_state(chosen),
        "provenance": "endpoint_profile",
        "camera_quality": (getattr(chosen, "endpoint_profile", None) or {}).get("hardware", {}).get("camera_quality")
        if isinstance(getattr(chosen, "endpoint_profile", None), dict)
        else None,
        "rank": _camera_rank(chosen),
    }
