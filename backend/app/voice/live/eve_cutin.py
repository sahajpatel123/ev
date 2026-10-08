"""Eve-initiated cut-in policy for the EV LIVE runtime.

A human conversation partner does not always wait for silence: she cuts in
for a ringing timer, an urgent warning, or a "oh — I know this one". Eve may
do the same, but only through this policy, and only when the owner actually
holds the floor. Cutting in is a deliberate, authorized floor take — never
an accident, never a backchannel (server listener speech stays cancelled per
the 2026-08-23 owner decision).

Triggers (explicit and inspectable):

- ``safety``: emergency lines. Bypass cooldown and quiet hours, all modes.
- ``timer_due``: owner-scheduled lines (timers, reminders the owner asked
  for). Attentive mode only; quiet hours bypassed because the owner
  scheduled them.
- ``urgent_alert``: urgent non-emergency lines. Attentive mode only.
- ``invited``: the owner explicitly said Eve may cut in ("stop me if you
  know"). One-shot: arming is consumed by the first authorized cut-in.
- ``known_answer``: the caller proved Eve already knows the answer (a repeat
  question with a previous answer id). The policy gates mode, emotion, and
  cooldown; the caller proves knowledge — the policy never claims it.

Hard gates shared by every trigger:

- the owner must hold the floor (speaking, Eve silent); otherwise there is
  nothing to cut into and the normal speech path owns the turn;
- ``passive`` mode denies everything except safety;
- ``sad`` / ``frustrated`` emotion denies everything except safety;
- a cooldown separates cut-ins (safety bypasses it).

This module is pure: no I/O, no models. It imports safely on a bare install.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Cut-in triggers.
TRIGGER_SAFETY = "safety"
TRIGGER_TIMER_DUE = "timer_due"
TRIGGER_URGENT_ALERT = "urgent_alert"
TRIGGER_INVITED = "invited"
TRIGGER_KNOWN_ANSWER = "known_answer"

TRIGGERS = frozenset(
    {
        TRIGGER_SAFETY,
        TRIGGER_TIMER_DUE,
        TRIGGER_URGENT_ALERT,
        TRIGGER_INVITED,
        TRIGGER_KNOWN_ANSWER,
    }
)

#: Minimum gap between two non-safety cut-ins.
CUT_IN_COOLDOWN_MS = 30_000

#: Emotions where any non-safety cut-in would feel tone-deaf.
_QUIET_EMOTIONS = frozenset({"sad", "frustrated"})

#: Explicit owner invitations to cut in ("stop me if you know the answer").
_INVITATION = re.compile(
    r"\b(?:stop me|cut me off|jump in|interrupt me|cut in|speak up)\s+if\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CutInDecision:
    """Whether Eve may take the floor right now, and why."""

    allowed: bool
    reason: str
    trigger: str = ""


def detect_invitation(text: str | None) -> bool:
    """True when the owner explicitly permits Eve to cut in."""

    return bool(text and _INVITATION.search(text))


@dataclass
class CutInPolicy:
    """Gates Eve-initiated floor takes. See the module docstring."""

    cooldown_ms: int = CUT_IN_COOLDOWN_MS

    def decide(
        self,
        state,
        trigger: str,
        *,
        now_ms: int,
        last_cut_in_ms: int | None,
        invited_armed: bool,
        grounded: bool = False,
    ) -> CutInDecision:
        if trigger not in TRIGGERS:
            return CutInDecision(False, "unknown_trigger", trigger)
        user_speaking = bool(getattr(state, "user_is_speaking", False))
        assistant_speaking = bool(getattr(state, "assistant_is_speaking", False))
        if not user_speaking or assistant_speaking:
            return CutInDecision(False, "no_owner_floor", trigger)
        mode = str(getattr(state, "listening_mode", "attentive"))
        if trigger == TRIGGER_SAFETY:
            return CutInDecision(True, "safety", trigger)
        if mode == "passive":
            return CutInDecision(False, "passive_mode", trigger)
        emotion = str(getattr(state, "emotional_context", "neutral"))
        if emotion in _QUIET_EMOTIONS:
            return CutInDecision(False, "emotional_space", trigger)
        if trigger == TRIGGER_INVITED:
            if mode != "attentive" and mode != "quiet":
                return CutInDecision(False, "mode_not_invited", trigger)
            if not invited_armed:
                return CutInDecision(False, "invitation_not_armed", trigger)
        elif trigger == TRIGGER_KNOWN_ANSWER:
            if mode != "attentive":
                return CutInDecision(False, "mode_not_attentive", trigger)
            if not grounded:
                return CutInDecision(False, "answer_not_grounded", trigger)
        elif mode != "attentive":
            return CutInDecision(False, "mode_not_attentive", trigger)
        if last_cut_in_ms is not None and now_ms - last_cut_in_ms < self.cooldown_ms:
            return CutInDecision(False, "cooldown", trigger)
        return CutInDecision(True, "authorized", trigger)
