"""In-memory face template index used by the recognition resolver.

The index is built per request from the decrypted enrollment payloads held by
``FaceEnrollmentService``. It contains normalized embedding vectors only --
never raw images, detector metadata, or ciphertext -- and it is never used to
identify a person who is not enrolled.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class FaceIndexEntry:
    """One enrolled person's current template plus its operating threshold."""

    entity_id: UUID
    label: str
    embedding: list[float]
    threshold: float
    algorithm: str
    provider: str
    degraded: bool
    enrollment_id: UUID | None = None


class FaceIndex:
    """Searchable collection of enrolled face templates.

    Every entry enforces its own calibrated threshold: a candidate only
    qualifies when its cosine similarity is at least that entry's threshold.
    An optional global ``threshold`` argument tightens the filter further.
    """

    def __init__(self) -> None:
        self.entries: list[FaceIndexEntry] = []

    def add(self, entry: FaceIndexEntry) -> None:
        """Add one enrolled template to the index."""
        self.entries.append(entry)

    def search(
        self,
        embedding: list[float],
        threshold: float | None = None,
    ) -> list[dict]:
        """Return matching candidates sorted by descending cosine similarity.

        Each candidate dict contains ``entity_id``, ``label``, ``similarity``,
        ``enrollment_id``, ``algorithm``, ``provider``, and ``degraded``. An
        entry is only returned when its similarity clears both its own
        calibrated threshold and, when supplied, the global ``threshold``.
        """
        from app.people.face_embed import cosine_similarity

        scored: list[tuple[float, dict]] = []
        for entry in self.entries:
            similarity = cosine_similarity(embedding, entry.embedding)
            if similarity < entry.threshold:
                continue
            if threshold is not None and similarity < threshold:
                continue
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
        return [candidate for _, candidate in scored]
