"""EVIE MIND — her dynamic inner state. Deterministic fast loop, slow
reflection worker. Flag-gated by MIND_ENABLED (off until CLI 6 integration
review)."""

from __future__ import annotations

from app.persona_mind.fast import observe, snapshot
from app.persona_mind.settings import MIND_ENABLED, MIND_VERSION
from app.persona_mind.types import MindSnapshot, TurnRecord

__all__ = [
    "MIND_ENABLED",
    "MIND_VERSION",
    "MindSnapshot",
    "TurnRecord",
    "observe",
    "snapshot",
]
