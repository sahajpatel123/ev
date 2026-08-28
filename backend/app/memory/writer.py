from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import MemoryCandidate
from app.memory.entities import link_entities
from app.memory.importance import score_importance
from app.memory.observe import log_memory
from app.models import Conflict, Memory, MemoryEvent
from app.utils.text import canonical_json, fingerprint, normalize_text, utcnow


@dataclass
class WriteResult:
    memory_id: str
    memory_type: str
    action: str
    text: str


NEGATIVE = {"hate", "dislike", "avoid", "don't like", "do not like", "no longer like", "not"}
POSITIVE = {"like", "love", "enjoy", "prefer", "want", "look forward to"}


class MemoryWriter:
    """Deduplicates, versions, conflict-checks, embeds, and stores memories."""

    def __init__(self, session: AsyncSession, embeddings=None) -> None:
        self.session = session
        self.embeddings = embeddings

    async def write_all(self, event, candidates: list[MemoryCandidate]) -> list[WriteResult]:
        from app.memory.retrieval import bump_memory_epoch

        results: list[WriteResult] = []
        wrote = False
        for candidate in candidates:
            candidate.importance = score_importance(event, candidate)
            result = await self._write_one(event, candidate)
            if result:
                results.append(result)
                wrote = True
        if wrote:
            # F1.1: any memory mutation invalidates authority-sensitive caches.
            bump_memory_epoch()
        return results

    async def _write_one(self, event, candidate: MemoryCandidate) -> WriteResult | None:
        fp = fingerprint({"memory_type": candidate.memory_type, "payload": canonical_json(candidate.payload)})
        existing = await self._find_same_fingerprint(candidate.memory_type, fp)
        if existing is not None:
            # Duplicate: link provenance, keep strongest confidence, no new row.
            await self._add_provenance(existing, event)
            existing.confidence = max(existing.confidence, candidate.confidence)
            existing.updated_time = utcnow()
            return WriteResult(str(existing.id), candidate.memory_type, "updated", candidate.text)

        key = self._semantic_key(candidate)
        prev = None
        if key is not None:
            prev = await self._find_current_by_key(candidate.memory_type, key)
        if prev is None and candidate.memory_type == "open_loop":
            from app.memory.loops import find_similar_loop

            prev = await find_similar_loop(self.session, candidate)
        if prev is None and candidate.memory_type == "preference" and (candidate.payload or {}).get(
            "replaces_latest"
        ):
            prev = await self._latest_current("preference")
        if prev is not None and candidate.memory_type == "open_loop":
            from app.memory.loops import inherit_loop_identity

            inherit_loop_identity(prev, candidate)
        if prev is not None and canonical_json(prev.payload) != canonical_json(candidate.payload):
            memory = await self._create_memory(event, candidate, prev=prev, reason="Value changed")
            prev.is_current = False
            prev.superseded_by_id = memory.id
            prev.valid_until = event.occurred_at or utcnow()
            if candidate.memory_type == "open_loop":
                from app.memory.loops import log_loop_transition

                log_loop_transition(str((candidate.payload or {}).get("status") or "open"), memory.id)
            elif candidate.memory_type == "decision":
                log_memory("memory.decision_superseded", extra={"memory_id": str(prev.id)})
            log_memory(
                "memory.superseded",
                extra={
                    "memory_id": str(prev.id),
                    "successor": str(memory.id),
                    "memory_type": candidate.memory_type,
                },
            )
            self.session.add(
                Conflict(
                    memory_id_a=prev.id,
                    memory_id_b=memory.id,
                    reason=f"Value changed from {prev.text} to {candidate.text}",
                    status="resolved",
                    resolution=f"Superseded by {memory.id}",
                    resolution_memory_id=memory.id,
                    resolved_time=utcnow(),
                )
            )
            return WriteResult(str(memory.id), candidate.memory_type, "updated", candidate.text)
        if prev is not None:
            await self._add_provenance(prev, event)
            return WriteResult(str(prev.id), candidate.memory_type, "updated", candidate.text)

        memory = await self._create_memory(event, candidate)
        action = "created"
        if candidate.memory_type == "open_loop":
            from app.memory.loops import log_loop_transition

            log_loop_transition(str((candidate.payload or {}).get("status") or "open"), memory.id)
        elif candidate.memory_type == "decision":
            log_memory("memory.decision_added", extra={"memory_id": str(memory.id)})
        await self._detect_conflicts(memory, candidate)
        return WriteResult(str(memory.id), candidate.memory_type, action, candidate.text)

    async def _create_memory(
        self,
        event,
        candidate: MemoryCandidate,
        *,
        prev: Memory | None = None,
        reason: str | None = None,
    ) -> Memory:
        now = utcnow()
        embedding = None
        if self.embeddings is not None:
            try:
                embedding = (await self.embeddings.embed([candidate.text]))[0]
            except Exception:
                embedding = None
        memory = Memory(
            memory_type=candidate.memory_type,
            text=candidate.text,
            payload=candidate.payload,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source_type=candidate.source_type,
            privacy_level=candidate.privacy_level,
            event_time=candidate.event_time or event.occurred_at,
            valid_from=candidate.valid_from or candidate.event_time or now,
            valid_until=candidate.valid_until,
            version_group=prev.version_group if prev else uuid4(),
            version=(prev.version + 1) if prev else 1,
            supersedes_id=prev.id if prev else None,
            reason_for_change=reason,
            fingerprint=fingerprint(
                {"memory_type": candidate.memory_type, "payload": canonical_json(candidate.payload)}
            ),
            embedding=embedding,
        )
        self.session.add(memory)
        await self.session.flush()
        self.session.add(MemoryEvent(memory_id=memory.id, event_id=event.id))
        await link_entities(
            self.session,
            memory.id,
            candidate.entities,
            embeddings=self.embeddings,
        )
        return memory

    async def _add_provenance(self, memory: Memory, event) -> None:
        exists = await self.session.execute(
            select(MemoryEvent).where(
                MemoryEvent.memory_id == memory.id,
                MemoryEvent.event_id == event.id,
            )
        )
        if exists.scalar_one_or_none() is None:
            self.session.add(MemoryEvent(memory_id=memory.id, event_id=event.id))

    async def _find_same_fingerprint(self, memory_type: str, fp: str) -> Memory | None:
        result = await self.session.execute(
            select(Memory).where(
                Memory.memory_type == memory_type,
                Memory.fingerprint == fp,
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
        )
        return result.scalars().first()

    async def _latest_current(self, memory_type: str) -> Memory | None:
        result = await self.session.execute(
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.valid_from.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def _find_current_by_key(self, memory_type: str, key: tuple) -> Memory | None:
        result = await self.session.execute(
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.valid_from.desc())
        )
        for memory in result.scalars().all():
            if self._semantic_key_for_payload(memory.memory_type, memory.payload) == key:
                return memory
        return None

    def _semantic_key(self, candidate: MemoryCandidate) -> tuple | None:
        return self._semantic_key_for_payload(candidate.memory_type, candidate.payload)

    def _semantic_key_for_payload(self, memory_type: str, payload: dict) -> tuple | None:
        if memory_type == "decision":
            topic = normalize_text((payload.get("topic") or payload.get("decision") or "")[:80])
            return ("decision", topic) if topic else None
        if memory_type == "preference":
            subject = normalize_text((payload.get("subject") or "")[:80])
            return ("preference", subject) if subject else None
        if memory_type == "goal":
            goal = normalize_text((payload.get("topic") or payload.get("goal") or "")[:80])
            return ("goal", goal) if goal else None
        if memory_type == "fact":
            subject = normalize_text((payload.get("subject") or "")[:80])
            prop = normalize_text((payload.get("property") or "")[:80])
            return ("fact", subject, prop) if subject and prop else None
        if memory_type == "open_loop":
            scope = normalize_text(str(payload.get("scope") or "")[:80])
            key = normalize_text(str(payload.get("loop_key") or payload.get("title") or "")[:80])
            return ("open_loop", scope, key) if key else None
        if memory_type == "rejection":
            topic = normalize_text((payload.get("topic") or payload.get("value") or "")[:80])
            return ("rejection", topic) if topic else None
        if memory_type == "hypothesis":
            topic = normalize_text((payload.get("topic") or payload.get("value") or "")[:80])
            return ("hypothesis", topic) if topic else None
        if memory_type == "summary" and payload.get("kind") == "episode":
            thread = str(payload.get("thread_id") or "")
            start = str(payload.get("window_start") or "")
            return ("episode", thread, start) if thread and start else None
        return None

    async def _detect_conflicts(self, memory: Memory, candidate) -> None:
        if candidate.memory_type == "observation":
            await self._detect_observation_conflict(memory)
        elif candidate.memory_type == "fact":
            await self._detect_fact_conflicts(memory)
        elif candidate.memory_type == "preference":
            await self._detect_preference_conflicts(memory)
        elif candidate.memory_type == "decision":
            await self._detect_decision_conflicts(memory)

    async def _add_open_conflict(self, memory: Memory, other: Memory, reason: str) -> None:
        existing = await self.session.execute(
            select(Conflict).where(
                ((Conflict.memory_id_a == memory.id) & (Conflict.memory_id_b == other.id))
                | ((Conflict.memory_id_a == other.id) & (Conflict.memory_id_b == memory.id)),
                Conflict.status == "open",
            )
        )
        if existing.scalar_one_or_none() is None:
            self.session.add(
                Conflict(
                    memory_id_a=memory.id,
                    memory_id_b=other.id,
                    reason=reason,
                    status="open",
                )
            )

    async def _detect_observation_conflict(self, memory: Memory) -> None:
        topic = normalize_text((memory.payload or {}).get("topic") or "")
        if not topic:
            return
        topic_tokens = set(topic.split())
        result = await self.session.execute(
            select(Memory).where(
                Memory.memory_type == "observation",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.id != memory.id,
            )
        )
        for other in result.scalars().all():
            other_topic = normalize_text((other.payload or {}).get("topic") or "")
            if not other_topic:
                continue
            other_tokens = set(other_topic.split())
            overlap = len(topic_tokens & other_tokens) / max(
                1, len(topic_tokens | other_tokens)
            )
            if other_topic != topic and overlap < 0.5:
                continue
            sent_a = self._sentiment(memory.text)
            sent_b = self._sentiment(other.text)
            if sent_a is not None and sent_b is not None and sent_a != sent_b:
                await self._add_open_conflict(
                    memory,
                    other,
                    f"Conflicting observations about '{topic}'",
                )

    async def _detect_fact_conflicts(self, memory: Memory) -> None:
        subject = normalize_text((memory.payload or {}).get("subject") or "")
        prop = normalize_text((memory.payload or {}).get("property") or "")
        value = normalize_text(str((memory.payload or {}).get("value") or ""))
        if not subject or not prop or not value:
            return
        result = await self.session.execute(
            select(Memory).where(
                Memory.memory_type == "fact",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.id != memory.id,
            )
        )
        for other in result.scalars().all():
            other_subject = normalize_text((other.payload or {}).get("subject") or "")
            other_prop = normalize_text((other.payload or {}).get("property") or "")
            other_value = normalize_text(str((other.payload or {}).get("value") or ""))
            if (
                other_subject == subject
                and other_prop == prop
                and other_value != value
                and other_value
            ):
                await self._add_open_conflict(
                    memory,
                    other,
                    f"Conflicting facts: {subject} {prop} is both '{other_value}' and '{value}'",
                )

    async def _detect_preference_conflicts(self, memory: Memory) -> None:
        subject = normalize_text((memory.payload or {}).get("subject") or "")
        over = normalize_text((memory.payload or {}).get("over") or "")
        polarity = self._preference_polarity(memory.payload)
        if not subject or polarity is None:
            return
        result = await self.session.execute(
            select(Memory).where(
                Memory.memory_type == "preference",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.id != memory.id,
            )
        )
        for other in result.scalars().all():
            other_subject = normalize_text((other.payload or {}).get("subject") or "")
            other_over = normalize_text((other.payload or {}).get("over") or "")
            other_polarity = self._preference_polarity(other.payload)
            same_subject = other_subject == subject
            reversed_pair = bool(over and other_subject == over and other_over == subject)
            if other_polarity is None:
                continue
            if same_subject and other_polarity != polarity:
                await self._add_open_conflict(
                    memory,
                    other,
                    f"Conflicting preferences about '{subject}'",
                )
            elif reversed_pair:
                await self._add_open_conflict(
                    memory,
                    other,
                    f"Conflicting preferences: '{subject}' vs '{over}'",
                )

    @staticmethod
    def _preference_polarity(payload: dict) -> str | None:
        value = normalize_text(str(payload.get("value") or ""))
        if any(token in value for token in NEGATIVE):
            return "negative"
        if any(token in value for token in POSITIVE):
            return "positive"
        return None

    async def _detect_decision_conflicts(self, memory: Memory) -> None:
        topic = normalize_text((memory.payload or {}).get("topic") or "")
        decision = normalize_text(str((memory.payload or {}).get("decision") or ""))
        if not topic or not decision:
            return
        topic_tokens = set(topic.split())
        result = await self.session.execute(
            select(Memory).where(
                Memory.memory_type == "decision",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.id != memory.id,
            )
        )
        for other in result.scalars().all():
            other_topic = normalize_text((other.payload or {}).get("topic") or "")
            other_decision = normalize_text(str((other.payload or {}).get("decision") or ""))
            if not other_topic or not other_decision or other_decision == decision:
                continue
            other_tokens = set(other_topic.split())
            overlap = len(topic_tokens & other_tokens) / max(1, len(topic_tokens | other_tokens))
            if overlap >= 0.5:
                await self._add_open_conflict(
                    memory,
                    other,
                    f"Conflicting decisions about '{topic}'",
                )

    def _sentiment(self, text: str) -> str | None:
        lowered = text.lower()
        if any(token in lowered for token in NEGATIVE):
            return "negative"
        if any(token in lowered for token in POSITIVE):
            return "positive"
        return None


async def redact_memories_for_event(session: AsyncSession, event_id) -> int:
    """Redact derived memories whose provenance includes a tombstoned event."""
    result = await session.execute(select(MemoryEvent).where(MemoryEvent.event_id == event_id))
    memory_ids = [row.memory_id for row in result.scalars().all()]
    if not memory_ids:
        return 0
    await session.execute(
        update(Memory).where(Memory.id.in_(memory_ids)).values(redacted=True, updated_time=utcnow())
    )
    return len(memory_ids)
