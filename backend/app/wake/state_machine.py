"""Explicit wake lifecycle ( §4 ) — one state machine, no stuck half-awake.

IDLE_EARS
↓ WAKE_CANDIDATE (Stage-1 hit)
↓ PRECISION_VERIFIED (Stage-2)
↓ FAST_OWNER_VERIFIED (wake-phrase speaker)
↓ LEASE_ACQUIRING (ConversationLease)
↓ SPECULATIVE_HANDOFF (Realtime establishing/feeding, but not committed)
↓ FULL_OWNER_CHECK + DIRECTED_SPEECH_CHECK
↓ ACCEPTED_CONVERSATION  or  SILENT_REJECT → IDLE_EARS

Failures always return safely to IDLE_EARS. No multiple competing wake sessions.
Speculative handoff may receive audio/transcribe/prepare reasoning, but must
NOT commit: no external actions, computer mutations, messages, calendar,
home, commitments, memory writes, or spoken final answer until final gate passes.
When final gate passes, release turn into Foundation V2 (existing TurnGate etc.).
If final gate fails, cancel silently, bounded diagnostics only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WakeState(StrEnum):
    IDLE_EARS = "IDLE_EARS"
    WAKE_CANDIDATE = "WAKE_CANDIDATE"
    PRECISION_VERIFIED = "PRECISION_VERIFIED"
    FAST_OWNER_VERIFIED = "FAST_OWNER_VERIFIED"
    LEASE_ACQUIRING = "LEASE_ACQUIRING"
    SPECULATIVE_HANDOFF = "SPECULATIVE_HANDOFF"
    FULL_OWNER_CHECK = "FULL_OWNER_CHECK"
    DIRECTED_SPEECH_CHECK = "DIRECTED_SPEECH_CHECK"
    ACCEPTED_CONVERSATION = "ACCEPTED_CONVERSATION"
    SILENT_REJECT = "SILENT_REJECT"


@dataclass
class WakeTransition:
    from_state: WakeState
    to_state: WakeState
    reason: str
    diagnostics: dict | None = None


# Permanent law (§5): speculative commit gate.
# These are the only gates that allow COMMIT.
COMMIT_GATES = frozenset(
    {
        WakeState.FULL_OWNER_CHECK,
        WakeState.DIRECTED_SPEECH_CHECK,
        WakeState.ACCEPTED_CONVERSATION,
    }
)

# Actions that are FORBIDDEN during SPECULATIVE_HANDOFF.
SPECULATIVE_FORBIDDEN = frozenset(
    {
        "external_action",
        "computer_mutation",
        "message_send",
        "calendar_mutation",
        "home_action",
        "commitment_create",
        "memory_write",
        "spoken_final_answer",
    }
)


class WakeStateMachine:
    """Deterministic, single-owner wake state machine."""

    def __init__(self) -> None:
        self.state = WakeState.IDLE_EARS
        self.history: list[WakeTransition] = []

    def reset_to_idle(self, reason: str = "return_to_idle") -> None:
        self._transition(WakeState.IDLE_EARS, reason)

    def candidate(self, *, confidence: float) -> None:
        self._transition(WakeState.WAKE_CANDIDATE, f"stage1_confidence={confidence:.2f}")

    def precision_verified(self, *, verifier_confidence: float) -> None:
        self._transition(WakeState.PRECISION_VERIFIED, f"stage2_confidence={verifier_confidence:.2f}")

    def fast_owner_verified(self, *, speaker_confidence: float) -> None:
        self._transition(WakeState.FAST_OWNER_VERIFIED, f"fast_speaker={speaker_confidence:.2f}")

    def lease_acquiring(self, *, candidate_device: str) -> None:
        self._transition(WakeState.LEASE_ACQUIRING, f"lease_candidate={candidate_device[:8]}")

    def speculative_handoff(self) -> None:
        self._transition(WakeState.SPECULATIVE_HANDOFF, "realtime_feeding_speculative")

    def full_owner_check(self, *, passed: bool, confidence: float) -> None:
        self._transition(WakeState.FULL_OWNER_CHECK, f"full_owner_passed={passed} conf={confidence:.2f}")
        if not passed:
            self.silent_reject("full_owner_failed")

    def directed_check(self, *, passed: bool, reason: str) -> None:
        self._transition(WakeState.DIRECTED_SPEECH_CHECK, f"directed_passed={passed} reason={reason}")
        if not passed:
            self.silent_reject(f"directed_failed:{reason}")

    def accept(self) -> None:
        self._transition(WakeState.ACCEPTED_CONVERSATION, "final_gate_passed_speculative_released")

    def silent_reject(self, reason: str) -> None:
        self._transition(WakeState.SILENT_REJECT, reason)
        self.reset_to_idle(f"silent_reject:{reason}")

    def is_speculative(self) -> bool:
        return self.state == WakeState.SPECULATIVE_HANDOFF

    def may_commit(self) -> bool:
        """True only after final gates have passed and turn is released to Foundation."""
        return self.state == WakeState.ACCEPTED_CONVERSATION

    def _transition(self, to: WakeState, reason: str, diagnostics: dict | None = None) -> None:
        self.history.append(WakeTransition(self.state, to, reason, diagnostics))
        self.state = to
