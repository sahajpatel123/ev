"""Follow-Me layer tests: intent bus, resolver, primary arbiter.

Offline and deterministic; no DB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.everywhere.followme import (
    FollowMeBus,
    pick_primary,
    resolve_target,
)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def _dev(device_id, presence, caps=(), device_type="phone", age_s=30, revoked=False):
    return {
        "device_id": device_id,
        "presence_state": presence,
        "capabilities": list(caps),
        "device_type": device_type,
        "last_seen_at": (NOW - timedelta(seconds=age_s)).isoformat(),
        "trust_state": "revoked" if revoked else "TRUSTED_OWNER_DEVICE",
    }


def test_post_and_ack_lifecycle():
    bus = FollowMeBus()
    intent = bus.post(kind="nudge", capability=None, args={"text": "hi"}, source_device_id="mac")
    assert intent["status"] == "PENDING"
    assert intent["capability"] == "nudge"
    acked = bus.ack(intent["id"], device_id="iphone16", status="DONE", note="shown")
    assert acked["status"] == "DONE"
    assert acked["receipts"][0]["device_id"] == "iphone16"
    assert bus.list(status="DONE")[0]["id"] == intent["id"]


def test_unknown_kind_and_bad_ttl_rejected():
    bus = FollowMeBus()
    with pytest.raises(ValueError):
        bus.post(kind="self.destruct", capability=None, args={}, source_device_id="mac")
    with pytest.raises(ValueError):
        bus.post(kind="nudge", capability=None, args={}, source_device_id="mac", ttl_seconds=0)


def test_ack_unknown_id_and_bad_status():
    bus = FollowMeBus()
    with pytest.raises(KeyError):
        bus.ack("nope", device_id="x", status="DONE")
    intent = bus.post(kind="nudge", capability=None, args={}, source_device_id="mac")
    with pytest.raises(ValueError):
        bus.ack(intent["id"], device_id="x", status="PENDING")


def test_expiry():
    bus = FollowMeBus()
    intent = bus.post(
        kind="nudge", capability=None, args={}, source_device_id="mac", ttl_seconds=1
    )
    intent["expires_at"] = (NOW - timedelta(seconds=5)).isoformat()
    bus._intents[intent["id"]] = intent
    listed = bus.list()
    assert listed[0]["status"] == "EXPIRED"


def test_resolve_target_prefers_online_and_recent():
    cands = [
        _dev("se", "RECENTLY_SEEN", caps=["camera"], age_s=120),
        _dev("16pro", "ONLINE", caps=["camera"], age_s=5),
        _dev("mac", "ONLINE", caps=["camera"], device_type="laptop", age_s=1),
    ]
    # Mac is newer but phones and recency tie-break: both online → recency wins → mac (1s)
    assert resolve_target("camera", cands, now=NOW)["device_id"] == "mac"
    cands[2]["presence_state"] = "DEGRADED"
    assert resolve_target("camera", cands, now=NOW)["device_id"] == "16pro"


def test_resolve_target_requires_capability_and_non_revoked():
    cands = [
        _dev("se", "ONLINE", caps=[], revoked=False),
        _dev("16pro", "ONLINE", caps=["camera"], revoked=True),
        _dev("mac", "ONLINE", caps=["camera"], device_type="laptop"),
    ]
    assert resolve_target("camera", cands, now=NOW)["device_id"] == "mac"
    assert resolve_target("lidar", cands, now=NOW) is None


def test_pick_primary_online_phone_wins():
    cands = [
        _dev("mac", "ONLINE", device_type="laptop", age_s=1),
        _dev("16pro", "ONLINE", device_type="phone", age_s=10),
        _dev("se", "OFFLINE", device_type="phone", age_s=400),
    ]
    assert pick_primary(cands, now=NOW)["device_id"] == "16pro"


def test_pick_primary_recently_seen_when_all_offline():
    cands = [
        _dev("mac", "OFFLINE", device_type="laptop", age_s=10),
        _dev("se", "RECENTLY_SEEN", device_type="phone", age_s=600),
    ]
    assert pick_primary(cands, now=NOW)["device_id"] == "se"


def test_pick_primary_empty():
    assert pick_primary([], now=NOW) is None
