"""Presence OS V1 API: end-to-end gateway tests over a trusted companion device."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _trusted_phone(client: AsyncClient, *, name: str) -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "primary_companion", "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    token = minted.json()["pairing_token"]
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": token,
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.08.20.1",
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    body = paired.json()
    phone.headers["Authorization"] = f"Bearer {body['device_token']}"
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200, promoted.text
    return phone


async def _make_goal(phone: AsyncClient, objective: str = "Handle this, tell me only if blocked") -> dict:
    resp = await phone.post("/v1/device-gateway/presence/goals", json={"objective": objective})
    assert resp.status_code == 200, resp.text
    return resp.json()["goal"]


async def test_create_goal_active_with_only_if_blocked(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence A")
    try:
        goal = await _make_goal(phone)
        assert goal["state"] == "ACTIVE"
        assert goal["interruption_policy"] == "ONLY_IF_BLOCKED"
        assert goal["objective"]
    finally:
        await phone.aclose()


async def test_list_filter_states_active(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence B")
    try:
        goal = await _make_goal(phone, "Handle this, tell me only if blocked")
        resp = await phone.get("/v1/device-gateway/presence/goals", params={"states": "ACTIVE"})
        assert resp.status_code == 200, resp.text
        ids = [g["goal_id"] for g in resp.json()["goals"]]
        assert goal["goal_id"] in ids
    finally:
        await phone.aclose()


async def test_illegal_transition_active_to_draft_409(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence C")
    try:
        goal = await _make_goal(phone)
        resp = await phone.post(
            f"/v1/device-gateway/presence/goals/{goal['goal_id']}/transition",
            json={"to": "DRAFT"},
        )
        assert resp.status_code == 409, resp.text
    finally:
        await phone.aclose()


async def test_park_resume_roundtrip(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence D")
    try:
        goal = await _make_goal(phone)
        gid = goal["goal_id"]
        parked = await phone.post(
            f"/v1/device-gateway/presence/goals/{gid}/transition",
            json={"to": "PARKED", "reason": "owner park"},
        )
        assert parked.status_code == 200, parked.text
        assert parked.json()["goal"]["state"] == "PARKED"
        resumed = await phone.post(
            f"/v1/device-gateway/presence/goals/{gid}/transition",
            json={"to": "ACTIVE", "reason": "owner resume"},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["goal"]["state"] == "ACTIVE"
    finally:
        await phone.aclose()


async def test_wait_time_condition_past_due_resume(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence E")
    try:
        goal = await _make_goal(phone)
        gid = goal["goal_id"]
        waited = await phone.post(
            f"/v1/device-gateway/presence/goals/{gid}/wait",
            json={"wait_state": "WAITING_FOR_DEVICE", "condition": {}, "reason": "waiting on device"},
        )
        assert waited.status_code == 200, waited.text
        assert waited.json()["goal"]["state"] == "WAITING_FOR_DEVICE"
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        cond = await phone.post(
            f"/v1/device-gateway/presence/goals/{gid}/conditions",
            json={"cond_class": "TIME", "payload": {"at": past}},
        )
        assert cond.status_code == 200, cond.text
        resumed = await phone.post(f"/v1/device-gateway/presence/goals/{gid}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["resumed"] is True
        assert resumed.json()["goal"]["state"] == "ACTIVE"
    finally:
        await phone.aclose()


async def test_node_upsert_idempotent_and_bad_kind_422(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence F")
    try:
        goal = await _make_goal(phone)
        gid = goal["goal_id"]
        payload = {
            "node_id": "n1",
            "kind": "CORE_READ",
            "target": "CORE",
            "status": "PENDING",
            "effect": "read",
            "risk": "R1",
            "depends_on": [],
            "verification": "",
        }
        first = await phone.post(f"/v1/device-gateway/presence/goals/{gid}/nodes", json=payload)
        assert first.status_code == 200, first.text
        second = await phone.post(f"/v1/device-gateway/presence/goals/{gid}/nodes", json=payload)
        assert second.status_code == 200, second.text
        assert second.json()["node"] == first.json()["node"]
        bad = await phone.post(
            f"/v1/device-gateway/presence/goals/{gid}/nodes",
            json={**payload, "node_id": "n2", "kind": "NOPE"},
        )
        assert bad.status_code == 422, bad.text
    finally:
        await phone.aclose()


async def test_teleport_returns_capsules(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence G")
    try:
        goal = await _make_goal(phone)
        resp = await phone.post(f"/v1/device-gateway/presence/goals/{goal['goal_id']}/teleport")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_capsule"]
        assert body["continuation"]
    finally:
        await phone.aclose()


async def test_situation_has_contracts_and_devices(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence H")
    try:
        await _make_goal(phone)
        resp = await phone.get("/v1/device-gateway/presence/situation")
        assert resp.status_code == 200, resp.text
        sit = resp.json()["situation"]
        assert "contracts" in sit
        assert "devices" in sit
    finally:
        await phone.aclose()


async def test_mission_keys(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence I")
    try:
        await _make_goal(phone)
        resp = await phone.get("/v1/device-gateway/presence/mission")
        assert resp.status_code == 200, resp.text
        mission = resp.json()["mission"]
        for key in ("now", "working", "waiting", "needs_you", "done_recently", "pocket"):
            assert key in mission, key
    finally:
        await phone.aclose()


async def test_what_changed_keys(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence J")
    try:
        await _make_goal(phone)
        resp = await phone.get("/v1/device-gateway/presence/what-changed")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "changes" in body
        assert "contracts" in body
    finally:
        await phone.aclose()


async def test_simulate_mac_offline(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence K")
    try:
        await _make_goal(phone)
        resp = await phone.post(
            "/v1/device-gateway/presence/simulate",
            json={"scenario": "Mac offline: home station offline, what waits?"},
        )
        assert resp.status_code == 200, resp.text
        sim = resp.json()["simulation"]
        assert sim["mutated"] is False
        assert sim["would_wait"]
    finally:
        await phone.aclose()


async def test_unknown_goal_404(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence L")
    try:
        resp = await phone.post(
            f"/v1/device-gateway/presence/goals/{uuid4()}/transition",
            json={"to": "PARKED"},
        )
        assert resp.status_code == 404, resp.text
    finally:
        await phone.aclose()


async def test_empty_objective_422(client: AsyncClient) -> None:
    phone = await _trusted_phone(client, name="Presence M")
    try:
        resp = await phone.post("/v1/device-gateway/presence/goals", json={"objective": "   "})
        assert resp.status_code == 422, resp.text
    finally:
        await phone.aclose()
