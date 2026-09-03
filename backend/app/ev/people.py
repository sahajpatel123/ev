"""Person finder: locate a person across user-owned memory (never camera scanning)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityRelationship, Event, Memory, MemoryEntity, ObservationRecord, RecognitionLog
from app.schemas import PersonWhereaboutsOut
from app.utils.text import normalize_text, utcnow


def _freshness(value: str | datetime | None, *, stale_after_seconds: int = 86_400) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    age = (utcnow() - value.astimezone(UTC)).total_seconds()
    return "fresh" if age <= stale_after_seconds else "stale"


async def whereabouts(session: AsyncSession, name: str) -> PersonWhereaboutsOut:
    from app.memory.life_archive.locate import name_lookup_keys

    normalized = normalize_text(name)
    entity = None
    relationship = None
    for candidate in name_lookup_keys(name):
        key = normalize_text(candidate)
        result = await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.canonical_key == f"person:{key}",
            )
        )
        entity = result.scalar_one_or_none()
        if entity is not None:
            normalized = key
            break
    if entity is None:
        for candidate in name_lookup_keys(name):
            if len(candidate.strip()) < 4:
                continue
            result = await session.execute(
                select(Entity)
                .where(Entity.entity_type == "person", Entity.name.ilike(f"%{candidate}%"))
                .limit(5)
            )
            entity = result.scalars().first()
            if entity is not None:
                normalized = normalize_text(entity.name or candidate)
                break

    # Mentions across events.
    mention_stmt = (
        select(Event)
        .where(Event.tombstoned_at.is_(None))
        .order_by(Event.occurred_at.desc())
        .limit(2000)
    )
    events = list((await session.execute(mention_stmt)).scalars().all())
    mentions = [
        e
        for e in events
        if normalized in normalize_text((e.content or {}).get("text") or "")
    ]
    recent_mentions = [
        {
            "event_id": str(e.id),
            "occurred_at": e.occurred_at.isoformat(),
            "source": e.source,
            "event_type": e.event_type,
            "text": ((e.content or {}).get("text") or "")[:240],
        }
        for e in mentions[:5]
    ]

    related_memories: list[dict] = []
    if entity is not None:
        links = (
            await session.execute(
                select(MemoryEntity, Memory)
                .join(Memory, Memory.id == MemoryEntity.memory_id)
                .where(
                    MemoryEntity.entity_id == entity.id,
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
                .order_by(Memory.event_time.desc())
                .limit(10)
            )
        ).all()
        for link, memory in links:
            if relationship is None and link.role != "related":
                relationship = link.role
            related_memories.append(
                {
                    "memory_id": str(memory.id),
                    "memory_type": memory.memory_type,
                    "text": memory.text,
                    "event_time": memory.event_time.isoformat(),
                    "confidence": memory.confidence,
                }
            )

    # Face-free identity hints: user-confirmed recognition sightings from
    # user-owned media, each traceable to its perception event + attachment.
    sightings: list[dict] = []
    if entity is not None:
        recognition_rows = (
            await session.execute(
                select(RecognitionLog)
                .where(
                    RecognitionLog.entity_id == entity.id,
                    RecognitionLog.source == "user",
                )
                .order_by(RecognitionLog.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        sightings = [
            {
                "recognition_id": str(row.id),
                "label": row.label,
                "confidence": row.confidence,
                "attachment_id": str(row.attachment_id) if row.attachment_id else None,
                "perception_event_id": str(row.live_event_id) if row.live_event_id else None,
                "confirmed_at": row.created_at.isoformat(),
            }
            for row in recognition_rows
        ]

    # Structured world-model observations are consent-checked at write time
    # and carry their own evidence/freshness fields.  They augment, rather
    # than replace, explicit face-recognition and reported-event evidence.
    world_observations: list[dict] = []
    if entity is not None:
        rows = (
            await session.execute(
                select(ObservationRecord)
                .where(
                    ObservationRecord.subject_type == "person",
                    ObservationRecord.deleted_at.is_(None),
                    ObservationRecord.subject == entity.name,
                )
                .order_by(ObservationRecord.observed_at.desc())
                .limit(10)
            )
        ).scalars().all()
        world_observations = [
            {
                "observation_id": str(row.id),
                "location": row.location,
                "observed_at": row.observed_at.isoformat(),
                "source_device": row.source_device,
                "evidence_ref": row.evidence_ref,
                "confidence": row.confidence,
                "freshness_state": _freshness(
                    row.observed_at,
                    stale_after_seconds=row.stale_after_seconds,
                ),
                "uncertainty": row.uncertainty,
            }
            for row in rows
        ]

    # AGENT 7 ROSTER fusion: enrolled identity, face/voice sightings, biodata.
    enrolled: dict | None = None
    face_sightings: list[dict] = []
    voice_sightings: list[dict] = []
    public_biodata: dict | None = None
    biodata_merged = False
    if entity is not None:
        from app.models import FaceEnrollment

        enrollment = (
            await session.execute(
                select(FaceEnrollment).where(
                    FaceEnrollment.entity_id == entity.id,
                    FaceEnrollment.is_current.is_(True),
                    FaceEnrollment.status == "active",
                    FaceEnrollment.redacted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if enrollment is not None:
            enrolled = {
                "id": str(enrollment.id),
                "version": enrollment.version,
                "algorithm": enrollment.algorithm,
                "embedding_dim": enrollment.embedding_dim,
                "threshold": enrollment.threshold,
                "sample_count": enrollment.sample_count,
                "status": enrollment.status,
                "created_at": enrollment.created_at.isoformat(),
            }

        face_rows = (
            await session.execute(
                select(RecognitionLog)
                .where(
                    RecognitionLog.entity_id == entity.id,
                    RecognitionLog.source.in_(("model", "user")),
                )
                .order_by(RecognitionLog.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        face_sightings = [
            {
                "recognition_id": str(row.id),
                "label": row.label,
                "confidence": row.confidence,
                "confirmed": row.source == "user",
                "source": row.source,
                "attachment_id": str(row.attachment_id) if row.attachment_id else None,
                "live_event_id": str(row.live_event_id) if row.live_event_id else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in face_rows
        ]

        voice_rows = (
            await session.execute(
                select(RecognitionLog)
                .where(
                    RecognitionLog.entity_id == entity.id,
                    RecognitionLog.source.in_(("voice", "speaker")),
                )
                .order_by(RecognitionLog.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        voice_items = [
            {
                "recognition_id": str(row.id),
                "label": row.label,
                "confidence": row.confidence,
                "source": row.source,
                "attachment_id": str(row.attachment_id) if row.attachment_id else None,
                "live_event_id": str(row.live_event_id) if row.live_event_id else None,
                "occurred_at": row.created_at.isoformat(),
                "provenance": "recognition_log",
            }
            for row in voice_rows
        ]
        voice_events = list(
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.source == "voice",
                        Event.tombstoned_at.is_(None),
                    )
                    .order_by(Event.occurred_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        for event in voice_events:
            if normalized not in normalize_text((event.content or {}).get("text") or ""):
                continue
            voice_items.append(
                {
                    "event_id": str(event.id),
                    "label": entity.name,
                    "confidence": None,
                    "source": event.source,
                    "occurred_at": event.occurred_at.isoformat(),
                    "provenance": "event",
                }
            )
        voice_items.sort(key=lambda item: str(item["occurred_at"]), reverse=True)
        voice_sightings = voice_items[:10]

    # Public-figure biodata is surfaced only for people who are not enrolled,
    # so a public figure is never silently merged into a private identity.
    if entity is None or enrolled is None:
        try:
            from app.people.biodata import BiodataResolver

            resolver = BiodataResolver(session)
            biodata_result = await resolver.resolve(name)
            schema = await resolver.to_schema(biodata_result)
            public_biodata = schema.model_dump(mode="json")
            if entity is not None:
                from app.models import PublicFigureCache

                cache_row = (
                    await session.execute(
                        select(PublicFigureCache).where(
                            PublicFigureCache.entity_id == entity.id,
                            PublicFigureCache.confirmed.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                biodata_merged = cache_row is not None
        except Exception:
            # Biodata is enrichment only; a provider outage or missing module
            # must never block the whereabouts answer.
            public_biodata = None

    last_seen = None
    if world_observations:
        latest_observation = world_observations[0]
        last_seen = {
            **latest_observation,
            "event_id": latest_observation["observation_id"],
            "source": "world_model",
            "freshness_state": _freshness(latest_observation["observed_at"]),
            "text": f"Observed {name} at {latest_observation['location']}.",
        }
    elif mentions:
        latest = mentions[0]
        last_seen = {
            "occurred_at": latest.occurred_at.isoformat(),
            "event_id": str(latest.id),
            "source": latest.source,
            "freshness_state": _freshness(latest.occurred_at),
            "text": ((latest.content or {}).get("text") or "")[:240],
        }
    elif sightings:
        latest_sighting = sightings[0]
        last_seen = {
            "occurred_at": latest_sighting["confirmed_at"],
            "event_id": latest_sighting["recognition_id"],
            "source": "vision",
            "freshness_state": _freshness(latest_sighting["confirmed_at"]),
            "text": f"Confirmed in a shared attachment ({latest_sighting['label']}).",
        }

    if entity is not None:
        if relationship is None:
            edge = (
                await session.execute(
                    select(EntityRelationship)
                    .where(
                        EntityRelationship.to_entity_id == entity.id,
                        EntityRelationship.valid_until.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if edge is not None:
                relationship = edge.relationship_type
        summary = str(entity.summary or "").strip()
        if summary and not any(item.get("text") == summary for item in related_memories):
            related_memories.insert(
                0,
                {
                    "memory_id": str(entity.id),
                    "memory_type": "life.person",
                    "text": summary[:400],
                    "event_time": utcnow().isoformat(),
                    "confidence": 0.9,
                },
            )

    return PersonWhereaboutsOut(
        name=name,
        entity_id=entity.id if entity else None,
        relationship=relationship,
        last_seen=last_seen,
        recent_mentions=recent_mentions,
        sightings=sightings,
        related_memories=related_memories,
        total_events=len(mentions),
        enrolled=enrolled,
        face_sightings=face_sightings,
        voice_sightings=voice_sightings,
        world_observations=world_observations,
        public_biodata=public_biodata,
        biodata_merged=biodata_merged,
    )
