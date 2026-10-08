"""Eve-initiated cut-in — policy gates, floor take/yield-back, marked speech."""

from __future__ import annotations

from app.voice.live.engine import LiveEngine, ManualClock
from app.voice.live.eve_cutin import (
    TRIGGER_INVITED,
    TRIGGER_KNOWN_ANSWER,
    TRIGGER_SAFETY,
    TRIGGER_TIMER_DUE,
    TRIGGER_URGENT_ALERT,
    CutInPolicy,
    detect_invitation,
)
from app.voice.live.events import FloorEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.floor import (
    FLOOR_EVE_HOLDS,
    FLOOR_IDLE,
    FLOOR_OWNER_HOLDS,
)
from app.voice.live.layer import _try_eve_cut_in
from app.voice.live.session import LiveSession
from app.voice.live.state import (
    LISTEN_ATTENTIVE,
    LISTEN_PASSIVE,
    LISTEN_QUIET,
    LiveConversationState,
)


def _owner_speaking(**overrides) -> LiveConversationState:
    state = LiveConversationState()
    state.user_is_speaking = True
    state.assistant_is_speaking = False
    state.listening_mode = LISTEN_ATTENTIVE
    state.emotional_context = "neutral"
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# --- policy ----------------------------------------------------------- #


def test_safety_cuts_in_everywhere() -> None:
    policy = CutInPolicy()
    for mode in (LISTEN_ATTENTIVE, LISTEN_QUIET, LISTEN_PASSIVE):
        for emotion in ("neutral", "sad", "frustrated", "urgent"):
            decision = policy.decide(
                _owner_speaking(listening_mode=mode, emotional_context=emotion),
                TRIGGER_SAFETY,
                now_ms=0,
                last_cut_in_ms=0,
                invited_armed=False,
            )
            assert decision.allowed, (mode, emotion)


def test_timer_and_alert_need_attentive_mode() -> None:
    policy = CutInPolicy()
    for trigger in (TRIGGER_TIMER_DUE, TRIGGER_URGENT_ALERT):
        allowed = policy.decide(
            _owner_speaking(),
            trigger,
            now_ms=60_000,
            last_cut_in_ms=None,
            invited_armed=False,
        )
        assert allowed.allowed
        for mode in (LISTEN_QUIET, LISTEN_PASSIVE):
            denied = policy.decide(
                _owner_speaking(listening_mode=mode),
                trigger,
                now_ms=60_000,
                last_cut_in_ms=None,
                invited_armed=False,
            )
            assert not denied.allowed, (trigger, mode)


def test_emotional_space_denies_non_safety() -> None:
    policy = CutInPolicy()
    for emotion in ("sad", "frustrated"):
        denied = policy.decide(
            _owner_speaking(emotional_context=emotion),
            TRIGGER_TIMER_DUE,
            now_ms=60_000,
            last_cut_in_ms=None,
            invited_armed=False,
        )
        assert not denied.allowed
        assert denied.reason == "emotional_space"


def test_invited_needs_arming_and_known_answer_needs_grounding() -> None:
    policy = CutInPolicy()
    denied = policy.decide(
        _owner_speaking(),
        TRIGGER_INVITED,
        now_ms=60_000,
        last_cut_in_ms=None,
        invited_armed=False,
    )
    assert denied.reason == "invitation_not_armed"
    armed = policy.decide(
        _owner_speaking(),
        TRIGGER_INVITED,
        now_ms=60_000,
        last_cut_in_ms=None,
        invited_armed=True,
    )
    assert armed.allowed
    ungrounded = policy.decide(
        _owner_speaking(),
        TRIGGER_KNOWN_ANSWER,
        now_ms=60_000,
        last_cut_in_ms=None,
        invited_armed=False,
        grounded=False,
    )
    assert ungrounded.reason == "answer_not_grounded"
    grounded = policy.decide(
        _owner_speaking(),
        TRIGGER_KNOWN_ANSWER,
        now_ms=60_000,
        last_cut_in_ms=None,
        invited_armed=False,
        grounded=True,
    )
    assert grounded.allowed


def test_cooldown_and_floor_and_unknown_trigger() -> None:
    policy = CutInPolicy()
    assert not policy.decide(
        _owner_speaking(),
        TRIGGER_TIMER_DUE,
        now_ms=10_000,
        last_cut_in_ms=0,
        invited_armed=False,
    ).allowed
    assert policy.decide(
        _owner_speaking(),
        TRIGGER_TIMER_DUE,
        now_ms=60_000,
        last_cut_in_ms=0,
        invited_armed=False,
    ).allowed
    idle = LiveConversationState()
    assert policy.decide(
        idle, TRIGGER_TIMER_DUE, now_ms=0, last_cut_in_ms=None, invited_armed=False
    ).reason == "no_owner_floor"
    assert policy.decide(
        _owner_speaking(), "telepathy", now_ms=0, last_cut_in_ms=None, invited_armed=False
    ).reason == "unknown_trigger"


def test_detect_invitation_phrases() -> None:
    assert detect_invitation("stop me if you know the answer")
    assert detect_invitation("Feel free to cut me off if I'm wrong")
    assert detect_invitation("jump in if you know this one")
    assert not detect_invitation("tell me about timers")
    assert not detect_invitation("")
    assert not detect_invitation(None)


# --- engine ----------------------------------------------------------- #


