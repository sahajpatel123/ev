"""Tests for the headless device listener agent (heartbeat + wake arbitration)."""

from __future__ import annotations

import base64
import json
from uuid import UUID

import httpx
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import select

from app.main import app
from app.models import RuntimeSession
from app.services import runtime as runtime_service
from clients.device_listener import DeviceListener

SAMPLE_A = b"owner-voice-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _listener_client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )


def _offline_handler(request: httpx.Request) -> Response:
    raise httpx.ConnectError("offline", request=request)


def _online_listener(
    *,
    queue_dir,
    requests: list[httpx.Request] | None = None,
) -> tuple[DeviceListener, AsyncClient]:
    seen = requests if requests is not None else []

    def handler(request: httpx.Request) -> Response:
        seen.append(request)
        if request.url.path == "/v1/events":
            return Response(201, json={"event": {"id": "event-1", "text": "captured"}})
        if request.url.path == "/v1/live/events":
            return Response(201, json=[{"id": "live-1"}])
        if request.url.path == "/v1/runtime/sync":
            return Response(
                200,
                json={
                    "runtime": {
                        "state": "verifying",
                        "device_id": "device-1",
                    },
                    "events": [{"kind": "wake", "id": "wake-1"}],
                },
            )
        return Response(404)

    client = AsyncClient(
        transport=MockTransport(handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    return DeviceListener(client, "device-1", queue_dir=queue_dir), client


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
        outcome = await listener.wake(
            signal_score=0.9,
            proximity_score=1.0,
            priority=0.8,
            text_hint="evie",
        )
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


async def test_listener_voice_cycle_runs_full_lifecycle() -> None:
    async with _listener_client() as client:
        resp = await client.post(
            "/v1/devices",
            json={"name": "voice-pi", "capabilities": ["voice", "wake"]},
        )
        assert resp.status_code == 201, resp.text
        device_id = resp.json()["device"]["id"]

        resp = await client.post(
            "/v1/training/consent", json={"track": "voice_enrollment"}
        )
        assert resp.status_code == 201
        resp = await client.post(
            "/v1/voice/enroll",
            json={
                "samples": [
                    {"audio_b64": b64(SAMPLE_A), "liveness_proof": "live"}
                    for _ in range(5)
                ],
                "reason": "listener test",
            },
        )
        assert resp.status_code == 201, resp.text
        resp = await client.post("/v1/identity/owner", json={"display_name": "Listener Owner"})
        assert resp.status_code == 201, resp.text

        listener = DeviceListener(client, device_id)
        await listener.heartbeat()
        result = await listener.voice_cycle(
            phrase="",
            samples=[b64(SAMPLE_A)],
            text="Remind me to water the plants",
            follow_up_text="Actually, tomorrow",
        )
        assert result["reply"]["state"] == "follow_up"
        assert result["reply"]["reply"]
        assert result["follow_up"]["state"] == "follow_up"
        assert result["follow_up"]["transcript"] == "Actually, tomorrow"


async def test_listener_offline_capture_queues_and_delivers_when_online(tmp_path) -> None:
    offline = AsyncClient(
        transport=MockTransport(_offline_handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    listener = DeviceListener(offline, "device-1", queue_dir=tmp_path)
    result = await listener.capture("remember offline")
    assert result["queued"] is True
    assert result["delivery"] == "event"
    assert len(listener.pending_captures()) == 1
    await offline.aclose()

    requests: list[httpx.Request] = []
    online, client = _online_listener(queue_dir=tmp_path, requests=requests)
    try:
        summary = await online.deliver_pending()
        assert summary == {"synced": 1, "dropped": 0, "quarantined": 0, "errors": [], "remaining": 0}
        assert online.pending_captures() == []
        assert requests[0].url.path == "/v1/events"
        assert requests[0].headers.get("idempotency-key")
    finally:
        await client.aclose()


async def test_listener_offline_retry_preserves_entire_queue(tmp_path) -> None:
    offline = AsyncClient(
        transport=MockTransport(_offline_handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    listener = DeviceListener(offline, "device-1", queue_dir=tmp_path)
    await listener.capture("first offline capture")
    await listener.capture("second offline capture")
    assert len(listener.pending_captures()) == 2

    summary = await listener.deliver_pending()
    assert summary["synced"] == 0
    assert summary["remaining"] == 2
    assert summary["errors"]
    assert len(listener.pending_captures()) == 2
    await offline.aclose()

    requests: list[httpx.Request] = []
    online, client = _online_listener(queue_dir=tmp_path, requests=requests)
    try:
        summary = await online.deliver_pending()
        assert summary["synced"] == 2
        assert summary["remaining"] == 0
        assert online.pending_captures() == []
    finally:
        await client.aclose()


async def test_listener_live_capture_posts_batch_to_live_events(tmp_path) -> None:
    requests: list[httpx.Request] = []
    listener, client = _online_listener(queue_dir=tmp_path, requests=requests)
    try:
        result = await listener.capture(
            "screen changed",
            live=True,
            channel="screen-activity",
            live_kind="screen",
            live_event_type="focus_change",
            payload={"app": "Code"},
        )
        assert result == [{"id": "live-1"}]
        request = requests[0]
        assert request.url.path == "/v1/live/events"
        body = json.loads(request.content)
        assert body["channel"] == "screen-activity"
        assert body["kind"] == "screen"
        assert body["events"] == [
            {
                "event_type": "focus_change",
                "payload": {"app": "Code"},
                "device_id": "device-1",
                "privacy_level": "normal",
            }
        ]
    finally:
        await client.aclose()


async def test_listener_poll_arbitration_reports_device_selection(tmp_path) -> None:
    listener, client = _online_listener(queue_dir=tmp_path)
    try:
        arbitration = await listener.poll_arbitration()
        assert arbitration["state"] == "verifying"
        assert arbitration["session_device_id"] == "device-1"
        assert arbitration["selected"] is True
        assert arbitration["latest_wake"]["id"] == "wake-1"
    finally:
        await client.aclose()


async def test_listener_quarantines_rejected_capture(tmp_path) -> None:
    def reject_handler(request: httpx.Request) -> Response:
        return Response(422, text="invalid payload")

    client = AsyncClient(
        transport=MockTransport(reject_handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    listener = DeviceListener(client, "device-1", queue_dir=tmp_path)
    try:
        result = await listener.capture("this will be rejected")
        assert result["quarantined"] is True
        assert "HTTP 422" in result["reason"]
        assert listener.pending_captures() == []
        quarantine = (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8")
        assert "this will be rejected" in quarantine
    finally:
        await client.aclose()


async def test_listener_retries_transient_http_capture(tmp_path) -> None:
    attempts = 0

    def transient_handler(request: httpx.Request) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Response(503, text="provider unavailable")
        return Response(201, json={"event": {"id": "event-recovered"}})

    client = AsyncClient(
        transport=MockTransport(transient_handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    listener = DeviceListener(client, "device-1", queue_dir=tmp_path)
    try:
        queued = await listener.capture("retry me")
        assert queued["queued"] is True
        assert queued["retryable"] is True
        assert "HTTP 503" in queued["reason"]
        assert listener.pending_captures()
        assert not (tmp_path / "quarantine.jsonl").exists()

        recovered = await listener.deliver_pending()
        assert recovered == {
            "synced": 1,
            "dropped": 0,
            "quarantined": 0,
            "errors": [],
            "remaining": 0,
        }
        assert attempts == 2
    finally:
        await client.aclose()
