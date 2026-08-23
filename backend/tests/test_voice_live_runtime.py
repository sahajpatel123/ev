"""EV LIVE runtime core: state, turn-taking, backchanneling, behavior.

These tests are engine-free and deterministic — a fake monotonic clock drives
every decision so the "silence is information" rules are provable without
real VAD/ASR/TTS.
"""

from __future__ import annotations

from app.voice.live.backchannel import BackchannelPolicy
from app.voice.live.behavior import BehaviorEnvelope, behavior_from_state, to_speech_style
from app.voice.live.events import BackchannelEvent, ReadyEvent, TtsChunkEvent
from app.voice.live.state import (
    INTERRUPT_BARGED_IN,
    INTERRUPT_PENDING,
    LISTEN_ATTENTIVE,
    LISTEN_PASSIVE,
    LISTEN_QUIET,
    LiveConversationState,
)
from app.voice.live.turn_taking import (
    TURN_KEEP_LISTENING,
    TURN_RESPOND_NOW,
    TURN_STAY_QUIET,
    TURN_USER_INTERRUPTED,
    TurnTakingConfig,
    TurnTakingPolicy,
    is_non_turn,
    pause_class,
)


class FakeClock:
    """Deterministic monotonic clock in milliseconds."""

    def __init__(self, start: int = 0) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, ms: int) -> None:
        self.now += ms


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def test_state_tracks_user_speech_start_and_end() -> None:
    state = LiveConversationState()
    state.note_user_speech_start(now_ms=1000)
    assert state.user_is_speaking
    assert state.last_user_speech_start_ms == 1000
    assert state.silence_since_ms is None
    state.note_user_speech_end(now_ms=3000)
    assert not state.user_is_speaking
    assert state.last_user_speech_end_ms == 3000
    assert state.silence_since_ms == 3000
    state.note_silence(now_ms=4000)
    assert state.silence_ms(now_ms=4500) == 1500


def test_state_detects_pending_interruption_then_barge_in() -> None:
    state = LiveConversationState()
    state.note_assistant_speech_start(now_ms=1000)
    assert state.assistant_is_speaking
    assert state.phase == "speaking"
    state.note_user_speech_start(now_ms=1500)
    assert state.interruption_state == INTERRUPT_PENDING
    assert state.phase == "speaking_and_listening"
    state.note_barge_in(now_ms=1600)
    assert not state.assistant_is_speaking
    assert state.interruption_state == INTERRUPT_BARGED_IN
    assert state.phase == "interrupted"
    assert state.response_generation == "idle"


def test_state_notes_backchannel_and_caps_history() -> None:
    state = LiveConversationState(history_limit=4)
    state.note_backchannel(now_ms=100)
    assert state.speaking_mode == "backchannel"
    assert state.backchannel_count == 1
    for i in range(10):
        state.push_history({"type": "partial", "text": f"t{i}", "at_ms": i})
    assert len(state.history) == 4
    assert state.last_transcript() == "t9"


# --------------------------------------------------------------------------- #
# Turn-taking: pause classes
# --------------------------------------------------------------------------- #


def test_pause_class_distinguishes_complete_trailing_thinking() -> None:
    assert pause_class("What's the weather?") == "complete"
    assert pause_class("what's the weather") == "complete"
    assert pause_class("Evie what's the weather") == "complete"
    assert pause_class("Evie") == "wake"
    assert pause_class("I was thinking maybe we could") == "trailing"
    assert pause_class("so yesterday I went to the store and") == "trailing"
    assert pause_class("And then this crazy thing") == "thinking"
    assert pause_class("") == "thinking"


def test_is_non_turn_recognizes_thinking_sounds() -> None:
    for text in ("hmm", "Hmm.", "uh", "um", "mhm", "yeah", "okay"):
        assert is_non_turn(text), text
    assert not is_non_turn("what's the weather")


# --------------------------------------------------------------------------- #
# Turn-taking: decisions
# --------------------------------------------------------------------------- #


def _scenario(**kwargs):
    clock = kwargs.pop("clock", FakeClock())
    config = kwargs.pop("config", None)
    policy = TurnTakingPolicy(config=config, clock_ms=clock)
    state = LiveConversationState()
    return clock, state, policy


def test_complete_sentence_pause_triggers_response() -> None:
    clock, state, policy = _scenario()
    clock.advance(100)
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(1500)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("What's the weather?", seq=1)
    clock.advance(200)
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_STAY_QUIET
    clock.advance(200)  # total pause 400ms > default 280ms end_pause
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_RESPOND_NOW
    assert decision.respond


