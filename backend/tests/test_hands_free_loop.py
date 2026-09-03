"""Hands-free state machine: wake gating, endpointing, follow-up, barge-in.

Every collaborator is a fake and every deadline is measured in samples fed, so
the whole file is deterministic and needs no speech model. Silence is a block
of zeros, "speech" is any nonzero pattern, and the fake VAD reads exactly that.
"""

from __future__ import annotations

import array
from dataclasses import dataclass, replace

import pytest

from app.audio.vad import SileroVadOnnx
from app.voice.hands_free_loop import (
    LiveConfig,
    LiveEvent,
    LiveReply,
    LiveState,
    LiveTurn,
    LiveVoiceLoop,
    classify_turn,
    strip_wake_prefix,
)
from app.voice.vosk_engine import WakeSignal

SAMPLE_RATE = 16000
FRAME_MS = 20


@pytest.fixture(autouse=True)
def fresh_db():
    """Nothing here touches the database; skip the per-test schema rebuild."""

    yield


def silence(ms: int) -> bytes:
    return array.array("h", [0] * (SAMPLE_RATE * ms // 1000)).tobytes()


def speech(ms: int) -> bytes:
    return array.array("h", [9000, -9000] * (SAMPLE_RATE * ms // 2000)).tobytes()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeVad:
    """Any nonzero sample is speech; a block of zeros is silence."""

    name = "fake-vad"

    async def block_probability(self, samples, sample_rate: int) -> float:
        return 1.0 if any(samples) else 0.0


@dataclass
class FakeResult:
    text: str
    confidence: float = 0.87


class FakeRecognizer:
    """Canned final transcript plus scripted partial hypotheses."""

    name = "fake-asr"

    def __init__(self, text: str, partials: list[str]) -> None:
        self.text = text
        self.partials = list(partials)
        self.calls = 0

    def feed(self, pcm: bytes) -> str | None:
        self.calls += 1
        # The first call is the ring-buffer backlog the loop replays on capture.
        if self.calls > 1 and self.partials:
            return self.partials.pop(0)
        return None

    def final(self) -> FakeResult:
        return FakeResult(text=self.text)


class RecognizerScript:
    """One canned recognizer per capture, in order; the last text repeats."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts) or [""]
        self.built: list[FakeRecognizer] = []

    def __call__(self) -> FakeRecognizer:
        text = self.texts[min(len(self.built), len(self.texts) - 1)]
        recognizer = FakeRecognizer(text, partials=text.split()[:1])
        self.built.append(recognizer)
        return recognizer


class ScriptedSpotter:
    """Emits chosen wake signals once the stream reaches chosen offsets."""

    def __init__(self, script=(), *, on_flush=()) -> None:
        self.script = [
            (SAMPLE_RATE * at_ms // 1000, signal) for at_ms, signal in script
        ]
        self.on_flush = list(on_flush)
        self.offset = 0
        self.flushes = 0
        self.resets = 0

    def feed(self, pcm: bytes) -> list[WakeSignal]:
        self.offset += len(pcm) // 2
        signals = []
        while self.script and self.script[0][0] <= self.offset:
            _, signal = self.script.pop(0)
            signals.append(replace(signal, end_offset=signal.end_offset or self.offset))
        return signals

    def flush(self) -> list[WakeSignal]:
        self.flushes += 1
        pending, self.on_flush = self.on_flush, []
        return [replace(s, end_offset=s.end_offset or self.offset) for s in pending]

    def reset(self) -> None:
        self.resets += 1


class RecordingResponder:
    def __init__(self, *, reply: str = "Sure.", duration_ms: int = 100, fail_turns=()) -> None:
        self.reply = reply
        self.duration_ms = duration_ms
        self.fail_turns = set(fail_turns)
        self.turns: list[LiveTurn] = []
        self.wakes: list[WakeSignal] = []
        self.interrupts = 0
        self.closed: list[str] = []

    async def open_session(self, *, wake: WakeSignal, wav: bytes) -> dict:
        self.wakes.append(wake)
        return {"session_id": "session-1", "state": "awake", "owner_enrolled": False}

    async def respond(self, turn: LiveTurn) -> LiveReply:
        self.turns.append(turn)
        if len(self.turns) in self.fail_turns:
            raise RuntimeError("responder exploded")
        return LiveReply(
            text=self.reply, duration_ms=self.duration_ms, session_id="session-1"
        )

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def close(self, *, reason: str) -> None:
        self.closed.append(reason)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def wake_at(ms: int, *, phrase: str = "hey evie", confidence: float = 0.93):
    """A pending hit at ``ms`` and the confirmation that follows it.

    Both carry the same ``end_offset`` — the decoder learns *when* the phrase
    ended, not a later position — so confirmation does not restart capture.
    """

    end = SAMPLE_RATE * ms // 1000
    return [
        (ms, WakeSignal(kind="pending", phrase=phrase, end_offset=end)),
        (
            ms + 60,
            WakeSignal(
                kind="confirmed", phrase=phrase, confidence=confidence, end_offset=end
            ),
        ),
    ]


def live_config(**overrides) -> LiveConfig:
    """Short windows so a whole conversation is a few thousand samples."""

    base = dict(
        sample_rate=SAMPLE_RATE,
        frame_ms=FRAME_MS,
        endpoint_silence_ms=100,
        min_speech_ms=40,
        max_utterance_ms=2_000,
        wake_grace_ms=200,
        follow_up_ms=200,
        barge_in_ms=60,
        ring_seconds=5.0,
        level_interval_ms=10_000,
        playback_grace_ms=100,
    )
    base.update(overrides)
    return LiveConfig(**base)


@dataclass
class Harness:
    loop: LiveVoiceLoop
    events: list[LiveEvent]
    responder: RecordingResponder
    spotter: ScriptedSpotter
    recognizers: RecognizerScript

    def types(self) -> list[str]:
        return [event.type for event in self.events if event.type != "level"]

    def of(self, kind: str) -> list[dict]:
        return [event.data for event in self.events if event.type == kind]

    def states(self) -> list[str]:
        return [data["state"] for data in self.of("state")]


def harness(
    *,
    script=(),
    on_flush=(),
    texts=("what did i decide about the project",),
    responder: RecordingResponder | None = None,
    vad=None,
    **config_overrides,
) -> Harness:
    events: list[LiveEvent] = []

    async def emit(event: LiveEvent) -> None:
        events.append(event)

    spotter = ScriptedSpotter(script, on_flush=on_flush)
    recognizers = RecognizerScript(*texts)
    responder = responder or RecordingResponder()
    loop = LiveVoiceLoop(
        responder=responder,
        emit=emit,
        spotter=spotter,
        recognizer_factory=recognizers,
        vad=vad or FakeVad(),
        config=live_config(**config_overrides),
        device_id="test-mic",
    )
    return Harness(loop, events, responder, spotter, recognizers)


async def settle(harness: Harness) -> None:
    """Await the responder task the loop dispatches for a completed turn."""

    task = harness.loop._task
    if task is not None and not task.done():
        await task


async def wake_turn(harness: Harness, *, command_ms: int = 240) -> None:
    """Wake, speak a command, and fall silent; leaves the loop in `speaking`."""

    await harness.loop.feed(silence(60))
    await harness.loop.feed(speech(command_ms))
    await harness.loop.feed(silence(140))
    await settle(harness)


async def follow_up_turn(harness: Harness, *, command_ms: int = 140) -> None:
    await harness.loop.feed(speech(command_ms))
    await harness.loop.feed(silence(140))
    await settle(harness)


# --------------------------------------------------------------------------- #
# Wake → command → reply
# --------------------------------------------------------------------------- #


async def test_wake_then_command_endpoints_and_answers_once() -> None:
    live = harness(script=wake_at(100))

    await wake_turn(live)

    assert live.types() == [
        "wake",
        "state",
        "partial",
        "state",
        "wake",
        "transcript",
        "state",
        "session",
        "reply",
        "state",
    ]
    assert live.states() == [
        LiveState.WAKING,
        LiveState.LISTENING,
        LiveState.THINKING,
        LiveState.SPEAKING,
    ]
    assert [data["stage"] for data in live.of("wake")] == ["pending", "confirmed"]
    assert live.of("partial")[0]["text"] == "what"
    assert live.of("transcript")[0] == {
        "text": "what did i decide about the project",
        "confidence": 0.87,
        "follow_up": False,
        "endpoint_reason": "endpoint",
    }
    assert live.of("reply")[0]["text"] == "Sure."

    turn = live.responder.turns[0]
    assert len(live.responder.turns) == 1
    assert turn.transcript.text == "what did i decide about the project"
    assert turn.follow_up is False
    assert turn.wake is not None and turn.wake.phrase == "hey evie"
    assert turn.wav.startswith(b"RIFF")
    assert live.responder.wakes[0].confidence == 0.93
    assert live.loop.state == LiveState.SPEAKING


async def test_wake_prefix_is_stripped_from_the_request() -> None:
    live = harness(script=wake_at(100), texts=("hey evie what time is it",))

    await wake_turn(live)

    assert live.responder.turns[0].transcript.text == "what time is it"


# --------------------------------------------------------------------------- #
# False-trigger guards
# --------------------------------------------------------------------------- #


async def test_unconfirmed_wake_discards_the_turn() -> None:
    """A pending hit the decoder never confirms must not reach the responder."""

    live = harness(
        script=[(100, WakeSignal(kind="pending", phrase="evie"))], on_flush=()
    )

    await wake_turn(live)

    assert live.spotter.flushes == 1
    assert live.of("dismissed") == [{"reason": "wake_not_confirmed"}]
    assert live.responder.turns == []
    assert live.responder.wakes == []
    assert live.types() == ["wake", "state", "partial", "dismissed", "state"]
    assert live.loop.state == LiveState.IDLE


async def test_rejected_wake_signal_discards_the_turn() -> None:
    live = harness(
        script=[
            (100, WakeSignal(kind="pending", phrase="evie")),
            (160, WakeSignal(kind="rejected", text="heavy rain")),
        ]
    )

    await wake_turn(live)

    assert live.of("dismissed") == [
        {"reason": "wake_not_confirmed", "heard": "heavy rain"}
    ]
    assert live.responder.turns == []
    assert live.loop.state == LiveState.IDLE


async def test_speech_without_a_wake_word_never_reaches_the_responder() -> None:
    live = harness(script=())

    await wake_turn(live, command_ms=400)

    assert live.types() == []
    assert live.responder.turns == []
    assert live.loop.state == LiveState.IDLE


async def test_bare_wake_without_a_command_times_out() -> None:
    live = harness(script=[(60, WakeSignal(kind="pending", phrase="evie"))])

    await live.loop.feed(silence(400))

    assert live.of("dismissed") == [{"reason": "no_command_after_wake"}]
    assert live.of("conversation_end") == [{"reason": "no_command"}]
    assert live.responder.turns == []
    assert live.loop.state == LiveState.IDLE


# --------------------------------------------------------------------------- #
# Follow-up window
# --------------------------------------------------------------------------- #


async def test_follow_up_needs_no_wake_word() -> None:
    live = harness(
        script=wake_at(100), texts=("what did i decide", "and what about tomorrow")
    )

    await wake_turn(live)
    await live.loop.playback_finished()
    assert live.loop.state == LiveState.FOLLOW_UP
    await follow_up_turn(live)

    assert [turn.transcript.text for turn in live.responder.turns] == [
        "what did i decide",
        "and what about tomorrow",
    ]
    assert [turn.follow_up for turn in live.responder.turns] == [False, True]
    assert live.of("transcript")[1]["follow_up"] is True
    assert live.spotter.flushes == 0
    assert live.loop.state == LiveState.SPEAKING


async def test_follow_up_window_closes_itself_in_silence() -> None:
    live = harness(script=wake_at(100))

    await wake_turn(live)
    await live.loop.playback_finished()
    await live.loop.feed(silence(240))

    assert live.of("conversation_end") == [{"reason": "follow_up_timeout"}]
    assert live.responder.closed == ["follow_up_timeout"]
    assert live.spotter.resets == 1
    assert live.loop.state == LiveState.IDLE


async def test_playback_budget_reopens_the_mic_without_a_client_report() -> None:
    """A client that never sends `playback_finished` must not strand the turn."""

    live = harness(script=wake_at(100))

    await wake_turn(live)
    assert live.loop.state == LiveState.SPEAKING
    # reply duration 100 ms + 100 ms grace, so 240 ms of silence overruns it.
    await live.loop.feed(silence(240))

    assert live.loop.state == LiveState.FOLLOW_UP
    assert live.of("conversation_end") == []


# --------------------------------------------------------------------------- #
# Barge-in
# --------------------------------------------------------------------------- #


async def test_speech_during_playback_barges_in() -> None:
    live = harness(script=wake_at(100), texts=("what did i decide", "stop the timer"))

    await wake_turn(live)
    await live.loop.feed(speech(80))

    assert live.of("barge_in") == [{"reason": "owner_spoke"}]
    assert live.responder.interrupts == 1
    assert live.loop.state == LiveState.LISTENING
    assert live.states()[-1] == LiveState.LISTENING

    await live.loop.feed(silence(140))
    await settle(live)
    assert live.responder.turns[1].transcript.text == "stop the timer"


# --------------------------------------------------------------------------- #
# Speech that is not a request
# --------------------------------------------------------------------------- #


async def test_dismissal_in_a_follow_up_ends_the_conversation() -> None:
    live = harness(script=wake_at(100), texts=("what did i decide", "never mind stop"))

    await wake_turn(live)
    await live.loop.playback_finished()
    await follow_up_turn(live)

    assert live.of("dismissed") == [
        {"reason": "dismissed_by_user", "text": "never mind stop"}
    ]
    assert live.of("conversation_end") == [{"reason": "user_dismissed"}]
    assert len(live.responder.turns) == 1
    assert live.responder.closed == ["user_dismissed"]
    assert live.loop.state == LiveState.IDLE


async def test_acknowledgement_in_a_follow_up_is_not_addressed_to_evie() -> None:
    live = harness(script=wake_at(100), texts=("what did i decide", "mm hmm"))

    await wake_turn(live)
    await live.loop.playback_finished()
    await follow_up_turn(live)

    assert live.of("dismissed") == [
        {"reason": "not_addressed_to_evie", "text": "mm hmm"}
    ]
    assert len(live.responder.turns) == 1
    assert live.loop.state == LiveState.FOLLOW_UP


async def test_acknowledgement_after_a_wake_word_is_answered() -> None:
    """The wake word is the proof it was addressed to EVIE, so "yeah" is a turn."""

    live = harness(script=wake_at(100), texts=("yeah",))

    await wake_turn(live)

    assert [turn.transcript.text for turn in live.responder.turns] == ["yeah"]
    assert live.of("dismissed") == []


async def test_empty_transcript_is_dismissed_without_a_turn() -> None:
    live = harness(script=wake_at(100), texts=("",))

    await wake_turn(live)

    assert live.of("dismissed") == [{"reason": "no_speech_recognized"}]
    assert live.responder.turns == []
    assert live.loop.state == LiveState.IDLE


# --------------------------------------------------------------------------- #
# Failures and telemetry
# --------------------------------------------------------------------------- #


async def test_responder_failure_surfaces_an_error_and_keeps_the_stream_alive() -> None:
    live = harness(
        script=[*wake_at(100), *wake_at(480)],
        texts=("what did i decide", "and what about tomorrow"),
        responder=RecordingResponder(fail_turns=(1,)),
    )

    await wake_turn(live)

    assert live.of("error") == [{"code": "turn_failed", "message": "responder exploded"}]
    assert live.loop.state == LiveState.IDLE

    await wake_turn(live)

    assert [turn.transcript.text for turn in live.responder.turns] == [
        "what did i decide",
        "and what about tomorrow",
    ]
    assert live.of("reply")[0]["text"] == "Sure."
    assert live.loop.state == LiveState.SPEAKING


async def test_a_streaming_vad_that_defers_its_decision_keeps_the_loop_alive() -> None:
    """Silero returns None until it has 512 samples; 20 ms frames are 320."""

    live = harness(script=(), vad=SileroVadOnnx(session_factory=lambda: object()))

    await live.loop.feed(speech(FRAME_MS))

    assert live.loop.state == LiveState.IDLE


class HoldingVad:
    """Returns None on odd calls, then a real probability — like Silero."""

    name = "holding"

    def __init__(self) -> None:
        self.calls = 0

    async def block_probability(self, samples, sample_rate: int) -> float | None:
        self.calls += 1
        if self.calls % 2 == 1:
            return None
        return 1.0 if any(samples) else 0.0


async def test_deferred_vad_holds_the_last_speech_decision() -> None:
    vad = HoldingVad()
    live = harness(script=(), vad=vad, level_interval_ms=20)

    await live.loop.feed(speech(FRAME_MS))
    await live.loop.feed(speech(FRAME_MS))
    await live.loop.feed(speech(FRAME_MS))

    assert [data["speech"] for data in live.of("level")] == [False, True, True]


async def test_level_events_prove_the_microphone_is_live() -> None:
    live = harness(script=(), level_interval_ms=40)

    await live.loop.feed(speech(100))
    await live.loop.feed(silence(100))

    levels = live.of("level")
    assert [data["speech"] for data in levels] == [True, True, False, False, False]
    assert levels[0]["level"] > 0.5
    assert levels[-1]["level"] == 0.0
    assert {data["state"] for data in levels} == {LiveState.IDLE}


# --------------------------------------------------------------------------- #
# Text classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("what did i decide about the project", "request"),
        ("remind me to call mom at six", "request"),
        ("no thanks i will do it myself", "request"),
        ("mm hmm", "acknowledgement"),
        ("yeah", "acknowledgement"),
        ("okay", "acknowledgement"),
        ("uh huh", "acknowledgement"),
        ("never mind, stop", "dismissal"),
        ("thanks", "dismissal"),
        ("that's all evie", "dismissal"),
        ("stop listening", "dismissal"),
        ("", "empty"),
        ("   ...  ", "empty"),
    ],
)
def test_classify_turn(text: str, kind: str) -> None:
    assert classify_turn(text) == kind


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        ("Hey Evie, what did I decide?", "what did I decide?"),
        ("hi evie remind me", "remind me"),
        ("hello evie. remind me", "remind me"),
        ("ok evie remind me", "remind me"),
        ("Okay Evie, remind me", "remind me"),
        ("yo evie remind me", "remind me"),
        ("evie what time is it", "what time is it"),
        ("evie", ""),
        ("every day is a good day", "every day is a good day"),
        ("hey there evie is not a prefix", "hey there evie is not a prefix"),
        ("", ""),
    ],
)
def test_strip_wake_prefix(text: str, stripped: str) -> None:
    assert strip_wake_prefix(text) == stripped
