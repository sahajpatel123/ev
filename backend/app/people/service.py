"""PEOPLE FROM LIFE: person resolution, context, and roster (no People tab).

Resolution is provenance-first: a person is known from (1) the consented face
roster, (2) memory (names, aliases, relationship roles such as "mom"), and
(3) the CONDUIT contacts adapter as a *candidate only*. A contact name never
silently creates a person and never enables face recognition by itself.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ConsentRecord,
    Entity,
    EntityRelationship,
    FaceEnrollment,
    Integration,
    MemoryEntity,
    RecognitionLog,
)
from app.schemas import (
    PersonCandidateOut,
    PersonContextOut,
    PersonResolveOut,
    PersonRosterEntryOut,
    PersonRosterOut,
)
from app.utils.text import normalize_text, utcnow


async def _active_enrollment(
    session: AsyncSession,
    entity_id: UUID,
) -> FaceEnrollment | None:
    return (
        await session.execute(
            select(FaceEnrollment).where(
                FaceEnrollment.entity_id == entity_id,
                FaceEnrollment.is_current.is_(True),
                FaceEnrollment.status == "active",
                FaceEnrollment.redacted.is_(False),
            )
        )
    ).scalar_one_or_none()


async def _roles_for_entity(
    session: AsyncSession,
    entity_id: UUID,
) -> list[dict]:
    rows = (
        await session.execute(
            select(MemoryEntity.role).where(
                MemoryEntity.entity_id == entity_id,
                MemoryEntity.role != "related",
            )
        )
    ).scalars().all()
    roles = list(dict.fromkeys(role for role in rows if role))
    relationships = (
        await session.execute(
            select(EntityRelationship.relationship_type).where(
                EntityRelationship.to_entity_id == entity_id,
                EntityRelationship.valid_until.is_(None),
            )
        )
    ).scalars().all()
    roles.extend(
        relationship
        for relationship in relationships
        if relationship and relationship not in roles
    )
    return [{"role": role} for role in roles[:5]]


async def _last_seen_for_entity(
    session: AsyncSession,
    entity: Entity,
    normalized: str,
) -> dict | None:
    sighting = (
        await session.execute(
            select(RecognitionLog)
            .where(
                RecognitionLog.entity_id == entity.id,
                RecognitionLog.source.in_(("user", "model")),
            )
            .order_by(RecognitionLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sighting is not None:
        created_at = sighting.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age = (utcnow() - created_at.astimezone(UTC)).total_seconds()
        return {
            "occurred_at": sighting.created_at.isoformat(),
            "recognition_id": str(sighting.id),
            "source": "face",
            "confidence": sighting.confidence,
            "freshness_state": "fresh" if age <= timedelta(days=1).total_seconds() else "stale",
            "text": f"Recognized {sighting.label}.",
        }
    from app.models import Event, ObservationRecord

    world_observation = (
        await session.execute(
            select(ObservationRecord)
            .where(
                ObservationRecord.subject_type == "person",
                ObservationRecord.deleted_at.is_(None),
            )
            .order_by(ObservationRecord.observed_at.desc())
            .limit(200)
        )
    ).scalars().all()
    for observation in world_observation:
        if normalize_text(observation.subject) == normalized:
            return {
                "occurred_at": observation.observed_at.isoformat(),
                "observation_id": str(observation.id),
                "source": "world_model",
                "source_device": observation.source_device,
                "evidence_ref": observation.evidence_ref,
                "confidence": observation.confidence,
                "freshness_state": observation.freshness_state,
                "uncertainty": observation.uncertainty,
                "text": f"Observed {observation.subject} at {observation.location}.",
            }

    events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.tombstoned_at.is_(None))
                .order_by(Event.occurred_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        text = (event.content or {}).get("text") or ""
        if normalized in normalize_text(text):
            return {
                "occurred_at": event.occurred_at.isoformat(),
                "event_id": str(event.id),
                "source": event.source,
                "text": text[:240],
            }
    return None


async def _entity_candidates(
    session: AsyncSession,
    name: str,
) -> list[dict]:
    """Roster + memory candidates. Never creates an entity."""
    normalized = normalize_text(name)
    persons = list(
        (
            await session.execute(
                select(Entity).where(Entity.entity_type == "person")
            )
        )
        .scalars()
        .all()
    )

    def alias_matches(entity: Entity) -> bool:
        return normalized in {
            normalize_text(alias) for alias in (entity.aliases or [])
        }

    exact = [entity for entity in persons if entity.canonical_key == f"person:{normalized}"]
    alias_hits = [entity for entity in persons if alias_matches(entity)]
    ilike_hits = [entity for entity in persons if normalized in normalize_text(entity.name)]

    role_rows = (
        await session.execute(
            select(MemoryEntity, Entity)
            .join(Entity, Entity.id == MemoryEntity.entity_id)
            .where(
                Entity.entity_type == "person",
                MemoryEntity.role == normalized,
            )
        )
    ).all()
    role_hits: list[tuple[Entity, str]] = [
        (entity, row.role) for row, entity in role_rows
    ]

    ordered: list[tuple[int, Entity, str | None]] = []
    seen: set[UUID] = set()
    for entity in exact:
        if entity.id not in seen:
            ordered.append((0, entity, None))
            seen.add(entity.id)
    for entity, role in role_hits:
        if entity.id not in seen:
            ordered.append((1, entity, role))
            seen.add(entity.id)
    for entity in alias_hits:
        if entity.id not in seen:
            ordered.append((2, entity, None))
            seen.add(entity.id)
    for entity in ilike_hits:
        if entity.id not in seen:
            ordered.append((3, entity, None))
            seen.add(entity.id)

    ordered.sort(key=lambda item: item[0])
    candidates: list[dict] = []
    for _, entity, candidate_role in ordered:
        enrollment = await _active_enrollment(session, entity.id)
        last_seen = await _last_seen_for_entity(session, entity, normalized)
        candidates.append(
            {
                "entity_id": entity.id,
                "name": entity.name,
                "relationship": candidate_role,
                "provenance": "roster" if enrollment is not None else "memory",
                "face_enrolled": enrollment is not None,
                "consent_id": enrollment.consent_id if enrollment else None,
                "last_seen": last_seen,
                "confidence": None,
                "candidate_only": False,
            }
        )
    return candidates


async def _contact_candidates(
    session: AsyncSession,
    name: str,
    *,
    actor: str,
) -> tuple[list[dict], bool]:
    """Consume CONDUIT's contacts adapter; contacts are candidates only."""
    from app.integrations import service as integrations

    integration = (
        await session.execute(
            select(Integration)
            .where(
                Integration.adapter == "contacts",
                Integration.status == "active",
            )
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if integration is None:
        return [], False
    try:
        outcome = await integrations.execute_action(
            session,
            integration.id,
            "contacts.resolve",
            {"query": name, "limit": 5},
            actor=actor,
        )
    except Exception:
        # Contacts are optional candidates; any bridge failure must never
        # block roster/memory resolution.
        return [], True
    payload = getattr(outcome, "result", None) or {}
    records = payload.get("contacts") or payload.get("results") or []
    candidates: list[dict] = []
    for record in list(records)[:5]:
        if not isinstance(record, dict):
            continue
        display = (
            record.get("name")
            or record.get("fullName")
            or record.get("displayName")
            or name
        )
        candidates.append(
            {
                "entity_id": None,
                "name": str(display),
                "relationship": None,
                "provenance": "contact",
                "face_enrolled": False,
                "consent_id": None,
                "last_seen": None,
                "confidence": None,
                "candidate_only": True,
                "contact": record,
            }
        )
    return candidates, True


async def resolve_person(
    session: AsyncSession,
    name: str,
    *,
    include_contacts: bool = True,
    actor: str = "people",
) -> PersonResolveOut:
    """Resolve a name like 'Mom' or 'Alex' with provenance, never auto-creating."""
    candidates = await _entity_candidates(session, name)
    contacts_available = False
    if include_contacts:
        contact_candidates, contacts_available = await _contact_candidates(
            session, name, actor=actor
        )
        candidates.extend(contact_candidates)
    return PersonResolveOut(
        query=name,
        candidates=[PersonCandidateOut(**candidate) for candidate in candidates],
        contacts_available=contacts_available,
    )


async def person_context(session: AsyncSession, name: str) -> PersonContextOut:
    """Person context for EVIE's present/get_person surfaces."""
    from app.ev.people import whereabouts

    resolved = await resolve_person(session, name, include_contacts=True)
    whereabouts_out = await whereabouts(session, name)
    entity_id = whereabouts_out.entity_id

    enrollment = (
        await _active_enrollment(session, entity_id) if entity_id is not None else None
    )
    consent: ConsentRecord | None = None
    if enrollment is not None and enrollment.consent_id is not None:
        consent = await session.get(ConsentRecord, enrollment.consent_id)

    how_known = await _roles_for_entity(session, entity_id) if entity_id else []
    provenance: list[dict] = []
    if enrollment is not None:
        provenance.append(
            {
                "kind": "enrollment",
                "enrollment_id": str(enrollment.id),
                "consent_id": str(enrollment.consent_id) if enrollment.consent_id else None,
                "algorithm": enrollment.algorithm,
                "sample_count": enrollment.sample_count,
            }
        )
    for role in how_known:
        provenance.append({"kind": "relationship", "role": role["role"]})
    if resolved.candidates and any(c.provenance == "contact" for c in resolved.candidates):
        provenance.append({"kind": "contact_candidate"})

    if enrollment is not None:
        match_state = "enrolled"
    elif any(c.provenance == "contact" for c in resolved.candidates):
        match_state = "contact_candidate"
    else:
        match_state = "unknown"

    return PersonContextOut(
        name=name,
        entity_id=entity_id,
        relationship=whereabouts_out.relationship,
        how_known=how_known,
        last_seen=whereabouts_out.last_seen,
        enrolled=whereabouts_out.enrolled,
        consent={
            "consent_id": str(consent.id) if consent else None,
            "granted_at": consent.granted_at.isoformat() if consent else None,
            "purpose": consent.purpose if consent else None,
        },
        match_state=match_state,
        provenance=provenance,
        face_sightings=whereabouts_out.face_sightings,
        voice_sightings=whereabouts_out.voice_sightings,
    )


async def roster(session: AsyncSession) -> PersonRosterOut:
    """List every person EV knows (enrolled and/or memory-linked)."""
    enrollment_rows = (
        await session.execute(
            select(FaceEnrollment, Entity)
            .join(Entity, Entity.id == FaceEnrollment.entity_id)
            .where(
                FaceEnrollment.is_current.is_(True),
                FaceEnrollment.status == "active",
                FaceEnrollment.redacted.is_(False),
            )
        )
    ).all()
    memory_rows = (
        await session.execute(
            select(MemoryEntity, Entity)
            .join(Entity, Entity.id == MemoryEntity.entity_id)
            .where(Entity.entity_type == "person")
        )
    ).all()

    entries: dict[UUID, dict] = {}
    for enrollment, entity in enrollment_rows:
        entries[entity.id] = {
            "entity_id": entity.id,
            "name": entity.name,
            "face_enrolled": True,
            "consent_id": enrollment.consent_id,
            "sample_count": enrollment.sample_count,
            "provenance": [
                {
                    "kind": "enrollment",
                    "enrollment_id": str(enrollment.id),
                    "algorithm": enrollment.algorithm,
                }
            ],
        }
    for link, entity in memory_rows:
        entry = entries.setdefault(
            entity.id,
            {
                "entity_id": entity.id,
                "name": entity.name,
                "face_enrolled": False,
                "consent_id": None,
                "sample_count": 0,
                "provenance": [],
            },
        )
        if link.role and link.role != "related":
            entry["provenance"].append({"kind": "relationship", "role": link.role})
            entry.setdefault("relationship", link.role)

    people: list[PersonRosterEntryOut] = []
    for entity_id, entry in entries.items():
        entity = await session.get(Entity, entity_id)
        if entity is None:
            continue
        normalized = normalize_text(entry["name"])
        last_seen = await _last_seen_for_entity(session, entity, normalized)
        people.append(
            PersonRosterEntryOut(
                entity_id=entity_id,
                name=entry["name"],
                relationship=entry.get("relationship"),
                face_enrolled=entry["face_enrolled"],
                consent_id=entry["consent_id"],
                sample_count=entry["sample_count"],
                last_seen=last_seen,
                provenance=entry["provenance"],
            )
        )
    people.sort(key=lambda person: person.name.lower())
    return PersonRosterOut(people=people, total=len(people))
