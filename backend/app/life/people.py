"""G1 person/relationship semantics over the EXISTING Entity graph.

Laws:
- NO second people database. Person rows are Entity(entity_type="person")
  with the canonical_key convention already used by app/ev/people.py
  ("person:<normalized>"); relationships are EntityRelationship edges.
- Owner identity is NOT duplicated. OwnerIdentity (app/identity) remains the
  single identity authority. Relationships are anchored to a lazily-created
  owner graph node (Entity entity_type="owner", canonical_key="person:owner")
  which is a GRAPH ANCHOR for edges, never an identity record.
- Owner-asserted relationship facts are explicit, sourced, and evented:
  relationship.created / relationship.updated in the canonical `events` table.
- NO biometric recognition, NO photo ingestion, NO face/voice training here.
  FaceEnrollment / VoicePrint stay separate dormant capability infrastructure.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityRelationship, Event
from app.utils.text import normalize_text, utcnow

# Normalized owner-facing vocabulary (G1). "other" absorbs everything else;
# free-text relation strings are normalized into this set.
RELATIONSHIP_TYPES = (
    "friend",
    "family",
    "partner",
    "parent",
    "sibling",
    "child",
    "colleague",
    "classmate",
    "professional_contact",
    "other",
)

SOURCE = "life"
PRIVACY_DEFAULT = "normal"


def _sha(content: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode()
    ).hexdigest()


def _public(row: EntityRelationship, person: Entity | None) -> dict:
    return {
        "id": str(row.id),
        "person": person.name if person else None,
        "person_entity_id": str(row.to_entity_id),
        "relation": row.relationship_type,
        "source_type": row.source_type,
        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
    }


async def owner_anchor(session: AsyncSession) -> Entity:
    """Get-or-create the owner graph anchor. NOT an identity record —
    OwnerIdentity stays the single authority; this node only anchors edges."""
    row = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "owner",
                Entity.canonical_key == "person:owner",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Entity(
            entity_type="owner",
            name="Owner",
            aliases=[],
            summary="The owner of this Evie instance (graph anchor only).",
            canonical_key="person:owner",
        )
        session.add(row)
        await session.flush()
    return row


async def ensure_person(
    session: AsyncSession,
    *,
    name: str,
    aliases: list[str] | None = None,
    summary: str | None = None,
) -> Entity:
    """Get-or-create a person Entity using the existing roster convention."""
    normalized = normalize_text(name)
    if not normalized:
        raise ValueError("empty_person_name")
    row = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.canonical_key == f"person:{normalized}",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Entity(
            entity_type="person",
            name=name.strip(),
            aliases=list(aliases or []),
            summary=summary,
            canonical_key=f"person:{normalized}",
        )
        session.add(row)
        await session.flush()
    elif aliases:
        merged = list(dict.fromkeys([*(row.aliases or []), *aliases]))
        if merged != list(row.aliases or []):
            row.aliases = merged
    return row


async def set_relationship(
    session: AsyncSession,
    *,
    actor: str,
    person_name: str,
    relation: str,
    note: str | None = None,
    privacy_level: str = PRIVACY_DEFAULT,
    device_id: str | None = None,
) -> dict:
    """Assert/update an owner-person relationship. Explicit creation only —
    never inferred from casual conversation in G1."""
    relation_norm = (relation or "").strip().lower().replace("-", "_").replace(" ", "_")
    if relation_norm not in RELATIONSHIP_TYPES:
        return {
            "ok": False,
            "error": "unknown_relation",
            "allowed": list(RELATIONSHIP_TYPES),
        }
    anchor = await owner_anchor(session)
    person = await ensure_person(session, name=person_name, summary=note)

    existing = (
        await session.execute(
            select(EntityRelationship)
            .where(
                EntityRelationship.from_entity_id == anchor.id,
                EntityRelationship.to_entity_id == person.id,
                EntityRelationship.valid_until.is_(None),
            )
            .order_by(EntityRelationship.created_time.desc())
        )
    ).scalars().first()

    async def _emit(event_type: str, content: dict) -> Event:
        row = Event(
            source=SOURCE,
            event_type=event_type,
            content={"actor": actor, **content},
            device_id=device_id,
            privacy_level=privacy_level,
            sha256=_sha({"t": event_type, **content}),
            occurred_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        return row

    if existing is not None and existing.relationship_type == relation_norm:
        return {
            "ok": True,
            "relationship": _public(existing, person),
            "unchanged": True,
            "spoken": f"{person.name} is already recorded as your {relation_norm}.",
        }

    if existing is not None:
        previous = existing.relationship_type
        existing.valid_until = utcnow()
        edge = EntityRelationship(
            from_entity_id=anchor.id,
            to_entity_id=person.id,
            relationship_type=relation_norm,
            weight=existing.weight,
            source_type="owner",
        )
        session.add(edge)
        await session.flush()
        await _emit(
            "relationship.updated",
            {
                "relationship_id": str(edge.id),
                "person": person.name,
                "person_entity_id": str(person.id),
                "relation": relation_norm,
                "previous_relation": previous,
            },
        )
        return {
            "ok": True,
            "relationship": _public(edge, person),
            "previous_relation": previous,
            "spoken": f"{person.name} is now recorded as your {relation_norm}.",
        }

    edge = EntityRelationship(
        from_entity_id=anchor.id,
        to_entity_id=person.id,
        relationship_type=relation_norm,
        source_type="owner",
    )
    session.add(edge)
    await session.flush()
    await _emit(
        "relationship.created",
        {
            "relationship_id": str(edge.id),
            "person": person.name,
            "person_entity_id": str(person.id),
            "relation": relation_norm,
        },
    )
    return {
        "ok": True,
        "relationship": _public(edge, person),
        "spoken": f"{person.name} is recorded as your {relation_norm}.",
    }


async def list_relationships(session: AsyncSession) -> list[dict]:
    """Active (open-ended) relationships of the owner anchor."""
    anchor = await owner_anchor(session)
    rows = (
        await session.execute(
            select(EntityRelationship)
            .where(
                EntityRelationship.from_entity_id == anchor.id,
                EntityRelationship.valid_until.is_(None),
            )
            .order_by(EntityRelationship.created_time.desc())
        )
    ).scalars().all()
    out = []
    for r in rows:
        person = await session.get(Entity, r.to_entity_id)
        out.append(_public(r, person))
    return out