def test_trailing_pause_waits_longer_than_sentence_end() -> None:
    clock, state, policy = _scenario()
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(2000)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("I was thinking maybe we could", seq=1)
    clock.advance(900)
    state.note_silence(now_ms=clock.now)
    # A complete sentence would have responded by 900ms; a trailing pause must
    # still be waiting for the user to continue.
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_STAY_QUIET
    assert "trailing" in decision.reason
    clock.advance(300)  # total 1200ms > trailing grace 1100ms
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_RESPOND_NOW


def test_thinking_pause_without_final_punctuation_waits() -> None:
    clock, state, policy = _scenario()
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(1200)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("And then this crazy thing", seq=1)
    clock.advance(500)
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_STAY_QUIET
    assert "thinking" in decision.reason
    clock.advance(300)  # total 800ms > thinking grace 700ms
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_RESPOND_NOW


def test_non_turn_never_triggers_response() -> None:
    clock, state, policy = _scenario()
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(600)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("hmm", seq=1)
    clock.advance(5000)
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action != TURN_RESPOND_NOW
    assert decision.action == TURN_KEEP_LISTENING


def test_user_speech_while_assistant_speaking_is_interruption() -> None:
    clock, state, policy = _scenario()
    state.note_assistant_speech_start(now_ms=clock.now)
    policy.on_assistant_speech_start()
    clock.advance(1200)
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_USER_INTERRUPTED
    assert decision.interrupted


def test_short_blip_is_not_an_interruption() -> None:
    clock, state, policy = _scenario(config=TurnTakingConfig(min_speech_ms=220))
    state.note_assistant_speech_start(now_ms=clock.now)
    policy.on_assistant_speech_start()
    clock.advance(500)
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(80)  # blip shorter than min_speech_ms
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_KEEP_LISTENING


def test_response_cooldown_prevents_double_trigger() -> None:
    clock, state, policy = _scenario()
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(2000)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("Done.", seq=1)
    clock.advance(900)
    state.note_silence(now_ms=clock.now)
    assert policy.decide(state, now_ms=clock.now).action == TURN_RESPOND_NOW
    # Immediately after, another decide tick must not re-trigger.
    clock.advance(200)
    state.note_silence(now_ms=clock.now)
    assert policy.decide(state, now_ms=clock.now).action == TURN_KEEP_LISTENING


def test_quiet_listening_mode_waits_longer() -> None:
    clock, state, policy = _scenario()
    state.listening_mode = LISTEN_QUIET
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(1500)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("What's up?", seq=1)
    clock.advance(1100)  # > 800ms default, < 1300ms quiet
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_STAY_QUIET
    clock.advance(300)  # total 1400ms > quiet 1300ms
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_RESPOND_NOW


def test_passive_mode_never_self_responds() -> None:
    clock, state, policy = _scenario()
    state.listening_mode = LISTEN_PASSIVE
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(2000)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("What's the weather?", seq=1)
    clock.advance(10_000)
    state.note_silence(now_ms=clock.now)
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_STAY_QUIET


def test_max_pause_never_forces_premature_reply_on_long_thinking() -> None:
    clock, state, policy = _scenario(config=TurnTakingConfig(max_pause_ms=3000))
    state.note_user_speech_start(now_ms=clock.now)
    policy.on_speech_start(now_ms=clock.now)
    clock.advance(800)
    state.note_user_speech_end(now_ms=clock.now)
    policy.on_speech_end(now_ms=clock.now)
    policy.on_partial("well I think", seq=1)
    clock.advance(10_000)
    state.note_silence(now_ms=clock.now)
    # Still not sentence-complete; even the cap keeps us waiting patiently.
    decision = policy.decide(state, now_ms=clock.now)
    assert decision.action == TURN_RESPOND_NOW


# --------------------------------------------------------------------------- #
# Backchanneling
# --------------------------------------------------------------------------- #


def _backchannel_scenario():
    state = LiveConversationState()
    policy = BackchannelPolicy()
    return state, policy


def test_backchannel_fires_while_user_holds_the_floor() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    decision = policy.decide(state, now_ms=2000)
    assert decision.should_backchannel
    assert decision.cue in ("Mhm.", "Yeah.", "Okay.")


