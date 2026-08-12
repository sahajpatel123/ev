"""Entity extraction, canonicalization, resolution, and human-confirmed merge.

Resolution is deterministic: alias tables and nickname families are derived
from the immutable event log (merge events add aliases), and embedding
similarity uses the configured embedder, which is deterministic for a fixed
model. Every merge is recorded as an ``entity.merge`` raw event so the rebuild
pipeline can replay it into an equivalent state.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef
from app.models import Entity, MemoryEntity

# --------------------------------------------------------------------------- #
# Normalization and nickname families
# --------------------------------------------------------------------------- #


def normalize_entity_name(name: str) -> str:
    """Canonical display-independent key: lowercase, accent-folded, ASCII."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = re.sub(r"[^a-z0-9' ]+", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


# Common English nickname families. These are candidate hints only: nothing is
# merged without an explicit human confirmation (or an alias already present).
NICKNAME_FAMILIES: list[set[str]] = [
    {"mike", "mikey", "michael"},
    {"bob", "robert"},
    {"bobby", "robert"},
    {"rob", "robert"},
    {"liz", "elizabeth"},
    {"beth", "elizabeth"},
    {"lizzy", "elizabeth"},
    {"betty", "elizabeth"},
    {"jim", "james"},
    {"jimmy", "james"},
    {"tom", "thomas"},
    {"tommy", "thomas"},
    {"dave", "david"},
    {"davey", "david"},
    {"pat", "patrick"},
    {"patti", "patricia"},
    {"patty", "patricia"},
    {"kate", "katherine"},
    {"katie", "katherine"},
    {"kathy", "katherine"},
    {"sue", "susan"},
    {"suzie", "susan"},
]


def nickname_family(name: str) -> str | None:
    normalized = normalize_entity_name(name)
    for family in NICKNAME_FAMILIES:
        if normalized in family:
            return "|".join(sorted(family))
    return None


def canonical_key_for(name: str, entity_type: str) -> str:
    return f"{entity_type}:{normalize_entity_name(name)}"


def _token_set(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", normalize_entity_name(name)))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


# --------------------------------------------------------------------------- #
# Extraction from text (unchanged interface)
# --------------------------------------------------------------------------- #


PLACE_STOPWORDS = {
    "in",
    "to",
    "at",
    "and",
    "the",
    "of",
    "for",
    "on",
    "a",
    "an",
    "with",
    "last",
    "this",
    "next",
    "year",
    "month",
    "week",
    "ago",
    "yesterday",
    "today",
    "tomorrow",
}


def _clean_place_name(name: str) -> str:
    tokens = name.split()
    while tokens and tokens[-1].lower() in PLACE_STOPWORDS:
        tokens.pop()
    return " ".join(tokens)


def extract_entities_from_text(text: str) -> list[EntityRef]:
    """Lightweight entity extraction: @mentions, relatives, residence."""
    refs: list[EntityRef] = []
    for match in re.findall(r"@([A-Za-z0-9_]+)", text):
        refs.append(EntityRef(name=match, entity_type="topic"))
    for relation, name in re.findall(
        r"(?:my|our)\s+(friend|colleague|boss|manager|mom|dad|mother|father|brother|sister|wife|husband|partner|girlfriend|boyfriend|roommate|neighbor)\s+([A-Z][a-z]+)",
        text,
        re.IGNORECASE,
    ):
        refs.append(EntityRef(name=name, entity_type="person", role=relation, weight=1.0))
    for place in re.findall(
        r"(?:live|lives|moved|move|based|travel|travelling|heading)\s+(?:in|to|at)\s+([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+)?)",
        text,
        re.IGNORECASE,
    ):
        place = _clean_place_name(place)
        if place:
            refs.append(EntityRef(name=place, entity_type="place"))
    # Deduplicate by (type, normalized name).
    seen: set[tuple[str, str]] = set()
    unique: list[EntityRef] = []
    for ref in refs:
        key = (ref.entity_type, normalize_entity_name(ref.name))
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@dataclass
class EntityCandidate:
    entity: Entity
    score: float
    reason: str


async def find_entity_candidates(
    session: AsyncSession,
    ref: EntityRef,
    *,
    embeddings=None,
    limit: int = 5,
) -> list[EntityCandidate]:
    """Rank existing entities that could represent ``ref`` (no auto-merge)."""
    name = normalize_entity_name(ref.name)
    if not name:
        return []
    rows = list(
        (
            await session.execute(
                select(Entity).where(Entity.entity_type == ref.entity_type)
            )
        ).scalars().all()
    )
    if not rows:
        return []

    family = nickname_family(name)
    tokens = _token_set(name)
    query_embedding: list[float] | None = None
    name_embeddings: dict[str, list[float]] = {}
    if embeddings is not None:
        try:
            vectors = await embeddings.embed([ref.name, *[row.name for row in rows]])
            query_embedding = vectors[0]
            name_embeddings = {
                normalize_entity_name(row.name): vectors[i + 1]
                for i, row in enumerate(rows)
            }
        except Exception:
            query_embedding = None

    candidates: list[EntityCandidate] = []
    for row in rows:
        row_name = normalize_entity_name(row.name)
        if row_name == name:
            candidates.append(EntityCandidate(entity=row, score=1.0, reason="exact"))
            continue
        alias_names = {normalize_entity_name(alias) for alias in (row.aliases or [])}
        if name in alias_names:
            candidates.append(EntityCandidate(entity=row, score=0.98, reason="alias"))
            continue
        if family is not None and nickname_family(row.name) == family:
            candidates.append(EntityCandidate(entity=row, score=0.9, reason="nickname"))
            continue
        overlap = _jaccard(tokens, _token_set(row.name))
        if overlap >= 0.5:
            candidates.append(
                EntityCandidate(
                    entity=row,
                    score=round(0.6 + 0.25 * overlap, 3),
                    reason="token",
                )
            )
            continue
        if query_embedding is not None and row_name in name_embeddings:
            similarity = cosine_similarity(query_embedding, name_embeddings[row_name])
            if similarity >= 0.65:
                candidates.append(
                    EntityCandidate(
                        entity=row,
                        score=round(min(0.95, similarity), 3),
                        reason="embedding",
                    )
                )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


async def resolve_entity_ref(
    session: AsyncSession,
    ref: EntityRef,
    *,
    embeddings=None,
) -> Entity:
    """Map a ref to its canonical entity, using exact/alias matches only."""
    canonical = canonical_key_for(ref.name, ref.entity_type)
    result = await session.execute(select(Entity).where(Entity.canonical_key == canonical))
    entity = result.scalar_one_or_none()
    if entity is not None:
        return entity
    # Alias-based resolution is deterministic: aliases only ever come from
    # confirmed merges, which are themselves replayed from the event log.
    rows = list(
        (
            await session.execute(
                select(Entity).where(Entity.entity_type == ref.entity_type)
            )
        ).scalars().all()
    )
    name = normalize_entity_name(ref.name)
    for row in rows:
        if name in {normalize_entity_name(alias) for alias in (row.aliases or [])}:
            return row
    return await get_or_create_entity(session, ref.name, ref.entity_type)


async def get_or_create_entity(session: AsyncSession, name: str, entity_type: str) -> Entity:
    canonical = canonical_key_for(name, entity_type)
    result = await session.execute(select(Entity).where(Entity.canonical_key == canonical))
    entity = result.scalar_one_or_none()
    if entity is not None:
        return entity
    entity = Entity(name=name, entity_type=entity_type, canonical_key=canonical)
    session.add(entity)
    await session.flush()
    return entity


async def link_entities(
    session: AsyncSession,
    memory_id,
    refs: list[EntityRef],
    *,
    embeddings=None,
) -> list:
    """Link a memory to canonical entities, deduplicating per entity."""
    linked: list[dict] = []
    seen: set[UUID] = set()
    for ref in refs:
        entity = await resolve_entity_ref(session, ref, embeddings=embeddings)
        if entity.id in seen:
            continue
        seen.add(entity.id)
        session.add(
            MemoryEntity(
                memory_id=memory_id,
                entity_id=entity.id,
                role=ref.role,
                weight=ref.weight,
            )
        )
        linked.append(
            {
                "id": str(entity.id),
                "name": entity.name,
                "entity_type": entity.entity_type,
                "role": ref.role,
            }
        )
    return linked


# --------------------------------------------------------------------------- #
# Human-confirmed merge (preserves both histories via the event log)
# --------------------------------------------------------------------------- #


async def merge_entities(
    session: AsyncSession,
    *,
    target: Entity,
    absorbed: Entity,
    event,
    reason: str,
) -> dict:
    """Merge ``absorbed`` into ``target``; both histories stay provenance-able."""
    if target.id == absorbed.id:
        raise ValueError("Cannot merge an entity into itself")
    if (absorbed.summary or "").startswith("Merged into "):
        return {"target_id": str(target.id), "absorbed_id": str(absorbed.id), "skipped": True}

    # Re-point memory links (dedupe against existing target links).
    absorbed_links = list(
        (
            await session.execute(
                select(MemoryEntity).where(MemoryEntity.entity_id == absorbed.id)
            )
        ).scalars().all()
    )
    target_link_ids = {
        row.memory_id
        for row in (
            await session.execute(
                select(MemoryEntity).where(MemoryEntity.entity_id == target.id)
            )
        ).scalars().all()
    }
    moved = 0
    for link in absorbed_links:
        if link.memory_id in target_link_ids:
            await session.execute(
                delete(MemoryEntity).where(
                    MemoryEntity.memory_id == link.memory_id,
                    MemoryEntity.entity_id == absorbed.id,
                )
            )
        else:
            link.entity_id = target.id
            target_link_ids.add(link.memory_id)
            moved += 1

    # Re-point relationships.
    from app.models import EntityRelationship

    rel_rows = list(
        (
            await session.execute(
                select(EntityRelationship).where(
                    (EntityRelationship.from_entity_id == absorbed.id)
                    | (EntityRelationship.to_entity_id == absorbed.id)
                )
            )
        ).scalars().all()
    )
    for rel in rel_rows:
        if rel.from_entity_id == absorbed.id:
            rel.from_entity_id = target.id
        if rel.to_entity_id == absorbed.id:
            rel.to_entity_id = target.id

    # Re-point recognition logs.
    from app.models import RecognitionLog

    await session.execute(
        update(RecognitionLog)
        .where(RecognitionLog.entity_id == absorbed.id)
        .values(entity_id=target.id)
    )

    # Absorbed identity becomes an alias of the canonical entity.
    alias_names = {normalize_entity_name(alias) for alias in (target.aliases or [])}
    alias_names.add(normalize_entity_name(absorbed.name))
    alias_names.update(normalize_entity_name(a) for a in (absorbed.aliases or []))
    alias_names.discard(normalize_entity_name(target.name))
    target.aliases = sorted(alias_names)
    occurred_at = event.occurred_at
    if occurred_at is None:
        occurred_at = datetime.now(UTC)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    occurred_iso = occurred_at.astimezone(UTC).replace(tzinfo=None).isoformat()
    absorbed.summary = (
        f"Merged into {target.name} on {occurred_iso}; "
        f"reason: {reason}; absorbed_canonical_key={absorbed.canonical_key}"
    )
    return {
        "target_id": str(target.id),
        "absorbed_id": str(absorbed.id),
        "moved_memory_links": moved,
        "repointed_relationships": len(rel_rows),
        "skipped": False,
    }


async def apply_entity_merge_event(session: AsyncSession, event) -> dict:
    """Replay a stored ``entity.merge`` event against the rebuilt layer."""
    content = event.content or {}
    target_key = content.get("target_canonical_key")
    absorbed_key = content.get("absorbed_canonical_key")
    if not target_key or not absorbed_key:
        return {"skipped": True, "reason": "missing keys"}
    target = (
        await session.execute(select(Entity).where(Entity.canonical_key == target_key))
    ).scalar_one_or_none()
    absorbed = (
        await session.execute(select(Entity).where(Entity.canonical_key == absorbed_key))
    ).scalar_one_or_none()
    if target is None or absorbed is None:
        return {"skipped": True, "reason": "entity missing"}
    return await merge_entities(
        session,
        target=target,
        absorbed=absorbed,
        event=event,
        reason=str(content.get("reason") or "entity merge"),
    )


# --------------------------------------------------------------------------- #
# Duplicate tracking
# --------------------------------------------------------------------------- #


async def duplicate_entity_stats(session: AsyncSession) -> dict:
    """Duplicate-entity rate over entities actually linked to memories.

    Near-duplicates are connected components of linked entities that share an
    exact normalized name, a nickname family, or a token-overlap of >= 0.8.
    """
    rows = list((await session.execute(select(Entity))).scalars().all())
    linked_ids = set(
        (await session.execute(select(MemoryEntity.entity_id))).scalars().all()
    )
    linked = [row for row in rows if row.id in linked_ids]
    by_type: dict[str, dict] = {}
    total_linked = len(linked)
    duplicate_count = 0
    for entity_type in sorted({row.entity_type for row in linked}):
        group = [row for row in linked if row.entity_type == entity_type]
        parent = {str(row.id): str(row.id) for row in group}

        families = {str(row.id): nickname_family(row.name) for row in group}
        tokens = {str(row.id): _token_set(row.name) for row in group}
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                same_name = normalize_entity_name(a.name) == normalize_entity_name(b.name)
                same_family = (
                    families[str(a.id)] is not None
                    and families[str(a.id)] == families[str(b.id)]
                )
                overlap = _jaccard(tokens[str(a.id)], tokens[str(b.id)])
                if same_name or same_family or overlap >= 0.8:
                    root_a, root_b = str(a.id), str(b.id)
                    while parent[root_a] != root_a:
                        root_a = parent[root_a]
                    while parent[root_b] != root_b:
                        root_b = parent[root_b]
                    if root_a != root_b:
                        parent[root_b] = root_a

        sizes: dict[str, int] = {}
        for row in group:
            root = str(row.id)
            while parent[root] != root:
                root = parent[root]
            sizes[root] = sizes.get(root, 0) + 1
        duplicates = sum(size - 1 for size in sizes.values() if size > 1)
        duplicate_count += duplicates
        total = len(group)
        by_type[entity_type] = {
            "unique": len(sizes),
            "total": total,
            "duplicates": duplicates,
            "duplicate_rate": round(duplicates / total, 4) if total else 0.0,
        }
    return {
        "total_entities": len(rows),
        "linked_entities": total_linked,
        "duplicate_entities": duplicate_count,
        "duplicate_rate": round(duplicate_count / total_linked, 4) if total_linked else 0.0,
        "by_type": by_type,
    }
