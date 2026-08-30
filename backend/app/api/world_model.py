"""HTTP surface for EV's evidence-backed personal world model."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.ev import world_model
from app.models import Entity
from app.people.erasure import erase_person
from app.schemas import (
    CameraStateOut,
    CameraStateUpdate,
    ObjectLastSeenOut,
    ObjectObservationCreate,
    ObservationCreate,
    ObservationOut,
    OwnerObjectCreate,
    OwnerObjectOut,
    PersonObservationCreate,
)
from app.utils.text import normalize_text

router = APIRouter(prefix="/v1/world-model", tags=["world-model"])


@router.post("/observations", response_model=ObservationOut, status_code=201)
async def create_observation(
    data: ObservationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObservationOut:
    try:
        row = await world_model.record_observation(
            session,
            subject=data.subject,
            subject_type=data.subject_type,
            object_or_event=data.object_or_event,
            action=data.action,
            location=data.location,
            observed_at=data.observed_at,
            source_device=data.source_device,
            evidence_ref=data.evidence_ref,
            confidence=data.confidence,
            uncertainty=data.uncertainty,
            consent_state=data.consent_state,
            retention_class=data.retention_class,
            stale_after_seconds=data.stale_after_seconds,
            fact_kind=data.fact_kind,
            metadata=data.metadata,
            persist_raw_frame=data.persist_raw_frame,
            actor=actor,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return ObservationOut.model_validate(row)


@router.get("/observations", response_model=list[ObservationOut])
async def get_observations(
    subject: str | None = None,
    location: str | None = None,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ObservationOut]:
    rows = await world_model.list_observations(
        session,
        subject=subject,
        location=location,
        action=action,
        limit=limit,
    )
    return [ObservationOut.model_validate(row) for row in rows]


@router.get("/locations/{location}/changes", response_model=list[ObservationOut])
async def location_changes(
    location: str,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ObservationOut]:
    """Return evidence-backed changes at a room/place, including stale state."""

    rows = await world_model.list_observations(
        session,
        location=location,
        action=action,
        limit=limit,
    )
    return [ObservationOut.model_validate(row) for row in rows]


@router.get("/observations/{observation_id}", response_model=ObservationOut)
async def get_observation(
    observation_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObservationOut:
    row = await session.get(world_model.ObservationRecord, observation_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Observation not found")
    row.freshness_state = world_model.freshness_state(
        row.observed_at,
        row.stale_after_seconds,
    )
    return ObservationOut.model_validate(row)


@router.get("/observations/{observation_id}/why")
async def explain_observation(
    observation_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    row = await session.get(world_model.ObservationRecord, observation_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Observation not found")
    return {
        "observation_id": str(row.id),
        "answer": f"I remember this because it was {row.fact_kind} at {row.observed_at.isoformat()}.",
        "source_device": row.source_device,
        "evidence_ref": row.evidence_ref,
        "confidence": row.confidence,
        "uncertainty": row.uncertainty,
        "consent_state": row.consent_state,
        "retention_class": row.retention_class,
        "freshness_state": world_model.freshness_state(row.observed_at, row.stale_after_seconds),
    }


@router.delete("/observations/{observation_id}", response_model=ObservationOut)
async def delete_observation(
    observation_id: UUID,
    reason: str = Query(default="user requested", max_length=512),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObservationOut:
    try:
        row = await world_model.forget_observation(
            session,
            observation_id,
            reason=reason,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Observation not found") from None
    await session.commit()
    return ObservationOut.model_validate(row)


@router.post("/objects", response_model=OwnerObjectOut, status_code=201)
async def create_object(
    data: OwnerObjectCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> OwnerObjectOut:
    try:
        row = await world_model.enroll_object(
            session,
            name=data.name,
            object_type=data.object_type,
            owner=data.owner,
            enrollment_source=data.enrollment_source,
            appearance_references=data.appearance_references,
            common_locations=data.common_locations,
            actor=actor,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return OwnerObjectOut.model_validate(row)


@router.get("/objects", response_model=list[OwnerObjectOut])
async def get_objects(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[OwnerObjectOut]:
    return [OwnerObjectOut.model_validate(row) for row in await world_model.list_objects(session, limit=limit)]


@router.get("/objects/by-name/{name}/last-seen", response_model=ObjectLastSeenOut)
async def object_last_seen_by_name(
    name: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObjectLastSeenOut:
    rows = await world_model.list_objects(session)
    row = next((item for item in rows if item.name.casefold() == name.casefold()), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return ObjectLastSeenOut.model_validate(await world_model.last_seen_object(session, row.id))


@router.get("/objects/{object_id}", response_model=OwnerObjectOut)
async def get_object(
    object_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> OwnerObjectOut:
    row = await session.get(world_model.OwnerObject, object_id)
    if row is None or row.status != "active" or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Object not found")
    if row.last_observed_at is not None:
        row.last_freshness_state = world_model.freshness_state(row.last_observed_at)
    return OwnerObjectOut.model_validate(row)


@router.post("/objects/{object_id}/observations", response_model=ObservationOut, status_code=201)
async def observe_object(
    object_id: UUID,
    data: ObjectObservationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObservationOut:
    try:
        row = await world_model.record_object_observation(
            session,
            object_id,
            location=data.location,
            observed_at=data.observed_at,
            source_device=data.source_device,
            evidence_ref=data.evidence_ref,
            confidence=data.confidence,
            uncertainty=data.uncertainty,
            action=data.action,
            fact_kind=data.fact_kind,
            possible_matches=data.possible_matches,
            metadata=data.metadata,
            persist_raw_frame=data.persist_raw_frame,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found") from None
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return ObservationOut.model_validate(row)


@router.get("/objects/{object_id}/last-seen", response_model=ObjectLastSeenOut)
async def object_last_seen(
    object_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObjectLastSeenOut:
    try:
        return ObjectLastSeenOut.model_validate(await world_model.last_seen_object(session, object_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found") from None


@router.get("/objects/{object_id}/why")
async def object_why(
    object_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    try:
        result = await world_model.last_seen_object(session, object_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found") from None
    return {"object_id": str(object_id), "answer": result["answer"], "why": result.get("why", {})}


@router.delete("/objects/{object_id}", response_model=OwnerObjectOut)
async def delete_object(
    object_id: UUID,
    reason: str = Query(default="user requested", max_length=512),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> OwnerObjectOut:
    try:
        row = await world_model.forget_object(session, object_id, reason=reason, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found") from None
    await session.commit()
    return OwnerObjectOut.model_validate(row)


@router.post("/people/observations", response_model=ObservationOut | dict, status_code=201)
async def observe_person(
    data: PersonObservationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ObservationOut | dict:
    try:
        row = await world_model.record_person_observation(
            session,
            person_name=data.person_name,
            location=data.location,
            observed_at=data.observed_at,
            source_device=data.source_device,
            evidence_ref=data.evidence_ref,
            confidence=data.confidence,
            uncertainty=data.uncertainty,
            consent_state=data.consent_state,
            action=data.action,
            actor=actor,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    if row is None:
        return {
            "unknown": True,
            "identity_persisted": False,
            "answer": "Unknown person; no permanent identity record was created.",
        }
    return ObservationOut.model_validate(row)


@router.delete("/people/{name}")
async def forget_person(
    name: str,
    reason: str = Query(default="user requested", max_length=512),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    count = await world_model.forget_person_observations(session, name, reason=reason, actor=actor)
    entity = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.canonical_key == f"person:{normalize_text(name)}",
            )
        )
    ).scalar_one_or_none()
    biometric_manifest = None
    if entity is not None:
        biometric_manifest = await erase_person(
            session,
            entity_id=entity.id,
            reason=reason,
            actor=actor,
        )
    await session.commit()
    return {
        "name": name,
        "observations_forgotten": count,
        "biometric_manifest": biometric_manifest,
        "reason": reason,
    }


@router.post("/forget-last-hour")
async def forget_last_hour(
    reason: str = Query(default="user requested", max_length=512),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    count = await world_model.forget_last_hour(session, reason=reason, actor=actor)
    await session.commit()
    return {"observations_forgotten": count, "reason": reason}


@router.put("/cameras/{device_id}", response_model=CameraStateOut)
async def set_camera_state(
    device_id: str,
    data: CameraStateUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> CameraStateOut:
    try:
        row = await world_model.update_camera_state(
            session,
            device_id,
            platform=data.platform,
            state=data.state,
            permission_state=data.permission_state,
            explicit_request=data.explicit_request,
            paused_reason=data.paused_reason,
            consent_state=data.consent_state,
            raw_frames_persisted=data.raw_frames_persisted,
            last_error=data.last_error,
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return CameraStateOut.model_validate(world_model.camera_state_out(row))


@router.get("/cameras", response_model=list[CameraStateOut])
async def get_camera_states(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[CameraStateOut]:
    return [
        CameraStateOut.model_validate(world_model.camera_state_out(row))
        for row in await world_model.list_camera_states(session)
    ]


@router.get("/cameras/{device_id}", response_model=CameraStateOut)
async def get_camera_state(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> CameraStateOut:
    return CameraStateOut.model_validate(
        world_model.camera_state_out(await world_model.get_camera_state(session, device_id))
    )
