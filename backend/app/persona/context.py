"""Persona context and block types. Frozen dataclasses, no I/O, no network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # Avoid a runtime import cycle with nightshift.py.
    from app.persona.nightshift import NightState


@dataclass(frozen=True)
class WorkState:
    """What the owner is working on right now (owner-supplied, never guessed)."""

    task: str | None = None
    last_result: str | None = None
    files_in_flight: tuple[str, ...] = ()


@dataclass(frozen=True)
class JokeMeta:
    """Which joke Evie told last (technique id + topic bucket)."""

    technique: str | None = None
    topic: str | None = None


@dataclass(frozen=True)
class PersonaContext:
    """Everything the persona layer may read for one turn.

    Only ``channel`` is required. Every other signal degrades toward calm
    when missing or unrecognized. ``receipts`` elements are read-only and
    may be ActionReceipt objects or plain dicts; persona code never mutates
    them and never invents delivery claims from them.
    """

    channel: str
    audience: str = "private"
    emotion: str | None = None
    urgency: float | None = None
    intent: str | None = None
    mode: str | None = None
    receipts: tuple[Any, ...] = ()
    confirmation_pending: bool = False
    sensitive_topic: str | None = None
    owner_msg: str | None = None
    local_time: datetime | None = None
    session_started: datetime | None = None
    work_state: WorkState | None = None
    first_commitment_tomorrow: str | None = None
    humor_slider: int | None = None
    serious_latch_turns_left: int = 0
    recent_joke_flags: tuple[bool, ...] = ()
    last_joke_meta: JokeMeta | None = None
    night_state: NightState | None = None
    thread_id: str | None = None
    turn_index: int = 0


@dataclass(frozen=True)
class ToneDecision:
    """ToneController output: the roast ceiling and voice for this turn."""

    roast: int
    humour_allowed: bool
    technique: str | None
    profanity: bool
    audience: str
    reasons: tuple[str, ...] = ()
    # Extra fields beyond the brief's core set (all defaulted):
    # - target="object": frustration humour aims at the object, never the owner.
    # - voice="owner": on_behalf drafting uses the owner's voice, not Evie's.
    # - shape: rhythm hint (short | full, one-liner for presence acks only).
    target: str | None = None
    voice: str = "evie"
    shape: str = "short"


@dataclass(frozen=True)
class PersonaBlock:
    """The single compiled persona block for one turn (THE entry-point output)."""

    text: str
    version: str
    tone: ToneDecision
    token_estimate: int
    dropped: tuple[str, ...] = field(default_factory=tuple)
