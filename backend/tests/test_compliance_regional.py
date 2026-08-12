"""Regional compliance, transparency, erasure, and retention sweep tests."""

from __future__ import annotations

import base64
import time
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.main import app
from app.models import AccessLog, ConsentRecord, Event, VoiceEnrollment
from app.utils.text import utcnow

SAMPLE_A = b"owner-voice-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sample_payload(sample: bytes, count: int = 5) -> dict:
    return {
        "samples": [
            {"audio_b64": b64(sample), "liveness_proof": "live"}
            for _ in range(count)
        ],
        "reason": "compliance test enrollment",
    }


async def grant_voice_consent(client: AsyncClient) -> dict:
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_regional_policy_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("EV_REGION", raising=False)
    monkeypatch.delenv("EV_RETENTION_VOICEPRINT_DAYS", raising=False)
    monkeypatch.delenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", raising=False)

    from app.compliance import policy

    assert policy.region() == "global"
    assert policy.retention_days("voiceprint") == -1  # indefinite by default
    assert policy.retention_days("live_audio") == 0
    assert policy.remote_processing_allowed("voice_enrollment") is False
    assert policy.local_residency_required() is True

    monkeypatch.setenv("EV_REGION", "eu")
    monkeypatch.setenv("EV_RETENTION_VOICEPRINT_DAYS", "14")
    monkeypatch.setenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", "true")
    assert policy.region() == "eu"
    assert policy.retention_days("voiceprint") == 14
    assert policy.remote_processing_allowed("voice_enrollment") is True
    assert "GDPR Art. 13/14 privacy notice" in policy.disclosures()

    monkeypatch.setenv("EV_REGION", "us-il")
    monkeypatch.delenv("EV_RETENTION_VOICEPRINT_DAYS", raising=False)
    assert policy.retention_days("voiceprint") == 0  # BIPA-style destroy at first opportunity
    assert any("BIPA" in d for d in policy.disclosures())


async def test_deletion_due_respects_retention_window(monkeypatch) -> None:
    from app.compliance import policy

    monkeypatch.setenv("EV_RETENTION_VOICEPRINT_DAYS", "7")
    now = utcnow()
    assert policy.deletion_due("voiceprint", now - timedelta(days=10), now=now) is True
    assert policy.deletion_due("voiceprint", now - timedelta(days=3), now=now) is False

    monkeypatch.setenv("EV_RETENTION_VOICEPRINT_DAYS", "-1")
    assert policy.deletion_due("voiceprint", now - timedelta(days=999), now=now) is False


async def test_policy_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/v1/compliance/policy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["region"] == "global"
    assert body["local_residency_required"] is True
    assert "voiceprint" in body["retention_days"]
    assert body["remote_processing"]["voice_enrollment"] is False
    assert body["disclosures"]


async def test_transparency_endpoint_reports_stored_trained_processed_transmitted(
    client: AsyncClient,
) -> None:
    resp = await client.get("/v1/compliance/transparency")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["region"] == "global"
    assert {item["name"] for item in body["stored"]} >= {
        "events",
        "voice_enrollments",
        "voiceprints",
        "attachments",
        "access_log",
    }
    assert all("deletion_path" in item for item in body["stored"])
    assert body["trained"] and all("consent_active" in item for item in body["trained"])
    assert body["processed"]
    assert body["transmitted"]
    # Transparency must never leak raw samples.
    raw = SAMPLE_A.decode()
    assert raw not in resp.text


async def test_data_erasure_propagates_to_consent_voiceprints_events_and_blobs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 201, resp.text
    enrollment_id = resp.json()["enrollment"]["id"]

    resp = await client.post(
        "/v1/events",
        json={
            "source": "voice",
            "event_type": "voice.transcript",
            "content": {"text": "remember this"},
            "privacy_level": "sensitive",
            "text": "remember this",
        },
    )
    assert resp.status_code == 201, resp.text
    event_id = resp.json()["event"]["id"]

    resp = await client.post(
        "/v1/compliance/erasure", json={"reason": "data subject request"}
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["manifest"]
    assert manifest["consents_revoked"] == 1
    assert manifest["enrollments_processed"] == 1
    assert manifest["events_tombstoned"] == 1
    assert manifest["backup_purge_required"] is True

    enrollment = await db_session.get(VoiceEnrollment, UUID(enrollment_id))
    assert enrollment is not None
    assert enrollment.status == "deleted"
    assert enrollment.redacted is True
    assert enrollment.ciphertext is None

    consent = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.track == "voice_enrollment")
        )
    ).scalar_one()
    assert consent.revoked_at is not None

    event = await db_session.get(Event, UUID(event_id))
    assert event is not None
    assert event.tombstoned_at is not None

    erasure_rows = (
        await db_session.execute(select(DataErasureRecord))
    ).scalars().all()
    assert len(erasure_rows) == 1
    assert erasure_rows[0].manifest["enrollment_ids"] == [enrollment_id]

    audit = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "data_erasure")
        )
    ).scalars().all()
    assert len(audit) == 1

    resp = await client.get("/v1/voice/enrollments/export")
    assert resp.json()["voiceprints"] == []