def test_engine_cut_in_takes_and_yields_floor() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    assert engine.floor.floor == FLOOR_OWNER_HOLDS

    decision = engine.consider_eve_cut_in(TRIGGER_TIMER_DUE, now_ms=200)
    assert decision.allowed
    assert engine.floor.floor == FLOOR_EVE_HOLDS

    engine.note_eve_cut_in_end(now_ms=300)
    assert engine.floor.floor == FLOOR_OWNER_HOLDS


def test_engine_cut_in_denied_leaves_floor() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    engine.set_listening_mode(LISTEN_PASSIVE)

    decision = engine.consider_eve_cut_in(TRIGGER_TIMER_DUE, now_ms=200)
    assert not decision.allowed
    assert engine.floor.floor == FLOOR_OWNER_HOLDS


def test_engine_invitation_arms_one_shot() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_transcript("stop me if you know this", now_ms=100)
    engine.push_speech(True, now_ms=200)

    first = engine.consider_eve_cut_in(TRIGGER_INVITED, now_ms=300)
    assert first.allowed
    engine.note_eve_cut_in_end(now_ms=400)
    clock.advance(60_000)
    second = engine.consider_eve_cut_in(TRIGGER_INVITED, now_ms=clock())
    assert not second.allowed
    assert second.reason == "invitation_not_armed"


# --- session ---------------------------------------------------------- #


async def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


def _speaking_session() -> tuple[LiveSession, LiveEngine]:
    clock = ManualClock(1_000)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    engine.push_speech(True, now_ms=1_000)
    return session, engine


async def test_session_cut_in_speaks_marked_line(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    session, engine = _speaking_session()

    spoken = await session.speak_eve_cut_in("Your timer is done.", trigger=TRIGGER_TIMER_DUE)
    assert spoken is True
    events = await _drain(session)

    chunks = [event for event in events if isinstance(event, TtsChunkEvent)]
    assert len(chunks) == 1
    assert chunks[0].eve_cut_in is True
    assert chunks[0].text == "Your timer is done."
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].eve_cut_in is True
    assert replies[0].cut_in_trigger == TRIGGER_TIMER_DUE
    floors = [event for event in events if isinstance(event, FloorEvent)]
    assert [event.floor for event in floors].count(FLOOR_EVE_HOLDS) >= 1
    assert floors[-1].floor == FLOOR_OWNER_HOLDS
    assert engine.floor.floor == FLOOR_OWNER_HOLDS


async def test_session_cut_in_respects_mute_and_quiet_hours(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    session, _engine = _speaking_session()
    await session.handle_client({"type": "control", "action": "mute"})
    assert await session.speak_eve_cut_in("Hi.", trigger=TRIGGER_TIMER_DUE) is False

    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: True)
    session2, _engine2 = _speaking_session()
    assert await session2.speak_eve_cut_in("Hi.", trigger=TRIGGER_TIMER_DUE) is False
    assert (
        await session2.speak_eve_cut_in("Hi.", trigger=TRIGGER_SAFETY, emergency=True)
        is True
    )


async def test_session_cut_in_honors_cooldown(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    session, _engine = _speaking_session()
    assert await session.speak_eve_cut_in("One.", trigger=TRIGGER_TIMER_DUE) is True
    assert await session.speak_eve_cut_in("Two.", trigger=TRIGGER_TIMER_DUE) is False


class _CutInBridge:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self._playback_active = False

    async def speak_ack(self, text: str) -> bool:
        self.acks.append(text)
        return True


async def test_session_cut_in_uses_bridge_voice(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    clock = ManualClock(1_000)
    engine = LiveEngine(clock_ms=clock)
    bridge = _CutInBridge()
    session = LiveSession(engine=engine, backchannel_enabled=False, gemini_live=bridge)
    engine.push_speech(True, now_ms=1_000)

    assert await session.speak_eve_cut_in("Stop.", trigger=TRIGGER_SAFETY) is True
    assert bridge.acks == ["Stop."]
    events = await _drain(session)
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert replies and replies[0].eve_cut_in is True


async def test_session_proactive_routes_cut_in_when_owner_speaks(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    session, _engine = _speaking_session()
    await session.speak_proactive("Timer done.", cut_in_trigger=TRIGGER_TIMER_DUE)
    events = await _drain(session)
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].eve_cut_in is True


async def test_try_eve_cut_in_only_qualifies_time_critical() -> None:
    class _Live:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def speak_eve_cut_in(self, text: str, **kwargs) -> bool:
            self.calls.append({"text": text, **kwargs})
            return True

    live = _Live()
    assert await _try_eve_cut_in(live, "Fire.", emergency=True, owner_scheduled=False) is True
    assert live.calls[-1]["trigger"] == TRIGGER_SAFETY
    assert await _try_eve_cut_in(live, "Timer.", emergency=False, owner_scheduled=True) is True
    assert live.calls[-1]["trigger"] == TRIGGER_TIMER_DUE
    assert await _try_eve_cut_in(live, "News.", emergency=False, owner_scheduled=False) is False
    assert await _try_eve_cut_in(object(), "Fire.", emergency=True, owner_scheduled=False) is False


def test_floor_yield_back_after_idle_cue() -> None:
    from app.voice.live.floor import FloorTracker

    floor = FloorTracker()
    floor.note_eve_speech_start(now_ms=100)
    floor.note_eve_speech_end(now_ms=200)
    assert floor.floor == FLOOR_IDLE
    moved = floor.note_yield_back_to_owner(now_ms=300)
    assert moved is not None
    assert floor.floor == FLOOR_OWNER_HOLDS
