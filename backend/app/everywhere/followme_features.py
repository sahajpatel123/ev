"""Follow-Me feature services: Pocket Executive, Look, Clipboard, Remote,
Presence HUD, Wake Mirror.

All deterministic, offline, honest about in-memory/bounded state. Models
never route devices; the bus receipt is the audit trail.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.everywhere.followme import _utcnow, bus, pick_primary, resolve_target
from app.everywhere.mesh import mesh_store

# ---------------------------------------------------------------- Clipboard

CLIPBOARD_MAX = 50
CLIPBOARD_DEFAULT_TTL = 1800
_clipboard: list[dict] = []
_clipboard_lock = threading.Lock()


def clipboard_push(*, source_device_id: str, kind: str, text: str, ttl_seconds: int) -> dict:
    if kind not in {"text", "url", "image_ref"}:
        raise ValueError("bad clipboard kind")
    if not text.strip():
        raise ValueError("empty clipboard text")
    now = _utcnow()
    item = {
        "id": str(uuid4()),
        "source_device_id": source_device_id,
        "kind": kind,
        "text": text[:4000],
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    with _clipboard_lock:
        _clipboard.insert(0, item)
        del _clipboard[CLIPBOARD_MAX:]
    bus.post(
        kind="clipboard.push",
        capability="clipboard",
        args={"item_id": item["id"], "kind": kind, "text": item["text"][:280]},
        source_device_id=source_device_id,
        ttl_seconds=ttl_seconds,
    )
    return item


def clipboard_list() -> list[dict]:
    now = _utcnow()
    with _clipboard_lock:
        out = []
        for item in _clipboard:
            try:
                expires = datetime.fromisoformat(item["expires_at"])
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires > now:
                out.append(dict(item))
        return out


def clipboard_clear() -> None:
    with _clipboard_lock:
        _clipboard.clear()


# ------------------------------------------------------------ Pocket Exec

HEADING_OUT_BUNDLE = [
    "se.remote_mode",
    "mac.lock_down",
    "digest.pause",
    "wake.follow_phone",
]


def heading_out(*, source_device_id: str | None, candidates: list[dict]) -> dict:
    """Deterministic heading-out bundle: posts the follow-me intents that
    would carry it out, returns the plan plus the intents for the receipt
    log. Unknown/offline devices are reported, not hidden."""
    primary = pick_primary(candidates)
    intents = []
    focus = bus.post(
        kind="focus.handoff",
        capability="focus.handoff",
        args={"bundle": HEADING_OUT_BUNDLE, "mode": "heading_out"},
        source_device_id=source_device_id,
        target=primary,
    )
    intents.append(focus)
    media = bus.post(
        kind="media.duck",
        capability="media",
        args={"mode": "pause_on_leaving"},
        source_device_id=source_device_id,
        target=resolve_target("media", candidates) or primary,
    )
    intents.append(media)
    return {
        "bundle": list(HEADING_OUT_BUNDLE),
        "primary_device_id": (primary or {}).get("device_id"),
        "intents": intents,
    }


# ---------------------------------------------------------------- Look


def request_look(*, source_device_id: str | None, reason: str, kind: str, candidates: list[dict]) -> dict:
    if kind not in {"photo", "screen"}:
        raise ValueError("kind must be photo|screen")
    capability = "camera" if kind == "photo" else "screen"
    target = resolve_target(capability, candidates)
    intent = bus.post(
        kind="look.request",
        capability=capability,
        args={"reason": reason[:280], "kind": kind},
        source_device_id=source_device_id,
        target=target,
    )
    return {"intent_id": intent["id"], "target_device_id": (target or {}).get("device_id"), "intent": intent}


# --------------------------------------------------------------- Remote

REMOTE_ACTIONS = frozenset({"device.echo", "device.ping", "mac.notify", "photo.capture"})


def request_remote(*, source_device_id: str | None, action: str, device_id: str | None, candidates: list[dict]) -> dict:
    if action not in REMOTE_ACTIONS:
        raise ValueError("unknown remote action")
    target = None
    if device_id:
        target = next((c for c in candidates if c.get("device_id") == device_id), None)
        if target is None:
            raise KeyError(device_id)
    else:
        target = resolve_target(action, candidates) or pick_primary(candidates)
    intent = bus.post(
        kind="nudge",
        capability=action,
        args={"action": action},
        source_device_id=source_device_id,
        target=target,
    )
    return {"request_id": intent["id"], "status": intent["status"], "target_device_id": (target or {}).get("device_id"), "intent": intent}


# ------------------------------------------------------------- HUD


_PRESENCE_RANK = {"ONLINE": 0, "RECENTLY_SEEN": 1, "DEGRADED": 2, "OFFLINE": 3}


def _presence_rank(candidate: dict) -> int:
    return _PRESENCE_RANK.get(str(candidate.get("presence_state") or "").upper(), 3)


def presence_hud(candidates: list[dict]) -> dict:
    receipts: dict[str, list[str]] = {}
    for intent in bus.list():
        target = intent.get("target_device_id")
        if target:
            receipts.setdefault(target, [])
            if intent["kind"] not in receipts[target]:
                receipts[target].append(intent["kind"])
    # One card per physical device: the registry can briefly hold two rows
    # for the same endpoint (a paired registration plus an auto-created
    # heartbeat shadow with the same display name). Keep the strongest
    # presence, then the most recent contact; merge receipts across rows so
    # no activity is hidden by the collapse.
    chosen: dict[str, dict] = {}
    merged_receipts: dict[str, list[str]] = {}
    for c in candidates:
        if c.get("trust_state") == "revoked":
            continue
        name = str(c.get("display_name") or c.get("device_id") or "")
        bucket = merged_receipts.setdefault(name, [])
        for kind in receipts.get(str(c.get("device_id")), []):
            if kind not in bucket:
                bucket.append(kind)
        prev = chosen.get(name)
        if prev is None:
            chosen[name] = c
            continue
        better_presence = _presence_rank(c) < _presence_rank(prev)
        same_presence_newer = (
            _presence_rank(c) == _presence_rank(prev)
            and str(c.get("last_seen_at") or "") > str(prev.get("last_seen_at") or "")
        )
        if better_presence or same_presence_newer:
            chosen[name] = c
    devices = []
    for name, c in chosen.items():
        battery = c.get("battery_percent")
        if battery is None:
            # Evie Mesh: a BLE advertisement fills the known
            # battery=null gap instead of leaving it empty.
            advertisement = mesh_store.advertisement_for(str(c.get("device_id")))
            battery = (advertisement or {}).get("battery_percent")
        devices.append(
            {
                "device_id": c.get("device_id"),
                "display_name": c.get("display_name"),
                "device_type": c.get("device_type"),
                "presence_state": c.get("presence_state"),
                "last_seen_at": c.get("last_seen_at"),
                "recent_intent_kinds": merged_receipts.get(name, [])[:5],
                "battery_percent": battery,
            }
        )
    primary = pick_primary(candidates)
    return {
        "devices": devices,
        "primary_device_id": (primary or {}).get("device_id"),
        "generated_at": _utcnow().isoformat(),
    }


# ----------------------------------------------------------- Wake mirror


def wake_mirror(*, source_device_id: str | None, thread_id: str, device_label: str) -> dict:
    if not thread_id.strip():
        raise ValueError("thread_id required")
    intent = bus.post(
        kind="wake.mirror",
        capability="wake.mirror",
        args={"thread_id": thread_id, "device_label": device_label},
        source_device_id=source_device_id,
    )
    return {
        "intent_id": intent["id"],
        "mirror_thread_id": f"{thread_id}:mirror",
        "intent": intent,
    }
