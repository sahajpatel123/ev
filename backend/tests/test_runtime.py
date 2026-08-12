"""Tests for the 24/7 runtime: state machine, wake arbitration, heartbeats,
action routing, and dead-letter recovery."""

from __future__ import annotations

import base64
from datetime import timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.models import RuntimeSession
from app.services import runtime as runtime_service
from app.services.runtime import daemon_tick, mark_verified
from app.utils.text import utcnow

SAMPLE_A = b"owner-voice-sample-" * 40
SAMPLE_B = b"other-speaker-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def grant_voice_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text


async def enroll_owner(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/voice/enroll",
        json={
            "samples": [
                {"audio_b64": b64(SAMPLE_A), "liveness_proof": "live"}
                for _ in range(5)
            ],
            "reason": "runtime test enrollment",
        },
    )
    assert resp.status_code == 201, resp.text


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


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


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
                "text_hint": "evie",
            },
            {
                "device_id": str(far["id"]),
                "signal_score": 0.2,
                "battery_percent": 20.0,
                "proximity_score": 0.1,
                "priority": 0.6,
                "text_hint": "evie",
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
            {"device_id": str(offline["id"]), "signal_score": 0.9, "text_hint": "evie"},
            {"device_id": str(no_wake["id"]), "signal_score": 0.9, "text_hint": "evie"},
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "no_eligible_device"
    reasons = {c["reason"] for c in outcome["candidates"]}
    assert reasons == {"offline", "no_wake_capability"}


async def test_wake_requires_real_engine_evidence(client: AsyncClient) -> None:
    """Client-supplied floats are never wake proof (FLEET_LAW §8)."""
    device = await register_device(client, "score-only-echo")
    await heartbeat(client, str(device["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.99}],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "no_eligible_device"
    assert outcome["candidates"][0]["reason"] == "missing_wake_evidence"

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(device["id"]),
                "signal_score": 0.99,
                "text_hint": "not the wake word",
            }
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["candidates"][0]["reason"] == "wake_not_detected"


async def test_quiet_hours_block_wake_unless_urgent(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    device = await register_device(client, "nightstand")
    await heartbeat(client, str(device["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(device["id"]),
                "signal_score": 0.8,
                "priority": 0.5,
                "text_hint": "evie",
            }
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "quiet_hours"

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(device["id"]),
                "signal_score": 0.8,
                "priority": 0.9,
                "text_hint": "evie",
            }
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is False
    assert outcome["state"] == "verifying"


async def test_focus_mode_blocks_wake_unless_urgent(client: AsyncClient) -> None:
    device = await register_device(client, "focus-phone")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/focus",
        json={"label": "Deep work", "kind": "task", "reason": "ship the runtime"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(device["id"]),
                "signal_score": 0.8,
                "priority": 0.5,
                "text_hint": "evie",
            }
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is True
    assert outcome["block_reason"] == "focus_mode"

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(device["id"]),
                "signal_score": 0.8,
                "priority": 0.95,
                "text_hint": "evie",
            }
        ],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["blocked"] is False
    assert outcome["state"] == "verifying"


async def test_runtime_state_machine_lifecycle_and_invalid_transition(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device = await register_device(client, "mac-mini")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["challenge_nonce"]
    assert outcome["challenge_phrase"]

    resp = await client.post(
        "/v1/runtime/verify",
        json={
            "session_id": outcome["session_id"],
            "nonce": outcome["challenge_nonce"],
            "phrase": outcome["challenge_phrase"],
            "samples": [b64(SAMPLE_A)],
            "liveness_proof": "live",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True
    assert resp.json()["state"] == "awake"

    for to_state in ["processing", "responding", "follow_up", "idle"]:
        resp = await client.post("/v1/runtime/transition", json={"to_state": to_state})
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == to_state

    resp = await client.post("/v1/runtime/transition", json={"to_state": "processing"})
    assert resp.status_code == 409


async def test_transition_to_awake_requires_owner_verification(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device = await register_device(client, "mac-gated")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    outcome = resp.json()

    resp = await client.post("/v1/runtime/transition", json={"to_state": "awake"})
    assert resp.status_code == 409
    assert "verification" in resp.json()["detail"].lower()

    # A stranger's voice cannot unlock the session either.
    resp = await client.post(
        "/v1/runtime/verify",
        json={
            "session_id": outcome["session_id"],
            "nonce": outcome["challenge_nonce"],
            "phrase": outcome["challenge_phrase"],
            "samples": [b64(SAMPLE_B)],
            "liveness_proof": "live",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verified"] is False

    resp = await client.post("/v1/runtime/transition", json={"to_state": "awake"})
    assert resp.status_code == 409


async def test_runtime_verify_rejects_nonce_replay(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device = await register_device(client, "mac-replay")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    outcome = resp.json()
    payload = {
        "session_id": outcome["session_id"],
        "nonce": outcome["challenge_nonce"],
        "phrase": outcome["challenge_phrase"],
        "samples": [b64(SAMPLE_A)],
        "liveness_proof": "live",
    }
    resp = await client.post("/v1/runtime/verify", json=payload)
    assert resp.status_code == 200
    assert resp.json()["verified"] is True

    resp = await client.post("/v1/runtime/verify", json=payload)
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "replay_rejected"


async def _verified_runtime_session(client: AsyncClient, device_id: str) -> dict:
    device = await register_device(client, device_id)
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    outcome = resp.json()
    resp = await client.post(
        "/v1/runtime/verify",
        json={
            "session_id": outcome["session_id"],
            "nonce": outcome["challenge_nonce"],
            "phrase": outcome["challenge_phrase"],
            "samples": [b64(SAMPLE_A)],
            "liveness_proof": "live",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True
    return outcome


async def test_runtime_utterance_full_cycle(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    outcome = await _verified_runtime_session(client, "mac-voice")

    resp = await client.post(
        "/v1/runtime/utterance",
        json={
            "session_id": outcome["session_id"],
            "text": "Remind me to call mom tomorrow",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["state"] == "follow_up"
    assert payload["transcript"] == "Remind me to call mom tomorrow"
    assert payload["reply"]
    assert payload["tts"]["ssml"].startswith("<speak>")
    assert payload["conversation_id"]

    resp = await client.post(
        "/v1/runtime/utterance",
        json={
            "session_id": outcome["session_id"],
            "text": "Also buy milk",
            "follow_up": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"]


async def test_runtime_utterance_requires_owner_verification(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device = await register_device(client, "mac-unverified")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    outcome = resp.json()
    resp = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "hello evie"},
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "not_verified"


async def test_runtime_sensitive_utterance_requires_reverification(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    resp = await client.post("/v1/identity/owner", json={"display_name": "Runtime Owner"})
    assert resp.status_code == 201, resp.text
    outcome = await _verified_runtime_session(client, "mac-sensitive")

    resp = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "Delete all my memories"},
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "reverification_required"

    resp = await client.post(
        "/v1/identity/reverification",
        json={"purpose": "voice.sensitive_action"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    resp = await client.post(
        "/v1/runtime/utterance",
        json={
            "session_id": outcome["session_id"],
            "text": "Delete all my memories",
            "reverify_token": token,
        },
    )
    assert resp.status_code == 200, resp.text


async def test_runtime_follow_up_window_expires(
    client: AsyncClient, db_session
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    outcome = await _verified_runtime_session(client, "mac-followup-expire")
    resp = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "Set a timer"},
    )
    assert resp.status_code == 200

    row = (
        await db_session.execute(
            select(RuntimeSession).where(RuntimeSession.id == UUID(outcome["session_id"]))
        )
    ).scalar_one()
    row.updated_at = utcnow() - timedelta(
        seconds=settings.runtime_followup_timeout_seconds + 5
    )
    await db_session.commit()

    resp = await client.post(
        "/v1/runtime/utterance",
        json={
            "session_id": outcome["session_id"],
            "text": "Actually five minutes",
            "follow_up": True,
        },
    )
    assert resp.status_code == 428
    assert resp.headers.get("x-error-code") == "follow_up_expired"


async def test_stale_session_times_out(client: AsyncClient, db_session) -> None:
    device = await register_device(client, "stale-echo")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
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
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
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
    assert {
        "database",
        "state_machine",
        "listeners",
        "dead_letters",
        "queue",
        "chat_provider",
        "asr",
        "tts",
    } <= names
    listeners = next(c for c in report["checks"] if c["name"] == "listeners")
    assert listeners["online_devices"] == 1
    assert listeners["listening_devices"] == 1
    asr = next(c for c in report["checks"] if c["name"] == "asr")
    tts = next(c for c in report["checks"] if c["name"] == "tts")
    assert asr["status"] == "ok"
    assert tts["status"] == "ok"


async def test_runtime_health_reports_unconfigured_network_voice_providers(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "voice_asr_provider", "openai_compat")
    monkeypatch.setattr(settings, "voice_asr_base_url", None)
    monkeypatch.setattr(settings, "voice_tts_provider", "openai_compat")
    monkeypatch.setattr(settings, "voice_tts_base_url", None)

    resp = await client.get("/v1/runtime/health")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["overall"] == "degraded"
    asr = next(c for c in report["checks"] if c["name"] == "asr")
    tts = next(c for c in report["checks"] if c["name"] == "tts")
    assert asr["status"] == "degraded"
    assert tts["status"] == "degraded"


async def test_daemon_tick_expires_stale_session_and_reports(
    client: AsyncClient, db_session
) -> None:
    device = await register_device(client, "daemon-echo")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
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


async def test_daemon_tick_re_enqueues_retrying_dead_letters(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    """Integration: the daemon tick recovers retrying DLQ letters and records
    the recovery in the RuntimeEvent feed (kind=daemon)."""
    resp = await client.post(
        "/v1/runtime/dead-letters",
        json={
            "queue": "ingestion",
            "job_id": "daemon-dlq-recovery",
            "payload": {
                "event_id": "daemon-dlq-recovery",
                "entrypoint": "app.workers.jobs.process_event",
                "args": ["daemon-dlq-recovery"],
            },
            "error": "boom",
        },
    )
    assert resp.status_code == 201, resp.text
    letter = resp.json()

    resp = await client.post(f"/v1/runtime/dead-letters/{letter['id']}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retrying"

    calls: list[str] = []

    def fake_re_enqueue(letter_row) -> bool:
        calls.append(str(letter_row.id))
        return True

    monkeypatch.setattr(runtime_service, "_re_enqueue_dead_letter", fake_re_enqueue)

    report = await daemon_tick(db_session)
    await db_session.commit()
    assert report["re_enqueued"] == 1
    assert calls == [letter["id"]]

    sync = (await client.get("/v1/runtime/sync")).json()
    daemon_events = [event for event in sync["events"] if event["kind"] == "daemon"]
    assert daemon_events
    assert daemon_events[0]["payload"]["re_enqueued"] == 1
    assert "overall" in daemon_events[0]["payload"]


async def test_soak_audit_detects_missed_daemon_ticks(
    db_session, monkeypatch
) -> None:
    """The 72 h launchd acceptance audit fails on any large tick gap."""
    from datetime import timedelta

    from app.models import RuntimeEvent
    from app.workers.runtime_healthcheck import _soak_report

    monkeypatch.setattr(settings, "runtime_daemon_tick_seconds", 30)
    now = utcnow()
    tick = now - timedelta(hours=2)
    while tick < now - timedelta(minutes=10):
        db_session.add(RuntimeEvent(kind="daemon", occurred_at=tick))
        tick += timedelta(seconds=30)
    tick += timedelta(minutes=10)
    while tick < now:
        db_session.add(RuntimeEvent(kind="daemon", occurred_at=tick))
        tick += timedelta(seconds=30)
    await db_session.commit()

    report = await _soak_report(window_hours=2, tolerance_gaps=1)
    assert report["healthy"] is False
    assert report["ticks"] > 0
    assert report["max_gap_seconds"] > 500


async def test_soak_audit_passes_regular_ticks(db_session, monkeypatch) -> None:
    from datetime import timedelta

    from app.models import RuntimeEvent
    from app.workers.runtime_healthcheck import _soak_report

    monkeypatch.setattr(settings, "runtime_daemon_tick_seconds", 30)
    now = utcnow()
    tick = now - timedelta(hours=1)
    while tick < now:
        db_session.add(RuntimeEvent(kind="daemon", occurred_at=tick))
        tick += timedelta(seconds=30)
    await db_session.commit()

    report = await _soak_report(window_hours=1, tolerance_gaps=1)
    assert report["healthy"] is True
    assert report["ticks"] >= 100


async def test_runtime_events_log_lifecycle_pulses(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device = await register_device(client, "pulse-phone")
    await heartbeat(client, str(device["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(device["id"]), "signal_score": 0.7, "text_hint": "evie"}],
    )
    assert resp.status_code == 200
    outcome = resp.json()

    resp = await client.post(
        "/v1/runtime/verify",
        json={
            "session_id": outcome["session_id"],
            "nonce": outcome["challenge_nonce"],
            "phrase": outcome["challenge_phrase"],
            "samples": [b64(SAMPLE_A)],
            "liveness_proof": "live",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True
    assert resp.json()["state"] == "awake"

    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "search_memory", "title": "log me", "auto_approve": True},
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/v1/runtime/dead-letters",
        json={"queue": "ingestion", "job_id": "pulse-dlq", "payload": {}, "error": "boom"},
    )
    assert resp.status_code == 201

    resp = await client.get("/v1/runtime/sync")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["schema_version"] == "ev.runtime.sync.v1"
    kinds = {event["kind"] for event in payload["events"]}
    assert {"wake", "heartbeat", "transition", "action", "dead_letter"} <= kinds


async def test_runtime_sync_returns_convergent_snapshot(client: AsyncClient) -> None:
    phone = await register_device(client, "sync-phone")
    watch = await register_device(client, "sync-watch")
    await heartbeat(client, str(phone["id"]))
    await heartbeat(client, str(watch["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[{"device_id": str(phone["id"]), "signal_score": 0.8, "text_hint": "evie"}],
    )
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    resp = await client.get("/v1/runtime/sync")
    payload = resp.json()
    assert payload["runtime"]["state"] == "verifying"
    assert payload["runtime"]["session_id"] == session_id
    assert len(payload["devices"]) == 2
    assert all(device["presence"] == "online" for device in payload["devices"])
    assert any(event["kind"] == "wake" for event in payload["events"])

    # Since filter: a future cursor returns nothing new, an old cursor returns all.
    future = "2999-01-01T00:00:00Z"
    resp = await client.get(f"/v1/runtime/sync?since={future}")
    assert resp.json()["events"] == []


async def test_runtime_sync_reports_policy_and_empty_latency(client: AsyncClient) -> None:
    resp = await client.get("/v1/runtime/sync")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["policy"]["daily_alert_budget"] == settings.daily_alert_budget
    assert payload["policy"]["heartbeat_grace_seconds"] == settings.runtime_heartbeat_grace_seconds
    assert payload["policy"]["focus_urgent_threshold"] == settings.runtime_focus_urgent_threshold
    assert payload["latency"]["session_id"] is None
    assert all(
        value is None
        for key, value in payload["latency"].items()
        if key != "session_id"
    )
    assert payload["digest"] is None


async def test_runtime_digest_batches_non_urgent_alerts(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "digest topic", "priority": 0.4, "metadata": {}},
    )
    assert resp.status_code == 201, resp.text
    await post_event(client, "thinking about the digest topic again")

    resp = await client.get("/v1/alerts/scan?window_days=30")
    assert resp.status_code == 200, resp.text

    resp = await client.get("/v1/alerts?status=pending")
    pending = resp.json()
    assert pending, "expected at least one pending alert"

    resp = await client.post("/v1/runtime/digest")
    assert resp.status_code == 200, resp.text
    digest = resp.json()
    assert digest["schema_version"] == "ev.runtime.digest.v1"
    assert digest["delivered"] >= 1
    assert all(alert["tier"] in ("useful", "background") for alert in digest["alerts"])

    resp = await client.get("/v1/alerts?status=pending")
    assert resp.json() == []

    resp = await client.get("/v1/runtime/sync")
    sync = resp.json()
    assert any(event["kind"] == "digest" for event in sync["events"])
    assert sync["digest"] is not None
    assert sync["digest"]["digest_id"] == digest["digest_id"]
    assert sync["digest"]["delivered"] == digest["delivered"]


async def test_daemon_builds_quiet_hours_digest_once(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "daemon digest topic", "priority": 0.4, "metadata": {}},
    )
    assert resp.status_code == 201, resp.text
    await post_event(client, "the daemon digest topic came up again")
    resp = await client.get("/v1/alerts/scan?window_days=30")
    assert resp.status_code == 200, resp.text

    report = await daemon_tick(db_session)
    await db_session.commit()
    assert report["digest"] is not None
    assert report["digest"]["delivered"] >= 1
    assert report["health"]["overall"] in ("ok", "degraded", "failed")

    second = await daemon_tick(db_session)
    await db_session.commit()
    assert second["digest"] is None  # one digest per day


async def test_full_runtime_lifecycle_e2e(client: AsyncClient, db_session) -> None:
    """End-to-end wake -> verify -> process -> respond -> follow-up -> idle
    across two devices, with latency markers and the event chain observable."""
    phone = await register_device(client, "e2e-phone")
    watch = await register_device(client, "e2e-watch")
    await heartbeat(client, str(phone["id"]))
    await heartbeat(client, str(watch["id"]))

    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(phone["id"]),
                "signal_score": 0.9,
                "battery_percent": 80.0,
                "proximity_score": 1.0,
                "priority": 0.6,
                "text_hint": "evie",
            },
            {
                "device_id": str(watch["id"]),
                "signal_score": 0.2,
                "battery_percent": 30.0,
                "proximity_score": 0.2,
                "priority": 0.6,
                "text_hint": "evie",
            },
        ],
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["winner"]["device_id"] == str(phone["id"])
    assert outcome["state"] == "verifying"
    session_id = UUID(outcome["session_id"])

    row = (
        await db_session.execute(select(RuntimeSession).where(RuntimeSession.id == session_id))
    ).scalar_one()
    await mark_verified(db_session, row, confidence=0.95, verifier_name="test")
    await db_session.commit()
    assert row.owner_verified is True

    status = (await client.get("/v1/runtime/status")).json()
    assert status["state"] == "awake"
    assert status["session"]["device_id"] == str(phone["id"])

    for to_state in ("processing", "responding", "follow_up", "idle"):
        resp = await client.post("/v1/runtime/transition", json={"to_state": to_state})
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == to_state

    sync = (await client.get("/v1/runtime/sync")).json()
    assert sync["runtime"]["state"] == "idle"
    assert sync["runtime"]["session_id"] == str(session_id)
    latency = sync["latency"]
    for key in (
        "wake_to_awake_ms",
        "wake_to_processing_ms",
        "wake_to_responding_ms",
        "wake_to_follow_up_ms",
    ):
        assert latency[key] is not None, key
    kinds = [event["kind"] for event in sync["events"]]
    assert kinds.count("transition") >= 5
    assert "wake" in kinds
    assert "verify" in kinds
    assert len(sync["devices"]) == 2
    assert all(device["presence"] == "online" for device in sync["devices"])
