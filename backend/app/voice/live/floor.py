"""Conversational floor ownership for the EV LIVE runtime.

Who holds the floor — the owner or Eve — is the single fact that makes
mutual interruption legible. Turn-taking decides *when* to respond; the floor
tracker records *who* is entitled to speak right now so that:

- an owner cut-in while Eve holds the floor is a contested floor that a
  confirmed interruption resolves (owner takes it),
- an Eve cut-in while the owner holds the floor is a deliberate, policy-gated
  floor take (never an accident),
- clients can render gestures (listening / yielding / thinking) from one
  authoritative value instead of guessing from audio levels.

The tracker is pure data: callers push speech-boundary notes and read the
current floor. It never talks to engines, and every transition carries a
reason so history stays honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Floor values (also used as protocol enum values on ``floor`` events).
FLOOR_IDLE = "idle"
FLOOR_OWNER_HOLDS = "owner_holds"
FLOOR_EVE_HOLDS = "eve_holds"
FLOOR_CONTESTED = "contested"
FLOOR_YIELDING = "yielding"


@dataclass
class FloorTransition:
    """One recorded change of floor ownership."""

    previous: str
    current: str
    reason: str
    at_ms: int


@dataclass
class FloorTracker:
    """Who holds the conversational floor right now."""

    floor: str = FLOOR_IDLE
    updated_at_ms: int = 0
    transitions: list[FloorTransition] = field(default_factory=list)
    history_limit: int = 16

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def owner_holds(self) -> bool:
        return self.floor == FLOOR_OWNER_HOLDS

    def eve_holds(self) -> bool:
        return self.floor == FLOOR_EVE_HOLDS

    def is_contested(self) -> bool:
        return self.floor == FLOOR_CONTESTED

    def snapshot(self) -> dict:
        return {"floor": self.floor, "updated_at_ms": self.updated_at_ms}

    def recent_transitions(self, limit: int = 8) -> list[FloorTransition]:
        return self.transitions[-limit:]

    # ------------------------------------------------------------------ #
    # Notes (the only way the floor changes)
    # ------------------------------------------------------------------ #

    def _move(self, current: str, reason: str, at_ms: int) -> FloorTransition | None:
        if current == self.floor:
            return None
        transition = FloorTransition(
            previous=self.floor, current=current, reason=reason, at_ms=at_ms
        )
        self.floor = current
        self.updated_at_ms = at_ms
        self.transitions.append(transition)
        while len(self.transitions) > self.history_limit:
            self.transitions.pop(0)
        return transition

    def note_owner_speech_start(self, *, now_ms: int) -> FloorTransition | None:
        """Owner audio began. Contests Eve's floor; otherwise owner holds it."""
        if self.floor == FLOOR_EVE_HOLDS:
            return self._move(FLOOR_CONTESTED, "owner_speech_while_eve_holds", now_ms)
        if self.floor in {FLOOR_IDLE, FLOOR_YIELDING}:
            return self._move(FLOOR_OWNER_HOLDS, "owner_speech_start", now_ms)
        return None

    def note_owner_speech_end(self, *, now_ms: int) -> FloorTransition | None:
        if self.floor == FLOOR_CONTESTED:
            # Unconfirmed overlap ended: Eve keeps the floor she never lost.
            return self._move(FLOOR_EVE_HOLDS, "overlap_ended_unconfirmed", now_ms)
        return None

    def note_eve_speech_start(self, *, now_ms: int) -> FloorTransition | None:
        if self.floor in {FLOOR_IDLE, FLOOR_OWNER_HOLDS, FLOOR_YIELDING}:
            return self._move(FLOOR_EVE_HOLDS, "eve_speech_start", now_ms)
        return None

    def note_eve_speech_end(self, *, now_ms: int) -> FloorTransition | None:
        if self.floor in {FLOOR_EVE_HOLDS, FLOOR_CONTESTED, FLOOR_YIELDING}:
            return self._move(FLOOR_IDLE, "eve_speech_end", now_ms)
        return None

    def note_owner_cut_in(self, *, now_ms: int) -> FloorTransition | None:
        """A *confirmed* owner interruption: the owner takes the floor."""
        self._move(FLOOR_YIELDING, "owner_cut_in_yield", now_ms)
        return self._move(FLOOR_OWNER_HOLDS, "owner_cut_in_confirmed", now_ms)

    def note_eve_cut_in(self, *, now_ms: int) -> FloorTransition | None:
        """A *policy-authorized* Eve interruption: Eve takes the floor."""
        self._move(FLOOR_YIELDING, "eve_cut_in_yield", now_ms)
        return self._move(FLOOR_EVE_HOLDS, "eve_cut_in_authorized", now_ms)

    def note_turn_committed(self, *, now_ms: int) -> FloorTransition | None:
        """Owner turn ended and a reply is starting: Eve takes the floor."""
        if self.floor == FLOOR_OWNER_HOLDS:
            return self._move(FLOOR_EVE_HOLDS, "turn_committed", now_ms)
        return None

    def note_yield_back_to_owner(self, *, now_ms: int) -> FloorTransition | None:
        """Eve finished a cut-in and returns the floor ("sorry — go on").

        Idle is included: on the pipeline path the cue already closed Eve's
        speech bracket before the yield-back runs, and the owner — still
        mid-turn — holds the floor again either way.
        """
        if self.floor in {FLOOR_EVE_HOLDS, FLOOR_CONTESTED, FLOOR_YIELDING, FLOOR_IDLE}:
            return self._move(FLOOR_OWNER_HOLDS, "eve_yield_back", now_ms)
        return None

    def reset(self, *, now_ms: int = 0) -> None:
        self.floor = FLOOR_IDLE
        self.updated_at_ms = now_ms
        self.transitions.clear()
