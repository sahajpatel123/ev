"""Regional compliance, transparency, erasure, and retention sweep tests."""

from __future__ import annotations

import base64
from datetime import timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.models import AccessLog, ConsentRecord, Event, VoiceEnrollment
from app.utils.text import utcnow

SAMPLE_A = b"owner-voice-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sample_payload(sample: bytes, count: int = 5) -> dict:
    return {
        "samples": [{"audio_b64": b64(sample)} for _ in range(count)],
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
    monkeypatch.delenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", raising=False)
    await grant_voice_consent(client)

    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 403, resp.text
    assert "remote" in resp.json()["detail"].lower()

    resp = await client.post(
        "/v1/training/voice/verify", json={"samples": [b64(SAMPLE_A)]}
    )
    assert resp.status_code == 403, resp.text

    # Explicit regional-policy approval re-enables the remote encoder.
    monkeypatch.setenv("EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING", "true")
    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 201, resp.text


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


def test_daemon_compliance_sweep_schedule(monkeypatch) -> None:
    from app.workers import runtime_daemon

    runtime_daemon._COMPLIANCE_LAST_RUN = 0.0
    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "0")
    assert runtime_daemon._compliance_due() is False  # disabled

    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "24")
    assert runtime_daemon._compliance_due() is True
    assert runtime_daemon._compliance_due() is False  # cooldown active

    monkeypatch.setenv("EV_COMPLIANCE_SWEEP_HOURS", "not-a-number")
    runtime_daemon._COMPLIANCE_LAST_RUN = 0.0
    assert runtime_daemon._compliance_due() is True  # safe default 24h
