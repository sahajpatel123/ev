"""E.D.I.T.H. command ledger + coordinated fleet lifecycle tests."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def register_device(client: AsyncClient, name: str, capabilities: list[str]) -> dict:
    resp = await client.post(
        "/v1/devices",
        json={"name": name, "capabilities": capabilities},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return {"id": body["device"]["id"], "token": body["token"]}


def device_client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_fleet_task_lifecycle_scoped_to_device(client: AsyncClient) -> None:
    camera = await register_device(client, "camera-phone", ["capture_photo"])
    other = await register_device(client, "audio-phone", ["capture_audio"])

    resp = await client.post(
        "/v1/fleet/tasks",
        json={
            "device_id": camera["id"],
            "task_type": "capture_photo",
            "payload": {"subject": "workbench"},
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    assert resp.json()["requested_by"] == "master"

    # The target device sees exactly its own pending task; other devices see none.
    async with device_client(camera["token"]) as cam:
        resp = await cam.get("/v1/fleet/tasks/pending")
        assert resp.status_code == 200, resp.text
        pending = resp.json()
        assert len(pending) == 1
        assert pending[0]["id"] == task_id

        resp = await cam.get(f"/v1/fleet/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "requested"

    async with device_client(other["token"]) as other_client:
        resp = await other_client.get("/v1/fleet/tasks/pending")
        assert resp.json() == []

        resp = await other_client.get(f"/v1/fleet/tasks/{task_id}")
        assert resp.status_code == 403

        resp = await other_client.post(f"/v1/fleet/tasks/{task_id}/accept")
        assert resp.status_code == 403

    # Lifecycle: accepted -> running is allowed, complete only after running.
    async with device_client(camera["token"]) as cam:
        resp = await cam.post(f"/v1/fleet/tasks/{task_id}/accept")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "accepted"
        assert resp.json()["accepted_by"] == "device:camera-phone"

        resp = await cam.post(f"/v1/fleet/tasks/{task_id}/start")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "running"

        resp = await cam.post(f"/v1/fleet/tasks/{task_id}/complete", json={"result": {"photo": "snap.jpg"}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed"
        assert resp.json()["completed_at"] is not None
        assert resp.json()["result"] == {"photo": "snap.jpg"}

    resp = await client.get(f"/v1/fleet/tasks/{task_id}")
    assert resp.status_code == 200
    task = resp.json()
    assert task["status"] == "completed"
    assert task["accepted_by"] == "device:camera-phone"
    assert task["result"] == {"photo": "snap.jpg"}

    resp = await client.get("/v1/fleet")
    assert resp.status_code == 200
    assert resp.json()["active_tasks"] == 0


async def test_fleet_task_capability_and_transition_validation(client: AsyncClient) -> None:
    device = await register_device(client, "phone", ["capture_photo"])

    resp = await client.post(
        "/v1/fleet/tasks",
        json={"device_id": device["id"], "task_type": "capture_audio", "payload": {}},
    )
    assert resp.status_code == 400, resp.text
    assert "capability" in resp.json()["detail"]

    # Universal task types require no declared capability.
    resp = await client.post(
        "/v1/fleet/tasks",
        json={"device_id": device["id"], "task_type": "ping", "payload": {}},
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    async with device_client(device["token"]) as dev:
        resp = await dev.post(f"/v1/fleet/tasks/{task_id}/complete")
        assert resp.status_code == 409, "requested -> completed must be rejected"

        resp = await dev.post(f"/v1/fleet/tasks/{task_id}/fail", json={"error": "device offline"})
        assert resp.status_code == 409

        resp = await dev.post(f"/v1/fleet/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    # Rejected dispatch and the successful ping both land in the ledger.
    resp = await client.get("/v1/commands")
    assert resp.status_code == 200
    commands = resp.json()
    assert any(c["command_type"] == "fleet.task.create" and c["status"] == "rejected" for c in commands)
    assert any(c["command_type"] == "fleet.task.cancelled" for c in commands)


async def test_command_ledger_is_explicit_auditable_and_scoped(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/focus",
        json={"label": "Ship EV command surface", "kind": "goal", "reason": "audit test"},
    )
    assert resp.status_code == 201, resp.text
    focus_id = resp.json()["id"]

    device = await register_device(client, "watch", ["ping"])

    resp = await client.get("/v1/commands")
    assert resp.status_code == 200, resp.text
    commands = resp.json()
    designate = next(c for c in commands if c["command_type"] == "focus.designate")
    assert designate["actor"] == "master"
    assert designate["status"] == "completed"
    assert designate["target_type"] == "goal"
    assert designate["target_id"] == focus_id
    assert designate["request"]["label"] == "Ship EV command surface"

    resp = await client.get(f"/v1/commands/{designate['id']}")
    assert resp.status_code == 200
    assert resp.json()["command_type"] == "focus.designate"

    # Devices only see commands they issued themselves.
    async with device_client(device["token"]) as watch:
        resp = await watch.get("/v1/commands")
        assert resp.status_code == 200
        assert all(c["actor"] == "device:watch" for c in resp.json())

        resp = await watch.get(f"/v1/commands/{designate['id']}")
        assert resp.status_code == 403

    # Ops center surfaces recent commands.
    resp = await client.get("/v1/ops/center")
    assert resp.status_code == 200
    assert any(c["command_type"] == "focus.designate" for c in resp.json()["recent_commands"])
