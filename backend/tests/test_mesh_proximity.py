"""Evie Mesh unit tests: proximity zones, evidence store, resolver
bias, targeting helpers, and every mesh intent vertex."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.everywhere import mesh
from app.everywhere.followme import bus, pick_primary, resolve_target


@pytest.fixture(autouse=True)
def _clean():
    mesh.mesh_store.reset()
    bus.reset()
    yield
    mesh.mesh_store.reset()
    bus.reset()


def _cand(
    device_id,
    presence="ONLINE",
    caps=(),
    device_type="phone",
    age_s=10,
    camera_rank=None,
):
    record: dict = {
        "device_id": device_id,
        "display_name": device_id,
        "device_type": device_type,
        "presence_state": presence,
        "capabilities": list(caps),
        "last_seen_at": (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(),
        "trust_state": "TRUSTED_OWNER_DEVICE",
    }
    if camera_rank is not None:
        record["camera_preference_rank"] = camera_rank
    return record


# ---------------------------------------------------------------- Zones


def test_zone_for_rssi_boundaries():
    assert mesh.zone_for_rssi(None) == "unknown"
    assert mesh.zone_for_rssi(-40.0) == "immediate"
    assert mesh.zone_for_rssi(-55.0) == "immediate"
    assert mesh.zone_for_rssi(-56.0) == "near"
    assert mesh.zone_for_rssi(-70.0) == "near"
    assert mesh.zone_for_rssi(-71.0) == "far"
    assert mesh.zone_for_rssi(-85.0) == "far"
    assert mesh.zone_for_rssi(-86.0) == "edge"
    assert mesh.zone_for_rssi(-110.0) == "edge"


# ----------------------------------------------------------------- Store


def test_store_records_observation_zone_and_map():
    record = mesh.mesh_store.observe(
        observer_device_id="mac", subject_device_id="16pro", rssi=-48.0,
        battery_percent=87.0, capabilities=["camera", "mesh"],
    )
    assert record["zone"] == "immediate"
    assert mesh.mesh_store.zone_between("mac", "16pro") == "immediate"
    assert mesh.mesh_store.proximity_map("mac") == {"16pro": 0}
    assert mesh.mesh_store.nearest("mac")[0]["subject_device_id"] == "16pro"


def test_proximity_map_resolves_ble_short_ids_for_full_registry_ids():
    mesh.mesh_store.observe(
        observer_device_id="mac-full-uuid", subject_device_id="cead65bc", rssi=-52.0
    )
    ranks = mesh.mesh_store.proximity_map(
        "mac-full-uuid",
        ["cead65bc-d483-4dbe-8271-7af0eb9f9324", "6168e987-0000-0000-0000-000000000000"],
    )
    assert ranks["cead65bc"] == 0
    assert ranks["cead65bc-d483-4dbe-8271-7af0eb9f9324"] == 0
    assert "6168e987-0000-0000-0000-000000000000" not in ranks


def test_store_rejects_self_observation_and_bad_inputs():
    with pytest.raises(ValueError):
        mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="mac")
    with pytest.raises(ValueError):
        mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="", rssi=-50.0)
    with pytest.raises(ValueError):
        mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="16pro", rssi=-200.0)
    with pytest.raises(ValueError):
        mesh.mesh_store.advertise(device_id="mac", battery_percent=150.0)


def test_store_nearest_orders_by_zone_then_rssi():
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="far_phone", rssi=-80.0)
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="near_phone", rssi=-62.0)
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="close_phone", rssi=-44.0)
    nearest = mesh.mesh_store.nearest("mac")
    assert [n["subject_device_id"] for n in nearest] == ["close_phone", "near_phone", "far_phone"]


def test_store_observation_ttl_expires():
    now = datetime.now(UTC)
    mesh.mesh_store.observe(
        observer_device_id="mac", subject_device_id="16pro", rssi=-50.0,
        now=now - timedelta(seconds=mesh.OBSERVATION_TTL_SECONDS + 5),
    )
    # Any later mutation prunes the stale observation.
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="se", rssi=-60.0, now=now)
    assert mesh.mesh_store.zone_between("mac", "16pro") == "unknown"
    assert mesh.mesh_store.zone_between("mac", "se") == "near"


def test_store_advertisement_battery_and_ttl():
    now = datetime.now(UTC)
    mesh.mesh_store.advertise(
        device_id="16pro", battery_percent=42.0, low_power=True,
        capabilities=["camera", "mesh"],
        now=now - timedelta(seconds=mesh.ADVERTISEMENT_TTL_SECONDS + 1),
    )
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="se", rssi=-55.0, now=now)
    assert mesh.mesh_store.advertisement_for("16pro") is None
    mesh.mesh_store.advertise(device_id="se", battery_percent=99.0, now=now)
    assert mesh.mesh_store.advertisement_for("se")["battery_percent"] == 99.0


# ---------------------------------------------------------- Resolver bias


def test_resolve_target_prefers_nearest_reachable():
    far = _cand("far_phone", caps=["camera"], age_s=1)
    near = _cand("near_phone", caps=["camera"], age_s=50)
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="near_phone", rssi=-50.0)
    target = resolve_target("camera", [far, near], proximity={"near_phone": 0})
    assert (target or {}).get("device_id") == "near_phone"


def test_resolve_target_presence_outranks_proximity():
    # A reachable ONLINE device beats a nearer stale one.
    online = _cand("online_phone", presence="ONLINE", caps=["camera"])
    recent = _cand("recent_phone", presence="RECENTLY_SEEN", caps=["camera"])
    proximity = {"recent_phone": 0, "online_phone": 3}
    target = resolve_target("camera", [online, recent], proximity=proximity)
    assert (target or {}).get("device_id") == "online_phone"


def test_pick_primary_proximity_beats_form_factor():
    far_phone = _cand("far_phone", device_type="phone")
    near_mac = _cand("near_mac", device_type="laptop")
    proximity = {"near_mac": 0, "far_phone": 3}
    primary = pick_primary([far_phone, near_mac], proximity=proximity)
    assert (primary or {}).get("device_id") == "near_mac"


def test_pick_primary_default_unchanged_without_proximity():
    phone = _cand("phone", device_type="phone", age_s=30)
    mac = _cand("mac", device_type="laptop", age_s=1)
    primary = pick_primary([phone, mac])
    assert (primary or {}).get("device_id") == "phone"


# -------------------------------------------------------------- Targeting


def test_converge_targets_nearest_first_and_skip_offline():
    a = _cand("a", device_type="phone")
    b = _cand("b", device_type="laptop")
    offline = _cand("offline", presence="OFFLINE")
    proximity = {"b": 0, "a": 2}
    targets = mesh.converge_targets([a, b, offline], proximity=proximity)
    assert [t["device_id"] for t in targets] == ["b", "a"]


def test_escalation_chain_nearest_phone_first():
    phone = _cand("phone", device_type="phone")
    watch = _cand("watch", device_type="watch")
    mac = _cand("mac", device_type="laptop")
    proximity = {"mac": 0, "phone": 2, "watch": 3}
    chain = mesh.escalation_chain([phone, watch, mac], proximity=proximity)
    assert [c["device_id"] for c in chain] == ["mac", "phone", "watch"]


def test_camera_target_prefers_proximity_then_rank():
    ranked_far = _cand("16pro", caps=["camera"], camera_rank=0)
    unranked_near = _cand("se", caps=["camera"], camera_rank=None)
    proximity = {"se": 0, "16pro": 3}
    target = mesh._camera_target([ranked_far, unranked_near], proximity=proximity)
    assert (target or {}).get("device_id") == "se"


def test_mac_target_prefers_computer_control_endpoint():
    plain = _cand("phone", caps=[])
    mac = _cand("mac", device_type="laptop", caps=["computer_control"])
    target = mesh._mac_target([plain, mac])
    assert (target or {}).get("device_id") == "mac"


# ------------------------------------------------------------ Vertices


def test_converge_beacon_broadcasts_with_receipt_targets():
    cands = [_cand("a"), _cand("b")]
    out = mesh.converge_beacon(source_device_id="mac", reason="find me", candidates=cands)
    assert out["target_count"] == 2
    assert out["targets"] == ["a", "b"]
    intent = out["intent"]
    assert intent["kind"] == "converge.beacon"
    assert intent["target_device_id"] is None  # broadcast: every device acks
    assert intent["args"]["reason"] == "find me"


def test_photo_capture_targets_best_camera_device():
    mac = _cand("mac", device_type="laptop", caps=[])
    phone = _cand("16pro", caps=["camera"], camera_rank=0)
    out = mesh.request_photo_capture(source_device_id="mac", reason="what is this", candidates=[mac, phone])
    assert out["target_device_id"] == "16pro"
    assert out["intent"]["kind"] == "photo.capture"
    assert out["intent"]["args"]["requester_device_id"] == "mac"


def test_shortcut_run_falls_back_to_primary():
    phone = _cand("phone", caps=["text"])
    out = mesh.request_shortcut_run(
        source_device_id="mac", shortcut="Morning Brief",
        args={"time": "07:00"}, candidates=[phone],
    )
    assert out["target_device_id"] == "phone"
    assert out["intent"]["args"]["shortcut"] == "Morning Brief"
    assert out["intent"]["args"]["args"] == {"time": "07:00"}


def test_sensor_read_registry_and_validation():
    assert mesh.SENSOR_REGISTRY["gps"]["risk"] == "R2"
    assert mesh.SENSOR_REGISTRY["barometer"]["risk"] == "R1"
    with pytest.raises(ValueError):
        mesh.request_sensor_read(source_device_id="mac", sensor="thermostat", candidates=[])
    phone = _cand("phone", caps=["sensor"])
    out = mesh.request_sensor_read(source_device_id="mac", sensor="barometer", candidates=[phone])
    assert out["sensor"] == "barometer"
    assert out["target_device_id"] == "phone"
    assert out["intent"]["kind"] == "sensor.read"


def test_escalate_nudge_carries_chain():
    phone = _cand("phone", device_type="phone")
    mac = _cand("mac", device_type="laptop")
    out = mesh.escalate_nudge(
        source_device_id="watch", title="Stand up", body="time to move",
        candidates=[phone, mac],
    )
    assert out["chain"] == ["phone", "mac"]
    assert out["first_target_device_id"] == "phone"
    assert out["intent"]["args"]["chain"] == ["phone", "mac"]


def test_clipboard_transform_validation_and_routing():
    with pytest.raises(ValueError):
        mesh.clipboard_transform(
            source_device_id="mac", transform="encrypt", text="secret",
            ttl_seconds=60, candidates=[],
        )
    with pytest.raises(ValueError):
        mesh.clipboard_transform(
            source_device_id="mac", transform="tidy", text="   ",
            ttl_seconds=60, candidates=[],
        )
    phone = _cand("phone", caps=["clipboard"])
    out = mesh.clipboard_transform(
        source_device_id="mac", transform="tidy", text="  hello   world  ",
        ttl_seconds=60, candidates=[phone],
    )
    assert out["transform"] == "tidy"
    assert out["target_device_id"] == "phone"
    assert out["intent"]["kind"] == "clipboard.transform"


def test_conversation_migrate_requires_thread_and_routes_voice():
    with pytest.raises(ValueError):
        mesh.conversation_migrate(
            source_device_id="mac", thread_id="  ", from_device_id=None, candidates=[],
        )
    phone = _cand("phone", caps=["foreground_voice"])
    out = mesh.conversation_migrate(
        source_device_id="mac", thread_id="t-123", from_device_id="se",
        candidates=[phone],
    )
    assert out["thread_id"] == "t-123"
    assert out["target_device_id"] == "phone"
    assert out["intent"]["args"]["from_device_id"] == "se"


def test_mac_verb_risk_table_and_approval_gate():
    assert mesh.mac_verb_risk("status") == "R1"
    assert mesh.mac_verb_risk("open_app") == "R2"
    with pytest.raises(ValueError):
        mesh.mac_verb_risk("self_destruct")
    mac = _cand("mac", device_type="laptop", caps=["computer_control"])
    out = mesh.request_mac_verb(
        source_device_id="16pro", verb="open_app",
        arguments={"name": "Safari"}, candidates=[mac],
    )
    assert out["verb"] == "open_app"
    assert out["risk"] == "R2"
    assert out["approval_required"] is True
    assert out["target_device_id"] == "mac"
    safe = mesh.request_mac_verb(
        source_device_id="16pro", verb="status", arguments={}, candidates=[mac],
    )
    assert safe["approval_required"] is False


def test_mesh_status_is_honest():
    mesh.mesh_store.observe(observer_device_id="mac", subject_device_id="16pro", rssi=-52.0)
    mesh.mesh_store.advertise(device_id="se", battery_percent=64.0)
    bus.post(kind="converge.beacon", capability="converge", args={}, source_device_id="mac")
    revoked = _cand("ghost")
    revoked["trust_state"] = "revoked"
    status = mesh.mesh_status([_cand("16pro"), _cand("se"), revoked])
    assert status["service_uuid"] == mesh.EV_MESH_SERVICE_UUID
    assert status["zones"]["immediate"] == 1
    assert status["advertisements"]["se"]["battery_percent"] == 64.0
    assert status["bus_intents_by_kind"] == {"converge.beacon": 1}
    assert status["storage"].startswith("in-memory")
    assert "open_app" in status["mac_verbs"]
    # Revoked devices are registry history, not mesh participants.
    assert status["known_devices"] == ["16pro", "se"]
