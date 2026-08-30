"""Continuous conversation state for the EV LIVE runtime.

This is the "conversation operating system" snapshot: instead of discrete
request/response turns, the runtime keeps one mutable state object that
tracks who is doing what, what the current intent is, whether the user
interrupted, how long the silence has lasted, what the assistant is
currently generating, and so on.

The state is pure data — it never talks to engines. ``turn_taking``,
``backchannel`` and ``behavior`` read and mutate it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

#: Phase labels (also used as protocol enum values).
PHASE_IDLE = "idle"
PHASE_ARMED = "armed"
PHASE_LISTENING = "listening"
PHASE_USER_SPEAKING = "user_speaking"
PHASE_USER_PAUSING = "user_pausing"
PHASE_THINKING = "thinking"
PHASE_SPEAKING = "speaking"
PHASE_SPEAKING_AND_LISTENING = "speaking_and_listening"
PHASE_INTERRUPTED = "interrupted"
PHASE_WAITING = "waiting"

#: Listening modes.
LISTEN_ATTENTIVE = "attentive"  # normal conversation: react to everything
LISTEN_QUIET = "quiet"  # stay quiet, wait for the user to finish before replying
LISTEN_PASSIVE = "passive"  # ambient: only wake-level relevance triggers a reply

#: Speaking modes.
SPEAK_NONE = "none"
SPEAK_BACKCHANNEL = "backchannel"
SPEAK_ACK = "acknowledging"
SPEAK_ANSWER = "answering"
SPEAK_FILLER = "filler"

#: Interruption states.
INTERRUPT_NONE = "none"
INTERRUPT_PENDING = "pending"  # user speech started while assistant speaking
INTERRUPT_BARGED_IN = "barged_in"  # assistant playback has been stopped

#: Response-generation states.
GEN_IDLE = "idle"
GEN_FOREGROUND = "foreground"
GEN_BACKGROUND = "background_delegated"
GEN_STREAMING = "streaming"
GEN_DONE = "done"


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class LiveConversationState:
    """The single continuously-updated state of one live conversation."""

    # --- who is doing what ------------------------------------------------
    user_is_speaking: bool = False
    assistant_is_speaking: bool = False

    # --- where we are in the interaction ----------------------------------
    phase: str = PHASE_LISTENING
    listening_mode: str = LISTEN_ATTENTIVE
    speaking_mode: str = SPEAK_NONE
    interruption_state: str = INTERRUPT_NONE
    response_generation: str = GEN_IDLE

    # --- what the conversation is about -----------------------------------
    user_intent: str | None = None
    emotional_context: str = "neutral"  # neutral | frustrated | excited | uncertain | urgent | calm | sad
    current_topic: str | None = None
    previous_turn: str | None = None
    pending_question: str | None = None
    tool_state: str = "none"  # none | running | waiting

    # --- timing ------------------------------------------------------------
    # monotonic milliseconds (see ``_now_ms``). All engine wiring uses the
    # same clock source so pause/thinking decisions stay deterministic.
    last_user_speech_start_ms: int | None = None
    last_user_speech_end_ms: int | None = None
    last_assistant_speech_start_ms: int | None = None
    last_assistant_speech_end_ms: int | None = None
    silence_since_ms: int | None = None  # when the user last produced audio
    last_backchannel_ms: int | None = None
    backchannel_count: int = 0

    # Perceived-latency budget for this turn (time-to-first-audio target).
    latency_budget_ms: int = 800

    # --- rolling memory ------------------------------------------------------
    # Ring of the last ``history_limit`` textual events (partials, transcripts,
    # replies, backchannels) so policies can reason about what just happened
    # without a database.
    history_limit: int = 24
    history: deque[dict] = field(default_factory=deque)

    # A monotonic sequence number, bumped on every accepted audio frame batch,
    # used by the turn-taker to reason about recency without wall-clock drift.
    frame_seq: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle helpers
    # ------------------------------------------------------------------ #

    def note_user_speech_start(self, *, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()
        self.user_is_speaking = True
        self.last_user_speech_start_ms = now
        if self.assistant_is_speaking:
            self.interruption_state = INTERRUPT_PENDING
            self.phase = PHASE_SPEAKING_AND_LISTENING
        else:
            self.phase = PHASE_USER_SPEAKING
        self.silence_since_ms = None

    def note_user_speech_end(self, *, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()
        self.user_is_speaking = False
        self.last_user_speech_end_ms = now
        self.silence_since_ms = now
        if self.assistant_is_speaking:
            self.phase = PHASE_SPEAKING
        elif self.phase == PHASE_USER_SPEAKING:
            self.phase = PHASE_USER_PAUSING

    def note_silence(self, *, now_ms: int | None = None) -> None:
        """A VAD-negative frame arrived; record that the user is quiet."""
        now = now_ms if now_ms is not None else _now_ms()
        if self.silence_since_ms is None and not self.user_is_speaking:
            self.silence_since_ms = now

    def note_assistant_speech_start(self, *, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()
        self.assistant_is_speaking = True
        self.last_assistant_speech_start_ms = now
        self.speaking_mode = SPEAK_ANSWER
        self.phase = (
            PHASE_SPEAKING_AND_LISTENING if self.user_is_speaking else PHASE_SPEAKING
        )

    def note_assistant_speech_end(self, *, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()
        self.assistant_is_speaking = False
        self.last_assistant_speech_end_ms = now
        self.speaking_mode = SPEAK_NONE
        self.interruption_state = INTERRUPT_NONE
        if not self.user_is_speaking:
            self.phase = PHASE_LISTENING

    def note_backchannel(self, *, now_ms: int | None = None) -> None:
        now = now_ms if now_ms is not None else _now_ms()
        self.speaking_mode = SPEAK_BACKCHANNEL
        self.assistant_is_speaking = True
        self.last_backchannel_ms = now
        self.backchannel_count += 1
        self.push_history({"type": "backchannel", "at_ms": now})

    def note_barge_in(self, *, now_ms: int | None = None) -> None:
        """Assistant playback was stopped because the user started talking."""
        now = now_ms if now_ms is not None else _now_ms()
        self.assistant_is_speaking = False
        self.last_assistant_speech_end_ms = now
        self.interruption_state = INTERRUPT_BARGED_IN
        self.speaking_mode = SPEAK_NONE
        self.response_generation = GEN_IDLE
        self.phase = PHASE_INTERRUPTED if self.user_is_speaking else PHASE_LISTENING
        self.push_history({"type": "barge_in", "at_ms": now})

    def begin_response(self, *, background: bool = False, now_ms: int | None = None) -> None:
        del now_ms  # retained for API symmetry; response timing is engine-owned
        self.response_generation = GEN_BACKGROUND if background else GEN_FOREGROUND
        self.phase = PHASE_THINKING

    def mark_streaming(self) -> None:
        self.response_generation = GEN_STREAMING

    def finish_response(self, *, now_ms: int | None = None) -> None:
        del now_ms
        self.response_generation = GEN_DONE
        self.phase = PHASE_WAITING

    def reset_turn_context(self) -> None:
        """Clear per-turn context after a reply lands."""
        self.user_intent = None
        self.pending_question = None
        self.tool_state = "none"
        self.response_generation = GEN_IDLE

    def silence_ms(self, *, now_ms: int | None = None) -> int | None:
        """How long the user has been quiet (None when the user is talking)."""
        if self.user_is_speaking or self.silence_since_ms is None:
            return None
        now = now_ms if now_ms is not None else _now_ms()
        return max(0, now - self.silence_since_ms)

    def time_since_user_speech(self, *, now_ms: int | None = None) -> int | None:
        if self.last_user_speech_end_ms is None:
            return None
        now = now_ms if now_ms is not None else _now_ms()
        return max(0, now - self.last_user_speech_end_ms)

    def time_since_backchannel(self, *, now_ms: int | None = None) -> int | None:
        if self.last_backchannel_ms is None:
            return None
        now = now_ms if now_ms is not None else _now_ms()
        return max(0, now - self.last_backchannel_ms)

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #

    def push_history(self, item: dict) -> None:
        self.history.append(item)
        while len(self.history) > self.history_limit:
            self.history.popleft()

    def last_transcript(self) -> str | None:
        for item in reversed(self.history):
            if item.get("type") in {"final_transcript", "partial"}:
                text = item.get("text") or ""
                if text.strip():
                    return text.strip()
        return None

    def last_partial(self) -> str | None:
        for item in reversed(self.history):
            if item.get("type") == "partial":
                text = item.get("text") or ""
                if text.strip():
                    return text.strip()
        return None

    def recent_history(self, limit: int = 8) -> list[dict]:
        return list(self.history)[-limit:]

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        """A JSON-serializable snapshot for ``state`` protocol events."""
        return {
            "user_is_speaking": self.user_is_speaking,
            "assistant_is_speaking": self.assistant_is_speaking,
            "phase": self.phase,
            "listening_mode": self.listening_mode,
            "speaking_mode": self.speaking_mode,
            "interruption_state": self.interruption_state,
            "response_generation": self.response_generation,
            "user_intent": self.user_intent,
            "emotional_context": self.emotional_context,
            "current_topic": self.current_topic,
            "tool_state": self.tool_state,
            "silence_ms": self.silence_ms(),
            "latency_budget_ms": self.latency_budget_ms,
            "frame_seq": self.frame_seq,
        }


def phase_label(state: LiveConversationState) -> str:
    return state.phase


def speaking_mode_label(state: LiveConversationState) -> str:
    return state.speaking_mode
