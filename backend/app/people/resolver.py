"""Face recognition resolver for AGENT 7 ROSTER.

Recognition matches ONE aligned crop only against consented, enrolled
templates. A crop that matches nothing resolves to ``unknown`` and is never
persisted, because there is deliberately no path that attempts to identify a
non-enrolled person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Entity, RecognitionLog
from app.people.index import FaceIndex, FaceIndexEntry

if TYPE_CHECKING:
    from app.people.face_embed import FaceCrop, FaceEmbedder


@dataclass(frozen=True)
class FaceRecognitionResult:
    """Outcome of matching one crop against the enrolled index."""

    resolved: bool
    unknown: bool
    label: str | None
    entity_id: UUID | None
    confidence: float
    threshold: float
    provider: str
    degraded: bool
    candidates: list[dict]
    recognition_id: UUID | None = None
    algorithm: str | None = None


def _maybe_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


class FaceResolver:
    """Resolve a face crop against enrolled people, or honestly say unknown."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        master_key: str,
        embedder: FaceEmbedder | None = None,
    ) -> None:
        from app.people.enrollment import FaceEnrollmentService

        self.session = session
        self.master_key = master_key
        self.service = FaceEnrollmentService(
            session, master_key=master_key, embedder=embedder
        )
        self.embedder = self.service.embedder

    async def recognize(
        self,
        crop: FaceCrop,
        *,
        write_log: bool = True,
    ) -> FaceRecognitionResult:
        """Match one aligned crop against enrolled templates only."""
        payloads = await self.service.current_payloads()
        if not payloads:
            return FaceRecognitionResult(
                resolved=False,
                unknown=True,
                label=None,
                entity_id=None,
                confidence=0.0,
                threshold=settings.face_threshold,
                provider=self.embedder.name,
                degraded=self.embedder.degraded,
                candidates=[],
                algorithm=self.embedder.name,
            )

        index = FaceIndex()
        thresholds_by_entity: dict[str, float] = {}
        for entity, payload in payloads:
            entry_threshold = float(payload.get("threshold", settings.face_threshold))
            index.add(
                FaceIndexEntry(
                    entity_id=entity.id,
                    label=entity.name,
                    embedding=list(payload.get("embedding", [])),
                    threshold=entry_threshold,
                    algorithm=str(payload.get("algorithm", self.embedder.name)),
                    provider=str(payload.get("provider", self.embedder.name)),
                    degraded=bool(payload.get("degraded", self.embedder.degraded)),
                )
            )
            thresholds_by_entity[str(entity.id)] = entry_threshold

        result = await self.embedder.embed(crop)
        candidates = index.search(result.embedding)
        if not candidates:
            display = self._top_candidates(index.entries, result.embedding, limit=5)
            confidence = float(display[0]["similarity"]) if display else 0.0
            return FaceRecognitionResult(
                resolved=False,
                unknown=True,
                label=None,
                entity_id=None,
                confidence=round(confidence, 6),
                threshold=settings.face_threshold,
                provider=result.provider,
                degraded=result.degraded,
                candidates=display,
                algorithm=getattr(result, "model", None) or self.embedder.name,
            )

        top = candidates[0]
        entity_id = UUID(top["entity_id"])
        entry_threshold = thresholds_by_entity.get(top["entity_id"], settings.face_threshold)
        recognition_id: UUID | None = None
        if write_log:
            row = RecognitionLog(
                entity_id=entity_id,
                label=top["label"],
                confidence=float(top["similarity"]),
                source="model",
                live_event_id=_maybe_uuid(crop.live_event_id),
                attachment_id=_maybe_uuid(crop.attachment_id),
            )
            self.session.add(row)
            await self.session.flush()
            recognition_id = row.id

        return FaceRecognitionResult(
            resolved=True,
            unknown=False,
            label=top["label"],
            entity_id=entity_id,
            confidence=float(top["similarity"]),
            threshold=entry_threshold,
            provider=result.provider,
            degraded=result.degraded,
            candidates=candidates[:5],
            recognition_id=recognition_id,
            algorithm=getattr(result, "model", None) or self.embedder.name,
        )

    async def confirm(
        self,
        recognition_id: UUID,
        *,
        correct_label: str | None = None,
        correct_entity_id: UUID | None = None,
        reason: str | None = None,
        actor: str = "user",
    ) -> RecognitionLog:
        """Turn a model sighting into a human-confirmed training signal.

        The model's confidence score is intentionally preserved: a correction
        is recorded as a command-ledger event, never as a fabricated score.
        """
        from app.ev.edith import record_command
        from app.memory.entities import get_or_create_entity
        from app.people.errors import FaceError

        row = await self.session.get(RecognitionLog, recognition_id)
        if row is None:
            raise FaceError(
                "Recognition log not found",
                status=404,
                code="recognition_not_found",
            )

        entity: Entity | None = None
        if correct_label:
            entity = await get_or_create_entity(self.session, correct_label, "person")
            if correct_entity_id is None:
                correct_entity_id = entity.id
            row.label = entity.name
            row.entity_id = entity.id
        elif correct_entity_id is not None:
            entity = await self.session.get(Entity, correct_entity_id)
            if entity is None:
                raise FaceError(
                    "Entity not found",
                    status=404,
                    code="entity_not_found",
                )
            row.entity_id = entity.id
            row.label = entity.name

        if correct_entity_id is not None and correct_label:
            row.entity_id = correct_entity_id

        row.source = "user"
        await record_command(
            self.session,
            command_type="people.recognition.confirm",
            actor=actor,
            target_type="recognition",
            target_id=str(recognition_id),
            request={
                "correct_label": correct_label,
                "correct_entity_id": (
                    str(correct_entity_id) if correct_entity_id is not None else None
                ),
                "reason": reason,
            },
            result={"confirmed": True, "source": "user"},
            status="completed",
        )
        await self.session.flush()
        return row

    @staticmethod
    def _top_candidates(
        entries: list[FaceIndexEntry],
        embedding: list[float],
        *,
        limit: int,
    ) -> list[dict]:
        """Return the top ``limit`` similarities regardless of threshold.

        Used only for the ``candidates`` display on an unknown result; it never
        influences resolution.
        """
        from app.people.face_embed import cosine_similarity

        scored: list[tuple[float, dict]] = []
        for entry in entries:
            similarity = cosine_similarity(embedding, entry.embedding)
            scored.append(
                (
                    similarity,
                    {
                        "entity_id": str(entry.entity_id),
                        "label": entry.label,
                        "similarity": round(float(similarity), 6),
                        "enrollment_id": (
                            str(entry.enrollment_id) if entry.enrollment_id is not None else None
                        ),
                        "algorithm": entry.algorithm,
                        "provider": entry.provider,
                        "degraded": entry.degraded,
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in scored[:limit]]
