"""EV LIVE — full-duplex turn-taking, backchannel, behavior, and session loop."""

from __future__ import annotations

import array
import asyncio
import base64

from app.voice.asr import EchoTranscriber
from app.voice.contracts import SynthesisResult, Transcript, VoiceError
from app.voice.live.asr_feed import LivePcmTranscriber, resolve_live_transcriber
from app.voice.live.backchannel import BackchannelPolicy
from app.voice.live.behavior import behavior_from_state, to_speech_style
from app.voice.live.delegate import needs_deep_work, thinking_filler
from app.voice.live.engine import LiveEngine, ManualClock
from app.voice.live.events import (
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    LatencyEvent,
    ReplyEvent,
    TtsChunkEvent,
)
from app.voice.live.session import LiveSession
from app.voice.live.state import (
    GEN_FOREGROUND,
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
from app.voice.speech import pop_speakable


def test_pause_class_treats_silence_as_information() -> None:
    assert pause_class("That's interesting.") == "complete"
    assert pause_class("I was thinking maybe") == "trailing"
    assert pause_class("I was thinking we could") == "trailing"
    assert pause_class("the sky is blue") == "thinking"
    assert pause_class("") == "thinking"
    assert pause_class("what's the weather") == "complete"
    assert pause_class("Evie what's the weather") == "complete"
    assert pause_class("text mom") == "complete"
    assert pause_class("Evie") == "wake"
    assert pause_class("hey evie") == "wake"


def test_non_turns_are_thinking_sounds() -> None:
    assert is_non_turn("hmm")
    assert is_non_turn("uh")
    assert is_non_turn("yeah")
    assert not is_non_turn("what's the weather")


def test_complete_sentence_responds_after_end_pause() -> None:
    clock = ManualClock(0)
    policy = TurnTakingPolicy(clock_ms=clock)
    state = LiveConversationState()
    policy.on_speech_start(now_ms=0)
    state.note_user_speech_start(now_ms=0)
    policy.on_partial("What's the weather.")
    policy.on_speech_end(now_ms=400)
    state.note_user_speech_end(now_ms=400)

    stay = policy.decide(state, now_ms=620)
    assert stay.action == TURN_STAY_QUIET
    go = policy.decide(state, now_ms=720)
    assert go.action == TURN_RESPOND_NOW
    assert go.reason.startswith("sentence-complete")


def test_thinking_pause_waits_longer_than_a_silence_threshold() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    policy.on_speech_start(now_ms=0)
    state.note_user_speech_start(now_ms=0)
    policy.on_partial("I don't know")
    policy.on_speech_end(now_ms=400)
    state.note_user_speech_end(now_ms=400)

    still = policy.decide(state, now_ms=1000)  # 600 ms silence — under thinking grace
    assert still.action == TURN_STAY_QUIET
    assert "thinking" in still.reason
    ready = policy.decide(state, now_ms=1200)
    assert ready.action == TURN_RESPOND_NOW


def test_trailing_clause_waits_for_continuation() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    policy.on_speech_start(now_ms=0)
    state.note_user_speech_start(now_ms=0)
    policy.on_partial("I was thinking maybe we could")
    policy.on_speech_end(now_ms=200)
    state.note_user_speech_end(now_ms=200)

    assert policy.decide(state, now_ms=1200).action == TURN_STAY_QUIET
    assert policy.decide(state, now_ms=1400).action == TURN_RESPOND_NOW


def test_wake_only_evie_waits_for_a_command_before_acking() -> None:
    clock = ManualClock(0)
    policy = TurnTakingPolicy(clock_ms=clock)
    state = LiveConversationState()
    policy.on_speech_start(now_ms=0)
    state.note_user_speech_start(now_ms=0)
    policy.on_partial("Evie")
    policy.on_speech_end(now_ms=300)
    state.note_user_speech_end(now_ms=300)

    stay = policy.decide(state, now_ms=700)
    assert stay.action == TURN_STAY_QUIET
    assert "wake" in stay.reason
    go = policy.decide(state, now_ms=1000)
    assert go.action == TURN_RESPOND_NOW


def test_empty_content_never_starts_a_turn() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    state.note_user_speech_end(now_ms=0)
    decision = policy.decide(state, now_ms=5000)
    assert decision.action == TURN_KEEP_LISTENING
    assert "no user content" in decision.reason


def test_in_flight_response_does_not_retrigger() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    policy.on_partial("Hello there.")
    state.note_user_speech_end(now_ms=0)
    state.response_generation = GEN_FOREGROUND
    decision = policy.decide(state, now_ms=5000)
    assert decision.action == TURN_KEEP_LISTENING
    assert "in flight" in decision.reason


def test_speaking_and_listening_phase_is_full_duplex() -> None:
    state = LiveConversationState()
    state.note_assistant_speech_start(now_ms=0)
    assert state.phase == "speaking"
    state.note_user_speech_start(now_ms=200)
    assert state.phase == "speaking_and_listening"
    assert state.interruption_state == "pending"


def test_user_speech_while_assistant_speaking_is_barge_in() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    state.user_is_speaking = True
    state.assistant_is_speaking = True
    decision = policy.decide(state, now_ms=10)
    assert decision.action == TURN_USER_INTERRUPTED


def test_commit_skips_the_thinking_pause() -> None:
    policy = TurnTakingPolicy()
    state = LiveConversationState()
    policy.on_partial("What's next on my calendar")
    state.note_user_speech_end(now_ms=0)
    decision = policy.commit(state, now_ms=50)
    assert decision.action == TURN_RESPOND_NOW
    assert decision.reason == "explicit commit"


def test_quiet_listening_mode_extends_the_pause() -> None:
    policy = TurnTakingPolicy(config=TurnTakingConfig())
    state = LiveConversationState()
    state.listening_mode = LISTEN_QUIET
    policy.on_partial("That's interesting.")
    state.note_user_speech_end(now_ms=0)
    assert policy.decide(state, now_ms=800).action == TURN_STAY_QUIET
    assert policy.decide(state, now_ms=1300).action == TURN_RESPOND_NOW


def test_backchannel_after_holding_the_floor() -> None:
    policy = BackchannelPolicy(min_speech_ms=1800, max_interval_ms=5000)
    state = LiveConversationState()
    state.user_is_speaking = True
    state.last_user_speech_start_ms = 0
    quiet = policy.decide(state, now_ms=500)
    assert quiet.cue is None
    cue = policy.decide(state, now_ms=2000)
    assert cue.should_backchannel
    assert cue.cue in {"Mhm.", "Yeah.", "Okay."}


def test_backchannel_stays_quiet_when_owner_is_sad() -> None:
    policy = BackchannelPolicy(min_speech_ms=100)
    state = LiveConversationState()
    state.user_is_speaking = True
    state.last_user_speech_start_ms = 0
    state.emotional_context = "sad"
    decision = policy.decide(state, now_ms=5000)
    assert decision.cue is None
    assert "emotional" in decision.reason


def test_behavior_envelope_separates_words_from_delivery() -> None:
    state = LiveConversationState()
    state.emotional_context = "sad"
    envelope = behavior_from_state(state, "I understand why that was frustrating.")
    assert envelope.semantic_content.startswith("I understand")
    assert envelope.interaction_mode == "empathetic"
    assert envelope.energy == "low"
    assert envelope.pace == "slow"
    assert envelope.pause_before_response_ms >= 200
    sad = to_speech_style(envelope)
    neutral = to_speech_style(behavior_from_state(LiveConversationState(), "Okay."))
    assert sad.warmth > neutral.warmth
    assert sad.urgency < neutral.urgency


def test_needs_deep_work_routes_search_and_keeps_small_talk_foreground() -> None:
    assert needs_deep_work("search the web for Surat weather")
    assert needs_deep_work("look up what happened in the markets today")
    assert thinking_filler("search the web for Surat weather") == "Searching."
    assert not needs_deep_work("hey")
    assert not needs_deep_work("what's the time")


def test_engine_emits_barge_in_the_instant_user_speaks() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=0)
    events = engine.push_speech(True, now_ms=80)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert engine.state.assistant_is_speaking is False
    assert engine.state.user_is_speaking is True