def test_no_backchannel_too_early_or_too_often() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    assert not policy.decide(state, now_ms=500).should_backchannel
    state.note_backchannel(now_ms=2000)
    # 2s after the last cue: inside max_interval_ms (5s) and below the long
    # narration re-cue window (9s) → stay quiet.
    assert not policy.decide(state, now_ms=4000).should_backchannel


def test_long_narration_recues_inside_interval() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    state.note_backchannel(now_ms=2000)
    state.note_assistant_speech_end(now_ms=2500)
    # 10s since the last cue: inside the 9s window is the guard, so 10s → cue.
    decision = policy.decide(state, now_ms=12_000)
    assert decision.should_backchannel


def test_no_backchannel_when_quiet_or_emotional() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    state.listening_mode = LISTEN_QUIET
    assert not policy.decide(state, now_ms=3000).should_backchannel
    state.listening_mode = LISTEN_ATTENTIVE
    state.emotional_context = "frustrated"
    assert not policy.decide(state, now_ms=3000).should_backchannel
    state.emotional_context = "neutral"
    state.user_intent = "urgent_request"
    assert not policy.decide(state, now_ms=3000).should_backchannel


def test_backchannel_per_turn_cap() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    decided: list[bool] = []
    for i in range(6):
        # decide() first — the engine then speaks the cue and records it via
        # note_backchannel() (which bumps the per-turn counter).
        decision = policy.decide(state, now_ms=2000 + i * 10_000 + 10_300)
        decided.append(decision.should_backchannel)
        if decision.should_backchannel:
            state.note_backchannel(now_ms=2000 + i * 10_000 + 10_500)
            state.note_assistant_speech_end(now_ms=2000 + i * 10_000 + 11_000)
    # max_per_turn=3 → the first three cues are allowed, then the cap holds.
    assert decided[:3] == [True, True, True], decided
    assert decided[3:] == [False, False, False], decided


def test_backchannel_variety_over_long_turn() -> None:
    state, policy = _backchannel_scenario()
    state.note_user_speech_start(now_ms=0)
    cues: set[str] = set()
    for i in range(3):
        decision = policy.decide(state, now_ms=2000 + i * 10_000 + 10_300)
        if decision.should_backchannel:
            cues.add(decision.cue)
            state.note_backchannel(now_ms=2000 + i * 10_000 + 10_500)
            state.note_assistant_speech_end(now_ms=2000 + i * 10_000 + 11_000)
    assert len(cues) >= 2, cues


# --------------------------------------------------------------------------- #
# Behavior envelope
# --------------------------------------------------------------------------- #


def test_frustrated_context_produces_direct_calm_envelope() -> None:
    state = LiveConversationState()
    state.emotional_context = "frustrated"
    envelope = behavior_from_state(state, transcript="it keeps failing")
    assert envelope.interaction_mode == "direct"
    assert envelope.energy == "low"
    assert envelope.pace == "normal"
    assert envelope.pause_before_response_ms == 160
    assert envelope.directness == "high"


def test_excited_context_produces_excited_fast_envelope() -> None:
    state = LiveConversationState()
    state.emotional_context = "excited"
    envelope = behavior_from_state(state)
    assert envelope.interaction_mode == "excited"
    assert envelope.energy == "high"
    assert envelope.pace == "fast"


def test_urgent_text_produces_direct_urgent_envelope() -> None:
    state = LiveConversationState()
    envelope = behavior_from_state(state, text="I need this ASAP hurry up")
    assert envelope.interaction_mode == "urgent"
    assert envelope.directness == "high"
    assert envelope.verbosity == "short and direct"
    assert envelope.interruptibility == "low"


def test_sad_words_produce_empathetic_envelope() -> None:
    state = LiveConversationState()
    envelope = behavior_from_state(state, text="I'm sorry, I had a rough day")
    assert envelope.interaction_mode == "empathetic"
    assert envelope.energy == "low"


def test_neutral_question_produces_neutral_envelope() -> None:
    state = LiveConversationState()
    envelope = behavior_from_state(state, text="What's the weather in Surat?")
    assert envelope.interaction_mode == "neutral"
    assert envelope.pace == "normal"


