"""Canonical owner turn record (G1.4).

Every final owner turn is exactly one record tied to the provider user item.
Reused from existing Event / LiveSession structures — no duplicate storage.
TurnGate consumes this, not an arbitrary tool string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class OwnerTurn:
    turn_id: str  # canonical turn id (Event.id or uuid)
    live_session_id: str
    provider_item_id: str | None
    owner_id: str  # actor
    device_id: str | None
    transcript: str  # final, owner speech only
    transcript_source: str | None  # provider | fallback_asr | text
    confidence: float | None  # null/unknown, not hardcoded 1.0
    committed_at: datetime | None
    transcription_completed_at: datetime | None
    sequence: int  # monotonic per session
    consumed: bool = False
    provider_response_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "live_session_id": self.live_session_id,
            "provider_item_id": self.provider_item_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "transcript": self.transcript,
            "transcript_source": self.transcript_source,
            "confidence": self.confidence,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "transcription_completed_at": self.transcription_completed_at.isoformat() if self.transcription_completed_at else None,
            "sequence": self.sequence,
        }


# In-memory registry for gate's exactly-once tracking (per process, bounded)
# For durability, the Event table is the source of truth; this is the gate's
# consumed set to prevent duplicate response.create for the same turn.
_CONSUMED_TURNS: set[str] = set()
_TURN_SEQUENCE: dict[str, int] = {}  # live_session_id -> seq

def next_sequence(live_session_id: str) -> int:
    seq = _TURN_SEQUENCE.get(live_session_id, 0) + 1
    _TURN_SEQUENCE[live_session_id] = seq
    return seq

def is_consumed(turn_id: str) -> bool:
    return turn_id in _CONSUMED_TURNS

def mark_consumed(turn_id: str):
    _CONSUMED_TURNS.add(turn_id)
    # Bounded: keep last 1000
    if len(_CONSUMED_TURNS) > 1000:
        # Evict oldest (arbitrary)
        oldest = next(iter(_CONSUMED_TURNS))
        _CONSUMED_TURNS.remove(oldest)

def create_owner_turn(
    *,
    live_session_id: str,
    provider_item_id: str | None,
    owner_id: str,
    device_id: str | None,
    transcript: str,
    transcript_source: str | None,
    confidence: float | None = None,  # null if not calibrated
    committed_at: datetime | None = None,
    transcription_completed_at: datetime | None = None,
    turn_id: str | None = None,
) -> OwnerTurn:
    # Confidence is NOT hardcoded 1.0; if provider does not report calibrated
    # confidence, we store None (unknown) instead of fake 1.0.
    return OwnerTurn(
        # G2 idempotency: a client-supplied stable request id (PWA text path)
        # becomes the canonical turn id so retries dedupe at the gate.
        turn_id=turn_id or uuid4().hex,
        live_session_id=live_session_id,
        provider_item_id=provider_item_id,
        owner_id=owner_id,
        device_id=device_id,
        transcript=transcript,
        transcript_source=transcript_source,
        confidence=confidence,
        committed_at=committed_at,
        transcription_completed_at=transcription_completed_at,
        sequence=next_sequence(live_session_id),
    )
