"""Continuous conversational engine for EV LIVE.

The engine is the real-time nervous system: it never talks to ASR, TTS, or
MiMo. Callers push VAD / ASR / playback signals; ``tick()`` decides
whether to listen, wait, backchannel, interrupt, or start a response. The
session / transport layer then acts on those decisions.

This split keeps turn-taking deterministic and offline-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.voice.live.backchannel import BackchannelDecision, BackchannelPolicy
from app.voice.live.behavior import BehaviorEnvelope, behavior_from_state
from app.voice.live.eve_cutin import (
    TRIGGER_INVITED,
    CutInDecision,
    CutInPolicy,
    detect_invitation,
)
from app.voice.live.events import (
    BargeInEvent,
    FloorEvent,
    GestureEvent,
    LiveEvent,
    PartialTranscriptEvent,
    StateEvent,
)
from app.voice.live.floor import FloorTracker
from app.voice.live.gesture import gesture_for
from app.voice.live.interrupt_v2 import (
    OwnerVerdict,
    fuse_owner_evidence,
    parse_owner_evidence,
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
        self.floor = FloorTracker()
        self._floor_drained = 0
        self.cut_ins = CutInPolicy()
        self._cut_in_last_ms: int | None = None
        self._cut_in_invited = False
        self._gesture_hint: str | None = None
        self._last_gesture_key: tuple[str, str] | None = None
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

    def _drain_floor(self, now: int) -> list[FloorEvent]:
        """Floor transitions since the last drain, oldest first."""

        pending = self.floor.transitions[self._floor_drained :]
        self._floor_drained = len(self.floor.transitions)
        return [
            FloorEvent(
                at_ms=now,
                floor=item.current,
                previous=item.previous,
                reason=item.reason,
            )
            for item in pending
        ]

    def _drain_gesture(self, now: int) -> list[GestureEvent]:
        """Current gesture when it changed; one-shot hints never stick."""

        hint, self._gesture_hint = self._gesture_hint, None
        resolved = gesture_for(
            floor=self.floor.floor,
            phase=self.state.phase,
            emotion=self.state.emotional_context,
            hint=hint,
        )
        key = (resolved.gesture, resolved.intensity)
        if key == self._last_gesture_key:
            return []
        self._last_gesture_key = key
        return [
            GestureEvent(
                at_ms=now,
                gesture=resolved.gesture,
                intensity=resolved.intensity,
                floor=self.floor.floor,
            )
        ]

    def push_speech(self, active: bool, *, now_ms: int | None = None) -> list[LiveEvent]:
        """VAD crossed into or out of speech."""

        now = int(now_ms if now_ms is not None else self.now())
        events: list[LiveEvent] = []
        self.state.frame_seq += 1
        if active and not self.state.user_is_speaking:
            assistant_was_speaking = self.state.assistant_is_speaking
            self.state.note_user_speech_start(now_ms=now)
            self.turns.on_speech_start(now_ms=now)
            self.floor.note_owner_speech_start(now_ms=now)
            if assistant_was_speaking:
                self._gesture_hint = "owner_cut_in"
                self.note_barge_in(now_ms=now)
                events.append(BargeInEvent(at_ms=now, reason="user_speech"))
        elif not active and self.state.user_is_speaking:
            self.state.note_user_speech_end(now_ms=now)
            self.turns.on_speech_end(now_ms=now)
            self.floor.note_owner_speech_end(now_ms=now)
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

    def arm_invited_cut_in(self) -> None:
        """Arm the one-shot owner invitation for Eve to cut in."""

        self._cut_in_invited = True

    def consider_eve_cut_in(
        self, trigger: str, *, grounded: bool = False, now_ms: int | None = None
    ) -> CutInDecision:
        """Authorize (or deny) Eve taking the floor while the owner speaks.

        On approval the floor moves through yielding to Eve; the caller
        speaks through the normal speech lane and ends with
        :meth:`note_eve_cut_in_end` so the floor yields back.
        """

        now = int(now_ms if now_ms is not None else self.now())
        decision = self.cut_ins.decide(
            self.state,
            trigger,
            now_ms=now,
            last_cut_in_ms=self._cut_in_last_ms,
            invited_armed=self._cut_in_invited,
            grounded=grounded,
        )
        if not decision.allowed:
            return decision
        self._cut_in_last_ms = now
        if trigger == TRIGGER_INVITED:
            self._cut_in_invited = False
        self._gesture_hint = trigger
        self.floor.note_eve_cut_in(now_ms=now)
        return decision

    def note_eve_cut_in_end(self, *, now_ms: int | None = None) -> None:
        """Eve finished a cut-in: yield the floor back to the owner."""

        now = int(now_ms if now_ms is not None else self.now())
        if self.state.user_is_speaking:
            self.floor.note_yield_back_to_owner(now_ms=now)
            self._gesture_hint = "yield_back"
        else:
            self.floor.note_eve_speech_end(now_ms=now)

    def push_transcript(self, text: str, *, now_ms: int | None = None) -> None:
        """A final (or committed) transcript is available."""

        now = int(now_ms if now_ms is not None else self.now())
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if detect_invitation(cleaned):
            self._cut_in_invited = True
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
            self.floor.note_eve_speech_start(now_ms=now)
        else:
            self.state.note_assistant_speech_end(now_ms=now)
            self.floor.note_eve_speech_end(now_ms=now)

    def set_listening_mode(self, mode: str) -> None:
        self.state.listening_mode = mode

    def note_barge_in(self, *, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else self.now())
        self.state.note_barge_in(now_ms=now)
        self.turns.on_barge_in()
        self.floor.note_owner_cut_in(now_ms=now)

    def push_owner_evidence(
        self, payload: dict | None, *, now_ms: int | None = None
    ) -> tuple[list[LiveEvent], OwnerVerdict]:
        """Fuse one client ``owner_evidence`` frame (Interrupt V2).

        Returns the events to emit plus the fusion verdict so the session
        can cancel the speech backend exactly once on confirmation.
        Unconfirmed frames emit nothing: ambiguous evidence must not flap
        playback or gestures.
        """

        now = int(now_ms if now_ms is not None else self.now())
        evidence = parse_owner_evidence(payload)
        verdict = fuse_owner_evidence(
            evidence, eve_speaking=self.state.assistant_is_speaking
        )
        if not verdict.confirmed:
            return [], verdict
        self.state.note_user_speech_start(now_ms=now)
        self.turns.on_speech_start(now_ms=now)
        self._gesture_hint = "owner_cut_in"
        self.note_barge_in(now_ms=now)
        return [
            BargeInEvent(
                at_ms=now,
                reason="owner_cut_in_v2",
                audio_played_ms=evidence.audio_played_ms,
                confidence=evidence.confidence or None,
                preroll_ms=evidence.preroll_ms,
                provider_response_id=evidence.response_id,
            )
        ], verdict

    def begin_response(self, *, background: bool = False, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else self.now())
        self.state.begin_response(background=background, now_ms=now_ms)
        self.turns.reset_turn()
        self.floor.note_turn_committed(now_ms=now)

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

        # OWNER DECISION 2026-08-23: Listener Presence / server backchannel
        # cues are CANCELLED product features. The server never emits
        # "Mhm."/"Yeah."/"Okay." listener speech — one speech authority only:
        # the normal assistant response. (Legacy BackchannelPolicy code
        # remains quarantined, unwired.)

        # Floor ownership is advisory metadata for gestures: old clients
        # ignore the unknown "floor" type; new clients render from it.
        events.extend(self._drain_floor(now))
        events.extend(self._drain_gesture(now))
        self._maybe_state_event(now, events)
        return EngineTick(
            decision=decision,
            events=events,
            backchannel=backchannel,
            envelope=envelope,
        )
