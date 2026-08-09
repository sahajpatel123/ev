"""Tests for the headless device listener agent (heartbeat + wake arbitration)."""

from __future__ import annotations

from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models import RuntimeSession
from app.services import runtime as runtime_service
from clients.device_listener import DeviceListener


def _listener_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )


async def test_listener_heartbeat_marks_device_online() -> None:
    async with _listener_client() as client:
        resp = await client.post(
            "/v1/devices",
            json={"name": "raspberry-pi", "capabilities": ["voice", "wake"]},
        )
        assert resp.status_code == 201, resp.text
        device_id = resp.json()["device"]["id"]

        listener = DeviceListener(client, device_id, battery_percent=64.0)
        result = await listener.heartbeat(latency_ms=12)
        assert result["device_id"] == device_id
        assert result["status"] == "ok"

        resp = await client.get("/v1/runtime/status")
        assert resp.status_code == 200
        device_status = resp.json()["devices"][0]
        assert device_status["device_id"] == device_id
        assert device_status["presence"] == "online"
        assert device_status["listener_state"] == "listening"
        assert device_status["battery_percent"] == 64.0


async def test_listener_wake_and_sync_convergence(db_session) -> None:
    async with _listener_client() as client:
        resp = await client.post(
            "/v1/devices",
            json={"name": "desk-echo", "capabilities": ["voice", "wake"]},
        )
        assert resp.status_code == 201, resp.text
        device_id = resp.json()["device"]["id"]

        listener = DeviceListener(client, device_id)
        await listener.heartbeat()
        outcome = await listener.wake(signal_score=0.9, proximity_score=1.0, priority=0.8)
        assert outcome["state"] == "verifying"
        assert outcome["winner"]["device_id"] == device_id

        snapshot = await listener.sync_state()
        assert snapshot["runtime"]["state"] == "verifying"
        assert snapshot["runtime"]["session_id"] == outcome["session_id"]
        assert any(device["device_id"] == device_id for device in snapshot["devices"])
        assert any(event["kind"] == "wake" for event in snapshot["events"])
        assert snapshot["policy"]["heartbeat_grace_seconds"] > 0
        assert snapshot["latency"]["wake_to_awake_ms"] is None  # still verifying

        session_row = (
            await db_session.execute(
                select(RuntimeSession).where(RuntimeSession.id == UUID(outcome["session_id"]))
            )
        ).scalar_one()
        await runtime_service.mark_verified(
            db_session, session_row, confidence=0.9, verifier_name="test"
        )
        await db_session.commit()

        snapshot = await listener.sync_state()
        assert snapshot["latency"]["wake_to_awake_ms"] is not None
        assert snapshot["latency"]["wake_to_awake_ms"] >= 0
