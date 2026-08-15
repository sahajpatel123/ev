"""Continuous conversational engine for EV LIVE.

The engine is the real-time nervous system: it never talks to ASR, TTS, or
DeepSeek. Callers push VAD / ASR / playback signals; ``tick()`` decides
whether to listen, wait, backchannel, interrupt, or start a response. The
session / transport layer then acts on those decisions.

This split keeps turn-taking deterministic and offline-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.voice.live.backchannel import BackchannelDecision, BackchannelPolicy
from app.voice.live.behavior import BehaviorEnvelope, behavior_from_state
from app.voice.live.events import (
    BargeInEvent,
    LiveEvent,
    PartialTranscriptEvent,
    StateEvent,
)
from app.voice.live.state import (
    PHASE_LISTENING,
    PHASE_WAITING,
    LiveConversationState,
)
from app.voice.live.turn_taking import (
    TURN_KEEP_LISTENING,
    TURN_RESPOND_NOW,
    TURN_STAY_QUIET,
    TURN_USER_INTERRUPTED,
    TurnDecision,
    TurnTakingConfig,
    TurnTakingPolicy,
)


class ManualClock:
    """Injectable monotonic clock (milliseconds) for deterministic tests."""

    def __init__(self, start_ms: int = 0) -> None:
        self.ms = int(start_ms)

    def __call__(self) -> int:
        return self.ms

    def advance(self, delta_ms: int) -> int:
        self.ms += int(delta_ms)
        return self.ms


def _wall_clock_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


@dataclass
class EngineTick:
    """One decision cycle of the live engine."""

    decision: TurnDecision
    events: list[LiveEvent] = field(default_factory=list)
    backchannel: BackchannelDecision | None = None
    envelope: BehaviorEnvelope | None = None


class LiveEngine:
    """The conversation operating system for one live session."""

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        turn_config: TurnTakingConfig | None = None,
        backchannel: BackchannelPolicy | None = None,
        backchannel_enabled: bool = True,
    ) -> None:
        self.clock = clock_ms or _wall_clock_ms
        self.state = LiveConversationState()
        self.turns = TurnTakingPolicy(config=turn_config, clock_ms=self.clock)
        self.backchannels = backchannel or BackchannelPolicy()
        self.backchannel_enabled = backchannel_enabled
        self._last_phase = self.state.phase
        self._last_interrupt = self.state.interruption_state

    def now(self) -> int:
        return int(self.clock())

    def _maybe_state_event(self, now: int, events: list[LiveEvent]) -> None:
        if (
            self.state.phase != self._last_phase
            or self.state.interruption_state != self._last_interrupt
        ):
            events.append(StateEvent(at_ms=now, state=self.state.snapshot()))
            self._last_phase = self.state.phase
            self._last_interrupt = self.state.interruption_state

    def push_speech(self, active: bool, *, now_ms: int | None = None) -> list[LiveEvent]:
        """VAD crossed into or out of speech."""

        now = int(now_ms if now_ms is not None else self.now())
        events: list[LiveEvent] = []
        self.state.frame_seq += 1
        if active and not self.state.user_is_speaking:
            assistant_was_speaking = self.state.assistant_is_speaking
            self.state.note_user_speech_start(now_ms=now)
            self.turns.on_speech_start(now_ms=now)
            if assistant_was_speaking:
                self.note_barge_in(now_ms=now)
                events.append(BargeInEvent(at_ms=now, reason="user_speech"))
        elif not active and self.state.user_is_speaking:
            self.state.note_user_speech_end(now_ms=now)
            self.turns.on_speech_end(now_ms=now)
        elif not active:
            self.state.note_silence(now_ms=now)
        self._maybe_state_event(now, events)
        return events

    def push_partial(self, text: str, *, seq: int = 0, now_ms: int | None = None) -> list[LiveEvent]:
        now = int(now_ms if now_ms is not None else self.now())
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        self.turns.on_partial(cleaned, seq=seq)
        self.state.push_history({"type": "partial", "text": cleaned, "seq": seq, "at_ms": now})
        return [
            PartialTranscriptEvent(
                at_ms=now, text=cleaned, sequence=seq, stable=False, confidence=0.0
            )
        ]

    def push_transcript(self, text: str, *, now_ms: int | None = None) -> None:
        """A final (or committed) transcript is available."""

        now = int(now_ms if now_ms is not None else self.now())
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self.turns.on_partial(cleaned)
        self.state.push_history({"type": "final_transcript", "text": cleaned, "at_ms": now})
        self.state.previous_turn = cleaned
        try:
            from app.ev.interaction import detect_emotion, detect_intent

            self.state.emotional_context = detect_emotion(cleaned)
            self.state.user_intent = detect_intent(cleaned)
        except Exception:  # noqa: BLE001 - live loop must never die on NLP
            self.state.emotional_context = self.state.emotional_context or "neutral"

    def push_assistant_speaking(self, active: bool, *, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else self.now())
        if active:
            self.state.note_assistant_speech_start(now_ms=now)
            self.turns.on_assistant_speech_start()
        else:
            self.state.note_assistant_speech_end(now_ms=now)

    def set_listening_mode(self, mode: str) -> None:
        self.state.listening_mode = mode

    def note_barge_in(self, *, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else self.now())
        self.state.note_barge_in(now_ms=now)
        self.turns.on_barge_in()

    def begin_response(self, *, background: bool = False, now_ms: int | None = None) -> None:
        self.state.begin_response(background=background, now_ms=now_ms)
        self.turns.reset_turn()

    def mark_streaming(self) -> None:
        self.state.mark_streaming()

    def finish_response(self, *, now_ms: int | None = None) -> None:
        self.state.finish_response(now_ms=now_ms)
        self.state.reset_turn_context()

    def envelope_for(self, text: str) -> BehaviorEnvelope:
        return behavior_from_state(self.state, text)

    def commit(self, *, now_ms: int | None = None) -> EngineTick:
        """Push-to-talk release / explicit end of the user's turn."""

        now = int(now_ms if now_ms is not None else self.now())
        decision = self.turns.commit(self.state, now_ms=now)
        return self._tick_from_decision(decision, now)

    def tick(self, *, now_ms: int | None = None) -> EngineTick:
        """Decide the next conversational action. Call many times per second."""

        now = int(now_ms if now_ms is not None else self.now())
        decision = self.turns.decide(self.state, now_ms=now)
        return self._tick_from_decision(decision, now)

    def _tick_from_decision(self, decision: TurnDecision, now: int) -> EngineTick:
        events: list[LiveEvent] = []
        backchannel: BackchannelDecision | None = None
        envelope: BehaviorEnvelope | None = None

        if decision.action == TURN_USER_INTERRUPTED:
            events.append(BargeInEvent(at_ms=now, reason=decision.reason or "user_speech"))
            self.note_barge_in(now_ms=now)
        elif decision.action == TURN_STAY_QUIET:
            if self.state.phase != PHASE_WAITING and not self.state.user_is_speaking:
                self.state.phase = PHASE_WAITING
        elif decision.action == TURN_KEEP_LISTENING:
            if (
                not self.state.assistant_is_speaking
                and not self.state.user_is_speaking
                and self.state.phase != PHASE_LISTENING
                and self.state.response_generation in {"idle", "done"}
            ):
                self.state.phase = PHASE_LISTENING
        elif decision.action == TURN_RESPOND_NOW:
            text = decision.last_partial or self.state.last_transcript() or ""
            envelope = self.envelope_for(text)

        if (
            self.backchannel_enabled
            and self.state.user_is_speaking
            and decision.action == TURN_KEEP_LISTENING
        ):
            backchannel = self.backchannels.decide(self.state, now_ms=now)
            if backchannel.should_backchannel and backchannel.cue:
                self.state.note_backchannel(now_ms=now)

        self._maybe_state_event(now, events)
        return EngineTick(
            decision=decision,
            events=events,
            backchannel=backchannel,
            envelope=envelope,
        )
