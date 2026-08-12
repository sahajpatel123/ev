"""Face recognition operating-point calibration.

Calibration computes a real ROC from cosine similarities produced by the
active embedder (deterministic hash provider in offline/dev mode, SFace ONNX
when weights are present). Scores are never fabricated: an offline result is
honestly reported as ``degraded=True``.
"""

from __future__ import annotations

import base64
import hashlib
import random
from binascii import Error as B64Error
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import FaceCalibrationReport
from app.utils.text import utcnow

if TYPE_CHECKING:
    from app.people.face_embed import FaceEmbedder


def roc_points(genuine: list[float], impostor: list[float]) -> list[dict]:
    """Build ROC points over unique score thresholds, highest first.

    Each point is ``{"threshold", "tar", "far"}`` where TAR is the fraction of
    genuine scores at or above the threshold and FAR is the same fraction over
    impostor scores. The ``1.0`` and ``-1.0`` endpoints are always included.
    """
    thresholds = sorted({1.0, -1.0, *genuine, *impostor}, reverse=True)
    n_genuine = len(genuine)
    n_impostor = len(impostor)
    points: list[dict] = []
    for threshold in thresholds:
        tar = (
            sum(1.0 for score in genuine if score >= threshold) / n_genuine
            if n_genuine
            else 0.0
        )
        far = (
            sum(1.0 for score in impostor if score >= threshold) / n_impostor
            if n_impostor
            else 0.0
        )
        points.append(
            {
                "threshold": round(float(threshold), 6),
                "tar": round(tar, 6),
                "far": round(far, 6),
            }
        )
    return points


def threshold_for_far(
    genuine: list[float],
    impostor: list[float],
    target_far: float = 1e-3,
) -> tuple[float, float, list[dict]]:
    """Pick the highest threshold whose FAR does not exceed ``target_far``.

    Returns ``(threshold, tar_at_threshold, roc)``. If no point reaches the
    target FAR, the point with the smallest real FAR is returned so the report
    never claims an operating point the data cannot support.
    """
    roc = roc_points(genuine, impostor)
    feasible = [point for point in roc if point["far"] <= target_far]
    if feasible:
        chosen = feasible[0]  # roc is sorted by threshold descending
    else:
        chosen = min(roc, key=lambda point: (point["far"], -point["threshold"]))
    return float(chosen["threshold"]), float(chosen["tar"]), roc


async def calibrate(
    trials: list[dict],
    *,
    embedder: FaceEmbedder | None = None,
    target_far: float = 1e-3,
) -> FaceCalibrationReport:
    """Calibrate the face operating point from same-person image trials.

    Each trial is ``{"person": str, "images": [base64-encoded crops]}``. Every
    unique image is embedded once (keyed by SHA-256 of its bytes); genuine
    pairs come from within-person pairs (max 100 per person) and impostor
    pairs are sampled deterministically from cross-person pairs (max 200).
    """
    from app.people.errors import FaceError
    from app.people.face_embed import FaceCrop, cosine_similarity, get_face_embedder

    embedder = embedder or get_face_embedder()
    cache: dict[str, list[float]] = {}
    persons: list[tuple[str, list[list[float]]]] = []

    for trial in trials:
        person = str(trial["person"])
        embeddings: list[list[float]] = []
        for image_b64 in trial["images"]:
            try:
                raw = base64.b64decode(image_b64, validate=True)
            except (B64Error, ValueError) as exc:
                raise FaceError(
                    "Invalid base64 image in calibration trial",
                    status=400,
                    code="invalid_image_data",
                ) from exc
            key = hashlib.sha256(raw).hexdigest()
            if key not in cache:
                crop = FaceCrop(
                    image_b64=image_b64,
                    quality=None,
                    confidence=None,
                    source="calibration",
                )
                result = await embedder.embed(crop)
                cache[key] = result.embedding
            embeddings.append(cache[key])
        persons.append((person, embeddings))

    genuine_scores: list[float] = []
    for _, embeddings in persons:
        pairs = [
            (embeddings[i], embeddings[j])
            for i in range(len(embeddings))
            for j in range(i + 1, len(embeddings))
        ]
        for left, right in pairs[:100]:
            genuine_scores.append(cosine_similarity(left, right))

    cross_pairs = [
        (left, right)
        for i in range(len(persons))
        for j in range(i + 1, len(persons))
        for left in persons[i][1]
        for right in persons[j][1]
    ]
    if len(cross_pairs) > 200:
        cross_pairs = random.Random(7).sample(cross_pairs, 200)
    impostor_scores = [cosine_similarity(left, right) for left, right in cross_pairs]

    threshold, tar, roc = threshold_for_far(
        genuine_scores, impostor_scores, target_far=target_far
    )
    return FaceCalibrationReport(
        provider=embedder.name,
        degraded=embedder.degraded,
        threshold=threshold,
        tar_at_target_far=tar,
        target_far=target_far,
        genuine_pairs=len(genuine_scores),
        impostor_pairs=len(impostor_scores),
        roc=roc,
        calibrated_at=utcnow(),
    )


async def apply_threshold(
    session: AsyncSession,
    *,
    master_key: str,
    threshold: float,
) -> int:
    """Persist a calibrated threshold into every active face enrollment.

    The decrypted enrollment payload is updated and re-encrypted at rest;
    enrollments without ciphertext (already deleted/redacted) are skipped.
    """
    from app.models import FaceEnrollment
    from app.voice.security import decrypt_payload, encrypt_payload

    rows = list(
        (
            await session.execute(
                select(FaceEnrollment).where(
                    FaceEnrollment.status == "active",
                    FaceEnrollment.is_current.is_(True),
                    FaceEnrollment.redacted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    updated = 0
    for row in rows:
        if not row.ciphertext or not row.salt:
            continue
        payload = decrypt_payload(row.ciphertext, row.salt, master_key=master_key)
        payload["threshold"] = threshold
        payload["calibrated"] = True
        payload["calibrated_at"] = now.isoformat()
        token, salt_hex = encrypt_payload(payload, master_key=master_key)
        row.ciphertext = token
        row.salt = salt_hex
        row.threshold = threshold
        row.updated_at = now
        updated += 1
    await session.flush()
    return updated
