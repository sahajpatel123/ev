"""Interrupt V2 + floor ownership — owner cut-in fusion and exactly-once cancel."""

from __future__ import annotations

from app.voice.live.engine import LiveEngine, ManualClock
from app.voice.live.events import BargeInEvent, FloorEvent
from app.voice.live.floor import (
    FLOOR_CONTESTED,
    FLOOR_EVE_HOLDS,
    FLOOR_IDLE,
    FLOOR_OWNER_HOLDS,
    FLOOR_YIELDING,
    FloorTracker,
)
from app.voice.live.interrupt_v2 import (
    AMBIGUOUS,
    IGNORED,
    OWNER_CONFIRMED,
    SELF,
    InterruptLatch,
    fuse_owner_evidence,
    parse_owner_evidence,
)
from app.voice.live.session import LiveSession


def _evidence(**overrides) -> dict:
    frame = {
        "type": "owner_evidence",
        "speech_ms": 400,
        "confidence": 0.9,
        "aec_active": True,
        "playback_active": True,
        "audio_played_ms": 1200,
        "preroll_ms": 800,
        "echo_score": 0.1,
        "mic_rms": 0.05,
        "response_id": "resp-1",
    }
    frame.update(overrides)
    return frame


# --- floor tracker ---------------------------------------------------- #


def test_floor_owner_speech_claims_idle_floor() -> None:
    floor = FloorTracker()
    moved = floor.note_owner_speech_start(now_ms=100)
    assert moved is not None
    assert floor.floor == FLOOR_OWNER_HOLDS
    assert floor.owner_holds()


def test_floor_overlap_contests_eve_floor_and_restores() -> None:
    floor = FloorTracker()
    floor.note_eve_speech_start(now_ms=100)
    assert floor.floor == FLOOR_EVE_HOLDS
    contested = floor.note_owner_speech_start(now_ms=200)
    assert contested is not None
    assert floor.floor == FLOOR_CONTESTED
    restored = floor.note_owner_speech_end(now_ms=300)
    assert restored is not None
    assert floor.floor == FLOOR_EVE_HOLDS


def test_floor_confirmed_cut_in_yields_then_holds() -> None:
    floor = FloorTracker()
    floor.note_eve_speech_start(now_ms=100)
    floor.note_owner_cut_in(now_ms=200)
    assert floor.floor == FLOOR_OWNER_HOLDS
    reasons = [item.reason for item in floor.transitions]
    assert "owner_cut_in_yield" in reasons
    assert "owner_cut_in_confirmed" in reasons
    assert floor.transitions[-2].current == FLOOR_YIELDING


def test_floor_eve_cut_in_and_turn_commit() -> None:
    floor = FloorTracker()
    floor.note_owner_speech_start(now_ms=100)
    floor.note_eve_cut_in(now_ms=200)
    assert floor.floor == FLOOR_EVE_HOLDS
    floor.note_eve_speech_end(now_ms=300)
    assert floor.floor == FLOOR_IDLE
    floor.note_owner_speech_start(now_ms=400)
    floor.note_turn_committed(now_ms=500)
    assert floor.floor == FLOOR_EVE_HOLDS


# --- fusion ----------------------------------------------------------- #


def test_fusion_confirms_clean_owner_evidence() -> None:
    verdict = fuse_owner_evidence(parse_owner_evidence(_evidence()), eve_speaking=True)
    assert verdict.verdict == OWNER_CONFIRMED
    assert verdict.confirmed


def test_fusion_ignores_evidence_while_eve_silent() -> None:
    verdict = fuse_owner_evidence(parse_owner_evidence(_evidence()), eve_speaking=False)
    assert verdict.verdict == IGNORED
    assert not verdict.confirmed


def test_fusion_marks_echo_as_self() -> None:
    verdict = fuse_owner_evidence(
        parse_owner_evidence(_evidence(echo_score=0.8)), eve_speaking=True
    )
    assert verdict.verdict == SELF


def test_fusion_never_confirms_ambiguous_frames() -> None:
    cases = [
        _evidence(speech_ms=40),
        _evidence(confidence=0.2),
        _evidence(aec_active=False),
        _evidence(mic_rms=0.9),
        _evidence(mic_rms=0.0001),
        {},
        None,
    ]
    for raw in cases:
        verdict = fuse_owner_evidence(parse_owner_evidence(raw), eve_speaking=True)
        assert verdict.verdict == AMBIGUOUS, raw
        assert not verdict.confirmed


def test_fusion_accepts_aec_client_confirmation_without_score() -> None:
    frame = _evidence()
    frame.pop("confidence")
    frame["client_confirmed"] = True
    verdict = fuse_owner_evidence(parse_owner_evidence(frame), eve_speaking=True)
    assert verdict.verdict == OWNER_CONFIRMED


def test_fusion_holds_no_aec_clients_to_a_higher_bar() -> None:
    strong = _evidence(aec_active=False, confidence=0.7, client_confirmed=True)
    assert (
        fuse_owner_evidence(parse_owner_evidence(strong), eve_speaking=True).verdict
        == OWNER_CONFIRMED
    )
    weak = _evidence(aec_active=False, confidence=0.55, client_confirmed=True)
    assert (
        fuse_owner_evidence(parse_owner_evidence(weak), eve_speaking=True).verdict
        == AMBIGUOUS
    )
    scoreless = _evidence(aec_active=False, client_confirmed=True)
    scoreless.pop("confidence")
    assert (
        fuse_owner_evidence(parse_owner_evidence(scoreless), eve_speaking=True).verdict
        == AMBIGUOUS
    )


