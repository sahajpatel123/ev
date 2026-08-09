"""Tests for consent-gated voice enrollment and voiceprints (Training track 1)."""

from __future__ import annotations

import base64

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VoiceAttemptLog, VoiceEnrollment

SAMPLE_A = b"owner-voice-sample-" * 40
SAMPLE_B = b"other-speaker-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sample_payload(sample: bytes, count: int = 5) -> dict:
    return {
        "samples": [{"audio_b64": b64(sample)} for _ in range(count)],
        "reason": "test enrollment",
    }


async def grant_voice_consent(client: AsyncClient) -> dict:
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def enroll(client: AsyncClient, sample: bytes) -> dict:
    resp = await client.post("/v1/voice/enroll", json=sample_payload(sample))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_voice_enrollment_requires_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 403
    assert "consent" in resp.json()["detail"].lower()


async def test_voice_verify_requires_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.status_code == 403


async def test_consent_lifecycle_grants_and_revokes(client: AsyncClient) -> None:
    consent = await grant_voice_consent(client)
    assert consent["track"] == "voice_enrollment"
    assert consent["revoked_at"] is None

    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201  # idempotent while active
    assert resp.json()["id"] == consent["id"]

    payload = await enroll(client, SAMPLE_A)
    assert payload["raw_samples_stored"] is False
    assert payload["sample_count"] == 5
    assert payload["enrollment"]["version"] == 1
    assert payload["enrollment"]["is_current"] is True
    assert payload["enrollment"]["status"] == "active"

    resp = await client.post(
        "/v1/training/consent/voice_enrollment/revoke",
        json={"reason": "privacy review"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_at"] is not None

    resp = await client.post("/v1/voice/enroll", json=sample_payload(SAMPLE_A))
    assert resp.status_code == 403

    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.status_code == 403

    # Wake is also consent-gated once the wake word is detected.
    resp = await client.post(
        "/v1/voice/wake",
        json={"device_id": "test-mac", "text_hint": "evie"},
    )
    assert resp.status_code == 403


async def test_voice_verify_accepts_owner_and_rejects_other(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll(client, SAMPLE_A)

    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    assert resp.json()["score"] >= resp.json()["threshold"]
    assert resp.json()["enrollment_id"] is not None

    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_B)]},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False
    assert resp.json()["score"] < resp.json()["threshold"]
    assert resp.json()["reason"] == "score_below_threshold"


async def test_voice_reenrollment_versions_and_rollback(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    first = await enroll(client, SAMPLE_A)
    first_id = first["enrollment"]["id"]
    assert first["enrollment"]["version"] == 1

    second = await enroll(client, SAMPLE_B)
    second_id = second["enrollment"]["id"]
    assert second["enrollment"]["version"] == 2
    assert second["enrollment"]["supersedes_id"] == first_id
    assert second["enrollment"]["is_current"] is True

    # v2 (other-speaker template) should reject the owner's original sample.
    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.json()["accepted"] is False

    resp = await client.post(
        f"/v1/voice/enrollments/{second_id}/rollback",
        json={"target_version": 1, "reason": "rollback re-enrollment"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1
    assert resp.json()["is_current"] is True

    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.json()["accepted"] is True


async def test_voice_revocation_and_deletion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    payload = await enroll(client, SAMPLE_A)
    enrollment_id = payload["enrollment"]["id"]

    resp = await client.post(
        f"/v1/voice/enrollments/{enrollment_id}/revoke",
        json={"reason": "voice changed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    resp = await client.post(
        "/v1/training/voice/verify",
        json={"samples": [b64(SAMPLE_A)]},
    )
    assert resp.json()["accepted"] is False

    resp = await client.post(
        f"/v1/voice/enrollments/{enrollment_id}/delete",
        json={"reason": "data subject deletion"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"

    resp = await client.get("/v1/voice/enrollments/export")
    assert resp.status_code == 200
    export = resp.json()
    assert export["voiceprints"] == []
    assert any(e["id"] == enrollment_id for e in export["enrollments"])

    enrollment = await db_session.get(VoiceEnrollment, enrollment_id)
    assert enrollment.status == "deleted"
    assert enrollment.redacted is True
    assert enrollment.ciphertext is None

    audit = (
        await db_session.execute(
            select(VoiceAttemptLog).where(VoiceAttemptLog.outcome == "deleted")
        )
    ).scalars().all()
    assert len(audit) == 1


async def test_voice_export_contains_template_not_raw_samples(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll(client, SAMPLE_A)

    resp = await client.get("/v1/voice/enrollments/export")
    assert resp.status_code == 200, resp.text
    export = resp.json()
    assert len(export["consents"]) == 1
    assert len(export["enrollments"]) == 1
    assert len(export["voiceprints"]) == 1
    voiceprint = export["voiceprints"][0]
    assert voiceprint["embedding"]
    assert len(voiceprint["embedding"]) == 192

    enrollment = await db_session.get(VoiceEnrollment, export["enrollments"][0]["id"])
    assert enrollment is not None
    assert enrollment.ciphertext is not None
    assert SAMPLE_A.decode() not in (enrollment.ciphertext or "")
