from __future__ import annotations

from app.contracts import MemoryCandidate
from app.models import Event

BASE_IMPORTANCE = {
    "decision": 1.0,
    "goal": 0.9,
    "fact": 0.85,
    "preference": 0.8,
    "pattern": 0.6,
    "episodic": 0.6,
    "summary": 0.5,
    "observation": 0.4,
}


def score_importance(event: Event, candidate: MemoryCandidate) -> float:
    base = BASE_IMPORTANCE.get(candidate.memory_type, 0.5)
    if candidate.source_type == "inferred":
        base *= 0.6
    elif candidate.source_type == "derived":
        base *= 0.5
    if candidate.privacy_level in ("sensitive", "never_send_to_model"):
        base = min(1.0, base + 0.1)
    if candidate.memory_type == "episodic" and event.event_type in ("voice", "image", "file", "share"):
        base = min(1.0, base + 0.05)
    return round(min(1.0, max(0.05, base)), 3)

