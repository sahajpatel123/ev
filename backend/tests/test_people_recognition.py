"""AGENT 7 ROSTER: consent-gated face enrollment, recognition, calibration, erasure."""

from __future__ import annotations

import base64
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.config import settings
from app.ml.settings import get_ml_settings
from app.models import (
    Event,
    FaceEnrollment,
    FaceSample,
    PublicFigureCache,
    RecognitionLog,
)
from app.people.face_embed import FaceCrop, get_face_embedder
from app.utils.text import utcnow


def img(seed: str, variant: int = 0) -> str:
    raw = (f"{seed}:{variant}".encode("ascii") * 80)
    return base64.b64encode(raw).decode("ascii")


def photos_for(seed: str, count: int = 5) -> list[dict]:
    return [
        {
            "image_b64": img(seed, 0),
            "quality": 0.92,
            "confidence": 0.97,
            "source": "photo",
        }
        for _ in range(count)
    ]


async def grant_face_consent(client: AsyncClient) -> dict:
    resp = await client.post(
        "/v1/training/consent",
        json={
            "track": "face_enrollment",
            "purpose": "face enrollment from owner photos",
            "scope": {"test": True},
            "source": "test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def enroll(
    client: AsyncClient,
    person_name: str,
    seed: str,
    count: int = 5,
) -> dict:
    resp = await client.post(
        "/v1/people/enrollments",
        json={"person_name": person_name, "photos": photos_for(seed, count)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_face_enrollment_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(
        "/v1/people/enrollments",
        json={"person_name": "Alice", "photos": photos_for("alice")},
    )
    assert resp.status_code == 403
    assert "consent" in resp.json()["detail"].lower()
    assert resp.headers.get("x-error-code") == "consent_required"

    count = await db_session.scalar(
        select(func.count()).select_from(FaceEnrollment)
    )
    assert count == 0


async def test_face_enrollment_rejects_few_photos(client: AsyncClient) -> None:
    await grant_face_consent(client)
    resp = await client.post(
        "/v1/people/enrollments",
        json={"person_name": "Alice", "photos": photos_for("alice", 4)},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("quality", 0.1, "enroll_quality"),
        ("confidence", 0.1, "enroll_confidence"),
    ],
)
async def test_face_enrollment_rejects_low_quality_or_confidence(
    client: AsyncClient,
    field: str,
    value: float,
    code: str,
) -> None:
    await grant_face_consent(client)
    payload = photos_for("alice")
    payload[0][field] = value
    resp = await client.post(
        "/v1/people/enrollments",
        json={"person_name": "Alice", "photos": payload},
    )
    assert resp.status_code == 422
    assert resp.headers.get("x-error-code") == code


async def test_face_enrollment_writes_encrypted_template_and_samples(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    payload = await enroll(client, "Alice", "alice", count=5)
    assert payload["raw_photos_stored"] is False
    assert payload["sample_count"] == 5
    assert payload["provider"] == "hash"
    assert payload["degraded"] is True

    enrollment = await db_session.get(
        FaceEnrollment, UUID(payload["enrollment"]["id"])
    )
    assert enrollment is not None
    assert enrollment.status == "active"
    assert enrollment.is_current is True
    assert enrollment.ciphertext is not None
    assert enrollment.salt is not None
    assert enrollment.consent_id is not None

    sample_count = await db_session.scalar(
        select(func.count())
        .select_from(FaceSample)
        .where(FaceSample.enrollment_id == enrollment.id)
    )
    assert sample_count == 5


async def test_whereabouts_fuses_enrolled_face_and_voice_mentions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    payload = await enroll(client, "Alice", "alice", count=5)

    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice", 0), "source": "photo"},
    )
    recognition_id = resp.json()["recognition_id"]
    await client.post(
        f"/v1/people/recognitions/{recognition_id}/confirm",
        json={"reason": "yes"},
    )

    db_session.add(
        Event(
            source="voice",
            event_type="speech",
            content={"text": "Alice called this morning"},
            occurred_at=utcnow(),
            ingested_at=utcnow(),
            privacy_level="normal",
            sha256="a" * 64,
        )
    )
    await db_session.flush()
    await db_session.commit()

    resp = await client.get("/v1/people/Alice/whereabouts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity_id"] == payload["enrollment"]["entity_id"]
    assert body["enrolled"] is not None
    assert body["enrolled"]["id"] == payload["enrollment"]["id"]
    assert any(item["confirmed"] for item in body["face_sightings"])
    assert any(
        item["provenance"] == "event" and item["source"] == "voice"
        for item in body["voice_sightings"]
    )
    assert body["total_events"] >= 1


async def test_face_recognition_resolves_enrolled_and_unknown_never_logs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice", count=5)

    # Same crop family (identical bytes) must resolve to the enrolled person.
    resp = await client.post(
        "/v1/people/recognize",
        json={
            "image_b64": img("alice", 0),
            "quality": 0.9,
            "confidence": 0.95,
            "source": "live_frame",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved"] is True
    assert body["unknown"] is False
    assert body["label"] == "Alice"
    assert body["entity_id"] is not None
    assert body["recognition_id"] is not None

    # A non-enrolled face MUST resolve to unknown and write no recognition log.
    resp = await client.post(
        "/v1/people/recognize",
        json={
            "image_b64": img("mallory", 0),
            "quality": 0.9,
            "confidence": 0.95,
            "source": "live_frame",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved"] is False
    assert body["unknown"] is True
    assert body["label"] is None
    assert body["entity_id"] is None
    assert body["recognition_id"] is None

    logs = list(
        (
            await db_session.execute(
                select(RecognitionLog).order_by(RecognitionLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 1  # only the resolved match was logged
    assert logs[0].source == "model"


async def test_face_recognition_unknown_without_enrollments_never_logs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("nobody", 0), "source": "live_frame"},
    )
    assert resp.status_code == 200
    assert resp.json()["unknown"] is True
    count = await db_session.scalar(
        select(func.count()).select_from(RecognitionLog)
    )
    assert count == 0


async def test_recognition_confirmation_and_correction_feed_back(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice", count=5)
    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice", 0), "source": "photo"},
    )
    recognition_id = resp.json()["recognition_id"]

    # Confirm the model suggestion.
    resp = await client.post(
        f"/v1/people/recognitions/{recognition_id}/confirm",
        json={"reason": "yes, that is Alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "user"
    row = await db_session.get(RecognitionLog, UUID(recognition_id))
    assert row is not None and row.source == "user"

    # Correct a wrong suggestion to a different person.
    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice", 0), "source": "photo"},
    )
    wrong_id = resp.json()["recognition_id"]
    resp = await client.post(
        f"/v1/people/recognitions/{wrong_id}/confirm",
        json={"correct_label": "Alice Corrected", "reason": "model mislabeled"},
    )
    assert resp.status_code == 200, resp.text
    corrected = resp.json()
    assert corrected["label"] == "Alice Corrected"
    assert corrected["entity_id"] is not None
    row = await db_session.get(RecognitionLog, UUID(wrong_id))
    assert row is not None and row.source == "user" and row.label == "Alice Corrected"


async def test_roc_calibration_applies_threshold(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice", count=5)
    trials = [
        {
            "person": "Alice",
            "images": [img("alice", 0) for _ in range(5)],
        },
        {
            "person": "Bob",
            "images": [img("bob", 0) for _ in range(5)],
        },
    ]
    resp = await client.post(
        "/v1/people/calibrate",
        json={"trials": trials, "target_far": 1e-3, "apply": True},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["genuine_pairs"] == 20
    assert report["impostor_pairs"] == 25
    assert -1.0 <= report["threshold"] <= 1.0
    assert report["tar_at_target_far"] >= 0.0
    assert len(report["roc"]) >= 2

    enrollment = (
        await db_session.execute(
            select(FaceEnrollment).where(
                FaceEnrollment.is_current.is_(True),
                FaceEnrollment.status == "active",
            )
        )
    ).scalar_one()
    assert enrollment.threshold == report["threshold"]


async def test_per_person_erasure_removes_every_trace(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    payload = await enroll(client, "Alice", "alice", count=5)
    entity_id = UUID(payload["enrollment"]["entity_id"])

    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice", 0), "source": "photo"},
    )
    assert resp.json()["recognition_id"] is not None

    db_session.add(
        PublicFigureCache(
            name="Alice",
            canonical_key="public:alice",
            entity_id=entity_id,
            data={"summary": "test"},
            source_url="https://en.wikipedia.org/wiki/Alice",
            license="CC0 / CC BY-SA 4.0",
            confirmed=True,
            expires_at=utcnow() + timedelta(seconds=settings.biodata_ttl_seconds),
        )
    )
    await db_session.flush()
    await db_session.commit()  # release the write lock before the API erasure call

    resp = await client.delete(
        f"/v1/people/{entity_id}",
        params={"reason": "privacy review"},
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()
    assert manifest["recognition_logs_deleted"] == 1
    assert manifest["face_samples_deleted"] == 5
    assert manifest["face_enrollments_processed"] == 1
    assert manifest["public_figure_cache_deleted"] == 1

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(FaceSample)
            .where(FaceSample.entity_id == entity_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RecognitionLog)
            .where(RecognitionLog.entity_id == entity_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PublicFigureCache)
            .where(PublicFigureCache.entity_id == entity_id)
        )
        == 0
    )
    enrollment = (
        await db_session.execute(
            select(FaceEnrollment).where(FaceEnrollment.entity_id == entity_id)
        )
    ).scalar_one()
    assert enrollment.status == "deleted"
    assert enrollment.ciphertext is None
    assert enrollment.salt is None
    assert enrollment.redacted is True

    erasure_count = await db_session.scalar(
        select(func.count()).select_from(DataErasureRecord)
    )
    assert erasure_count == 1


async def test_real_embedder_factory_degrades_without_weights(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "face_provider", "sface")
    monkeypatch.setattr(settings, "face_model_path", "/nonexistent/sface.onnx")
    embedder = get_face_embedder()
    result = await embedder.embed(
        FaceCrop(image_b64=img("factory", 0), quality=0.9, confidence=0.9)
    )
    assert result.provider == "sface"
    assert result.degraded is True
    assert len(result.embedding) == settings.face_embedding_dim


async def test_real_sface_embedder_runs_when_weights_present() -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    model_path = Path(get_ml_settings().ml_model_dir) / "face-sface.onnx"
    if not model_path.is_file():
        pytest.skip("SFace weights not cached; real-engine test skipped offline")

    from app.people.face_embed import SFaceOnnxEmbedder

    image = np.zeros((112, 112, 3), dtype=np.uint8)
    image[::4, :, 0] = 255
    image[:, ::4, 1] = 128
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    embedder = SFaceOnnxEmbedder(model_path=str(model_path))
    result = await embedder.embed(
        FaceCrop(
            image_b64=base64.b64encode(encoded.tobytes()).decode(),
            quality=0.5,
            confidence=0.5,
            source="smoke",
        )
    )
    assert result.provider == "sface"
    assert result.degraded is False
    assert len(result.embedding) == 128
    assert abs(sum(v * v for v in result.embedding) - 1.0) < 1e-4
