"""Focused tests for Agent 2's evidence-backed world-model persistence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.world_memory import (
    ObservationContract,
    delete_observation,
    delete_person_observations,
    enroll_owner_object,
    explain_observation,
    forget_observation,
    forget_person_observations,
    freshness_for,
    last_seen_evidence,
    mark_stale_evidence,
    observation_dict,
    owner_object_dict,
    person_record,
    record_observation,
    record_owner_object_observation,
    record_person_observation,
    upsert_camera_state,
)
from app.models import ConsentRecord, Entity, FaceEnrollment
from app.utils.text import utcnow


def at(hour: int = 12) -> datetime:
    return datetime(2026, 8, 17, hour, 0, tzinfo=UTC)


def contract(**overrides) -> ObservationContract:
    values = {
        "subject": "owner",
        "object": "backpack",
        "action": "seen",
        "location": "lab",
        "timestamp": at(),
        "source_device": "mac-camera",
        "evidence_ref": "attachment:abc123",
        "confidence": 0.91,
        "uncertainty": "partially occluded",
        "consent_state": "granted",
        "retention_class": "short",
        "stale_after_seconds": 60,
        "fact_kind": "observed",
    }
    values.update(overrides)
    return ObservationContract(**values)


async def test_observation_contract_is_idempotent_and_discards_raw_frames(
    db_session: AsyncSession,
) -> None:
    observation = contract(
        fact_kind="guessed",
        metadata={
            "camera_id": "cam-1",
            "raw_frame": b"must never be persisted",
            "nested": {"image_b64": "secret"},
        },
    )
    first = await record_observation(db_session, observation, actor="vision")
    retry = await record_observation(db_session, observation, actor="retry")

    assert retry.id == first.id
    assert first.fact_kind == "guessed"
    assert first.metadata_["raw_frame_persisted"] is False
    assert first.metadata_["raw_frame_discarded"] is True
    assert "raw_frame" not in first.metadata_
    assert "image_b64" not in first.metadata_["nested"]
    assert first.metadata_["recorded_by"] == "vision"
    serialized = observation_dict(first, now=at() + timedelta(seconds=61))
    assert serialized["object"] == "backpack"
    assert serialized["timestamp"].startswith("2026-08-17T12:00:00")
    assert serialized["freshness_state"] == "stale"


async def test_stale_evidence_and_explain_source_are_explicit(
    db_session: AsyncSession,
) -> None:
    base = utcnow()
    later = base + timedelta(seconds=61)
    row = await record_observation(db_session, contract(timestamp=base), actor="test")
    assert freshness_for(row.observed_at, stale_after_seconds=60, now=later) == "stale"
    assert await mark_stale_evidence(db_session, now=later) == 1
    explanation = await explain_observation(db_session, row.id, now=later)
    assert explanation["source"] == {
        "device": "mac-camera",
        "evidence_ref": "attachment:abc123",
        "observed_at": base.isoformat(),
        "kind": "observed",
        "consent_state": "granted",
    }
    assert "confidence 0.910" in explanation["why"]


async def test_owner_object_last_seen_ignores_guesses_and_converges(
    db_session: AsyncSession,
) -> None:
    enrolled = await enroll_owner_object(
        db_session,
        name="blue backpack",
        object_type="bag",
        appearance_references=["attachment:one"],
        common_locations=["lab"],
    )
    retry = await enroll_owner_object(
        db_session,
        name="blue backpack",
        object_type="bag",
        appearance_references=["attachment:one", "attachment:two"],
    )
    assert retry.id == enrolled.id
    assert retry.appearance_references == ["attachment:one", "attachment:two"]

    observed = await record_owner_object_observation(
        db_session,
        enrolled.id,
        contract(object="blue backpack", timestamp=at(13)),
    )
    await record_owner_object_observation(
        db_session,
        enrolled.id,
        contract(
            object="blue backpack",
            timestamp=at(14),
            fact_kind="guessed",
            confidence=0.32,
            metadata={"possible_matches": [{"name": "blue backpack", "confidence": 0.32}]},
        ),
        possible_matches=[{"name": "blue backpack", "confidence": 0.32}],
    )
    details = await owner_object_dict(db_session, enrolled.id, now=at(15))
    assert details["last_seen"]["timestamp"].startswith("2026-08-17T13:00:00")
    assert details["last_seen"]["confidence"] == observed.confidence
    assert details["possible_matches"][0]["confidence"] == 0.32


async def test_person_identity_requires_active_enrollment_consent(
    db_session: AsyncSession,
) -> None:
    entity = Entity(
        name="Alice",
        entity_type="person",
        canonical_key="person:alice",
    )
    consent = ConsentRecord(
        track="face_enrollment",
        purpose="test",
        scope={"owner": True},
        source="test",
    )
    db_session.add_all([entity, consent])
    await db_session.flush()
    db_session.add(
        FaceEnrollment(
            entity_id=entity.id,
            consent_id=consent.id,
            sample_count=5,
            ciphertext="encrypted-test-template",
            salt="test-salt",
        )
    )
    await db_session.flush()

    known = await record_person_observation(
        db_session,
        contract(subject="candidate", object="person", action="present"),
        person_name="Alice",
    )
    assert known.subject == "Alice"
    assert known.metadata_["identity_status"] == "enrolled"
    assert known.consent_state == "granted"
    known_record = await person_record(db_session, "Alice")
    assert known_record["enrolled"] is True
    assert known_record["last_seen"]["subject"] == "Alice"
    assert await forget_person_observations(db_session, "Alice", reason="forget sighting") == 1
    assert (await person_record(db_session, "Alice"))["last_seen"] is None

    unknown = await record_person_observation(
        db_session,
        contract(subject="stranger", object="person", action="present"),
        person_name="Not Enrolled",
    )
    assert unknown.subject == "unknown"
    assert unknown.metadata_["identity_status"] == "unknown"
    assert "entity_id" not in unknown.metadata_
    unknown_record = await person_record(db_session, "Not Enrolled")
    assert unknown_record["identity_status"] == "unknown"
    assert unknown_record["enrolled"] is False
    assert unknown_record["last_seen"] is None

    consent.revoked_at = at(16)
    revoked = await person_record(db_session, "Alice")
    assert revoked["consent_state"] == "revoked"
    assert revoked["enrolled"] is False

    # Deletion is separate from forget and does not erase enrollment ownership.
    assert await delete_person_observations(db_session, "Alice") == 1


async def test_forget_delete_and_camera_frame_defaults(
    db_session: AsyncSession,
) -> None:
    row = await record_observation(db_session, contract(idempotency_key=str(uuid4())))
    await forget_observation(db_session, row.id, reason="no longer useful", at=at(17))
    assert await last_seen_evidence(db_session, subject="owner") is None
    assert row.deleted_at == at(17)
    await delete_observation(db_session, row.id)
    assert await db_session.get(type(row), row.id) is None

    camera = await upsert_camera_state(db_session, device_id="camera-1")
    assert camera.raw_frames_persisted is False
    with pytest.raises(ValueError, match="explicit camera consent"):
        await upsert_camera_state(
            db_session,
            device_id="camera-1",
            persist_raw_frames=True,
            consent_state="not_granted",
        )
    camera = await upsert_camera_state(
        db_session,
        device_id="camera-1",
        persist_raw_frames=True,
        consent_state="granted",
    )
    assert camera.raw_frames_persisted is True
