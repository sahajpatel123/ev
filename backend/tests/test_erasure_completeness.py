"""Erasure completeness: every biometric/derived-personal table is covered.

AGENT 19 VAULT acceptance: the erasure manifest enumerates the covered tables
and the post-erasure state proves the content is gone (deleted, tombstoned,
redacted, or ciphertext-nulled) for each one.
"""

from __future__ import annotations

import base64
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.models import (
    AccessLog,
    AdapterRegistration,
    Attachment,
    ConsentRecord,
    Entity,
    Event,
    FaceEnrollment,
    FaceSample,
    FilterLedger,
    FilterRecalibration,
    ModelCallLog,
    PersonalizationCalibration,
    PublicFigureCache,
    RecognitionLog,
    TrainingCorpusSnapshot,
    VoiceEnrollment,
    VoicePrint,
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _voice_sample_payload() -> dict:
    return {
        "samples": [
            {"audio_b64": b64(b"erasure-sample-" * 20), "liveness_proof": "live"}
            for _ in range(5)
        ],
        "reason": "erasure completeness enrollment",
    }


async def _seed_all_biometric_and_derived_rows(
    client, db_session: AsyncSession
) -> None:
    for track in (
        "voice_enrollment",
        "face_enrollment",
        "training_corpus",
        "life_data_personalization",
        "adapter_fine_tuning",
        "filter_self_improvement",
        "chat_egress",
    ):
        resp = await client.post("/v1/training/consent", json={"track": track})
        assert resp.status_code == 201, resp.text

    enrolled = await client.post("/v1/voice/enroll", json=_voice_sample_payload())
    assert enrolled.status_code == 201, enrolled.text

    entity = Entity(
        entity_type="person",
        name="Test Person",
        canonical_key=f"person:test-{uuid4()}",
        summary="derived summary that must die",
    )
    db_session.add(entity)
    await db_session.flush()

    face = FaceEnrollment(
        entity_id=entity.id,
        ciphertext="fernet-face-template",
        salt="face-salt",
        sample_count=5,
    )
    db_session.add(face)
    await db_session.flush()
    db_session.add(
        FaceSample(
            enrollment_id=face.id,
            entity_id=entity.id,
            ciphertext="fernet-face-sample",
            salt="sample-salt",
        )
    )
    db_session.add(
        RecognitionLog(label="Test Person", entity_id=entity.id, source="user")
    )
    db_session.add(
        PublicFigureCache(
            name="Ada Lovelace",
            canonical_key="public-figure:ada-lovelace",
            data={"summary": "cached biodata"},
            source_url="https://example.test/ada",
        )
    )
    db_session.add(
        AdapterRegistration(
            name="style-adapter",
            eval_metrics={"profile": {"style": ["assertive"], "avg_length": 42}},
        )
    )
    db_session.add(
        PersonalizationCalibration(
            calibrations={"memory_type": {"decision": 1.7}},
            evidence={"corrections": 12},
        )
    )
    db_session.add(
        FilterRecalibration(
            metrics={"block_rate": 0.4},
            proposals=[{"threshold": 0.5}],
            policy={"thresholds": {"block": 0.5}},
        )
    )
    db_session.add(
        TrainingCorpusSnapshot(
            entries=[{"text": "private training entry"}],
            source_counts={"filter_ledger": 1},
            entry_count=1,
            content_hash="a" * 64,
        )
    )

    thread_id = uuid4()
    now = None
    from app.utils.text import utcnow

    now = utcnow()
    event = Event(
        source="voice",
        event_type="voice.transcript",
        content={"text": "private spoken words"},
        conversation_id=thread_id,
        sha256="c" * 64,
        occurred_at=now,
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        Attachment(
            event_id=event.id,
            filename="clip.wav",
            content_type="audio/wav",
            size_bytes=1024,
            storage_key=f"attachments/{uuid4()}.wav",
            sha256="d" * 64,
        )
    )
    db_session.add(
        FilterLedger(
            request_id="erasure-completeness-req",
            conversation_id=thread_id,
            stage="input",
            action="run",
            name="input_filter",
            severity="info",
            detail={"flags": []},
            draft="private spoken words",
            final_text="ok",
        )
    )
    db_session.add(
        ModelCallLog(
            request_id="erasure-completeness-req",
            actor="voice",
            provider="mock",
            model="mock",
            status="ok",
            envelope={"memories": [{"text": "private spoken words"}]},
            envelope_hash="e" * 64,
        )
    )
    await db_session.commit()


BIOMETRIC_DERIVED_TABLES = {
    "voice_enrollments",
    "voice_prints",
    "face_enrollments",
    "face_samples",
    "recognition_log",
    "public_figure_cache",
    "personalization_calibrations",
    "filter_recalibrations",
    "adapter_registrations",
    "training_corpus_snapshots",
    "filter_ledger",
    "model_call_log",
    "events",
    "attachments",
    "consent_records",
}


async def test_erasure_covers_every_biometric_and_derived_table(
    client, db_session: AsyncSession
) -> None:
    await _seed_all_biometric_and_derived_rows(client, db_session)

    resp = await client.post(
        "/v1/compliance/erasure", json={"reason": "completeness drill"}
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["manifest"]

    # The manifest must enumerate every biometric/derived table.
    assert set(manifest["covered_tables"]) >= BIOMETRIC_DERIVED_TABLES

    # Voice enrollment + voiceprint ciphertext destroyed.
    enrollments = (await db_session.execute(select(VoiceEnrollment))).scalars().all()
    assert len(enrollments) >= 1
    assert all(e.status == "deleted" and e.redacted and e.ciphertext is None for e in enrollments)
    prints = (await db_session.execute(select(VoicePrint))).scalars().all()
    assert len(prints) >= 1
    assert all(
        p.redacted and p.embedding_ciphertext is None and p.embedding_salt is None
        for p in prints
    )

    # Face templates redacted, samples/sightings/cache destroyed.
    faces = (await db_session.execute(select(FaceEnrollment))).scalars().all()
    assert len(faces) == 1
    assert faces[0].status == "deleted" and faces[0].redacted
    assert faces[0].ciphertext is None and faces[0].salt is None
    assert (await db_session.execute(select(func.count()).select_from(FaceSample))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(RecognitionLog))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(PublicFigureCache))).scalar_one() == 0

    # Derived personalization/adapters/filter content destroyed.
    adapters = (await db_session.execute(select(AdapterRegistration))).scalars().all()
    assert len(adapters) == 1
    assert adapters[0].redacted and adapters[0].eval_metrics == {}
    assert adapters[0].adapter_ref is None
    calibrations = (
        (await db_session.execute(select(PersonalizationCalibration)))
        .scalars()
        .all()
    )
    assert len(calibrations) == 1
    assert calibrations[0].is_current is False
    assert calibrations[0].calibrations == {} and calibrations[0].evidence == {}
    recalibrations = (
        (await db_session.execute(select(FilterRecalibration))).scalars().all()
    )
    assert len(recalibrations) == 1
    assert recalibrations[0].redacted
    assert (
        recalibrations[0].metrics == {}
        and recalibrations[0].proposals == []
        and recalibrations[0].policy == {}
    )
    snapshots = (
        (await db_session.execute(select(TrainingCorpusSnapshot))).scalars().all()
    )
    assert len(snapshots) == 1
    assert snapshots[0].redacted and snapshots[0].entries == []
    assert snapshots[0].content_hash is None

    # Event content tombstoned, ledger/model-call content redacted, blobs gone.
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    assert events[0].tombstoned_at is not None
    ledger = (
        await db_session.execute(
            select(FilterLedger).where(
                FilterLedger.request_id == "erasure-completeness-req"
            )
        )
    ).scalar_one()
    assert ledger.draft is None and ledger.final_text is None
    call = (
        await db_session.execute(
            select(ModelCallLog).where(
                ModelCallLog.request_id == "erasure-completeness-req"
            )
        )
    ).scalar_one()
    assert call.envelope.get("redacted") is True and call.tool_calls == []
    assert (await db_session.execute(select(func.count()).select_from(Attachment))).scalar_one() == 0

    # Every granted consent is revoked.
    consents = (await db_session.execute(select(ConsentRecord))).scalars().all()
    assert len(consents) == 7
    assert all(c.revoked_at is not None for c in consents)

    # Manifest counters match reality, and the audit trail exists.
    assert manifest["consents_revoked"] == 7
    assert manifest["enrollments_processed"] == 1
    assert manifest["face_enrollments_processed"] == 1
    assert manifest["face_samples_deleted"] == 1
    assert manifest["recognition_sightings_deleted"] == 1
    assert manifest["biodata_cache_deleted"] == 1
    assert manifest["adapter_registrations_redacted"] == 1
    assert manifest["personalization_calibrations_redacted"] == 1
    assert manifest["filter_recalibrations_redacted"] == 1
    assert manifest["corpus_snapshots_redacted"] == 1
    assert manifest["filter_ledger_redacted"] == 1
    assert manifest["model_call_envelopes_redacted"] == 1
    assert manifest["events_tombstoned"] == 1
    assert manifest["attachments_deleted"] == 1
    assert manifest["backup_purge_required"] is True

    erasure_rows = (await db_session.execute(select(DataErasureRecord))).scalars().all()
    assert len(erasure_rows) == 1
    audit = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "data_erasure")
        )
    ).scalars().all()
    assert len(audit) == 1
