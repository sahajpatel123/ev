"""Evidence-backed personal world-model persistence.

This module is deliberately narrower than the general memory writer.  It stores
structured observations and the small amount of state needed to answer
"what was last seen?" for an owner-enrolled object or a consented person.  It
does not identify strangers, persist camera frames, or call a provider.

The database models are additive Agent 2 models in :mod:`app.models`.  The
helpers here keep writes deterministic so retries converge on one row, while
leaving raw event and policy ownership to their existing modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CameraState,
    ConsentRecord,
    Entity,
    FaceEnrollment,
    ObservationRecord,
    OwnerObject,
)
from app.utils.text import canonical_json, normalize_text, utcnow

EpistemicKind = Literal["observed", "reported", "inferred", "guessed"]
FreshnessState = Literal["fresh", "stale"]

DEFAULT_STALE_AFTER_SECONDS = 86_400
DEFAULT_RETENTION_CLASS = "standard"

# These names are references to media, not media payloads.  Any matching key
# is removed before JSON reaches the database.  This is intentionally broader
# than the current camera clients so a future client cannot accidentally make
# raw-frame persistence the default by choosing a new field name.
_RAW_FRAME_KEYS = {
    "raw_frame",
    "raw_frames",
    "frame",
    "frame_bytes",
    "image",
    "image_b64",
    "image_bytes",
    "pixels",
}


def _utc(value: datetime | None) -> datetime:
    """Return an aware UTC datetime without changing the represented instant."""

    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean_text(value: str, *, field_name: str, max_length: int) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} is too long")
    return cleaned


class ObservationContract(BaseModel):
    """The version-one observation contract used by the persistence helpers.

    ``timestamp`` accepts the wire-level timestamp name; ``observed_at`` and
    ``object``/``event`` are accepted as input aliases for callers that mirror
    the SQL column or the natural-language contract.  The stored row remains
    explicit about ``object`` and ``observed_at``.
    """

    subject: str = Field(min_length=1, max_length=256)
    subject_type: str = Field(default="owner", min_length=1, max_length=32)
    object_or_event: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("object_or_event", "object", "event"),
    )
    action: str = Field(min_length=1, max_length=128)
    location: str = Field(default="unknown", max_length=512)
    timestamp: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "observed_at"),
    )
    source_device: str = Field(default="unknown", min_length=1, max_length=128)
    evidence_ref: str = Field(default="unknown", min_length=1, max_length=512)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainty: str = Field(default="unknown", max_length=512)
    consent_state: str = Field(default="unknown", min_length=1, max_length=32)
    retention_class: str = Field(default=DEFAULT_RETENTION_CLASS, min_length=1, max_length=64)
    stale_after_seconds: int = Field(default=DEFAULT_STALE_AFTER_SECONDS, ge=0)
    fact_kind: EpistemicKind = "observed"
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=512)

    @field_validator(
        "subject",
        "subject_type",
        "object_or_event",
        "action",
        "location",
        "source_device",
        "evidence_ref",
        "uncertainty",
        "consent_state",
        "retention_class",
        mode="before",
    )
    @classmethod
    def normalize_string(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def normalize_idempotency_key(cls, value: Any) -> str | None:
        if value is None:
            return None
        return " ".join(str(value).split()).strip() or None

    @field_validator("confidence")
    @classmethod
    def finite_confidence(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("confidence must be finite")
        return round(float(value), 6)

    @model_validator(mode="after")
    def normalize_timestamp(self) -> ObservationContract:
        self.timestamp = _utc(self.timestamp)
        if self.evidence_ref.startswith("data:"):
            raise ValueError("evidence_ref must be a reference, not inline media")
        return self

    @property
    def observed_at(self) -> datetime:
        """SQL-facing name for the wire-level ``timestamp`` field."""

        return _utc(self.timestamp)

    @property
    def object(self) -> str:  # noqa: A003 - mirrors the contract field name
        return self.object_or_event

    def stable_payload(self, *, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "subject_type": self.subject_type,
            "object": self.object_or_event,
            "action": self.action,
            "location": self.location,
            "timestamp": self.observed_at.isoformat(),
            "source_device": self.source_device,
            "evidence_ref": self.evidence_ref,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "consent_state": self.consent_state,
            "retention_class": self.retention_class,
            "stale_after_seconds": self.stale_after_seconds,
            "fact_kind": self.fact_kind,
            "metadata": metadata,
        }


def _discard_raw_frames(value: Any, discarded: list[str], path: str = "") -> Any:
    """Copy JSON-like metadata while dropping raw media payloads."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            child_path = f"{path}.{key_text}" if path else key_text
            if (
                normalized_key in _RAW_FRAME_KEYS
                or "raw_frame" in normalized_key
                or normalized_key.endswith("_frame_bytes")
            ):
                discarded.append(child_path)
                continue
            result[key_text] = _discard_raw_frames(child, discarded, child_path)
        return result
    if isinstance(value, (bytes, bytearray, memoryview)):
        discarded.append(path or "metadata")
        return None
    if isinstance(value, list):
        return [
            _discard_raw_frames(child, discarded, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    if isinstance(value, tuple):
        return [
            _discard_raw_frames(child, discarded, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    return value


def _safe_metadata(contract: ObservationContract, extra: Mapping[str, Any] | None = None) -> dict:
    discarded: list[str] = []
    value = _discard_raw_frames({**contract.metadata, **(extra or {})}, discarded)
    metadata = value if isinstance(value, dict) else {}
    explicitly_requested = bool(
        metadata.get("persist_raw_frame") or metadata.get("raw_frame_requested")
    )
    # Keep the fact that the privacy default was applied, never the discarded
    # payload.  The paths are useful for diagnostics and contain no content.
    metadata["raw_frame_persisted"] = explicitly_requested
    if discarded:
        metadata["raw_frame_discarded"] = True
        metadata["raw_frame_discarded_fields"] = sorted(set(discarded))[:32]
    return metadata


def _coerce_contract(
    observation: ObservationContract | Mapping[str, Any] | None,
    fields: Mapping[str, Any],
) -> ObservationContract:
    if observation is None:
        return ObservationContract(**dict(fields))
    if fields:
        raise TypeError("observation fields cannot be combined with an observation object")
    if isinstance(observation, ObservationContract):
        return observation
    return ObservationContract(**dict(observation))


def _observation_id(contract: ObservationContract, metadata: dict[str, Any]) -> UUID:
    stable_key = contract.idempotency_key or canonical_json(contract.stable_payload(metadata=metadata))
    return uuid5(NAMESPACE_URL, f"ev.observation.v1:{stable_key}")


def observation_dict(row: ObservationRecord, *, now: datetime | None = None) -> dict[str, Any]:
    """Serialize one row with both the contract and its current freshness."""

    current_time = _utc(now)
    freshness = freshness_for(
        row.observed_at,
        stale_after_seconds=row.stale_after_seconds,
        now=current_time,
    )
    return {
        "id": str(row.id),
        "subject": row.subject,
        "subject_type": row.subject_type,
        "object": row.object_or_event,
        "object_or_event": row.object_or_event,
        "action": row.action,
        "location": row.location,
        "timestamp": _utc(row.observed_at).isoformat(),
        "observed_at": _utc(row.observed_at).isoformat(),
        "source_device": row.source_device,
        "evidence_ref": row.evidence_ref,
        "confidence": row.confidence,
        "uncertainty": row.uncertainty,
        "consent_state": row.consent_state,
        "retention_class": row.retention_class,
        "freshness_state": freshness,
        "stale_after_seconds": row.stale_after_seconds,
        "fact_kind": row.fact_kind,
        "metadata": dict(row.metadata_ or {}),
        "deleted_at": _utc(row.deleted_at).isoformat() if row.deleted_at else None,
        "created_at": _utc(row.created_at).isoformat() if row.created_at else None,
    }


async def record_observation(
    session: AsyncSession,
    observation: ObservationContract | Mapping[str, Any] | None = None,
    *,
    actor: str = "system",
    **fields: Any,
) -> ObservationRecord:
    """Persist an observation exactly once for its deterministic identity.

    ``actor`` is retained in metadata for explainability.  It is not used to
    grant consent or to turn an inferred/guessed claim into an observation.
    Retries with the same contract return the original row.
    """

    contract = _coerce_contract(observation, fields)
    metadata = _safe_metadata(contract)
    row_id = _observation_id(contract, metadata)
    metadata["recorded_by"] = actor
    existing = await session.get(ObservationRecord, row_id)
    if existing is not None:
        return existing

    row = ObservationRecord(
        id=row_id,
        subject=contract.subject,
        subject_type=contract.subject_type,
        object_or_event=contract.object_or_event,
        action=contract.action,
        location=contract.location,
        observed_at=contract.observed_at,
        source_device=contract.source_device,
        evidence_ref=contract.evidence_ref,
        confidence=contract.confidence,
        uncertainty=contract.uncertainty,
        consent_state=contract.consent_state,
        retention_class=contract.retention_class,
        freshness_state=freshness_for(
            contract.observed_at,
            stale_after_seconds=contract.stale_after_seconds,
        ),
        stale_after_seconds=contract.stale_after_seconds,
        fact_kind=contract.fact_kind,
        metadata_=metadata,
    )

    # The deterministic UUID handles retries.  A savepoint makes the helper
    # safe when two workers race to insert the same observation without
    # rolling back the caller's surrounding transaction.
    inserted = False
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
            inserted = True
    except IntegrityError:
        inserted = False
    if inserted:
        return row
    existing = await session.get(ObservationRecord, row_id)
    if existing is None:
        raise RuntimeError("observation write conflicted but no winner is visible")
    return existing


write_observation = record_observation


def freshness_for(
    observed_at: datetime,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> FreshnessState:
    current_time = _utc(now)
    deadline = _utc(observed_at) + timedelta(seconds=max(0, stale_after_seconds))
    return "stale" if current_time >= deadline else "fresh"


async def mark_stale_evidence(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Materialize stale state for active observations without deleting them."""

    current_time = _utc(now)
    rows = list(
        (
            await session.execute(
                select(ObservationRecord).where(ObservationRecord.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    objects = {
        str(row.id): row
        for row in (await session.execute(select(OwnerObject))).scalars().all()
    }
    changed = 0
    for row in rows:
        state = freshness_for(
            row.observed_at,
            stale_after_seconds=row.stale_after_seconds,
            now=current_time,
        )
        if row.freshness_state != state:
            row.freshness_state = state
            changed += 1
        owner_object = objects.get(str((row.metadata_ or {}).get("owner_object_id")))
        if (
            owner_object is not None
            and row.fact_kind != "guessed"
            and owner_object.last_observed_at is not None
            and _utc(row.observed_at) >= _utc(owner_object.last_observed_at)
        ):
            owner_object.last_freshness_state = state
    return changed


async def last_seen_evidence(
    session: AsyncSession,
    *,
    subject: str | None = None,
    entity_id: UUID | None = None,
    owner_object_id: UUID | None = None,
    object_or_event: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the latest active evidence and its dynamic stale status."""

    rows = list(
        (
            await session.execute(
                select(ObservationRecord)
                .where(ObservationRecord.deleted_at.is_(None))
                .order_by(ObservationRecord.observed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    normalized_subject = normalize_text(subject) if subject else None
    target_entity = str(entity_id) if entity_id else None
    target_object = str(owner_object_id) if owner_object_id else None
    for row in rows:
        # A guess may be retained as an explainable candidate, but it is not
        # evidence that an object or person was actually seen.
        if row.fact_kind == "guessed":
            continue
        metadata = row.metadata_ or {}
        if normalized_subject and normalize_text(row.subject) != normalized_subject:
            continue
        if target_entity and str(metadata.get("entity_id")) != target_entity:
            continue
        if target_object and str(metadata.get("owner_object_id")) != target_object:
            continue
        if object_or_event and normalize_text(row.object_or_event) != normalize_text(object_or_event):
            continue
        return observation_dict(row, now=now)
    return None


async def enroll_owner_object(
    session: AsyncSession,
    *,
    name: str,
    object_type: str = "thing",
    owner: str = "owner",
    appearance_references: list[str] | None = None,
    common_locations: list[str] | None = None,
    enrollment_source: str = "user",
) -> OwnerObject:
    """Create or converge an explicitly owner-enrolled object record."""

    clean_name = _clean_text(name, field_name="name", max_length=256)
    clean_owner = _clean_text(owner, field_name="owner", max_length=256)
    clean_type = _clean_text(object_type, field_name="object_type", max_length=64)
    clean_source = _clean_text(
        enrollment_source,
        field_name="enrollment_source",
        max_length=128,
    )
    existing = next(
        (
            row
            for row in (await session.execute(select(OwnerObject))).scalars().all()
            if normalize_text(row.owner) == normalize_text(clean_owner)
            and normalize_text(row.name) == normalize_text(clean_name)
            and normalize_text(row.object_type) == normalize_text(clean_type)
        ),
        None,
    )
    if existing is not None:
        existing.status = "active"
        existing.deleted_at = None
        existing.appearance_references = _merge_strings(
            existing.appearance_references,
            appearance_references,
        )
        existing.common_locations = _merge_strings(existing.common_locations, common_locations)
        return existing

    row = OwnerObject(
        id=uuid5(
            NAMESPACE_URL,
            f"ev.owner-object.v1:{normalize_text(clean_owner)}:{normalize_text(clean_type)}:{normalize_text(clean_name)}",
        ),
        owner=clean_owner,
        name=clean_name,
        object_type=clean_type,
        enrollment_source=clean_source,
        appearance_references=_merge_strings([], appearance_references),
        common_locations=_merge_strings([], common_locations),
        last_freshness_state="unknown",
        possible_matches=[],
        status="active",
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = await session.get(OwnerObject, row.id)
        if winner is None:
            raise RuntimeError("owner object write conflicted but no winner is visible") from None
        return winner
    return row


def _merge_strings(existing: list | None, incoming: list[str] | None) -> list[str]:
    values: list[str] = []
    for value in [*(existing or []), *(incoming or [])]:
        cleaned = " ".join(str(value).split()).strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return values


async def record_owner_object_observation(
    session: AsyncSession,
    object_id: UUID | str,
    observation: ObservationContract | Mapping[str, Any],
    *,
    actor: str = "system",
    possible_matches: list[dict[str, Any]] | None = None,
) -> ObservationRecord:
    """Record evidence for an enrolled object and update only newer last-seen state."""

    try:
        target_id = UUID(str(object_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("object_id must be a UUID") from exc
    owner_object = await session.get(OwnerObject, target_id)
    if owner_object is None or owner_object.status != "active" or owner_object.deleted_at:
        raise KeyError(f"Owner object {object_id} is not active")

    contract = _coerce_contract(observation, {})
    metadata = {**contract.metadata, "owner_object_id": str(owner_object.id)}
    contract = contract.model_copy(update={"metadata": metadata})
    row = await record_observation(session, contract, actor=actor)
    if row.fact_kind == "guessed":
        candidates = possible_matches or (row.metadata_ or {}).get("possible_matches") or []
        owner_object.possible_matches = candidates[:32]
        return row

    observed_at = _utc(row.observed_at)
    if owner_object.last_observed_at is None or observed_at >= _utc(owner_object.last_observed_at):
        owner_object.last_observed_location = row.location
        owner_object.last_observed_at = row.observed_at
        owner_object.last_evidence_ref = row.evidence_ref
        owner_object.last_confidence = row.confidence
        owner_object.last_uncertainty = row.uncertainty
        owner_object.last_freshness_state = freshness_for(
            row.observed_at,
            stale_after_seconds=row.stale_after_seconds,
        )
    return row


async def owner_object_dict(
    session: AsyncSession,
    object_id: UUID | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    owner_object = await session.get(OwnerObject, UUID(str(object_id)))
    if owner_object is None:
        raise KeyError(f"Owner object {object_id} not found")
    latest = await last_seen_evidence(session, owner_object_id=owner_object.id, now=now)
    freshness = (
        latest["freshness_state"] if latest is not None else owner_object.last_freshness_state
    )
    return {
        "id": str(owner_object.id),
        "owner": owner_object.owner,
        "name": owner_object.name,
        "object_type": owner_object.object_type,
        "enrolled": owner_object.status == "active" and owner_object.deleted_at is None,
        "enrollment_source": owner_object.enrollment_source,
        "appearance_references": list(owner_object.appearance_references or []),
        "common_locations": list(owner_object.common_locations or []),
        "last_seen": {
            "location": owner_object.last_observed_location,
            "timestamp": _utc(owner_object.last_observed_at).isoformat(),
            "evidence_ref": owner_object.last_evidence_ref,
            "confidence": owner_object.last_confidence,
            "uncertainty": owner_object.last_uncertainty,
            "freshness_state": freshness,
        }
        if owner_object.last_observed_at
        else None,
        "possible_matches": list(owner_object.possible_matches or []),
        "status": owner_object.status,
        "deleted_at": _utc(owner_object.deleted_at).isoformat() if owner_object.deleted_at else None,
    }


async def _resolve_person(session: AsyncSession, name_or_id: str | UUID) -> Entity | None:
    try:
        entity_id = UUID(str(name_or_id))
    except (TypeError, ValueError):
        entity_id = None
    if entity_id is not None:
        return await session.get(Entity, entity_id)
    normalized = normalize_text(str(name_or_id))
    return (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.canonical_key == f"person:{normalized}",
            )
        )
    ).scalar_one_or_none()


async def _person_consent(
    session: AsyncSession,
    enrollment: FaceEnrollment | None,
) -> tuple[ConsentRecord | None, str]:
    if enrollment is None or enrollment.consent_id is None:
        return None, "not_granted"
    consent = await session.get(ConsentRecord, enrollment.consent_id)
    if consent is None:
        return None, "not_granted"
    return consent, "revoked" if consent.revoked_at is not None else "granted"


async def record_person_observation(
    session: AsyncSession,
    observation: ObservationContract | Mapping[str, Any],
    *,
    person_name: str | None = None,
    entity_id: UUID | None = None,
    actor: str = "system",
) -> ObservationRecord:
    """Persist a person sighting as identified only with active consent.

    An unenrolled or revoked person is intentionally represented as
    ``subject='unknown'``.  The helper never creates an ``Entity`` from a
    candidate label and never stores a stranger's name as an identity claim.
    """

    contract = _coerce_contract(observation, {})
    entity = await session.get(Entity, entity_id) if entity_id else None
    if entity is None and person_name:
        entity = await _resolve_person(session, person_name)

    enrollment: FaceEnrollment | None = None
    consent: ConsentRecord | None = None
    consent_state = "not_granted"
    if entity is not None:
        enrollment = (
            await session.execute(
                select(FaceEnrollment).where(
                    FaceEnrollment.entity_id == entity.id,
                    FaceEnrollment.is_current.is_(True),
                    FaceEnrollment.status == "active",
                    FaceEnrollment.redacted.is_(False),
                )
            )
        ).scalars().first()
        consent, consent_state = await _person_consent(session, enrollment)

    identified = (
        entity is not None
        and enrollment is not None
        and consent is not None
        and consent_state == "granted"
    )
    metadata = dict(contract.metadata)
    metadata["identity_status"] = "enrolled" if identified else "unknown"
    if identified and entity is not None:
        metadata["entity_id"] = str(entity.id)
        metadata["consent_id"] = str(consent.id) if consent else None
        metadata["enrollment_id"] = str(enrollment.id) if enrollment else None
    else:
        metadata.pop("entity_id", None)
        metadata.pop("consent_id", None)
        metadata.pop("enrollment_id", None)

    identity_contract = contract.model_copy(
        update={
            "subject": entity.name if identified and entity is not None else "unknown",
            "subject_type": "person",
            "consent_state": consent_state,
            "metadata": metadata,
        }
    )
    return await record_observation(session, identity_contract, actor=actor)


async def person_record(
    session: AsyncSession,
    name_or_id: str | UUID,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return consent state, enrollment evidence, and real last-seen evidence."""

    entity = await _resolve_person(session, name_or_id)
    if entity is None:
        return {
            "name": str(name_or_id),
            "entity_id": None,
            "identity_status": "unknown",
            "consent_state": "not_granted",
            "enrolled": False,
            "last_seen": None,
            "why": "No owner-known person record exists; identity remains unknown.",
        }

    enrollment = (
        await session.execute(
            select(FaceEnrollment).where(
                FaceEnrollment.entity_id == entity.id,
                FaceEnrollment.is_current.is_(True),
            )
        )
    ).scalars().first()
    consent, consent_state = await _person_consent(session, enrollment)
    identified = (
        enrollment is not None
        and enrollment.status == "active"
        and not enrollment.redacted
        and consent is not None
        and consent_state == "granted"
    )
    last_seen = (
        await last_seen_evidence(
            session,
            entity_id=entity.id,
            subject=entity.name,
            now=now,
        )
        if identified
        else None
    )
    return {
        "name": entity.name,
        "entity_id": str(entity.id),
        "identity_status": "enrolled" if identified else "unknown",
        "consent_state": consent_state,
        "enrolled": identified,
        "enrollment_id": str(enrollment.id) if identified and enrollment else None,
        "consent_id": str(consent.id) if identified and consent else None,
        "last_seen": last_seen,
        "why": (
            "Active enrollment is linked to unrevoked owner consent."
            if identified
            else "Identity is not surfaced because active enrollment consent is absent or revoked."
        ),
    }


async def explain_observation(
    session: AsyncSession,
    observation_id: UUID | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explain why an observation is retained and what source supports it."""

    row = await session.get(ObservationRecord, UUID(str(observation_id)))
    if row is None:
        raise KeyError(f"Observation {observation_id} not found")
    payload = observation_dict(row, now=now)
    payload["source"] = {
        "device": row.source_device,
        "evidence_ref": row.evidence_ref,
        "observed_at": _utc(row.observed_at).isoformat(),
        "kind": row.fact_kind,
        "consent_state": row.consent_state,
    }
    payload["why"] = (
        f"{row.fact_kind} evidence from {row.source_device}; confidence "
        f"{row.confidence:.3f}; uncertainty: {row.uncertainty}."
    )
    return payload


observation_source = explain_observation


async def forget_observation(
    session: AsyncSession,
    observation_id: UUID | str,
    *,
    reason: str = "user requested",
    at: datetime | None = None,
) -> ObservationRecord:
    """Exclude an observation while retaining a tombstone for auditability."""

    row = await session.get(ObservationRecord, UUID(str(observation_id)))
    if row is None:
        raise KeyError(f"Observation {observation_id} not found")
    if row.deleted_at is None:
        row.deleted_at = _utc(at)
        metadata = dict(row.metadata_ or {})
        metadata["forgotten"] = True
        metadata["forget_reason"] = _clean_text(reason, field_name="reason", max_length=512)
        metadata["forgotten_at"] = _utc(row.deleted_at).isoformat()
        row.metadata_ = metadata
    return row


async def delete_observation(session: AsyncSession, observation_id: UUID | str) -> None:
    """Permanently remove the derived observation row, not its source evidence."""

    row = await session.get(ObservationRecord, UUID(str(observation_id)))
    if row is None:
        return
    await session.delete(row)
    await session.flush()


async def forget_owner_object(
    session: AsyncSession,
    object_id: UUID | str,
    *,
    reason: str = "user requested",
    at: datetime | None = None,
) -> OwnerObject:
    owner_object = await session.get(OwnerObject, UUID(str(object_id)))
    if owner_object is None:
        raise KeyError(f"Owner object {object_id} not found")
    owner_object.status = "forgotten"
    owner_object.deleted_at = _utc(at)
    observations = list(
        (
            await session.execute(
                select(ObservationRecord).where(ObservationRecord.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for row in observations:
        if str((row.metadata_ or {}).get("owner_object_id")) == str(owner_object.id):
            await forget_observation(session, row.id, reason=reason, at=at)
    return owner_object


async def delete_owner_object(session: AsyncSession, object_id: UUID | str) -> None:
    owner_object = await session.get(OwnerObject, UUID(str(object_id)))
    if owner_object is None:
        return
    observations = list(
        (
            await session.execute(
                select(ObservationRecord).where(ObservationRecord.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for row in observations:
        if str((row.metadata_ or {}).get("owner_object_id")) == str(owner_object.id):
            await delete_observation(session, row.id)
    await session.delete(owner_object)
    await session.flush()


async def forget_person_observations(
    session: AsyncSession,
    name_or_id: str | UUID,
    *,
    reason: str = "user requested",
    at: datetime | None = None,
) -> int:
    """Forget derived sightings for one enrolled person without deleting consent data."""

    entity = await _resolve_person(session, name_or_id)
    if entity is None:
        return 0
    target_id = str(entity.id)
    normalized_name = normalize_text(entity.name)
    rows = list(
        (
            await session.execute(
                select(ObservationRecord).where(
                    ObservationRecord.deleted_at.is_(None),
                    ObservationRecord.subject_type == "person",
                )
            )
        )
        .scalars()
        .all()
    )
    selected = [
        row
        for row in rows
        if str((row.metadata_ or {}).get("entity_id")) == target_id
        or normalize_text(row.subject) == normalized_name
    ]
    for row in selected:
        await forget_observation(session, row.id, reason=reason, at=at)
    return len(selected)


async def delete_person_observations(
    session: AsyncSession,
    name_or_id: str | UUID,
) -> int:
    """Permanently remove derived sightings, leaving enrollment/consent ownership intact."""

    entity = await _resolve_person(session, name_or_id)
    if entity is None:
        return 0
    target_id = str(entity.id)
    normalized_name = normalize_text(entity.name)
    rows = list(
        (
            await session.execute(
                select(ObservationRecord).where(
                    ObservationRecord.subject_type == "person",
                )
            )
        )
        .scalars()
        .all()
    )
    selected = [
        row
        for row in rows
        if str((row.metadata_ or {}).get("entity_id")) == target_id
        or normalize_text(row.subject) == normalized_name
    ]
    for row in selected:
        await session.delete(row)
    await session.flush()
    return len(selected)


async def sweep_observation_retention(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Forget observations with an explicit ``retention_seconds`` policy.

    A plain retention class is descriptive until its policy is configured;
    only an explicit duration in metadata can trigger deletion here.  This
    avoids silently applying a guessed lifetime to personal observations.
    """

    current_time = _utc(now)
    rows = list(
        (
            await session.execute(
                select(ObservationRecord).where(ObservationRecord.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    deleted = 0
    for row in rows:
        raw_seconds = (row.metadata_ or {}).get("retention_seconds")
        if raw_seconds is None:
            continue
        try:
            retention_seconds = int(raw_seconds)
        except (TypeError, ValueError):
            continue
        if retention_seconds < 0:
            continue
        if current_time >= _utc(row.created_at) + timedelta(seconds=retention_seconds):
            await forget_observation(
                session,
                row.id,
                reason=f"retention policy: {row.retention_class}",
                at=current_time,
            )
            deleted += 1
    return deleted


async def upsert_camera_state(
    session: AsyncSession,
    *,
    device_id: str,
    platform: str = "unknown",
    state: str = "off",
    visible: bool = False,
    permission_state: str = "unknown",
    explicit_request: bool = False,
    paused_reason: str | None = None,
    consent_state: str = "not_granted",
    persist_raw_frames: bool = False,
    last_error: str | None = None,
) -> CameraState:
    """Converge camera status while defaulting to discard-only frame handling."""

    clean_device = _clean_text(device_id, field_name="device_id", max_length=128)
    if persist_raw_frames and consent_state not in {
        "granted",
        "camera_granted",
        "owner_confirmed",
        "explicit",
    }:
        raise ValueError("raw frame persistence requires explicit camera consent")
    row = (
        await session.execute(
            select(CameraState).where(CameraState.device_id == clean_device)
        )
    ).scalars().first()
    values = {
        "platform": _clean_text(platform, field_name="platform", max_length=32),
        "state": _clean_text(state, field_name="state", max_length=32),
        "visible": bool(visible),
        "permission_state": _clean_text(
            permission_state,
            field_name="permission_state",
            max_length=32,
        ),
        "explicit_request": bool(explicit_request),
        "paused_reason": paused_reason,
        "consent_state": _clean_text(consent_state, field_name="consent_state", max_length=32),
        "raw_frames_persisted": bool(persist_raw_frames),
        "last_error": last_error,
    }
    if row is not None:
        for key, value in values.items():
            setattr(row, key, value)
        return row
    row = CameraState(
        id=uuid5(NAMESPACE_URL, f"ev.camera-state.v1:{normalize_text(clean_device)}"),
        device_id=clean_device,
        **values,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = (
            await session.execute(
                select(CameraState).where(CameraState.device_id == clean_device)
            )
        ).scalars().first()
        if winner is None:
            raise RuntimeError("camera state write conflicted but no winner is visible") from None
        return winner
    return row


__all__ = [
    "EpistemicKind",
    "ObservationContract",
    "delete_observation",
    "delete_person_observations",
    "delete_owner_object",
    "enroll_owner_object",
    "explain_observation",
    "freshness_for",
    "forget_observation",
    "forget_person_observations",
    "forget_owner_object",
    "last_seen_evidence",
    "mark_stale_evidence",
    "observation_dict",
    "observation_source",
    "owner_object_dict",
    "person_record",
    "record_observation",
    "record_owner_object_observation",
    "record_person_observation",
    "sweep_observation_retention",
    "upsert_camera_state",
    "write_observation",
]