def test_engine_tick_responds_after_a_complete_pause() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=0)
    engine.push_partial("That's interesting.", seq=1, now_ms=100)
    engine.push_speech(False, now_ms=400)
    stay = engine.tick(now_ms=620)
    assert stay.decision.action == TURN_STAY_QUIET
    go = engine.tick(now_ms=720)
    assert go.decision.action == TURN_RESPOND_NOW
    assert go.envelope is not None
    assert go.envelope.semantic_content == "That's interesting."


async def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


class _BlockingSynthesizer:
    """Deterministic TTS double: blocks until released, observes cancel."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def synthesize(self, text: str, *, style) -> SynthesisResult:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return SynthesisResult(
            text=text,
            provider="blocking",
            style=style,
            audio=b"\x00\x01" * 80,
            content_type="audio/wav",
        )


async def test_live_session_text_commit_runs_intelligence() -> None:
    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        yield ReplyEvent(at_ms=0, text=f"got {text}")

    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, respond=respond, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "what's next on my calendar"})
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    types = [event.type for event in events]
    assert "final_transcript" in types
    assert "reply" in types
    assert heard == ["what's next on my calendar"]
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == "got what's next on my calendar"


async def test_live_session_barge_in_cancels_a_slow_reply() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str, envelope):
        started.set()
        try:
            await asyncio.sleep(30)
            yield ReplyEvent(at_ms=0, text="should not land")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, respond=slow, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "explain this at length please"})
    await asyncio.wait_for(started.wait(), timeout=1)
    clock.advance(80)
    await session.handle_client({"type": "speech", "active": True})
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    events = await _drain(session)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert not any(
        isinstance(event, ReplyEvent) and event.text == "should not land" for event in events
    )


async def test_backchannel_synthesis_does_not_hold_the_floor() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    synth = _BlockingSynthesizer()
    session = LiveSession(engine=engine, synthesizer=synth)
    await session.handle_client({"type": "speech", "active": True})
    clock.advance(2000)
    await session.tick()
    await asyncio.wait_for(synth.started.wait(), timeout=1)
    # While the cue is being synthesized the owner is still holding the floor:
    # the assistant must not look like it is speaking (that would turn every
    # subsequent tick into a self-interruption and abort the owner's ASR).
    assert engine.state.assistant_is_speaking is False
    assert engine.state.speaking_mode == "none"
    synth.release.set()
    await asyncio.sleep(0.05)
    session.close()


async def test_backchannel_is_cancelled_at_a_new_turn_boundary() -> None:
    """A delayed listening cue must not speak after the owner starts a turn."""

    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    synth = _BlockingSynthesizer()
    session = LiveSession(engine=engine, synthesizer=synth)
    await session.handle_client({"type": "speech", "active": True})
    clock.advance(2000)
    await session.tick()
    await asyncio.wait_for(synth.started.wait(), timeout=1)

    await session.emit(
        FinalTranscriptEvent(at_ms=session.now(), text="new question", provider="text")
    )
    await asyncio.wait_for(synth.cancelled.wait(), timeout=1)
    assert [event.type for event in session.outbound._queue] == ["final_transcript"]
    session.close()


async def test_live_session_barge_in_cancels_filler_before_reply() -> None:
    synth = _BlockingSynthesizer()
    replied = asyncio.Event()

    async def respond(text: str, envelope):
        replied.set()
        yield ReplyEvent(at_ms=0, text="should not land")

    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(
        engine=engine,
        synthesizer=synth,
        respond=respond,
        backchannel_enabled=False,
    )
    await session.handle_client(
        {"type": "text", "text": "search the web for Surat weather"}
    )
    await asyncio.wait_for(synth.started.wait(), timeout=1)
    assert session._respond_task is not None
    assert not session._respond_task.done()

    clock.advance(80)
    await session.handle_client({"type": "speech", "active": True})
    await asyncio.wait_for(synth.cancelled.wait(), timeout=1)
    synth.release.set()
    await asyncio.sleep(0.05)

    events = await _drain(session)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert not replied.is_set()
    assert not any(
        isinstance(event, ReplyEvent) and event.text == "should not land"
        for event in events
    )


async def test_live_session_sleep_phrase_closes_the_channel() -> None:
    ended: list[str] = []

    async def on_sleep(text: str) -> None:
        ended.append(text)

    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        on_sleep=on_sleep,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "that's all"})
    events = await _drain(session)
    assert session._closed is True
    assert ended == ["that's all"]
    assert any(isinstance(event, ErrorEvent) and event.code == "session_ended" for event in events)
    assert not any(isinstance(event, ReplyEvent) for event in events)


def test_live_route_is_registered() -> None:
    from app.api.voice import router

    paths = [getattr(route, "path", "") for route in router.routes]
    assert "/v1/voice/live" in paths
    assert "/v1/voice/live/open" in paths


def _speech_pcm(*, seconds: float = 0.25, amplitude: int = 4000) -> bytes:
    n = max(1, int(16000 * seconds))
    return array.array("h", [amplitude if i % 2 == 0 else -amplitude for i in range(n)]).tobytes()


def _silence_pcm(*, seconds: float = 0.1) -> bytes:
    return array.array("h", [0] * max(1, int(16000 * seconds))).tobytes()


class _PhraseFromPcm:
    """Shipped-path transcriber double: consumes PCM and returns a real phrase."""

    name = "phrase-from-pcm"

    def __init__(self, text: str = "What's next on my calendar.") -> None:
        self.text = text
        self.calls = 0
        self.bytes_seen = 0

    async def transcribe(self, **kwargs) -> Transcript:
        audio_b64 = kwargs.get("audio_b64")
        assert audio_b64, "live feed must pass audio, not a text hint"
        raw = base64.b64decode(audio_b64)
        self.bytes_seen += len(raw)
        self.calls += 1
        return Transcript(text=self.text, confidence=0.92, language="en", provider=self.name)


async def test_live_session_pcm_with_working_transcriber_commits_and_speaks() -> None:
    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        yield TtsChunkEvent(at_ms=0, index=0, text="Checking your calendar.")
        yield ReplyEvent(at_ms=1, text="Checking your calendar.")

    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        transcriber=_PhraseFromPcm(),
        respond=respond,
        asr_partial_interval_ms=50,
        backchannel_enabled=False,
    )
    await session.handle_client(_speech_pcm(seconds=0.25))
    clock.advance(250)
    await session.handle_client(_speech_pcm(seconds=0.25))
    await asyncio.sleep(0.15)
    clock.advance(50)
    await session.handle_client(_silence_pcm())
    await asyncio.sleep(0.15)
    clock.advance(500)
    await session.tick()
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    types = [event.type for event in events]
    assert "final_transcript" in types
    final = next(event for event in events if isinstance(event, FinalTranscriptEvent))
    assert final.text.rstrip(".!?") == "What's next on my calendar"
    assert "tts_chunk" in types
    assert "reply" in types
    assert heard
    assert heard[0].rstrip(".!?") == "What's next on my calendar"
    chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert chunk.text
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == "Checking your calendar."


async def test_live_session_wake_only_evie_acks_yes_without_responder() -> None:
    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        yield ReplyEvent(at_ms=0, text="should not run")

    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        respond=respond,
        backchannel_enabled=False,
    )
    for name in ("Evie", "EVIE", "hey evie"):
        heard.clear()
        await session.handle_client({"type": "text", "text": name})
        if session._respond_task is not None:
            await session._respond_task
        events = await _drain(session)
        assert heard == [], name
        assert not any(isinstance(event, ReplyEvent) for event in events), name
        chunk = next(
            (event for event in events if isinstance(event, TtsChunkEvent)),
            None,
        )
        assert chunk is not None, name
        assert chunk.text == "Yes?"
        assert session._closed is False
        assert session.engine.state.assistant_is_speaking is False

    await session.handle_client({"type": "text", "text": "what's next on my calendar"})
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    assert heard == ["what's next on my calendar"]
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == "should not run"


async def test_live_session_evie_plus_command_goes_to_chat_not_yes() -> None:
    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        yield ReplyEvent(at_ms=0, text=f"got {text}")

    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        respond=respond,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "Evie what's the weather"})
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    assert heard == ["what's the weather"]
    assert not any(
        isinstance(event, TtsChunkEvent) and event.text == "Yes?" for event in events
    )
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == "got what's the weather"


async def test_live_session_wake_ack_does_not_barge_in_the_next_words() -> None:
    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        yield ReplyEvent(at_ms=0, text=f"got {text}")

    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        respond=respond,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "Evie"})
    events = await _drain(session)
    assert any(isinstance(event, TtsChunkEvent) and event.text == "Yes?" for event in events)
    assert session.engine.state.assistant_is_speaking is False

    clock.advance(80)
    await session.handle_client({"type": "speech", "active": True})
    events = await _drain(session)
    assert not any(isinstance(event, BargeInEvent) for event in events)

    clock.advance(400)
    await session.handle_client({"type": "speech", "active": False})
    await session.handle_client({"type": "text", "text": "what's the weather"})
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    assert heard == ["what's the weather"]


async def test_live_session_ttfa_emits_speakable_prefix_before_full_reply() -> None:
    full = "Right now it is mostly clear tonight leftover. Sun is on the west side."
    first, rest = pop_speakable(full)
    assert first is not None
    assert rest.strip()
    assert first != full

    clock = ManualClock(1000)

    async def respond(text: str, envelope):
        yield TtsChunkEvent(at_ms=clock(), index=0, text=first)
        clock.advance(2000)
        yield TtsChunkEvent(at_ms=clock(), index=1, text=rest.strip())
        yield ReplyEvent(at_ms=clock(), text=full)

    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        respond=respond,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "what's the weather"})
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    kinds = [event.type for event in events]
    assert kinds.index("tts_chunk") < kinds.index("reply")
    latency = next(event for event in events if isinstance(event, LatencyEvent))
    assert latency.metric == "ttfa"
    assert latency.ms <= 800
    first_chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert first_chunk.text == first
    assert first_chunk.text != full
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == full


async def test_live_session_echo_asr_does_not_swallow_speech_silently() -> None:
    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        transcriber=EchoTranscriber(),
        respond=None,
        asr_partial_interval_ms=50,
        backchannel_enabled=False,
    )
    await session.handle_client(_speech_pcm(seconds=0.3))
    clock.advance(80)
    await session.handle_client(_speech_pcm(seconds=0.2))
    await asyncio.sleep(0.08)
    await session.handle_client(_silence_pcm())
    await asyncio.sleep(0.15)
    events = await _drain(session)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors, "echo ASR must emit a visible error, not drop PCM silently"
    assert any(
        event.code in {"asr_unusable", "asr_echo_no_audio", "asr_unavailable"}
        for event in errors
    )
    assert not any(isinstance(event, ReplyEvent) for event in events)


async def test_live_session_pcm_without_transcriber_is_not_silent() -> None:
    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        transcriber=None,
        backchannel_enabled=False,
    )
    await session.handle_client(_speech_pcm(seconds=0.25))
    events = await _drain(session)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert errors[0].code == "asr_unavailable"
    assert errors[0].fatal is False


def test_macos_live_conversation_still_streams_pcm() -> None:
    """EV.app opens live on launch and streams 16 kHz PCM (cannot play the app here)."""

    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "macos" / "Sources" / "EV" / "LiveConversation.swift"
    assert source.is_file(), source
    text = source.read_text(encoding="utf-8")
    assert "openLiveVoice" in text
    assert "enqueuePCM" in text
    assert "LiveVoiceMicrophone" in text
    assert "WS /v1/voice/live" in text or "connect(sessionId" in text


def test_live_transcriber_never_returns_none_on_echo() -> None:
    from app.voice.live.transport import live_transcriber

    wrapped = resolve_live_transcriber(EchoTranscriber())
    assert isinstance(wrapped, LivePcmTranscriber)
    attached = live_transcriber()
    assert attached is not None


class _EmptyThenPhrase:
    """Stock-path fallback: first clip empty, later clips are a real phrase."""

    name = "empty-then-phrase"

    def __init__(self, text: str = "What's next on my calendar.") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, **kwargs) -> Transcript:
        assert kwargs.get("audio_b64"), "stock wrapper must feed PCM"
        self.calls += 1
        if self.calls == 1:
            raise VoiceError("empty short clip", status=502, code="asr_empty_result")
        return Transcript(text=self.text, confidence=0.91, language="en", provider=self.name)


def _stock_echo_wrapper(fallback) -> LivePcmTranscriber:
    engine = resolve_live_transcriber(EchoTranscriber())
    assert isinstance(engine, LivePcmTranscriber)
    engine._fallback_factory = None
    engine._fallback = fallback
    return engine


async def test_stock_echo_wrapper_empty_then_phrase_commits_and_speaks() -> None:
    """resolve_live_transcriber(echo) + empty-then-text fallback: later phrase commits."""

    heard: list[str] = []
    full = "Right now it is mostly clear tonight leftover. Sun is on the west side."
    first, rest = pop_speakable(full)
    assert first is not None and rest.strip() and first != full

    async def respond(text: str, envelope):
        heard.append(text)
        yield TtsChunkEvent(at_ms=0, index=0, text=first)
        yield TtsChunkEvent(at_ms=0, index=1, text=rest.strip())
        yield ReplyEvent(at_ms=1, text=full)

    fallback = _EmptyThenPhrase()
    clock = ManualClock(0)
    session = LiveSession(
        engine=LiveEngine(clock_ms=clock),
        transcriber=_stock_echo_wrapper(fallback),
        respond=respond,
        asr_partial_interval_ms=50,
        backchannel_enabled=False,
    )
    await session.handle_client(_speech_pcm(seconds=0.35))
    clock.advance(350)
    await asyncio.sleep(0.12)
    await session.handle_client(_speech_pcm(seconds=0.35))
    clock.advance(350)
    await asyncio.sleep(0.15)
    await session.handle_client(_silence_pcm())
    await asyncio.sleep(0.15)
    clock.advance(500)
    await session.tick()
    if session._respond_task is not None:
        await session._respond_task
    events = await _drain(session)
    types = [event.type for event in events]
    assert fallback.calls >= 2, fallback.calls
    assert "final_transcript" in types
    final = next(event for event in events if isinstance(event, FinalTranscriptEvent))
    assert final.text.rstrip(".!?") == "What's next on my calendar"
    assert heard
    assert heard[0].rstrip(".!?") == "What's next on my calendar"
    assert "tts_chunk" in types
    assert types.index("tts_chunk") < types.index("reply")
    latency = next(event for event in events if isinstance(event, LatencyEvent))
    assert latency.metric == "ttfa"
    assert latency.ms <= 800
    first_chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert first_chunk.text == first
    assert first_chunk.text != full
    reply = next(event for event in events if isinstance(event, ReplyEvent))
    assert reply.text == full
