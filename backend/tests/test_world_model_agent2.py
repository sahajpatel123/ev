"""Agent 2 acceptance tests for evidence-backed perception and health data."""

from datetime import timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConsentRecord, Entity, FaceEnrollment, ObservationRecord
from app.utils.text import utcnow


async def test_object_enrollment_last_seen_and_stale_evidence(
    client: AsyncClient,
) -> None:
    enrolled = await client.post(
        "/v1/world-model/objects",
        json={
            "name": "AirPods",
            "object_type": "device",
            "appearance_references": ["attachment:owner-photo-1"],
            "common_locations": ["left side of Mac desk"],
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    object_id = enrolled.json()["id"]

    observed_at = (utcnow() - timedelta(days=2)).isoformat()
    observed = await client.post(
        f"/v1/world-model/objects/{object_id}/observations",
        json={
            "location": "left side of Mac desk",
            "observed_at": observed_at,
            "source_device": "mac-camera",
            "evidence_ref": "attachment:desk-photo-22",
            "confidence": 0.91,
            "uncertainty": "the case may have moved after the photo",
        },
    )
    assert observed.status_code == 201, observed.text
    body = (await client.get(f"/v1/world-model/objects/{object_id}/last-seen")).json()
    assert body["found"] is True
    assert body["location"] == "left side of Mac desk"
    assert body["freshness_state"] == "stale"
    assert body["evidence_ref"] == "attachment:desk-photo-22"
    assert "strongest evidence" in body["answer"]
    assert "definitely" not in body["answer"].lower()


async def test_camera_states_are_explicit_visible_and_permission_gated(
    client: AsyncClient,
) -> None:
    denied = await client.put(
        "/v1/world-model/cameras/mac-primary",
        json={"state": "active", "permission_state": "denied", "explicit_request": True},
    )
    assert denied.status_code == 403

    state = await client.put(
        "/v1/world-model/cameras/mac-primary",
        json={"state": "permission_denied", "permission_state": "denied"},
    )
    assert state.status_code == 200, state.text
    assert state.json()["visible"] is False

    active = await client.put(
        "/v1/world-model/cameras/mac-primary",
        json={
            "state": "active",
            "permission_state": "authorized",
            "explicit_request": True,
            "consent_state": "camera_granted",
        },
    )
    assert active.status_code == 200, active.text
    assert active.json()["state"] == "active"
    assert active.json()["visible"] is True
    assert active.json()["raw_frames_persisted"] is False

    paused = await client.put(
        "/v1/world-model/cameras/mac-primary",
        json={"state": "paused", "paused_reason": "system locked"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["visible"] is False


async def test_unknown_person_is_not_persisted_and_enrolled_person_requires_consent(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    unknown = await client.post(
        "/v1/world-model/people/observations",
        json={
            "person_name": "Unknown",
            "location": "workshop",
            "source_device": "iphone-16-pro",
            "evidence_ref": "live:frame-1",
            "confidence": 0.88,
        },
    )
    assert unknown.status_code == 201, unknown.text
    assert unknown.json()["unknown"] is True
    assert unknown.json()["identity_persisted"] is False
    assert await db_session.scalar(select(ObservationRecord).where(ObservationRecord.subject == "Unknown")) is None

    entity = Entity(
        entity_type="person",
        name="Alice",
        canonical_key="person:alice",
        aliases=[],
    )
    db_session.add(entity)
    consent = ConsentRecord(
        track="face_enrollment",
        purpose="world-model test",
        scope={"test": True},
        source="test",
    )
    db_session.add(consent)
    await db_session.flush()
    db_session.add(
        FaceEnrollment(
            entity_id=entity.id,
            consent_id=consent.id,
            sample_count=5,
            ciphertext="encrypted-template",
            salt="test-salt",
        )
    )
    await db_session.commit()
    enrolled = await client.post(
        "/v1/world-model/people/observations",
        json={
            "person_name": "Alice",
            "location": "front door",
            "source_device": "iphone-se-2020",
            "evidence_ref": "photo:alice-1",
            "confidence": 0.95,
            "consent_state": "explicit",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    forgotten = await client.delete("/v1/world-model/people/Alice")
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["observations_forgotten"] == 1


async def test_observation_keeps_guesses_as_non_fact_candidates_and_discards_raw_frames(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    guess = await client.post(
        "/v1/world-model/observations",
        json={
            "subject": "owner",
            "object": "wallet",
            "action": "seen",
            "location": "desk",
            "source_device": "mac-camera",
            "evidence_ref": "attachment:wallet-1",
            "confidence": 0.4,
            "uncertainty": "uncertain",
            "consent_state": "owner_confirmed",
            "fact_kind": "guessed",
        },
    )
    assert guess.status_code == 201, guess.text
    assert guess.json()["fact_kind"] == "guessed"
    assert (await client.get("/v1/world-model/observations")).json()[0]["fact_kind"] == "guessed"

    created = await client.post(
        "/v1/world-model/observations",
        json={
            "subject": "owner",
            "object": "wallet",
            "action": "seen",
            "location": "desk",
            "source_device": "mac-camera",
            "evidence_ref": "attachment:wallet-2",
            "confidence": 0.8,
            "uncertainty": "may have moved",
            "consent_state": "owner_confirmed",
            "metadata": {"raw_frame": "do-not-store", "camera_mode": "one-shot"},
        },
    )
    assert created.status_code == 201, created.text
    stored = await db_session.get(ObservationRecord, UUID(created.json()["id"]))
    assert stored is not None
    assert "raw_frame" not in (stored.metadata_ or {})
    assert stored.metadata_["raw_frame_persisted"] is False


async def test_health_snapshot_reports_freshness_and_zepp_provenance(
    client: AsyncClient,
) -> None:
    synced_at = (utcnow() - timedelta(days=3)).isoformat()
    snapshot = await client.post(
        "/v1/health/snapshot",
        json={
            "source": "amazfit_helio",
            "device_id": "iphone-16-pro",
            "synced_at": synced_at,
            "metrics": {"sleep_hours": 6.5, "resting_hr": 62},
            "source_metadata": {"zepp_account": "owner-local"},
        },
    )
    assert snapshot.status_code == 201, snapshot.text
    body = snapshot.json()
    assert body["freshness_state"] == "stale"
    assert body["synced_at"]
    assert body["source_metadata"]["provider_chain"][:3] == [
        "Amazfit Helio",
        "Zepp",
        "Apple Health",
    ]

    summary = await client.get("/v1/health/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["freshness_state"] == "stale"
    assert summary.json()["last_sync_at"]

    trend = await client.get("/v1/health/trends", params={"metric": "sleep_hours"})
    assert trend.status_code == 200, trend.text
    assert trend.json()["freshness_state"] == "stale"
