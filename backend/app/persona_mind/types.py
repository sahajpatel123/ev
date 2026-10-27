"""Public interface types. Frozen dataclasses, no I/O, no network.

This is the contract CLI 6 integrates against. Field names and the
``observe`` / ``snapshot`` signatures are frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TurnRecord:
    owner_id: str
    thread_id: str
    turn_id: str
    ts: datetime
    owner_msg: str
    evie_reply: str
    channel: str
    audience: str
    tone: dict  # from persona ToneDecision (roast, technique...)
    reaction_to_prev: str | None  # positive | neutral | negative
    receipts: tuple  # (tool_name, ok, error_class | None) — NEVER args/payloads
    now_line: str | None


@dataclass(frozen=True)
class MindSnapshot:
    text: str
    token_estimate: int
    version: str  # "mind.v1"
    state_age_s: float
