"""API-facing compatibility façade for the canonical :mod:`world_memory` layer.

The persistence contract lives in ``world_memory.py``.  This module keeps the
HTTP surface readable and provides the small, imperative helpers used by the
world-model router without introducing a second storage implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import world_memory as memory
from app.ev.world_memory import EpistemicKind
from app.models import CameraState, ObservationRecord, OwnerObject
from app.services.access_log import log_access
from app.utils.text import normalize_text, utcnow

CAMERA_STATES = {
    "off",
    "paused",
    "active",
    "explicit_one_shot",
    "permission_denied",
    "denied",
    "unavailable",
    "error",
}
CAMERA_STATE_ALIASES = {
    "one_shot": "explicit_one_shot",
    "one-shot": "explicit_one_shot",
    "permission-denied": "permission_denied",
    "permission_denied": "denied",
}
PERSON_CONSENT_STATES = {"granted", "explicit", "owner_confirmed", "consented"}
DEFAULT_STALE_AFTER_SECONDS = memory.DEFAULT_STALE_AFTER_SECONDS
_EPISTEMIC_KINDS = frozenset({"observed", "reported", "inferred", "guessed"})


def _epistemic_kind(value: str) -> EpistemicKind:
    if value in _EPISTEMIC_KINDS:
        return cast(EpistemicKind, value)
    return "observed"


def _as_utc(value: datetime | None) -> datetime:
    value = value or utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def freshness_state(
    observed_at: datetime | None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    *,
    now: datetime | None = None,
) -> str:
    if observed_at is None:
        return "unknown"
    return memory.freshness_for(
        observed_at,
        stale_after_seconds=stale_after_seconds,
        now=now,
    )


async def record_observation(
    session: AsyncSession,
    *,
    subject: str,
    subject_type: str = "owner",
    object_or_event: str,
    action: str,
    location: str,
    observed_at: datetime | None = None,
    source_device: str,
    evidence_ref: str,
    confidence: float,
    uncertainty: str,
    consent_state: str,
    retention_class: str = "standard",
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    fact_kind: str = "observed",
    metadata: dict | None = None,
    persist_raw_frame: bool = False,
    actor: str = "api",
) -> ObservationRecord:
    payload = dict(metadata or {})
    payload["raw_frame_requested"] = bool(persist_raw_frame)
    return await memory.record_observation(
        session,
        memory.ObservationContract(
            subject=subject,
            subject_type=subject_type,
            object_or_event=object_or_event,
            action=action,
            location=location,
            timestamp=observed_at,
            source_device=source_device,
            evidence_ref=evidence_ref,
            confidence=confidence,
            uncertainty=uncertainty,
            consent_state=consent_state,
            retention_class=retention_class,
            stale_after_seconds=stale_after_seconds,
            fact_kind=_epistemic_kind(fact_kind),
            metadata=payload,
        ),
        actor=actor,
    )


async def enroll_object(
    session: AsyncSession,
    *,
    name: str,
    object_type: str = "thing",
    owner: str = "owner",
    enrollment_source: str = "user",
    appearance_references: list | None = None,
    common_locations: list[str] | None = None,
    actor: str = "api",
) -> OwnerObject:
    row = await memory.enroll_owner_object(
        session,
        name=name,
        object_type=object_type,
        owner=owner,
        enrollment_source=enrollment_source,
        appearance_references=[str(item) for item in (appearance_references or [])],
        common_locations=common_locations,
    )
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="world_model.enroll_object",
        resource_type="object",
        resource_ids=[row.id],
        details={"name": row.name, "object_type": row.object_type},
    )
    return row


async def record_object_observation(
    session: AsyncSession,
    object_id: UUID,
    *,
    location: str,
    observed_at: datetime | None = None,
    source_device: str,
    evidence_ref: str,
    confidence: float,
    uncertainty: str = "visual match may be wrong or the object may have moved",
    action: str = "seen",
    fact_kind: str = "observed",
    possible_matches: list | None = None,
    metadata: dict | None = None,
    persist_raw_frame: bool = False,
    actor: str = "api",
) -> ObservationRecord:
    payload = dict(metadata or {})
    if possible_matches:
        payload["possible_matches"] = possible_matches
    owner_object = await session.get(OwnerObject, object_id)
    if owner_object is None:
        raise KeyError(f"Object {object_id} not found")
    return await memory.record_owner_object_observation(
        session,
        object_id,
        memory.ObservationContract(
            subject="owner",
            subject_type="object",
            object_or_event=owner_object.name,
            action=action,
            location=location,
            timestamp=observed_at,
            source_device=source_device,
            evidence_ref=evidence_ref,
            confidence=confidence,
            uncertainty=uncertainty,
            consent_state="owner_confirmed",
            retention_class="standard",
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
            fact_kind=_epistemic_kind(fact_kind),
            metadata={**payload, "raw_frame_requested": bool(persist_raw_frame)},
        ),
        actor=actor,
        possible_matches=possible_matches,
    )


async def last_seen_object(
    session: AsyncSession,
    object_id: UUID,
    *,
    now: datetime | None = None,
) -> dict:
    obj = await session.get(OwnerObject, object_id)
    if obj is None or obj.status != "active" or obj.deleted_at is not None:
        raise KeyError(f"Object {object_id} not found")
    latest = await memory.last_seen_evidence(
        session,
        owner_object_id=obj.id,
        now=now,
    )
    if latest is None:
        return {
            "object_id": str(obj.id),
            "name": obj.name,
            "found": False,
            "location": None,
            "freshness_state": "unknown",
            "evidence_ref": None,
            "observed_at": None,
            "confidence": None,
            "uncertainty": "No observation has been recorded.",
            "answer": f"I have not observed your {obj.name} yet.",
        }
    observed_at = latest.get("observed_at") or latest.get("timestamp")
    observed_dt = _as_utc(datetime.fromisoformat(observed_at)) if isinstance(observed_at, str) else observed_at
    stale_note = " I have not observed it since." if latest["freshness_state"] == "stale" else ""
    observed_label = observed_dt.isoformat() if observed_dt is not None else "unknown time"
    return {
        "object_id": str(obj.id),
        "name": obj.name,
        "found": True,
        "location": latest["location"],
        "freshness_state": latest["freshness_state"],
        "evidence_ref": latest["evidence_ref"],
        "observed_at": observed_dt,
        "confidence": latest["confidence"],
        "uncertainty": latest["uncertainty"],
        "fact_kind": latest["fact_kind"],
        "answer": f"The strongest evidence puts your {obj.name} at {latest['location']} at {observed_label}.{stale_note}",
        "why": {
            "source_device": latest["source_device"],
            "evidence_ref": latest["evidence_ref"],
            "observed_at": observed_dt,
            "confidence": latest["confidence"],
            "uncertainty": latest["uncertainty"],
        },
    }


async def list_objects(session: AsyncSession, *, limit: int = 100) -> list[OwnerObject]:
    return list(
        (
            await session.execute(
                select(OwnerObject)
                .where(OwnerObject.status == "active", OwnerObject.deleted_at.is_(None))
                .order_by(OwnerObject.name.asc())
                .limit(min(max(limit, 1), 500))
            )
        )
        .scalars()
        .all()
    )


async def list_observations(
    session: AsyncSession,
    *,
    subject: str | None = None,
    location: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[ObservationRecord]:
    rows = list(
        (
            await session.execute(
                select(ObservationRecord)
                .where(ObservationRecord.deleted_at.is_(None))
                .order_by(ObservationRecord.observed_at.desc())
                .limit(min(max(limit, 1), 500))
            )
        )
        .scalars()
        .all()
    )
    if subject:
        rows = [row for row in rows if normalize_text(subject) in normalize_text(row.subject)]
    if location:
        rows = [row for row in rows if normalize_text(location) in normalize_text(row.location)]
    if action:
        rows = [row for row in rows if normalize_text(action) == normalize_text(row.action)]
    for row in rows:
        row.freshness_state = freshness_state(row.observed_at, row.stale_after_seconds)
    return rows


async def forget_observation(
    session: AsyncSession,
    observation_id: UUID,
    *,
    reason: str = "user requested",
    actor: str = "api",
) -> ObservationRecord:
    row = await memory.forget_observation(session, observation_id, reason=reason)
    await log_access(
        session,
        actor=actor,
        action="delete",
        endpoint="world_model.forget_observation",
        resource_type="observation",
        resource_ids=[row.id],
        details={"reason": reason},
    )
    return row


async def forget_last_hour(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    reason: str = "user requested",
    actor: str = "api",
) -> int:
    cutoff = _as_utc(now) - timedelta(hours=1)
    rows = list(
        (
            await session.execute(
                select(ObservationRecord).where(
                    ObservationRecord.deleted_at.is_(None),
                    ObservationRecord.observed_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await memory.forget_observation(session, row.id, reason=reason, at=now)
    if rows:
        await log_access(
            session,
            actor=actor,
            action="delete",
            endpoint="world_model.forget_last_hour",
            resource_type="observation",
            resource_ids=[row.id for row in rows],
            details={"reason": reason, "count": len(rows)},
        )
    return len(rows)


async def forget_object(
    session: AsyncSession,
    object_id: UUID,
    *,
    reason: str = "user requested",
    actor: str = "api",
) -> OwnerObject:
    row = await memory.forget_owner_object(session, object_id, reason=reason)
    await log_access(
        session,
        actor=actor,
        action="delete",
        endpoint="world_model.forget_object",
        resource_type="object",
        resource_ids=[row.id],
        details={"reason": reason},
    )
    return row


async def forget_person_observations(
    session: AsyncSession,
    name: str,
    *,
    reason: str = "user requested",
    actor: str = "api",
) -> int:
    count = await memory.forget_person_observations(session, name, reason=reason)
    if count:
        await log_access(
            session,
            actor=actor,
            action="delete",
            endpoint="world_model.forget_person",
            resource_type="person_observation",
            resource_ids=[],
            details={"reason": reason, "name": name, "count": count},
        )
    return count


async def record_person_observation(
    session: AsyncSession,
    *,
    person_name: str | None,
    location: str,
    observed_at: datetime | None = None,
    source_device: str,
    evidence_ref: str,
    confidence: float,
    uncertainty: str = "visual similarity is not certain identity",
    consent_state: str = "explicit",
    action: str = "seen",
    actor: str = "api",
) -> ObservationRecord | None:
    row = await memory.record_person_observation(
        session,
        memory.ObservationContract(
            subject=person_name or "unknown",
            subject_type="person",
            object_or_event="person",
            action=action,
            location=location,
            timestamp=observed_at,
            source_device=source_device,
            evidence_ref=evidence_ref,
            confidence=confidence,
            uncertainty=uncertainty,
            consent_state=consent_state,
            retention_class="sensitive-minimized",
            fact_kind="observed",
        ),
        person_name=person_name,
        actor=actor,
    )
    return row if row.metadata_.get("identity_status") == "enrolled" else None


def _camera_state_dict(row: CameraState) -> dict:
    return {
        "id": str(row.id),
        "device_id": row.device_id,
        "platform": row.platform,
        "state": row.state,
        "visible": bool(row.visible),
        "permission_state": row.permission_state,
        "explicit_request": bool(row.explicit_request),
        "paused_reason": row.paused_reason,
        "consent_state": row.consent_state,
        "raw_frames_persisted": bool(row.raw_frames_persisted),
        "last_error": row.last_error,
        "updated_at": row.updated_at,
    }


async def update_camera_state(
    session: AsyncSession,
    device_id: str,
    *,
    platform: str = "mac",
    state: str,
    permission_state: str | None = None,
    explicit_request: bool | None = None,
    paused_reason: str | None = None,
    consent_state: str | None = None,
    raw_frames_persisted: bool = False,
    last_error: str | None = None,
    actor: str = "api",
) -> CameraState:
    canonical_state = CAMERA_STATE_ALIASES.get(state.strip().lower(), state.strip().lower())
    if canonical_state not in CAMERA_STATES:
        raise ValueError(f"unsupported camera state: {state}")
    existing = await get_camera_state(session, device_id)
    permission = permission_state or (existing.permission_state if existing else "unknown")
    request = (
        bool(explicit_request)
        if explicit_request is not None
        else bool(existing.explicit_request) if existing else False
    )
    consent = consent_state or (existing.consent_state if existing else "not_granted")
    if explicit_request is None and canonical_state in {
        "off",
        "denied",
        "permission_denied",
        "unavailable",
        "error",
    }:
        request = False
    if canonical_state in {"active", "explicit_one_shot"}:
        if permission != "authorized":
            raise PermissionError("camera permission is not authorized")
        if not request:
            raise PermissionError("camera capture requires an explicit visible user request")
        if consent not in PERSON_CONSENT_STATES | {"camera_granted", "not_applicable"}:
            raise PermissionError("camera consent is not granted")
    if raw_frames_persisted and consent not in PERSON_CONSENT_STATES | {"camera_granted"}:
        raise PermissionError("raw-frame retention requires explicit camera consent")
    existing = await memory.upsert_camera_state(
        session,
        device_id=device_id,
        platform=platform,
        state=canonical_state,
        visible=canonical_state in {"active", "explicit_one_shot"},
        permission_state=permission,
        explicit_request=request,
        paused_reason=paused_reason,
        consent_state=consent,
        persist_raw_frames=raw_frames_persisted,
        last_error=last_error if canonical_state == "error" else None,
    )
    # Re-apply the validated values because the upsert above is intentionally
    # a low-level persistence helper that also supports non-HTTP collectors.
    existing.state = canonical_state
    existing.permission_state = permission
    existing.explicit_request = request
    existing.consent_state = consent
    existing.visible = canonical_state in {"active", "explicit_one_shot"}
    existing.raw_frames_persisted = bool(raw_frames_persisted)
    existing.updated_at = utcnow()
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="world_model.update_camera_state",
        resource_type="camera",
        resource_ids=[existing.id],
        details={"device_id": device_id, "state": canonical_state, "visible": existing.visible},
    )
    return existing


async def get_camera_state(session: AsyncSession, device_id: str) -> CameraState | None:
    return (
        await session.execute(select(CameraState).where(CameraState.device_id == device_id))
    ).scalar_one_or_none()


async def list_camera_states(session: AsyncSession) -> list[CameraState]:
    return list((await session.execute(select(CameraState).order_by(CameraState.device_id))).scalars().all())


def camera_state_out(row: CameraState | None) -> dict:
    if row is None:
        return {
            "id": None,
            "device_id": None,
            "platform": None,
            "state": "off",
            "visible": False,
            "permission_state": "unknown",
            "explicit_request": False,
            "paused_reason": None,
            "consent_state": "not_granted",
            "raw_frames_persisted": False,
            "last_error": None,
            "updated_at": None,
        }
    return _camera_state_dict(row)


ObservationContract = memory.ObservationContract
