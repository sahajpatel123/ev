"""Life-data personalization: evidence-backed importance/retrieval learning.

Derives per-memory-type importance multipliers from logged user signals
(corrections, usefulness, recommendation follow-through) instead of assuming
more training is better. Calibrations are versioned, consent-gated, reversible,
and applied transparently by the retriever (the locked scoring formula is
unchanged — learning adjusts the importance *signal*).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DecisionOutcome,
    Memory,
    PersonalizationCalibration,
    Prediction,
    ResponseLog,
)
from app.services.access_log import log_access
from app.training.consent import active_consent, require_consent
from app.utils.text import utcnow

TRACK = "life_data_personalization"

MIN_EVIDENCE = 3
MIN_MULTIPLIER = 0.8
MAX_MULTIPLIER = 1.2
DEFAULT_MULTIPLIER = 1.0


async def gather_evidence(session: AsyncSession) -> dict:
    """Aggregate per-memory-type signals from response logs plus global signals."""
    logs = list((await session.execute(select(ResponseLog))).scalars().all())
    memory_ids = {
        str(pid)
        for log in logs
        for pid in (log.provenance_ids or [])
    }
    type_by_id: dict[str, str] = {}
    if memory_ids:
        rows = (
            await session.execute(
                select(Memory.id, Memory.memory_type).where(
                    Memory.id.in_([UUID(pid) for pid in memory_ids])
                )
            )
        ).all()
        type_by_id = {str(mid): mtype for mid, mtype in rows}

    evidence: dict[str, dict] = {}
    for log in logs:
        for pid in log.provenance_ids or []:
            memory_type = type_by_id.get(str(pid))
            if memory_type is None:
                continue
            bucket = evidence.setdefault(
                memory_type,
                {"rated": 0, "corrected": 0, "useful": 0, "followed": 0},
            )
            if any(
                value is not None
                for value in (
                    log.was_useful,
                    log.followed_recommendation,
                    log.was_correction,
                )
            ):
                bucket["rated"] += 1
            if log.was_correction is True:
                bucket["corrected"] += 1
            if log.was_useful is True:
                bucket["useful"] += 1
            if log.followed_recommendation is True:
                bucket["followed"] += 1

    predictions = list(
        (
            await session.execute(
                select(Prediction).where(Prediction.reviewed_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    prediction_accuracy = (
        round(sum(1 for p in predictions if p.outcome == "correct") / len(predictions), 3)
        if predictions
        else None
    )
    outcomes_reviewed = len(
        list(
            (
                await session.execute(
                    select(DecisionOutcome).where(DecisionOutcome.status == "reviewed")
                )
            )
            .scalars()
            .all()
        )
    )
    evidence["__global__"] = {
        "prediction_accuracy": prediction_accuracy,
        "decision_outcomes_reviewed": outcomes_reviewed,
    }
    return evidence


def derive_calibrations(evidence: dict) -> dict[str, float]:
    """Bounded importance multipliers from evidence; neutral when under-powered."""
    calibrations: dict[str, float] = {}
    for memory_type, bucket in evidence.items():
        if memory_type == "__global__":
            continue
        rated = int(bucket.get("rated", 0))
        if rated < MIN_EVIDENCE:
            continue
        multiplier = DEFAULT_MULTIPLIER
        correction_rate = bucket.get("corrected", 0) / rated
        if correction_rate >= 0.3:
            multiplier -= min(0.2, correction_rate * 0.5)
        if bucket.get("useful", 0) / rated >= 0.7:
            multiplier += 0.05
        if bucket.get("followed", 0) / rated >= 0.7:
            multiplier += 0.05
        calibrations[memory_type] = round(
            min(MAX_MULTIPLIER, max(MIN_MULTIPLIER, multiplier)), 3
        )
    return calibrations


async def current_calibration(session: AsyncSession) -> PersonalizationCalibration | None:
    result = await session.execute(
        select(PersonalizationCalibration)
        .where(PersonalizationCalibration.is_current.is_(True))
        .order_by(PersonalizationCalibration.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def calibration_multipliers(session: AsyncSession) -> dict[str, float]:
    """Active multipliers for retrieval. Empty unless consent is active."""
    if await active_consent(session, TRACK) is None:
        return {}
    calibration = await current_calibration(session)
    if calibration is None:
        return {}
    return dict(calibration.calibrations or {})


async def calibrate(
    session: AsyncSession,
    *,
    actor: str,
    reason: str | None = None,
) -> PersonalizationCalibration:
    consent = await require_consent(session, TRACK)
    evidence = await gather_evidence(session)
    multipliers = derive_calibrations(evidence)
    current = await current_calibration(session)
    version = (current.version + 1) if current is not None else 1
    if current is not None:
        current.is_current = False
    row = PersonalizationCalibration(
        version=version,
        is_current=True,
        calibrations=multipliers,
        evidence=evidence,
        reason_for_change=reason or "evidence-backed importance calibration",
        consent_id=consent.id,
        supersedes_id=current.id if current is not None else None,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="personalization_calibrate",
        endpoint="POST /v1/training/personalization/calibrate",
        resource_type="personalization",
        resource_ids=[row.id],
        details={
            "version": version,
            "calibrated_types": len(multipliers),
        },
    )
    return row


async def list_calibrations(session: AsyncSession) -> list[PersonalizationCalibration]:
    result = await session.execute(
        select(PersonalizationCalibration).order_by(
            PersonalizationCalibration.version.desc()
        )
    )
    return list(result.scalars().all())


async def rollback(
    session: AsyncSession,
    *,
    target_version: int,
    actor: str,
    reason: str | None = None,
) -> PersonalizationCalibration:
    await require_consent(session, TRACK)
    result = await session.execute(
        select(PersonalizationCalibration).where(
            PersonalizationCalibration.version == target_version
        )
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise KeyError(f"Calibration version {target_version} not found")
    current = await current_calibration(session)
    if current is not None and current.id != target.id:
        current.is_current = False
    target.is_current = True
    if reason:
        target.reason_for_change = reason
    await log_access(
        session,
        actor=actor,
        action="personalization_rollback",
        endpoint="POST /v1/training/personalization/rollback",
        resource_type="personalization",
        resource_ids=[target.id],
        details={"target_version": target_version},
    )
    return target


async def delete_all(session: AsyncSession, *, actor: str, reason: str) -> int:
    """Data-subject deletion: redact all calibration snapshots, disable learning."""
    rows = list((await session.execute(select(PersonalizationCalibration))).scalars().all())
    for row in rows:
        row.calibrations = {}
        row.evidence = {}
        row.is_current = False
        row.reason_for_change = f"{reason} (redacted)"
    await log_access(
        session,
        actor=actor,
        action="personalization_delete",
        endpoint="POST /v1/training/personalization/delete",
        resource_type="personalization",
        resource_ids=[r.id for r in rows],
        details={"redacted": len(rows), "reason": reason},
    )
    return len(rows)


__all__ = [
    "DEFAULT_MULTIPLIER",
    "MIN_EVIDENCE",
    "calibrate",
    "calibration_multipliers",
    "current_calibration",
    "delete_all",
    "derive_calibrations",
    "gather_evidence",
    "list_calibrations",
    "rollback",
]
