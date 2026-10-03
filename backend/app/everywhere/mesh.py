"""Evie Mesh — the ambient cross-device layer (2026-10-02).

Everything here is a deterministic projection over real evidence:
BLE proximity observations, self-advertisements (battery, low-power,
capabilities), and typed Follow-Me intents. No model routes devices,
nothing is fabricated, and every answer is honest about what it does
not know (unknown proximity stays UNKNOWN, never a guess).

The store is in-memory and bounded, exactly like the Follow-Me bus:
durability is the intent receipts, not a database table.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.everywhere.followme import _utcnow, bus, pick_primary, resolve_target

# Stable service UUID every EV device advertises over BLE (CoreBluetooth
# peripheral mode). One UUID, one mesh, no cloud relay.
EV_MESH_SERVICE_UUID = "9F5E2C7A-4B1D-4E63-8A0F-2C7D1B3E5A90"

# ---------------------------------------------------------------- Zones

ZONE_RANK = {"immediate": 0, "near": 1, "far": 2, "edge": 3, "unknown": 4}
UNKNOWN_ZONE = "unknown"

OBSERVATION_TTL_SECONDS = 60
ADVERTISEMENT_TTL_SECONDS = 300
MAX_OBSERVATIONS = 2000
MAX_ADVERTISEMENTS = 64
MAX_OBSERVATIONS_PER_POST = 64


def zone_for_rssi(rssi: float | None) -> str:
    """RSSI -> proximity zone. Bounded, deterministic, no ML."""
    if rssi is None:
        return UNKNOWN_ZONE
    if rssi >= -55:
        return "immediate"
    if rssi >= -70:
        return "near"
    if rssi >= -85:
        return "far"
    return "edge"


def proximity_rank(zone: str | None) -> int:
    return ZONE_RANK.get(zone or UNKNOWN_ZONE, 4)


# ------------------------------------------------------------- Store


class ProximityStore:
    """Bounded, thread-safe, TTL-expiring mesh evidence.

    Two kinds of truth: observations (observer X saw subject Y at
    zone Z) and advertisements (device Y says its battery is N% and
    it can do caps C). Both expire; neither pretends to be permanent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observations: dict[tuple[str, str], dict] = {}
        self._advertisements: dict[str, dict] = {}

    def observe(
        self,
        *,
        observer_device_id: str,
        subject_device_id: str,
        rssi: float | None = None,
        battery_percent: float | None = None,
        capabilities: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict:
        if not observer_device_id.strip() or not subject_device_id.strip():
            raise ValueError("observer and subject device ids required")
        if observer_device_id == subject_device_id:
            raise ValueError("a device cannot observe itself")
        if rssi is not None and not (-120.0 <= rssi <= 0.0):
            raise ValueError("rssi out of range")
        stamp = now or _utcnow()
        zone = zone_for_rssi(rssi)
        record = {
            "observer_device_id": observer_device_id,
            "subject_device_id": subject_device_id,
            "rssi": rssi,
            "zone": zone,
            "battery_percent": battery_percent,
            "capabilities": [str(c)[:64] for c in (capabilities or [])][:32],
            "observed_at": stamp.isoformat(),
        }
        with self._lock:
            self._prune_locked(stamp)
            self._observations[(observer_device_id, subject_device_id)] = record
            if len(self._observations) > MAX_OBSERVATIONS:
                oldest = min(
                    self._observations.items(),
                    key=lambda kv: kv[1]["observed_at"],
                )[0]
                del self._observations[oldest]
        return dict(record)

    def advertise(
        self,
        *,
        device_id: str,
        battery_percent: float | None = None,
        low_power: bool = False,
        capabilities: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict:
        if not device_id.strip():
            raise ValueError("device_id required")
        if battery_percent is not None and not (0.0 <= battery_percent <= 100.0):
            raise ValueError("battery_percent out of range")
        stamp = now or _utcnow()
        record = {
            "device_id": device_id,
            "battery_percent": battery_percent,
            "low_power": bool(low_power),
            "capabilities": [str(c)[:64] for c in (capabilities or [])][:32],
            "advertised_at": stamp.isoformat(),
        }
        with self._lock:
            self._prune_locked(stamp)
            self._advertisements[device_id] = record
            if len(self._advertisements) > MAX_ADVERTISEMENTS:
                oldest = min(
                    self._advertisements.items(),
                    key=lambda kv: kv[1]["advertised_at"],
                )[0]
                del self._advertisements[oldest]
        return dict(record)

    def matrix(self) -> dict:
        with self._lock:
            observations = [dict(v) for v in self._observations.values()]
            advertisements = {k: dict(v) for k, v in self._advertisements.items()}
        return {
            "observations": observations,
            "advertisements": advertisements,
        }

    def zone_between(self, observer_device_id: str, subject_device_id: str) -> str:
        with self._lock:
            record = self._observations.get((observer_device_id, subject_device_id))
        return (record or {}).get("zone") or UNKNOWN_ZONE

    def nearest(
        self, observer_device_id: str, limit: int = 3
    ) -> list[dict]:
        limit = max(1, min(int(limit), 16))
        with self._lock:
            seen = [
                dict(v)
                for (observer, _subject), v in self._observations.items()
                if observer == observer_device_id
            ]
        seen.sort(key=lambda r: (proximity_rank(r.get("zone")), -(r.get("rssi") or -120.0)))
        return seen[:limit]

    def advertisements(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._advertisements.items()}

    def advertisement_for(self, device_id: str) -> dict | None:
        with self._lock:
            record = self._advertisements.get(device_id)
        return dict(record) if record else None

    def counts(self) -> dict:
        with self._lock:
            return {
                "observations": len(self._observations),
                "advertisements": len(self._advertisements),
            }

    def zone_summary(self) -> dict[str, int]:
        with self._lock:
            records = list(self._observations.values())
        summary = {zone: 0 for zone in ZONE_RANK}
        for record in records:
            summary[record.get("zone") or UNKNOWN_ZONE] += 1
        return summary

    def proximity_map(
        self,
        observer_device_id: str,
        device_ids: list[str] | None = None,
    ) -> dict[str, int]:
        """subject_device_id -> zone rank, for one observer.

        This is the only proximity projection the resolvers accept:
        "who is physically near THIS device right now".

        BLE peers advertise only an 8-char short id (``ev-<shortId>``),
        so callers that know the registry's full device ids pass them in
        ``device_ids`` and receive full-id keys resolved from the short
        subject keys. Without this, proximity could never bias targeting
        against registry UUIDs.
        """
        with self._lock:
            records = [
                v
                for (observer, _subject), v in self._observations.items()
                if observer == observer_device_id
            ]
        ranks = {
            r["subject_device_id"]: proximity_rank(r.get("zone")) for r in records
        }
        for device_id in device_ids or []:
            if device_id in ranks:
                continue
            alias = _resolve_short_alias(device_id, ranks)
            if alias is not None:
                ranks[device_id] = ranks[alias]
        return ranks

    def reset(self) -> None:
        with self._lock:
            self._observations.clear()
            self._advertisements.clear()

    def _prune_locked(self, now: datetime) -> None:
        for obs_key, record in list(self._observations.items()):
            expires = _parse_ts(record.get("observed_at"))
            if expires is not None and expires + timedelta(seconds=OBSERVATION_TTL_SECONDS) <= now:
                del self._observations[obs_key]
        for ad_key, record in list(self._advertisements.items()):
            expires = _parse_ts(record.get("advertised_at"))
            if expires is not None and expires + timedelta(seconds=ADVERTISEMENT_TTL_SECONDS) <= now:
                del self._advertisements[ad_key]


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


mesh_store = ProximityStore()


# ------------------------------------------------- Resolver integration


def _with_proximity(
    candidates: list[dict],
    proximity: dict[str, int] | None,
) -> dict[str, int]:
    """Merge a caller-supplied proximity map (already observer-scoped)."""
    return {str(k): int(v) for k, v in (proximity or {}).items()}


def _resolve_short_alias(device_id: str, known: dict[str, int]) -> str | None:
    """Find the BLE short-id key that resolves to a full registry id.

    Peers advertise only ``ev-<first 8 chars>``; accept an 8+ char
    prefix relationship in either direction so a full UUID finds its
    advertised short key and vice versa.
    """
    for subject in known:
        if len(subject) >= 8 and device_id.startswith(subject):
            return subject
        if len(device_id) >= 8 and subject.startswith(device_id):
            return subject
    return None


# ------------------------------------------------------------ Targeting


def converge_targets(
    candidates: list[dict],
    *,
    proximity: dict[str, int] | None = None,
) -> list[dict]:
    """Every live device a converge beacon should wake, nearest first."""
    live = [
        c
        for c in candidates
        if c.get("trust_state") != "revoked"
        and (c.get("presence_state") or "OFFLINE") != "OFFLINE"
    ]
    ranks = _with_proximity(candidates, proximity)

    def key(c: dict) -> tuple:
        device_id = str(c.get("device_id") or "")
        seen = str(c.get("last_seen_at") or "")
        return (ranks.get(device_id, 4), seen, device_id)

    return sorted(live, key=key)


def escalation_chain(
    candidates: list[dict],
    *,
    proximity: dict[str, int] | None = None,
) -> list[dict]:
    """Deterministic nudge escalation order.

    Nearest device first (that is where the owner is), then phones,
    then watches, then desktops. Auditable, stable, no guessing.
    """
    live = [
        c
        for c in candidates
        if c.get("trust_state") != "revoked"
        and (c.get("presence_state") or "OFFLINE") != "OFFLINE"
    ]
    ranks = _with_proximity(candidates, proximity)

    def key(c: dict) -> tuple:
        device_id = str(c.get("device_id") or "")
        kind = str(c.get("device_type") or "").lower()
        tier = 0 if kind in {"phone", "iphone", "ios"} else 1 if kind == "watch" else 2
        return (
            ranks.get(device_id, 4),
            tier,
            str(c.get("last_seen_at") or ""),
            device_id,
        )

    return sorted(live, key=key)


def _camera_target(
    candidates: list[dict],
    *,
    proximity: dict[str, int] | None = None,
) -> dict | None:
    """Best camera device: proximity, then presence, then camera rank.

    Mirrors the CapabilityRouter's camera_preference_rank rule so the
    16 Pro (rank 0) wins when it is reachable and near.
    """
    ranks = _with_proximity(candidates, proximity)
    online = [
        c
        for c in candidates
        if "camera" in (c.get("capabilities") or [])
        and (c.get("presence_state") or "OFFLINE") != "OFFLINE"
        and c.get("trust_state") != "revoked"
    ]
    if not online:
        return None

    def key(c: dict) -> tuple:
        device_id = str(c.get("device_id") or "")
        camera_rank = c.get("camera_preference_rank")
        seen = str(c.get("last_seen_at") or "")
        return (
            ranks.get(device_id, 4),
            camera_rank is None,
            camera_rank if camera_rank is not None else 99,
            seen,
            device_id,
        )

    return sorted(online, key=key)[0]


def _mac_target(
    candidates: list[dict],
    *,
    proximity: dict[str, int] | None = None,
) -> dict | None:
    """The Mac (or any computer_control endpoint) to run a verb on."""
    ranks = _with_proximity(candidates, proximity)
    macs = [
        c
        for c in candidates
        if (
            "computer_control" in (c.get("capabilities") or [])
            or str(c.get("role") or "").lower() == "home_station"
            or str(c.get("device_type") or "").lower() in {"desktop", "mac", "macos", "laptop"}
        )
        and (c.get("presence_state") or "OFFLINE") != "OFFLINE"
        and c.get("trust_state") != "revoked"
    ]
    if not macs:
        return None

    def key(c: dict) -> tuple:
        device_id = str(c.get("device_id") or "")
        return (ranks.get(device_id, 4), str(c.get("last_seen_at") or ""), device_id)

    return sorted(macs, key=key)[0]


# ------------------------------------------------------------ Vertices


def converge_beacon(
    *,
    source_device_id: str | None,
    reason: str,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Broadcast: every live device chimes, flashes, haptics.

    Broadcast intent (no single target) — each device acks its own
    receipt, so the receipt log IS the convergence proof.
    """
    intent = bus.post(
        kind="converge.beacon",
        capability="converge",
        args={"reason": reason[:280], "pattern": "chime"},
        source_device_id=source_device_id,
        target=None,
        ttl_seconds=60,
    )
    targets = converge_targets(candidates, proximity=proximity)
    return {
        "intent_id": intent["id"],
        "targets": [str(t.get("device_id")) for t in targets],
        "target_count": len(targets),
        "intent": intent,
    }


def request_photo_capture(
    *,
    source_device_id: str | None,
    reason: str,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Ask the best camera device to capture, with provenance.

    The capturing device returns the frame through the existing
    capture pipeline; the intent args carry the requesting device so
    the memory records WHO captured and WHO asked.
    """
    target = _camera_target(candidates, proximity=proximity) or resolve_target(
        "camera", candidates, proximity=proximity
    )
    intent = bus.post(
        kind="photo.capture",
        capability="camera",
        args={"reason": reason[:280], "requester_device_id": source_device_id},
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=120,
    )
    return {
        "intent_id": intent["id"],
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


def request_shortcut_run(
    *,
    source_device_id: str | None,
    shortcut: str,
    args: dict | None,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Run a named Shortcut/AppIntent on the best shortcut-capable device."""
    target = resolve_target("shortcut", candidates, proximity=proximity) or pick_primary(
        candidates, proximity=proximity
    )
    clean_args = {
        str(k)[:128]: str(v)[:2000] for k, v in (args or {}).items()
    }
    intent = bus.post(
        kind="shortcut.run",
        capability="shortcut",
        args={"shortcut": shortcut[:128], "args": clean_args},
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=300,
    )
    return {
        "intent_id": intent["id"],
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


# Sensors a phone can honestly read. GPS and flashlight are R2: they
# need explicit per-read consent on the device, never silently.
SENSOR_REGISTRY: dict[str, dict] = {
    "barometer": {"risk": "R1", "description": "Barometric pressure (both iPhones carry one)"},
    "altitude": {"risk": "R1", "description": "Relative altitude from the barometer"},
    "battery": {"risk": "R1", "description": "Battery percent and charging state"},
    "steps": {"risk": "R1", "description": "Today's step count (HealthKit, owner-approved)"},
    "gps": {"risk": "R2", "description": "Coarse location; per-read consent on device"},
    "flashlight": {"risk": "R2", "description": "Torch on/off; per-read consent on device"},
}


def request_sensor_read(
    *,
    source_device_id: str | None,
    sensor: str,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    if sensor not in SENSOR_REGISTRY:
        raise ValueError(f"unknown sensor: {sensor}")
    target = resolve_target("sensor", candidates, proximity=proximity) or pick_primary(
        candidates, proximity=proximity
    )
    intent = bus.post(
        kind="sensor.read",
        capability="sensor",
        args={"sensor": sensor, "risk": SENSOR_REGISTRY[sensor]["risk"]},
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=120,
    )
    return {
        "intent_id": intent["id"],
        "sensor": sensor,
        "risk": SENSOR_REGISTRY[sensor]["risk"],
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


def escalate_nudge(
    *,
    source_device_id: str | None,
    title: str,
    body: str,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """One nudge that walks the escalation chain until acked."""
    chain = escalation_chain(candidates, proximity=proximity)
    chain_ids = [str(c.get("device_id")) for c in chain]
    intent = bus.post(
        kind="nudge.escalate",
        capability="notification",
        args={"title": title[:128], "body": body[:512], "chain": chain_ids},
        source_device_id=source_device_id,
        target=chain[0] if chain else None,
        ttl_seconds=180,
    )
    return {
        "intent_id": intent["id"],
        "chain": chain_ids,
        "first_target_device_id": chain_ids[0] if chain_ids else None,
        "intent": intent,
    }


CLIPBOARD_TRANSFORMS = frozenset({"tidy", "translate", "summarize", "speak"})


def clipboard_transform(
    *,
    source_device_id: str | None,
    transform: str,
    text: str,
    ttl_seconds: int,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Route clipboard THROUGH evie: the target applies the transform
    (tidy/translate/summarize/speak) before pasting.

    The backend only validates and routes; the transform itself runs
    on the target device — never faked here.
    """
    if transform not in CLIPBOARD_TRANSFORMS:
        raise ValueError(f"unknown transform: {transform}")
    if not text.strip():
        raise ValueError("empty clipboard text")
    target = resolve_target("clipboard", candidates, proximity=proximity) or pick_primary(
        candidates, proximity=proximity
    )
    intent = bus.post(
        kind="clipboard.transform",
        capability="clipboard",
        args={
            "transform": transform,
            "text": text[:4000],
            "source_device_id": source_device_id,
        },
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=ttl_seconds,
    )
    return {
        "intent_id": intent["id"],
        "transform": transform,
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


def conversation_migrate(
    *,
    source_device_id: str | None,
    thread_id: str,
    from_device_id: str | None,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Mid-conversation handoff: same thread, new device takes over."""
    if not thread_id.strip():
        raise ValueError("thread_id required")
    target = resolve_target(
        "foreground_voice", candidates, proximity=proximity
    ) or pick_primary(candidates, proximity=proximity)
    intent = bus.post(
        kind="conversation.migrate",
        capability="foreground_voice",
        args={"thread_id": thread_id[:128], "from_device_id": from_device_id},
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=120,
    )
    return {
        "intent_id": intent["id"],
        "thread_id": thread_id,
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


# Mac verbs the native Mac app actually handles (MacControlService).
# R1 = safe/read-only, R2 = needs on-device approval before running.
MAC_VERBS: dict[str, str] = {
    "status": "R1",
    "list_apps": "R1",
    "inspect_ui": "R1",
    "screen_look": "R1",
    "request_accessibility": "R1",
    "cancel": "R1",
    "open_app": "R2",
    "activate_app": "R2",
    "close_app": "R2",
    "open_url": "R2",
    "app_action": "R2",
    "ui_action": "R2",
    "keyboard": "R2",
    "window_op": "R2",
    "file_op": "R2",
}


def mac_verb_risk(verb: str) -> str:
    risk = MAC_VERBS.get(str(verb).strip().lower())
    if risk is None:
        raise ValueError(f"unknown mac verb: {verb}")
    return risk


def request_mac_verb(
    *,
    source_device_id: str | None,
    verb: str,
    arguments: dict | None,
    candidates: list[dict],
    proximity: dict[str, int] | None = None,
) -> dict:
    """Run a real Mac verb from any device, approval-gated by risk.

    R2 verbs land on the Mac with an approval prompt: a human is
    always in the loop for anything that touches apps, windows,
    keyboard, files, or URLs.
    """
    verb_clean = str(verb).strip().lower()
    risk = mac_verb_risk(verb_clean)
    target = _mac_target(candidates, proximity=proximity)
    clean_args = {
        str(k)[:128]: str(v)[:2000] for k, v in (arguments or {}).items()
    }
    intent = bus.post(
        kind="mac.verb",
        capability="computer_control",
        args={"verb": verb_clean, "arguments": clean_args, "risk": risk},
        source_device_id=source_device_id,
        target=target,
        ttl_seconds=300,
    )
    return {
        "intent_id": intent["id"],
        "verb": verb_clean,
        "risk": risk,
        "approval_required": risk != "R1",
        "target_device_id": (target or {}).get("device_id"),
        "intent": intent,
    }


# ---------------------------------------------------------------- HUD


def mesh_status(candidates: list[dict]) -> dict:
    """One honest card: what the mesh knows right now."""
    matrix = mesh_store.matrix()
    intents = bus.list()
    by_kind: dict[str, int] = {}
    for intent in intents:
        by_kind[intent.get("kind") or "?"] = by_kind.get(intent.get("kind") or "?", 0) + 1
    device_ids = {str(c.get("device_id")) for c in candidates}
    return {
        "service_uuid": EV_MESH_SERVICE_UUID,
        "zones": mesh_store.zone_summary(),
        "observations": [o for o in matrix["observations"]],
        "advertisements": matrix["advertisements"],
        "known_devices": sorted(device_ids),
        "bus_intents_by_kind": by_kind,
        "mac_verbs": dict(MAC_VERBS),
        "sensors": {k: v["risk"] for k, v in SENSOR_REGISTRY.items()},
        "storage": "in-memory (bounded, TTL-expiring); receipts are the audit trail",
    }
