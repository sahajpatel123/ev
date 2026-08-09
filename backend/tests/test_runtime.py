"""Tests for the 24/7 runtime: state machine, wake arbitration, heartbeats,
action routing, and dead-letter recovery."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.models import RuntimeSession
from app.services.runtime import daemon_tick
from app.utils.text import utcnow


async def register_device(
    client: AsyncClient,
    name: str,
    capabilities: list[str] | None = None,
) -> dict:
    resp = await client.post(
        "/v1/devices",
        json={"name": name, "capabilities": capabilities or ["voice", "wake"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["device"]


async def heartbeat(client: AsyncClient, device_id: str, **overrides: object) -> None:
    payload = {"device_id": device_id, "status": "ok", "listener_state": "listening"}
    payload.update(overrides)
    resp = await client.post("/v1/runtime/heartbeat", json=payload)
    assert resp.status_code == 201, resp.text


async def test_heartbeat_marks_device_online(client: AsyncClient) -> None:
    device = await register_device(client, "mac-studio")
    await heartbeat(client, str(device["id"]), battery_percent=92.0)

    resp = await client.get("/v1/runtime/status")
    assert resp.status_code == 200
    status = resp.json()
    assert status["state"] == "idle"
    assert status["online_count"] == 1
    device_status = status["devices"][0]
    assert device_status["device_id"] == str(device["id"])
    assert device_status["presence"] == "online"
    assert device_status["battery_percent"] == 92.0
    assert device_status["listener_state"] == "listening"


async def test_wake_arbitration_picks_best_device(client: AsyncClient) -> None:
    near = await register_device(client, "iphone")
    far = await register_device(client, "watch")
    await heartbeat(client, str(near["id"]))
    await heartbeat(client, str(far["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(near["id"]),
                "signal_score": 0.9,
                "battery_percent": 80.0,
                "proximity_score": 1.0,
                "priority": 0.6,
            },
            {
                "device_id": str(far["id"]),
                "signal_score": 0.2,
                "battery_percent": 20.0,
                "proximity_score": 0.1,
                "priority": 0.6,
            },
        ],
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["state"] == "verifying"
    assert outcome["blocked"] is False
    assert outcome["winner"]["device_id"] == str(near["id"])
    assert outcome["session_id"]
    winner = next(c for c in outcome["candidates"] if c["selected"])
    assert winner["device_id"] == str(near["id"])

    resp = await client.get("/v1/runtime/status")
    status = resp.json()
    assert status["state"] == "verifying"
    assert status["session"]["device_id"] == str(near["id"])


async def test_wake_requires_online_wake_capable_device(client: AsyncClient) -> None:
    offline = await register_device(client, "desk-echo")
    no_wake = await register_device(client, "monitor", capabilities=["screen"])
    await heartbeat(client, str(no_wake["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {"device_id": str(offline["id"]), "signal_score": 0.9},
            {"device_id": str(no_wake["id"]), "signal_score": 0.9},
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "no_eligible_device"
    reasons = {c["reason"] for c in outcome["candidates"]}
    assert reasons == {"offline", "no_wake_capability"}


async def test_quiet_hours_block_wake_unless_urgent(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    device = await register_device(client, "nightstand")
    await heartbeat(client, str(device["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.8, "priority": 0.5}],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "quiet_hours"

    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.8, "priority": 0.9}],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is False
    assert outcome["state"] == "verifying"


async def test_runtime_state_machine_lifecycle_and_invalid_transition(
    client: AsyncClient,
) -> None:
    device = await register_device(client, "mac-mini")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7}],
    )
    assert resp.status_code == 200

    for to_state in ["awake", "processing", "responding", "follow_up", "idle"]:
        resp = await client.post("/v1/runtime/transition", json={"to_state": to_state})
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == to_state

    resp = await client.post("/v1/runtime/transition", json={"to_state": "processing"})
    assert resp.status_code == 409


async def test_stale_session_times_out(client: AsyncClient, db_session) -> None:
    device = await register_device(client, "stale-echo")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7}],
    )
    session_id = UUID(resp.json()["session_id"])

    row = (
        await db_session.execute(select(RuntimeSession).where(RuntimeSession.id == session_id))
    ).scalar_one()
    row.updated_at = utcnow() - timedelta(seconds=settings.runtime_verify_timeout_seconds + 5)
    await db_session.commit()

    resp = await client.get("/v1/runtime/status")
    status = resp.json()
    assert status["state"] == "idle"
    assert status["session"]["end_reason"] == "verifying_timeout"


async def test_action_routing_approval_and_execution(client: AsyncClient) -> None:
    device = await register_device(client, "action-phone")
    await heartbeat(client, str(device["id"]))
    await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7}],
    )

    resp = await client.post(
        "/v1/runtime/actions",
        json={
            "action_type": "notification",
            "title": "Remind about demo",
            "payload": {"text": "Demo in 30m"},
            "device_id": str(device["id"]),
        },
    )
    assert resp.status_code == 201, resp.text
    action = resp.json()
    assert action["status"] == "pending"
    assert action["requires_approval"] is True
    assert action["session_id"]

    resp = await client.post(f"/v1/runtime/actions/{action['id']}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["approved_by"] == "master"

    resp = await client.post(
        f"/v1/runtime/actions/{action['id']}/execute",
        json={"result": {"delivered": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "executed"
    assert resp.json()["result"] == {"delivered": True}

    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "search_memory", "title": "local search", "auto_approve": True},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "approved"
    assert resp.json()["requires_approval"] is False

    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "fleet_task", "payload": {"task_type": "capture_photo"}},
    )
    assert resp.status_code == 201
    denied = await client.post(
        f"/v1/runtime/actions/{resp.json()['id']}/deny",
        json={"reason": "not now"},
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    assert denied.json()["denied_reason"] == "not now"


async def test_dead_letter_record_retry_discard(client: AsyncClient) -> None:
    payload = {"queue": "ingestion", "job_id": "job-1", "payload": {"event_id": "e1"}, "error": "boom"}
    resp = await client.post("/v1/runtime/dead-letters", json=payload)
    assert resp.status_code == 201, resp.text
    letter = resp.json()
    assert letter["status"] == "new"
    assert letter["attempts"] == 1

    resp = await client.post("/v1/runtime/dead-letters", json=payload)
    assert resp.status_code == 201
    assert resp.json()["attempts"] == 2

    resp = await client.post(f"/v1/runtime/dead-letters/{letter['id']}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retrying"

    resp = await client.post("/v1/runtime/dead-letters", json=payload)
    assert resp.status_code == 201
    assert resp.json()["attempts"] == 3
    assert resp.json()["status"] == "discarded"

    resp = await client.get("/v1/runtime/status")
    assert resp.json()["dead_letters"] == {
        "new": 0,
        "retrying": 0,
        "discarded": 1,
        "resolved": 0,
    }

    resp = await client.post(f"/v1/runtime/dead-letters/{letter['id']}/discard")
    assert resp.status_code == 200
    assert resp.json()["status"] == "discarded"


async def test_dead_letter_carries_entrypoint_for_recovery(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/runtime/dead-letters",
        json={
            "queue": "ingestion",
            "job_id": "job-recover",
            "payload": {
                "event_id": "e9",
                "entrypoint": "app.workers.jobs.process_event",
                "args": ["e9"],
            },
            "error": "boom",
        },
    )
    assert resp.status_code == 201
    letter = resp.json()
    assert letter["payload"]["entrypoint"] == "app.workers.jobs.process_event"

    resp = await client.post(f"/v1/runtime/dead-letters/{letter['id']}/retry")
    assert resp.status_code == 200
    # In sync test mode there is no Redis to re-enqueue onto, but the letter
    # must remain observable in a retryable state rather than disappearing.
    assert resp.json()["status"] == "retrying"


async def test_runtime_health_reports_checks(client: AsyncClient) -> None:
    device = await register_device(client, "health-phone")
    await heartbeat(client, str(device["id"]), battery_percent=55.0)

    resp = await client.get("/v1/runtime/health")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["schema_version"] == "ev.runtime.health.v1"
    assert report["overall"] in ("ok", "degraded", "failed")
    names = {check["name"] for check in report["checks"]}
    assert {"database", "state_machine", "listeners", "dead_letters", "queue", "chat_provider"} <= names
    listeners = next(c for c in report["checks"] if c["name"] == "listeners")
    assert listeners["online_devices"] == 1
    assert listeners["listening_devices"] == 1


async def test_daemon_tick_expires_stale_session_and_reports(
    client: AsyncClient, db_session
) -> None:
    device = await register_device(client, "daemon-echo")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7}],
    )
    session_id = UUID(resp.json()["session_id"])

    row = (
        await db_session.execute(select(RuntimeSession).where(RuntimeSession.id == session_id))
    ).scalar_one()
    row.updated_at = utcnow() - timedelta(seconds=settings.runtime_verify_timeout_seconds + 5)
    await db_session.commit()

    report = await daemon_tick(db_session)
    await db_session.commit()
    assert report["expired_session_id"] == str(session_id)
    assert report["health"]["state"] == "idle"
    assert report["health"]["overall"] in ("ok", "degraded", "failed")
    assert "re_enqueued" in report