def test_fusion_rejects_unconfirmed_scoreless_frames() -> None:
    frame = _evidence()
    frame.pop("confidence")
    verdict = fuse_owner_evidence(parse_owner_evidence(frame), eve_speaking=True)
    assert verdict.verdict == AMBIGUOUS


def test_latch_claims_each_response_once() -> None:
    latch = InterruptLatch()
    assert latch.claim("resp-1") is True
    assert latch.claim("resp-1") is False
    assert latch.claim("resp-2") is True


# --- engine ----------------------------------------------------------- #


def test_engine_confirmed_evidence_barges_in() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=100)

    events, verdict = engine.push_owner_evidence(_evidence(), now_ms=200)
    assert verdict.confirmed
    assert len(events) == 1
    barged = events[0]
    assert isinstance(barged, BargeInEvent)
    assert barged.reason == "owner_cut_in_v2"
    assert barged.audio_played_ms == 1200
    assert barged.confidence == 0.9
    assert barged.preroll_ms == 800
    assert barged.provider_response_id == "resp-1"
    assert engine.state.interruption_state == "barged_in"
    assert engine.floor.floor == FLOOR_OWNER_HOLDS


def test_engine_unconfirmed_evidence_is_silent() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=100)

    events, verdict = engine.push_owner_evidence(_evidence(confidence=0.1), now_ms=200)
    assert not verdict.confirmed
    assert events == []
    assert engine.state.assistant_is_speaking is True
    assert engine.floor.floor == FLOOR_EVE_HOLDS


def test_engine_tick_drains_floor_transitions() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=100)

    tick = engine.tick(now_ms=200)
    floors = [event for event in tick.events if isinstance(event, FloorEvent)]
    assert [event.floor for event in floors] == [FLOOR_EVE_HOLDS]
    assert floors[0].previous == FLOOR_IDLE

    again = engine.tick(now_ms=300)
    assert [event for event in again.events if isinstance(event, FloorEvent)] == []


# --- session ---------------------------------------------------------- #


async def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


async def test_session_confirmed_cut_in_cancels_speech_once() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    engine.push_assistant_speaking(True, now_ms=100)

    await session.handle_client(_evidence())
    events = await _drain(session)
    barged = [event for event in events if isinstance(event, BargeInEvent)]
    assert len(barged) == 1
    assert barged[0].reason == "owner_cut_in_v2"
    floors = [event for event in events if isinstance(event, FloorEvent)]
    assert floors, "a confirmed cut-in must move the floor"
    assert floors[-1].floor == FLOOR_OWNER_HOLDS

    # A duplicate frame for the same response claims nothing new.
    await session.handle_client(_evidence())
    again = await _drain(session)
    assert [event for event in again if isinstance(event, BargeInEvent)] == []


async def test_session_ambiguous_evidence_is_silent() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    engine.push_assistant_speaking(True, now_ms=100)

    await session.handle_client(_evidence(echo_score=0.9))
    events = await _drain(session)
    assert [event for event in events if isinstance(event, BargeInEvent)] == []
    assert engine.state.assistant_is_speaking is True


async def test_session_drops_evidence_while_muted() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    engine.push_assistant_speaking(True, now_ms=100)
    await session.handle_client({"type": "control", "action": "mute"})
    await _drain(session)

    await session.handle_client(_evidence())
    events = await _drain(session)
    assert [event for event in events if isinstance(event, BargeInEvent)] == []


class _FakeBridge:
    def __init__(self) -> None:
        self.interrupts: list[dict] = []
        self._playback_active = True
        self._assistant_open = True
        self._response_active = True

    def set_playback(self, active: bool) -> None:
        self._playback_active = bool(active)

    async def interrupt_for_user(self, **kwargs) -> dict:
        self.interrupts.append(kwargs)
        return {"latched": True}


async def test_session_live_path_cancels_bridge_exactly_once() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    bridge = _FakeBridge()
    session = LiveSession(engine=engine, backchannel_enabled=False, gemini_live=bridge)
    await session.handle_client({"type": "playback", "active": True})

    await session.handle_client(_evidence())
    assert len(bridge.interrupts) == 1
    assert bridge.interrupts[0]["reason"] == "owner_cut_in_v2"
    assert bridge.interrupts[0]["audio_played_ms"] == 1200

    await session.handle_client(_evidence())
    assert len(bridge.interrupts) == 1


async def test_session_live_path_gates_raw_speech_during_playback() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    bridge = _FakeBridge()
    session = LiveSession(engine=engine, backchannel_enabled=False, gemini_live=bridge)
    await session.handle_client({"type": "playback", "active": True})
    await _drain(session)

    # Echo-unsafe VAD must not forge engine speech or a barge-in.
    await session.handle_client({"type": "speech", "active": True})
    events = await _drain(session)
    assert [event for event in events if isinstance(event, BargeInEvent)] == []
    assert engine.state.user_is_speaking is False
