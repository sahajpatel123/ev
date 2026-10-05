"""Evie Mesh endpoint tests: proximity evidence, advertise, converge,
photo capture, shortcut bridge, sensor reads, nudge escalation,
clipboard transforms, conversation migration, and Mac verbs."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.device_gateway.presence import note as note_presence
from app.everywhere import mesh
from app.everywhere.followme import bus
from app.main import app


@pytest.fixture(autouse=True)
def _clean():
    mesh.mesh_store.reset()
    bus.reset()
    yield
    mesh.mesh_store.reset()
    bus.reset()


async def _pair(client: AsyncClient, name: str) -> tuple[str, AsyncClient]:
    """Pair a device and mark it ONLINE (heartbeat), exactly
    like a live EV client does on connect."""
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.10.02.01",
            "instance_id": name + "-tab",
            "capabilities": ["mesh", "camera", "text"],
        },
    )
    assert paired.status_code == 200
    payload = paired.json()
    device_id = str(payload["device"]["device_id"])
    note_presence(device_id)
    phone.headers["Authorization"] = f"Bearer {payload['device_token']}"
    return device_id, phone


# ---------------------------------------------------------- Proximity


async def test_proximity_observe_and_read_back(client):
    pro_id, pro = await _pair(client, "Mesh-16P")
    se_id, se = await _pair(client, "Mesh-SE")

    observed = await pro.post(
        "/v1/everywhere/proximity/observe",
        json={
            "observations": [
                {"subject_device_id": se_id, "rssi": -47.5, "battery_percent": 81.0,
                 "capabilities": ["camera", "mesh"]},
            ]
        },
    )
    assert observed.status_code == 200
    assert observed.json()["count"] == 1
    assert observed.json()["recorded"][0]["zone"] == "immediate"

    readout = await pro.get("/v1/everywhere/proximity")
    assert readout.status_code == 200
    body = readout.json()
    assert body["observer_device_id"] == pro_id
    assert body["proximity_ranks"] == {se_id: 0}
    assert body["nearest"][0]["subject_device_id"] == se_id
    assert body["advertisements"] == {}

    # A different observer has its own, empty projection.
    se_readout = await se.get("/v1/everywhere/proximity")
    assert se_readout.json()["proximity_ranks"] == {}


async def test_proximity_observe_requires_device_identity(client):
    response = await client.post(
        "/v1/everywhere/proximity/observe",
        json={"observations": [{"subject_device_id": "x"}]},
    )
    assert response.status_code == 401
    response = await client.get("/v1/everywhere/proximity")
    # Master key sees the global matrix, but no observer-scoped ranks.
    assert response.status_code == 200
    assert response.json()["observer_device_id"] is None


async def test_mesh_advertise_and_rejects_bad_battery(client):
    _pro_id, pro = await _pair(client, "Mesh-16P")
    ok = await pro.post(
        "/v1/everywhere/mesh/advertise",
        json={"battery_percent": 55.0, "low_power": False, "capabilities": ["mesh", "camera"]},
    )
    assert ok.status_code == 200
    assert ok.json()["advertisement"]["battery_percent"] == 55.0
    bad = await pro.post("/v1/everywhere/mesh/advertise", json={"battery_percent": 500.0})
    assert bad.status_code == 422
    denied = await client.post("/v1/everywhere/mesh/advertise", json={"battery_percent": 50.0})
    assert denied.status_code == 401


async def test_mesh_advertise_and_status_carry_low_power(client):
    """Wire lock: both payloads carry snake_case `low_power` (bool).

    The Swift record decodes `low_power`; a missing or camelCase key
    threw keyNotFound on every mesh refresh while voice kept working.
    """
    pro_id, pro = await _pair(client, "Mesh-LP")
    ok = await pro.post(
        "/v1/everywhere/mesh/advertise",
        json={"battery_percent": 12.0, "low_power": True, "capabilities": ["mesh"]},
    )
    assert ok.status_code == 200
    assert ok.json()["advertisement"]["low_power"] is True
    status = await client.get("/v1/everywhere/mesh/status")
    assert status.status_code == 200
    ads = status.json()["advertisements"]
    assert ads[pro_id]["low_power"] is True
    assert all(isinstance(ad.get("low_power"), bool) for ad in ads.values())


# ------------------------------------------------------------ Vertices


async def test_converge_broadcasts_to_live_devices(client):
    await _pair(client, "Mesh-16P")
    await _pair(client, "Mesh-SE")
    response = await client.post(
        "/v1/everywhere/converge", json={"reason": "where are you"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_count"] == 2
    assert body["intent"]["kind"] == "converge.beacon"
    assert body["intent"]["target_device_id"] is None


async def test_photo_capture_intent_targets_camera_device(client):
    pro_id, pro = await _pair(client, "Mesh-16P")
    se_id, se = await _pair(client, "Mesh-SE")
    # The SE sees the 16 Pro right next to it: the nearest
    # camera device should be the one asked to capture.
    await se.post(
        "/v1/everywhere/proximity/observe",
        json={"observations": [{"subject_device_id": pro_id, "rssi": -45.0}]},
    )
    response = await se.post(
        "/v1/everywhere/photo/capture", json={"reason": "look at this"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"]["kind"] == "photo.capture"
    assert body["intent"]["args"]["requester_device_id"] == se_id
    assert body["target_device_id"] == pro_id


async def test_shortcut_run_intent(client):
    response = await client.post(
        "/v1/everywhere/shortcut/run",
        json={"shortcut": "Morning Brief", "args": {"time": "07:00"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"]["kind"] == "shortcut.run"
    assert body["intent"]["args"]["shortcut"] == "Morning Brief"
    assert body["intent"]["args"]["args"] == {"time": "07:00"}


async def test_sensor_read_validates_and_routes(client):
    bad = await client.post("/v1/everywhere/sensor/read", json={"sensor": "thermostat"})
    assert bad.status_code == 422
    ok = await client.post("/v1/everywhere/sensor/read", json={"sensor": "barometer"})
    assert ok.status_code == 200
    assert ok.json()["sensor"] == "barometer"
    assert ok.json()["risk"] == "R1"
    assert ok.json()["intent"]["kind"] == "sensor.read"


async def test_nudge_escalate_builds_chain(client):
    await _pair(client, "Mesh-16P")
    await _pair(client, "Mesh-SE")
    response = await client.post(
        "/v1/everywhere/nudge/escalate",
        json={"title": "Stand up", "body": "time to move"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["chain"]) == 2
    assert body["intent"]["args"]["chain"] == body["chain"]
    assert body["first_target_device_id"] == body["chain"][0]


async def test_clipboard_transform_validates(client):
    bad = await client.post(
        "/v1/everywhere/clipboard/transform",
        json={"transform": "encrypt", "text": "secret"},
    )
    assert bad.status_code == 422
    empty = await client.post(
        "/v1/everywhere/clipboard/transform",
        json={"transform": "tidy", "text": "   "},
    )
    assert empty.status_code == 422
    ok = await client.post(
        "/v1/everywhere/clipboard/transform",
        json={"transform": "tidy", "text": "  hello  ", "ttl_seconds": 60},
    )
    assert ok.status_code == 200
    assert ok.json()["transform"] == "tidy"
    assert ok.json()["intent"]["kind"] == "clipboard.transform"


async def test_conversation_migrate_route(client):
    bad = await client.post(
        "/v1/everywhere/conversation/migrate", json={"thread_id": "  "}
    )
    assert bad.status_code == 422
    ok = await client.post(
        "/v1/everywhere/conversation/migrate",
        json={"thread_id": "t-123", "from_device_id": "se"},
    )
    assert ok.status_code == 200
    assert ok.json()["thread_id"] == "t-123"
    assert ok.json()["intent"]["kind"] == "conversation.migrate"


async def test_mac_verb_approval_gate(client):
    bad = await client.post("/v1/everywhere/mac/verb", json={"verb": "self_destruct"})
    assert bad.status_code == 422
    safe = await client.post(
        "/v1/everywhere/mac/verb", json={"verb": "status", "arguments": {}}
    )
    assert safe.status_code == 200
    assert safe.json()["approval_required"] is False
    risky = await client.post(
        "/v1/everywhere/mac/verb",
        json={"verb": "open_app", "arguments": {"name": "Safari"}},
    )
    assert risky.status_code == 200
    body = risky.json()
    assert body["approval_required"] is True
    assert body["risk"] == "R2"
    assert body["intent"]["args"]["verb"] == "open_app"


async def test_mesh_status_and_intent_ack_flow(client):
    pro_id, pro = await _pair(client, "Mesh-16P")
    await pro.post(
        "/v1/everywhere/proximity/observe",
        json={"observations": [{"subject_device_id": "ghost", "rssi": -58.0}]},
    )
    await pro.post("/v1/everywhere/mesh/advertise", json={"battery_percent": 77.0})

    status = await client.get("/v1/everywhere/mesh/status")
    assert status.status_code == 200
    body = status.json()
    assert body["service_uuid"] == mesh.EV_MESH_SERVICE_UUID
    assert body["advertisements"][pro_id]["battery_percent"] == 77.0
    assert body["zones"]["near"] == 1

    # Broadcast -> targeted device acks -> receipt proves convergence.
    converge = await client.post("/v1/everywhere/converge", json={"reason": "dinner"})
    intent_id = converge.json()["intent_id"]
    ack = await pro.post(
        f"/v1/everywhere/intents/{intent_id}/ack",
        json={"status": "DONE", "note": "chimed on 16 Pro"},
    )
    assert ack.status_code == 200
    assert ack.json()["intent"]["status"] == "DONE"
    receipts = ack.json()["intent"]["receipts"]
    assert receipts[0]["device_id"] == pro_id


async def test_intents_endpoint_accepts_all_mesh_kinds(client):
    for kind, capability in (
        ("converge.beacon", "converge"),
        ("photo.capture", "camera"),
        ("shortcut.run", "shortcut"),
        ("sensor.read", "sensor"),
        ("nudge.escalate", "notification"),
        ("clipboard.transform", "clipboard"),
        ("conversation.migrate", "foreground_voice"),
        ("mac.verb", "computer_control"),
    ):
        response = await client.post(
            "/v1/everywhere/intents",
            json={"kind": kind, "capability": capability, "args": {}},
        )
        assert response.status_code == 200, kind
        assert response.json()["intent"]["kind"] == kind
    rejected = await client.post(
        "/v1/everywhere/intents",
        json={"kind": "self.destruct", "capability": "nope", "args": {}},
    )
    assert rejected.status_code == 422