def test_behavior_envelope_maps_to_speech_style() -> None:
    envelope = BehaviorEnvelope(
        semantic_content="I understand why that was frustrating.",
        interaction_mode="empathetic",
        energy="low",
        pace="slow",
        interruptibility="high",
        pause_before_response_ms=240,
    )
    style = to_speech_style(envelope)
    assert style.warmth >= 0.85
    assert style.urgency <= 0.2
    urgent = to_speech_style(
        BehaviorEnvelope(
            semantic_content="Do it now.",
            interaction_mode="urgent",
            energy="high",
            pace="fast",
        )
    )
    assert urgent.urgency >= 0.75
    assert urgent.mode == "emergency"
    assert urgent.brevity >= 0.85
    excited = to_speech_style(
        BehaviorEnvelope(
            semantic_content="That's amazing!",
            interaction_mode="excited",
            energy="high",
            pace="fast",
        )
    )
    assert excited.urgency >= 0.4


def test_envelope_serializes_as_dict() -> None:
    envelope = behavior_from_state(
        LiveConversationState(emotional_context="sad"),
        text="I miss her",
    )
    data = envelope.as_dict()
    assert data["interaction_mode"] == "empathetic"
    assert data["pause_before_response_ms"] == 240
    assert data["energy"] in {"low", "medium", "high"}
    assert "verbosity" in data
    assert "directness" in data


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def test_events_serialize_with_type_and_stamp() -> None:
    ready = ReadyEvent(at_ms=10, session_id="s1", config={"sample_rate": 16000})
    assert ready.as_dict()["type"] == "ready"
    assert ready.as_dict()["at_ms"] == 10
    assert ready.as_dict()["session_id"] == "s1"
    chunk = TtsChunkEvent(at_ms=20, index=0, text="Hi.", audio_b64="QUJD")
    data = chunk.as_dict()
    assert data["index"] == 0
    assert data["audio_b64"] == "QUJD"
    back = BackchannelEvent(at_ms=30, text="Mhm.")
    assert back.as_dict()["text"] == "Mhm."


# --------------------------------------------------------------------------- #
# LiveEngine orchestration
# --------------------------------------------------------------------------- #


from app.voice.live.engine import LiveEngine, ManualClock


def test_engine_push_speech_while_assistant_speaking_emits_barge_in() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=1000)
    clock.advance(500)
    events = engine.push_speech(True, now_ms=1500)
    assert any(event.type == "barge_in" for event in events)
    assert engine.state.interruption_state == "barged_in"
    assert not engine.state.assistant_is_speaking


def test_engine_tick_responds_after_complete_pause() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    clock.advance(1500)
    engine.push_partial("What's the weather?", seq=1, now_ms=1600)
    engine.push_speech(False, now_ms=1600)
    clock.advance(900)
    tick = engine.tick(now_ms=2500)
    assert tick.decision.action == TURN_RESPOND_NOW
    assert tick.envelope is not None
    assert tick.envelope.interaction_mode in {"neutral", "casual"}


def test_engine_commit_forces_response() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    clock.advance(1200)
    engine.push_partial("send the email", seq=1, now_ms=1300)
    tick = engine.commit(now_ms=1300)
    assert tick.decision.action == TURN_RESPOND_NOW
    assert tick.decision.reason == "explicit commit"


def test_engine_backchannels_while_user_holds_floor() -> None:
    # OWNER DECISION 2026-08-23: listener backchannel cues are CANCELLED.
    # The engine must never produce one, even with legacy flags left on.
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.backchannel_enabled = True
    engine.push_speech(True, now_ms=100)
    clock.advance(3000)
    tick = engine.tick(now_ms=3100)
    assert tick.backchannel is None


def test_engine_does_not_backchannel_when_disabled() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock, backchannel_enabled=False)
    engine.push_speech(True, now_ms=100)
    clock.advance(3000)
    tick = engine.tick(now_ms=3100)
    assert tick.backchannel is None or not tick.backchannel.should_backchannel


def test_engine_resets_after_response_finish() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    clock.advance(1500)
    engine.push_partial("What time is it?", seq=1, now_ms=1600)
    engine.push_speech(False, now_ms=1600)
    clock.advance(900)
    tick = engine.tick(now_ms=2500)
    assert tick.decision.action == TURN_RESPOND_NOW
    engine.begin_response(now_ms=2600)
    engine.mark_streaming()
    engine.push_assistant_speaking(True, now_ms=2800)
    engine.finish_response(now_ms=5000)
    engine.push_assistant_speaking(False, now_ms=5100)
    assert engine.state.response_generation == "idle"
    assert engine.state.phase == "listening"


def test_engine_state_events_emitted_on_phase_change() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=1000)
    tick = engine.tick(now_ms=1100)
    assert any(event.type == "state" for event in tick.events)
