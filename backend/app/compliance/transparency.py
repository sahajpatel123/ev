"""Transparency center: what EV stores, trains, processes, and transmits."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AccessLog,
    AdapterRegistration,
    Attachment,
    ConsentRecord,
    Event,
    FaceEnrollment,
    FaceSample,
    FilterRecalibration,
    Memory,
    PersonalizationCalibration,
    PublicFigureCache,
    RecognitionLog,
    VoiceEnrollment,
    VoicePrint,
)
from app.utils.text import utcnow

from .policy import (
    ACCESS_LOG,
    EVENT,
    FACEPRINT,
    INTEGRATION_CACHE,
    LIVE_AUDIO,
    TRAINING_SNAPSHOT,
    VOICEPRINT,
    local_residency_required,
    region,
    remote_processing_allowed,
    residency_mode,
    retention_days,
)


async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def transparency_report(session: AsyncSession) -> dict:
    """Build the transparency report from live configuration and stores."""
    consents = list(
        (
            await session.execute(
                select(ConsentRecord).where(ConsentRecord.revoked_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    active_tracks = {row.track for row in consents}
    stored = [
        {
            "name": "events",
            "purpose": "immutable transcript of inputs (text, voice, live data)",
            "count": await _count(session, Event),
            "encrypted_at_rest": False,
            "retention_days": retention_days(EVENT),
            "deletion_path": "DELETE /v1/events/{id} (tombstone + redaction)",
        },
        {
            "name": "memories",
            "purpose": "derived facts/preferences/decisions rebuilt from events",
            "count": await _count(session, Memory),
            "encrypted_at_rest": False,
            "retention_days": retention_days(EVENT),
            "deletion_path": "redaction cascade from event tombstone",
        },
        {
            "name": "voice_enrollments",
            "purpose": "consent-gated biometric identity record",
            "count": await _count(session, VoiceEnrollment),
            "encrypted_at_rest": True,
            "retention_days": retention_days(VOICEPRINT),
            "deletion_path": "POST /v1/voice/enrollments/{id}/delete or /v1/compliance/erasure",
        },
        {
            "name": "voiceprints",
            "purpose": "encrypted speaker template (never raw audio)",
            "count": await _count(session, VoicePrint),
            "encrypted_at_rest": True,
            "retention_days": retention_days(VOICEPRINT),
            "deletion_path": "same as voice_enrollments; ciphertext nulled on delete",
        },
        {
            "name": "face_enrollments",
            "purpose": "consent-gated biometric identity record (face templates)",
            "count": await _count(session, FaceEnrollment),
            "encrypted_at_rest": True,
            "retention_days": retention_days(FACEPRINT),
            "deletion_path": "POST /v1/compliance/erasure (ciphertext nulled)",
        },
        {
            "name": "face_samples",
            "purpose": "encrypted per-sample face templates kept for quality control",
            "count": await _count(session, FaceSample),
            "encrypted_at_rest": True,
            "retention_days": retention_days(FACEPRINT),
            "deletion_path": "POST /v1/compliance/erasure (rows deleted)",
        },
        {
            "name": "recognition_log",
            "purpose": "user-confirmed recognition sightings over owner media",
            "count": await _count(session, RecognitionLog),
            "encrypted_at_rest": False,
            "retention_days": retention_days(EVENT),
            "deletion_path": "POST /v1/compliance/erasure (sightings deleted)",
        },
        {
            "name": "public_figure_cache",
            "purpose": "licensed public-figure biodata cache (rebuildable)",
            "count": await _count(session, PublicFigureCache),
            "encrypted_at_rest": False,
            "retention_days": retention_days(INTEGRATION_CACHE),
            "deletion_path": "POST /v1/compliance/erasure (cache deleted)",
        },
        {
            "name": "personalization_calibrations",
            "purpose": "derived retrieval calibration snapshots",
            "count": await _count(session, PersonalizationCalibration),
            "encrypted_at_rest": False,
            "retention_days": retention_days(TRAINING_SNAPSHOT),
            "deletion_path": "POST /v1/compliance/erasure (evidence cleared)",
        },
        {
            "name": "filter_recalibrations",
            "purpose": "derived filter threshold reports and applied policies",
            "count": await _count(session, FilterRecalibration),
            "encrypted_at_rest": False,
            "retention_days": retention_days(TRAINING_SNAPSHOT),
            "deletion_path": "POST /v1/compliance/erasure (policy cleared)",
        },
        {
            "name": "adapter_registrations",
            "purpose": "versioned EVIE adapter registry with eval metrics",
            "count": await _count(session, AdapterRegistration),
            "encrypted_at_rest": False,
            "retention_days": retention_days(TRAINING_SNAPSHOT),
            "deletion_path": "POST /v1/compliance/erasure (eval metrics cleared)",
        },
        {
            "name": "attachments",
            "purpose": "files and audio blobs referenced by events",
            "count": await _count(session, Attachment),
            "encrypted_at_rest": False,
            "retention_days": retention_days(LIVE_AUDIO),
            "deletion_path": "erasure physically deletes blobs from object store",
        },
        {
            "name": "access_log",
            "purpose": "audit of reads/writes/exports/deletions",
            "count": await _count(session, AccessLog),
            "encrypted_at_rest": False,
            "retention_days": retention_days(ACCESS_LOG),
            "deletion_path": "retention sweep or user deletion",
        },
        {
            "name": "integration_cache",
            "purpose": "short-lived cached integration payloads",
            "count": 0,
            "encrypted_at_rest": False,
            "retention_days": retention_days(INTEGRATION_CACHE),
            "deletion_path": "cache eviction",
        },
    ]
    trained = [
        {
            "track": track,
            "consent_active": track in active_tracks,
            "retention_days": retention_days(TRAINING_SNAPSHOT),
            "deletion_path": "consent revocation + erasure",
        }
        for track in (
            "voice_enrollment",
            "face_enrollment",
            "training_corpus",
            "life_data_personalization",
            "adapter_fine_tuning",
            "filter_self_improvement",
            "chat_egress",
        )
    ]
    processed = [
        {
            "stage": "wake-word detection",
            "transient": True,
            "retained": "no audio retained",
        },
        {
            "stage": "speaker verification",
            "transient": True,
            "retained": "encrypted template only; sample hashes for replay guard",
        },
        {
            "stage": "transcription/ASR",
            "transient": True,
            "retained": "transcript stored as an event with user-visible lifecycle",
        },
        {
            "stage": "memory extraction",
            "transient": False,
            "retained": "derived memories with provenance and redaction cascade",
        },
    ]
    transmitted = [
        {
            "kind": "voiceprint",
            "provider": settings.voiceprint_provider,
            "remote": settings.voiceprint_provider == "http",
            "destination": settings.voiceprint_base_url,
            "consent_track": "voice_enrollment",
        },
        {
            "kind": "embeddings",
            "provider": settings.embedding_provider,
            "remote": settings.embedding_provider == "http",
            "destination": settings.embedding_base_url,
            "consent_track": "training_corpus",
        },
        {
            "kind": "chat",
            "provider": settings.chat_provider,
            "remote": settings.chat_provider not in ("echo", "mock"),
            "destination": settings.deepseek_base_url,
            "consent_track": "chat_egress",
            "consent_active": "chat_egress" in active_tracks,
            "remote_gate_allowed": remote_processing_allowed("chat_egress"),
        },
        {
            "kind": "object_store",
            "backend": settings.object_store_backend,
            "remote": settings.object_store_backend == "s3",
            "destination": settings.s3_endpoint_url,
            "consent_track": None,
        },
    ]
    return {
        "generated_at": utcnow().isoformat(),
        "region": region(),
        "residency_mode": residency_mode(),
        "local_residency_required": local_residency_required(),
        "stored": stored,
        "trained": trained,
        "processed": processed,
        "transmitted": transmitted,
    }


async def transparency_summary(session: AsyncSession) -> str:
    """Plain-language 'what leaves this machine' report readable in 30 seconds."""
    report = await transparency_report(session)
    lines = [
        "EV data-egress summary",
        f"Generated: {report['generated_at']}",
        f"Region: {report['region']} · residency mode: {report['residency_mode']}",
        "",
        "What leaves this machine:",
    ]
    for item in report["transmitted"]:
        destination = item.get("destination") or "local"
        remote = "REMOTE" if item.get("remote") else "local only"
        consent_track = item.get("consent_track")
        if consent_track:
            active = item.get("consent_active")
            gate = item.get("remote_gate_allowed")
            consent_state = "active consent" if active else "NO ACTIVE CONSENT"
            gate_state = "remote gate open" if gate else "remote gate closed"
            flag = f"{consent_state} · {gate_state}"
        else:
            flag = "no consent track (local backend)"
        lines.append(
            f"- {item.get('kind', '?')} -> {destination} [{remote}] ({flag})"
        )
    lines.append("")
    lines.append(
        "Stored biometric data is encrypted at rest and destroyed by "
        "POST /v1/compliance/erasure; retention windows are listed in "
        "GET /v1/compliance/transparency."
    )
    return "\n".join(lines)
