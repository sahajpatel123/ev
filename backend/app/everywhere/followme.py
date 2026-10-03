"""Follow-Me layer: one intent bus + one primary-device arbiter.

Creative cross-device core: any device can drop a short-lived, typed intent
onto ONE bus; a deterministic resolver picks the best online target for the
intent's capability; the target acks; every post/ack leaves a receipt.

Design laws:
- Deterministic, offline, no model routes devices.
- Honest gaps: in-memory store (bounded, TTL-expiring); durability is the
  receipts, not a database table. Never pretends to be persistent.
- No client-supplied identity: callers pass what auth gave them.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

INTENT_KINDS: frozenset[str] = frozenset(
    {
        "focus.handoff",
        "clipboard.push",
        "nudge",
        "look.request",
        "screen.share",
        "media.duck",
        "wake.mirror",
        # Evie Mesh (2026-10-02): ambient cross-device intents.
        "converge.beacon",
        "photo.capture",
        "shortcut.run",
        "sensor.read",
        "nudge.escalate",
        "clipboard.transform",
        "conversation.migrate",
        "mac.verb",
    }
)

STATUSES: frozenset[str] = frozenset({"PENDING", "CLAIMED", "DONE", "FAILED", "EXPIRED"})

MAX_INTENTS = 500
DEFAULT_TTL_SECONDS = 900

_PRESENCE_RANK = {"ONLINE": 0, "RECENTLY_SEEN": 1, "DEGRADED": 2, "OFFLINE": 3}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts

def resolve_target(
    capability: str,
    candidates: list[dict],
    *,
    now: datetime | None = None,
    proximity: dict[str, int] | None = None,
) -> dict | None:
    """Deterministic target for an intent: capability match first,
    then presence rank, then BLE proximity (nearest wins among
    reachable devices), then recency, then device_id for a stable
    tie-break. ``proximity`` is an observer-scoped device_id -> zone
    rank map (see app.everywhere.mesh)."""
    matching = [
        c
        for c in candidates
        if capability in (c.get("capabilities") or [])
        and c.get("presence_state") != "OFFLINE"
        and c.get("trust_state") != "revoked"
    ]
    if not matching:
        return None
    now = now or _utcnow()
    ranks = proximity or {}

    def sort_key(c: dict) -> tuple:
        seen = _parse_ts(c.get("last_seen_at"))
        age = (now - seen).total_seconds() if seen else float("inf")
        return (
            _PRESENCE_RANK.get(c.get("presence_state") or "OFFLINE", 3),
            ranks.get(str(c.get("device_id")), 4),
            age,
            str(c.get("device_id") or ""),
        )

    return sorted(matching, key=sort_key)[0]


def pick_primary(
    candidates: list[dict],
    *,
    now: datetime | None = None,
    proximity: dict[str, int] | None = None,
) -> dict | None:
    """Where is the owner right now? Pure projection over presence,
    with BLE proximity outranking form factor: the device the owner
    is physically nearest becomes primary, even across types."""
    live = [c for c in candidates if c.get("trust_state") != "revoked"]
    if not live:
        return None
    now = now or _utcnow()
    ranks = proximity or {}

    def sort_key(c: dict) -> tuple:
        seen = _parse_ts(c.get("last_seen_at"))
        age = (now - seen).total_seconds() if seen else float("inf")
        # Phones win ties: they are the face Evie answers on.
        is_phone = 0 if str(c.get("device_type") or "").lower() in {"phone", "iphone", "ios"} else 1
        return (
            _PRESENCE_RANK.get(c.get("presence_state") or "OFFLINE", 3),
            ranks.get(str(c.get("device_id")), 4),
            is_phone,
            age,
            str(c.get("device_id") or ""),
        )

    return sorted(live, key=sort_key)[0]


class FollowMeBus:
    """Bounded, thread-safe, TTL-expiring intent bus with receipts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._intents: dict[str, dict] = {}

    def post(
        self,
        *,
        kind: str,
        capability: str | None,
        args: dict | None,
        source_device_id: str | None,
        target: dict | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict:
        if kind not in INTENT_KINDS:
            raise ValueError(f"unknown intent kind: {kind}")
        if ttl_seconds <= 0 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds out of range")
        now = _utcnow()
        intent: dict = {
            "id": str(uuid4()),
            "kind": kind,
            "capability": capability or kind,
            "args": dict(args or {}),
            "source_device_id": source_device_id,
            "target_device_id": (target or {}).get("device_id"),
            "status": "PENDING",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "receipts": [],
        }
        with self._lock:
            self._expire_locked(now)
            if len(self._intents) >= MAX_INTENTS:
                oldest = min(self._intents.values(), key=lambda i: i["created_at"])
                del self._intents[str(oldest["id"])]
            self._intents[intent["id"]] = intent
        return dict(intent)

    def ack(self, intent_id: str, *, device_id: str | None, status: str, note: str = "") -> dict:
        if status not in STATUSES or status == "PENDING":
            raise ValueError(f"bad ack status: {status}")
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None:
                raise KeyError(intent_id)
            intent["status"] = status
            intent["receipts"].append(
                {
                    "device_id": device_id,
                    "status": status,
                    "note": note[:500],
                    "at": _utcnow().isoformat(),
                }
            )
            return dict(intent)

    def get(self, intent_id: str) -> dict | None:
        with self._lock:
            intent = self._intents.get(intent_id)
            return dict(intent) if intent else None

    def list(self, *, status: str | None = None, kind: str | None = None) -> list[dict]:
        with self._lock:
            self._expire_locked(_utcnow())
            out = list(self._intents.values())
        if status:
            out = [i for i in out if i["status"] == status]
        if kind:
            out = [i for i in out if i["kind"] == kind]
        return sorted(out, key=lambda i: i["created_at"], reverse=True)

    def reset(self) -> None:
        with self._lock:
            self._intents.clear()

    def _expire_locked(self, now: datetime) -> None:
        for intent in list(self._intents.values()):
            expires = _parse_ts(intent.get("expires_at"))
            if (
                intent["status"] in {"PENDING", "CLAIMED"}
                and expires is not None
                and expires <= now
            ):
                intent["status"] = "EXPIRED"


bus = FollowMeBus()
