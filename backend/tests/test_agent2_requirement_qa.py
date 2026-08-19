"""Requirement-focused QA for Agent 2's identity/world-model seams.

The repository already has broad ROSTER and perception coverage.  This file
keeps the Agent 2 acceptance checks together so the unfinished seams are
visible without taking ownership of the policy core, dispatch, voice, or UI.

The checks are intended to remain green as the world-model seams are wired
through the API and legacy people context.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Base,
    CameraState,
    FaceSample,
    HealthSnapshot,
    ObservationRecord,
    OwnerObject,
    RecognitionLog,
)
from app.utils.text import utcnow


def image(seed: str) -> str:
    return base64.b64encode(seed.encode("ascii") * 80).decode("ascii")


def photos(seed: str, count: int = 5) -> list[dict]:
    return [
        {
            "image_b64": image(seed),
            "quality": 0.92,
            "confidence": 0.97,
            "source": "photo",
        }
        for _ in range(count)
    ]


async def grant_face_consent(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/training/consent",
        json={
            "track": "face_enrollment",
            "purpose": "Agent 2 requirement QA",
            "scope": {"test": True},
            "source": "test",
        },
    )
    assert response.status_code == 201, response.text


async def enroll_face(client: AsyncClient, name: str, seed: str) -> dict:
    response = await client.post(
        "/v1/people/enrollments",
        json={"person_name": name, "photos": photos(seed)},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_agent2_world_model_tables_are_registered() -> None:
    """The Agent 2 migration has corresponding ORM tables in test databases."""

    assert {
        "observations",
        "owner_objects",
        "camera_states",
    } <= set(Base.metadata.tables)


async def test_owner_object_enrollment_keeps_evidence_and_freshness_fields(
    db_session: AsyncSession,
) -> None:
    observed_at = utcnow() - timedelta(minutes=3)
    obj = OwnerObject(
        owner="owner",
        name="field notebook",
        object_type="personal_item",
        enrollment_source="owner_photo",
        appearance_references=[{"evidence_ref": "attachment:photo-1"}],
        common_locations=["desk"],
        last_observed_location="desk",
        last_observed_at=observed_at,
        last_evidence_ref="observation:obs-1",
        last_confidence=0.94,
        last_uncertainty="partially occluded",
        last_freshness_state="fresh",
        possible_matches=[{"label": "black notebook", "confidence": 0.41}],
        status="active",
    )
    observation = ObservationRecord(
        subject="owner",
        subject_type="owner",
        object_or_event="field notebook",
        action="seen",
        location="desk",
        observed_at=observed_at,
        source_device="iphone",
        evidence_ref="attachment:photo-1",
        confidence=0.94,
        uncertainty="partially occluded",
        consent_state="granted",
        retention_class="derived_only",
        freshness_state="fresh",
        stale_after_seconds=3600,
        fact_kind="observed",
        metadata_={"raw_frame_persisted": False},
    )
    db_session.add_all([obj, observation])
    await db_session.commit()

    stored_object = await db_session.get(OwnerObject, obj.id)
    stored_observation = await db_session.get(ObservationRecord, observation.id)
    assert stored_object is not None
    assert stored_object.enrollment_source == "owner_photo"
    assert stored_object.last_evidence_ref == "observation:obs-1"
    assert stored_object.last_freshness_state == "fresh"
    assert stored_observation is not None
    assert stored_observation.fact_kind == "observed"
    assert stored_observation.metadata_["raw_frame_persisted"] is False


async def test_camera_state_records_permission_denial_without_raw_frames(
    db_session: AsyncSession,
) -> None:
    state = CameraState(
        device_id="macbook-camera",
        platform="macos",
        state="paused",
        visible=False,
        permission_state="denied",
        explicit_request=True,
        paused_reason="camera permission denied",
        consent_state="granted",
        raw_frames_persisted=False,
        last_error="Grant EV camera access in System Settings",
    )
    db_session.add(state)
    await db_session.commit()

    stored = await db_session.get(CameraState, state.id)
    assert stored is not None
    assert stored.state == "paused"
    assert stored.permission_state == "denied"
    assert stored.visible is False
    assert stored.explicit_request is True
    assert stored.raw_frames_persisted is False
    assert "camera" in (stored.last_error or "").lower()


def test_camera_helper_has_explicit_permission_states_and_denial_contract() -> None:
    """The hardware helper has no injectable camera in CI, so guard its seam."""

    root = Path(__file__).resolve().parents[2]
    source = (root / "helpers/evvision/Sources/evvision/main.swift").read_text()
    camera = source[source.index("func captureCamera"):source.index("// MARK: - Self-test OCR")]

    assert "case .authorized:" in camera
    assert "case .notDetermined:" in camera
    assert "default:" in camera
    assert camera.count("throw EvError.cameraDenied") >= 2
    assert '"captured": true' in camera
    assert '"persisted": persisted' in camera


async def test_face_enrollment_and_unknown_paths_are_separate(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_face_consent(client)
    enrolled = await enroll_face(client, "Alice", "alice")

    match = await client.post(
        "/v1/people/recognize",
        json={"image_b64": image("alice"), "source": "live_frame"},
    )
    assert match.status_code == 200, match.text
    assert match.json()["resolved"] is True
    assert match.json()["unknown"] is False
    assert match.json()["label"] == "Alice"

    unknown = await client.post(
        "/v1/people/recognize",
        json={"image_b64": image("stranger"), "source": "live_frame"},
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["resolved"] is False
    assert unknown.json()["unknown"] is True
    assert unknown.json()["entity_id"] is None
    assert unknown.json()["recognition_id"] is None

    logs = list((await db_session.execute(select(RecognitionLog))).scalars().all())
    assert len(logs) == 1
    assert str(logs[0].entity_id) == enrolled["enrollment"]["entity_id"]


async def test_face_enrollment_delete_removes_samples_and_invalidates_match(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_face_consent(client)
    enrolled = await enroll_face(client, "Alice", "alice")
    enrollment_id = enrolled["enrollment"]["id"]

    deleted = await client.post(
        f"/v1/people/enrollments/{enrollment_id}/delete",
        params={"reason": "Agent 2 forget test"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"

    assert await db_session.scalar(select(func.count()).select_from(FaceSample)) == 0
    unknown = await client.post(
        "/v1/people/recognize",
        json={"image_b64": image("alice"), "source": "live_frame"},
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["unknown"] is True
    assert unknown.json()["recognition_id"] is None


async def test_face_enrollment_does_not_persist_raw_photos(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_face_consent(client)
    response = await client.post(
        "/v1/people/enrollments",
        json={"person_name": "Alice", "photos": photos("alice")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["raw_photos_stored"] is False
    assert "raw_image" not in FaceSample.__table__.columns
    sample = (await db_session.execute(select(FaceSample))).scalars().first()
    assert sample is not None
    assert image("alice") not in (sample.ciphertext or "")


async def test_last_seen_marks_old_evidence_stale(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_face_consent(client)
    enrolled = await enroll_face(client, "Alice", "alice")
    old = utcnow() - timedelta(days=3)
    db_session.add(
        RecognitionLog(
            entity_id=UUID(enrolled["enrollment"]["entity_id"]),
            label="Alice",
            confidence=0.91,
            source="user",
            created_at=old,
        )
    )
    await db_session.commit()

    context = await client.get("/v1/people/Alice/context")
    assert context.status_code == 200, context.text
    assert context.json()["last_seen"]["freshness_state"] == "stale"


async def test_healthkit_snapshot_preserves_freshness_and_zepp_metadata(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await client.post(
        "/v1/health/snapshot",
        json={
            "occurred_at": (utcnow() - timedelta(minutes=5)).isoformat(),
            "synced_at": utcnow().isoformat(),
            "source": "amazfit_helio",
            "device_id": "iphone-qa",
            "metrics": {"heart_rate": 72, "hrv_ms": 54.2, "steps": 8123},
            "permission_state": "authorized",
            "units": {"heart_rate": "bpm", "hrv_ms": "ms", "steps": "count"},
            "source_metadata": {
                "provider": "HealthKit",
                "source_name": "Zepp",
                "bundle_id": "com.huami.watch.zh",
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["permission_state"] == "authorized"
    assert body["source_metadata"]["source_name"] == "Zepp"
    assert body["freshness_state"] == "fresh"

    stored = await db_session.get(HealthSnapshot, UUID(body["id"]))
    assert stored is not None
    assert stored.source_metadata["bundle_id"] == "com.huami.watch.zh"


async def test_healthkit_old_snapshot_is_marked_stale(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/health/snapshot",
        json={
            "occurred_at": (utcnow() - timedelta(days=2)).isoformat(),
            "synced_at": (utcnow() - timedelta(days=2)).isoformat(),
            "source": "healthkit",
            "metrics": {"heart_rate": 72},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["freshness_state"] == "stale"