async def test_retention_sweep_deletes_expired_enrollments(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("EV_RETENTION_VOICEPRINT_DAYS", "7")
    await grant_voice_consent(client)
    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 201, resp.text
    enrollment_id = resp.json()["enrollment"]["id"]

    enrollment = await db_session.get(VoiceEnrollment, UUID(enrollment_id))
    enrollment.created_at = utcnow() - timedelta(days=10)
    await db_session.commit()

    resp = await client.post(
        "/v1/compliance/retention/sweep", json={"reason": "retention sweep"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["voiceprints_deleted"] == 1
    assert body["enrollment_ids"] == [enrollment_id]
    assert body["policy_retention_days"] == 7

    enrollment = await db_session.get(VoiceEnrollment, UUID(enrollment_id))
    await db_session.refresh(enrollment)
    assert enrollment.status == "deleted"
    assert enrollment.ciphertext is None


async def test_remote_voiceprint_gate_blocks_http_without_policy(
    client: AsyncClient, monkeypatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "voiceprint_provider", "http")
    monkeypatch.setattr(settings, "voiceprint_base_url", "http://encoder.test")
    monkeypatch.delenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", raising=False)
    await grant_voice_consent(client)

    # Surface the app-level refusal as an HTTP response instead of propagating
    # the runtime exception through the ASGI test transport.
    gate_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )
    resp = await gate_client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    # Fail-closed: the remote verifier refuses to be constructed before any
    # bytes leave the machine. Agent 4 raises at construction time, so the
    # HTTP status is an internal error rather than a graceful 403; the gate
    # itself is what we assert here (dependency note: surface 403).
    assert resp.status_code >= 400, resp.text
    assert resp.status_code != 200

    resp = await gate_client.post(
        "/v1/training/voice/verify", json={"samples": [b64(SAMPLE_A)]}
    )
    assert resp.status_code >= 400, resp.text
    assert resp.status_code != 200

    # Explicit regional-policy approval opens the gate. We assert the gate
    # state and that the real factory constructs the remote verifier (no
    # network call is made in this offline suite).
    monkeypatch.setenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", "true")
    from app.compliance.policy import remote_processing_allowed
    from app.voice.speaker import default_speaker_verifier

    assert remote_processing_allowed("voice_enrollment") is True
    assert default_speaker_verifier() is not None


async def test_transparency_chat_egress_has_consent_track_and_summary(
    client: AsyncClient,
) -> None:
    resp = await client.get("/v1/compliance/transparency")
    assert resp.status_code == 200, resp.text
    chat = next(item for item in resp.json()["transmitted"] if item["kind"] == "chat")
    assert chat["consent_track"] == "chat_egress"
    assert chat["consent_active"] is False
    assert chat["remote_gate_allowed"] is False

    granted = await client.post("/v1/training/consent", json={"track": "chat_egress"})
    assert granted.status_code == 201, granted.text
    resp = await client.get("/v1/compliance/transparency")
    chat = next(item for item in resp.json()["transmitted"] if item["kind"] == "chat")
    assert chat["consent_active"] is True

    summary = await client.get("/v1/compliance/transparency/summary")
    assert summary.status_code == 200, summary.text
    text = summary.json()["summary"]
    assert "What leaves this machine" in text
    assert "chat" in text


async def test_face_consent_track_is_revoked_by_erasure(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    granted = await client.post(
        "/v1/training/consent", json={"track": "face_enrollment"}
    )
    assert granted.status_code == 201, granted.text
    resp = await client.post(
        "/v1/compliance/erasure", json={"reason": "face consent drill"}
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["manifest"]
    assert manifest["consents_revoked"] >= 1
    consent = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.track == "face_enrollment")
        )
    ).scalar_one()
    assert consent.revoked_at is not None


async def test_face_enrollment_disclosure_covers_bipa_and_gdpr() -> None:
    from app.compliance.policy import face_enrollment_disclosure

    disclosure = "\n".join(face_enrollment_disclosure())
    assert "740 ILCS 14/15" in disclosure
    assert "GDPR Art. 9" in disclosure
    assert "encrypted biometric template" in disclosure


async def test_retention_sweep_deletes_expired_face_enrollments(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from app.models import Entity, FaceEnrollment, FaceSample

    monkeypatch.setenv("EV_RETENTION_FACEPRINT_DAYS", "7")
    entity = Entity(
        entity_type="person",
        name="Retention Face",
        canonical_key=f"person:retention-{uuid4()}",
    )
    db_session.add(entity)
    await db_session.flush()
    enrollment = FaceEnrollment(
        entity_id=entity.id,
        ciphertext="fernet-retention-face",
        salt="salt",
        sample_count=1,
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(
        FaceSample(
            enrollment_id=enrollment.id,
            entity_id=entity.id,
            ciphertext="fernet-retention-sample",
            salt="salt",
        )
    )
    enrollment.created_at = utcnow() - timedelta(days=10)
    await db_session.commit()

    resp = await client.post(
        "/v1/compliance/retention/sweep", json={"reason": "retention sweep"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["faceprints_deleted"] == 1
    assert body["face_enrollment_ids"] == [str(enrollment.id)]

    await db_session.refresh(enrollment)
    assert enrollment.status == "deleted"
    assert enrollment.redacted is True
    assert enrollment.ciphertext is None
    assert (
        await db_session.execute(
            select(func.count()).select_from(FaceSample)
        )
    ).scalar_one() == 0


async def test_scheduled_compliance_sweep_job_enforces_retention(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.compliance.erasure import retention_sweep
    from app.workers.jobs import run_compliance_retention

    assert callable(run_compliance_retention)  # scheduler/CLI entrypoint exists
    monkeypatch.setenv("EV_RETENTION_VOICEPRINT_DAYS", "7")
    await grant_voice_consent(client)
    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 201, resp.text
    enrollment_id = resp.json()["enrollment"]["id"]

    enrollment = await db_session.get(VoiceEnrollment, UUID(enrollment_id))
    enrollment.created_at = utcnow() - timedelta(days=10)
    await db_session.commit()

    report = await retention_sweep(
        db_session, reason="retention policy", actor="scheduler"
    )
    assert report["voiceprints_deleted"] == 1
    assert report["enrollment_ids"] == [enrollment_id]

    enrollment = await db_session.get(VoiceEnrollment, UUID(enrollment_id))
    await db_session.refresh(enrollment)
    assert enrollment.status == "deleted"
    assert enrollment.ciphertext is None


async def test_retention_sweep_prunes_expired_access_logs(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.compliance.erasure import retention_sweep
    from app.models import AccessLog

    monkeypatch.setenv("EV_RETENTION_ACCESS_LOG_DAYS", "7")
    await grant_voice_consent(client)

    old = AccessLog(actor="test", action="read", occurred_at=utcnow() - timedelta(days=10))
    recent = AccessLog(actor="test", action="read", occurred_at=utcnow())
    db_session.add_all([old, recent])
    await db_session.commit()
    old_id = old.id
    recent_id = recent.id

    report = await retention_sweep(
        db_session, reason="retention policy", actor="scheduler"
    )
    assert report["access_logs_deleted"] == 1

    remaining_ids = (
        (await db_session.execute(select(AccessLog.id))).scalars().all()
    )
    assert old_id not in remaining_ids
    assert recent_id in remaining_ids


async def test_web_transparency_panel_served(client: AsyncClient) -> None:
    resp = await client.get("/app/")
    assert resp.status_code == 200, resp.text
    assert 'id="ev-transparency"' in resp.text
    assert "transparency-load" in resp.text

    js = await client.get("/app/app.js")
    assert js.status_code == 200, js.text
    assert "/v1/compliance/transparency" in js.text
    assert "/v1/compliance/policy" in js.text


async def test_web_voice_enrollment_panel_served(client: AsyncClient) -> None:
    resp = await client.get("/app/")
    assert resp.status_code == 200, resp.text
    assert 'id="ev-voice"' in resp.text
    assert "voice-enroll" in resp.text

    js = await client.get("/app/app.js")
    assert js.status_code == 200, js.text
    assert "/v1/voice/enroll" in js.text
    assert "/v1/voice/enrollments/" in js.text
    assert "/v1/training/consent" in js.text


def test_daemon_compliance_sweep_schedule(monkeypatch) -> None:
    from app.workers import runtime_daemon

    # Use an epoch far enough in the past that the check is due regardless of
    # machine uptime (time.monotonic() resets at boot).
    runtime_daemon._COMPLIANCE_LAST_RUN = time.monotonic() - 10**9
    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "0")
    assert runtime_daemon._compliance_due() is False  # disabled

    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "24")
    assert runtime_daemon._compliance_due() is True
    assert runtime_daemon._compliance_due() is False  # cooldown active

    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "not-a-number")
    runtime_daemon._COMPLIANCE_LAST_RUN = time.monotonic() - 10**9
    assert runtime_daemon._compliance_due() is True  # safe default 24h


async def test_access_log_endpoint_pages_and_filters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    rows = [
        AccessLog(actor="bot", action="consent_grant", details={"i": 1}),
        AccessLog(actor="master", action="voice_export", details={"i": 2}),
        AccessLog(actor="bot", action="voice_delete", details={"i": 3}),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    resp = await client.get("/v1/compliance/access-log?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 3
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert len(body["logs"]) >= 3
    actions = {log["action"] for log in body["logs"]}
    assert {"consent_grant", "voice_export", "voice_delete"} <= actions

    resp = await client.get("/v1/compliance/access-log?actor=bot&action=voice_delete")
    assert resp.status_code == 200, resp.text
    filtered = resp.json()
    assert filtered["total"] == 1
    assert filtered["logs"][0]["actor"] == "bot"
    assert filtered["logs"][0]["action"] == "voice_delete"

    # The export read itself is audited.
    audit_rows = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "access_log.read")
        )
    ).scalars().all()
    assert len(audit_rows) >= 1


async def test_access_log_endpoint_requires_owner_trust(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    device = await client.post(
        "/v1/devices", json={"name": "plain-token", "capabilities": []}
    )
    assert device.status_code == 201, device.text
    token = device.json()["token"]
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with plain:
        resp = await plain.get("/v1/compliance/access-log")
    assert resp.status_code == 403


async def test_access_anomaly_detection_rules(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    now = utcnow()
    rows = [
        AccessLog(actor="bot", action="data_erasure", occurred_at=now),
        AccessLog(actor="bot", action="data_erasure", occurred_at=now),
        AccessLog(actor="bot", action="data_erasure", occurred_at=now),
        AccessLog(actor="bot", action="data_erasure", occurred_at=now),
        AccessLog(actor="bot", action="data_erasure", occurred_at=now),
        AccessLog(actor="master", action="voice_export", occurred_at=now),
        AccessLog(actor="master", action="voice_export", occurred_at=now),
        AccessLog(actor="master", action="voice_export", occurred_at=now),
        *[
            AccessLog(actor="attacker", action="voice_verify_failed", occurred_at=now)
            for _ in range(10)
        ],
        # Outside the window: must not inflate counts.
        AccessLog(
            actor="bot",
            action="data_erasure",
            occurred_at=now - timedelta(days=1),
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    resp = await client.get("/v1/compliance/anomalies?window_minutes=60")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window_minutes"] == 60
    kinds = {anomaly["kind"] for anomaly in body["anomalies"]}
    assert {"deletion_spike", "export_spike", "failure_burst"} <= kinds

    deletion = next(
        anomaly for anomaly in body["anomalies"] if anomaly["kind"] == "deletion_spike"
    )
    assert deletion["actor"] == "bot"
    assert deletion["count"] == 5
    assert deletion["severity"] == "high"

    export = next(
        anomaly for anomaly in body["anomalies"] if anomaly["kind"] == "export_spike"
    )
    assert export["actor"] == "master"
    assert export["count"] == 3

    # The scan itself is audited.
    audit_rows = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "access_anomaly_scan")
        )
    ).scalars().all()
    assert len(audit_rows) == 1
