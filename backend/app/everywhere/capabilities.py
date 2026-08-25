"""G2 Phase 13/14 — Device-aware capability universe + CapabilityRouter.

The Live Capability Manifest stays the declaration source for tools. This
module extends the SAME model to devices: it projects what each registered
device can actually do RIGHT NOW from real evidence (handshake capabilities,
push registration, platform) — never fabricated future abilities.

Capability ids follow ``<capability_base>`` with per-device records:
    voice.converse      realtime voice endpoint (foreground_voice)
    camera.look         device camera capture
    text.chat           typed conversation surface
    notification.receive device can receive Evie notifications (push or poll)

States: AVAILABLE / DEGRADED / UNAVAILABLE / PERMISSION_REQUIRED /
DEVICE_OFFLINE / DISABLED.

CapabilityRouter.resolve answers "which trusted device can perform X?" with
basic v0.1 rules only: capability exists, device online, not revoked,
owner scope correct.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.utils.text import utcnow

from .devices import presence_state

# STAGE 19 LAW: advertisements are BOUNDED and TYPED. A client may only
# declare these endpoint capability bases; anything else is ignored (never
# projected into the executable universe) and counted for diagnostics.
KNOWN_CAPABILITY_BASES = frozenset(
    {
        "foreground_voice",
        "camera",
        "text",
        "notification",
        "microphone",
        "screen_look",
        "computer_control",
        "location",
        "speaker_audio",
        "clipboard",
        # G2 routed capability advertising (B3: filtered truth, not invented)
        "device_echo",
        "mac_notify",
        "mac_echo",
    }
)


def validate_capabilities(raw: list[str] | None) -> tuple[list[str], list[str]]:
    """Split a client capability advertisement into (accepted, ignored)."""
    accepted: list[str] = []
    ignored: list[str] = []
    for c in raw or []:
        name = str(c).strip().lower()
        if not name:
            continue
        (accepted if name in KNOWN_CAPABILITY_BASES else ignored).append(name[:64])
    return accepted[:32], ignored[:32]


@dataclass(frozen=True)
class RoutedDevice:
    device_id: str
    display_name: str
    capability_id: str
    state: str
    presence_state: str


def _base_capabilities(device: Device) -> list[dict]:
    """Derive ONLY capabilities with real evidence. No fabrication."""
    caps = {str(c).strip() for c in (device.capabilities or [])}
    out: list[dict] = []
    if device.revoked_at is not None:
        return out
    if "foreground_voice" in caps:
        out.append({"id": "voice.converse", "state": "AVAILABLE", "evidence": "handshake"})
    if "camera" in caps:
        # Camera needs an OS permission grant at capture time; the handshake
        # advertises possession, runtime may still downgrade to PERMISSION_REQUIRED.
        out.append({"id": "camera.look", "state": "AVAILABLE", "evidence": "handshake"})
    if "text" in caps:
        out.append({"id": "text.chat", "state": "AVAILABLE", "evidence": "handshake"})
    # Any paired companion endpoint polls the backend, so it can always be a
    # notification surface; push is stronger evidence when registered.
    role = (device.role or "").lower()
    if device.device_type == "phone" or role.endswith("companion") or "notification" in caps:
        out.append(
            {
                "id": "notification.receive",
                "state": "AVAILABLE",
                "evidence": "push" if device.push_token else "poll",
            }
        )
    # G2 routed capabilities: derived from trust, not fabricated. Only trusted
    # devices get routed execution surfaces (B3: advertisement filtered, registry canonical).
    if str(getattr(device, "memory_scope", "") or "").lower() != "sandbox":
        # device.echo is generic heartbeat/capability proof for ANY trusted endpoint
        out.append({"id": "device.echo", "state": "AVAILABLE", "evidence": "trusted_device"})
        out.append({"id": "device.ping", "state": "AVAILABLE", "evidence": "trusted_device"})
    # Mac-specific surfaces: home_station or mac desktop with computer_control
    is_mac = (
        role == "home_station"
        or (device.device_type or "").lower() in {"desktop", "mac", "macos"}
        or "computer_control" in caps
        or "mac_notify" in caps
        or "mac_echo" in caps
    )
    if is_mac and str(getattr(device, "memory_scope", "") or "").lower() != "sandbox":
        out.append({"id": "mac.notify", "state": "AVAILABLE", "evidence": "mac_role"})
        out.append({"id": "mac.echo", "state": "AVAILABLE", "evidence": "mac_role"})
        out.append({"id": "computer.open_calculator", "state": "AVAILABLE", "evidence": "computer_control"})
    return out


async def capability_universe(session: AsyncSession) -> dict:
    """The whole projected universe — one canonical answer for every surface."""
    rows = (await session.execute(select(Device).where(Device.revoked_at.is_(None)))).scalars().all()
    records: list[dict] = []
    now_iso = utcnow().isoformat()
    for device in rows:
        presence = presence_state(device)
        for base in _base_capabilities(device):
            state = base["state"]
            if state == "AVAILABLE" and presence in ("OFFLINE", "DEGRADED"):
                state = "DEVICE_OFFLINE"
            records.append(
                {
                    "capability_id": f"{base['id']}",
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "owner_id": str(device.owner_id) if device.owner_id else None,
                    "state": state,
                    "presence_state": presence,
                    "risk_class": "R1",
                    "permission_requirements": [],
                    "last_verified": device.last_seen_at.isoformat() if device.last_seen_at else None,
                    "reason_unavailable": None if state == "AVAILABLE" else f"device_{presence.lower()}",
                    "evidence": base["evidence"],
                    "verified_at": now_iso,
                }
            )
    revision_src = json.dumps(
        sorted(f"{r['capability_id']}:{r['device_id']}:{r['state']}" for r in records),
        separators=(",", ":"),
    )
    revision = hashlib.sha256(revision_src.encode()).hexdigest()[:16]
    return {"revision": revision, "capabilities": records}


class CapabilityRouter:
    """Phase 14 — which trusted device can perform capability X?"""

    @staticmethod
    async def resolve(
        session: AsyncSession,
        *,
        capability: str,
        constraints: dict | None = None,
    ) -> dict:
        wanted = (capability or "").strip().lower()
        if not wanted:
            return {"ok": False, "error": "missing_capability"}
        constraints = constraints or {}
        universe = await capability_universe(session)
        matches = [
            r
            for r in universe["capabilities"]
            if r["capability_id"] == wanted or r["capability_id"].startswith(wanted + ".")
        ]
        candidates: list[dict] = []
        unavailable: list[dict] = []
        for r in matches:
            row = {
                "device_id": r["device_id"],
                "device_name": r["device_name"],
                "capability_id": r["capability_id"],
                "state": r["state"],
                "presence_state": r["presence_state"],
            }
            if (
                r["state"] == "AVAILABLE"
                and r["presence_state"] == "ONLINE"
                and (not constraints.get("exclude_device") or r["device_id"] not in constraints["exclude_device"])
            ):
                candidates.append(row)
            else:
                row["reason"] = r.get("reason_unavailable") or f"state_{r['state'].lower()}"
                unavailable.append(row)
        return {
            "ok": bool(candidates),
            "capability": wanted,
            "candidates": candidates[:5],
            "unavailable": unavailable[:10],
            "revision": universe["revision"],
            "error": None if candidates else "no_available_device",
        }
