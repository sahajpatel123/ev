"""Gesture envelope — shared vocabulary, engine drain, one-shot hints."""

from __future__ import annotations

from app.voice.live.engine import LiveEngine, ManualClock
from app.voice.live.eve_cutin import TRIGGER_INVITED, TRIGGER_TIMER_DUE
from app.voice.live.events import GestureEvent
from app.voice.live.gesture import (
    GESTURE_CONTESTED,
    GESTURE_IDLE,
    GESTURE_LISTENING,
    GESTURE_SPEAKING,
    GESTURE_THINKING,
    GESTURE_URGENT,
    GESTURE_YIELD_BACK,
    GESTURE_YIELDING,
    gesture_for,
)


def _gestures(tick) -> list[GestureEvent]:
    return [event for event in tick.events if isinstance(event, GestureEvent)]


def test_mapper_covers_floor_phase_emotion_hint() -> None:
    assert gesture_for(floor="idle", phase="listening").gesture == GESTURE_IDLE
    assert gesture_for(floor="idle", phase="thinking").gesture == GESTURE_THINKING
    assert gesture_for(floor="owner_holds", phase="user_speaking").gesture == GESTURE_LISTENING
    sad = gesture_for(floor="owner_holds", phase="user_speaking", emotion="sad")
    assert (sad.gesture, sad.intensity) == (GESTURE_LISTENING, "low")
    assert gesture_for(floor="eve_holds", phase="speaking").gesture == GESTURE_SPEAKING
    assert gesture_for(floor="eve_holds", phase="thinking").gesture == GESTURE_THINKING
    assert gesture_for(floor="contested", phase="speaking").gesture == GESTURE_CONTESTED
    assert gesture_for(floor="yielding", phase="speaking").gesture == GESTURE_YIELDING
    urgent = gesture_for(floor="eve_holds", phase="speaking", hint="safety")
    assert (urgent.gesture, urgent.intensity) == (GESTURE_URGENT, "high")
    assert (
        gesture_for(floor="eve_holds", phase="speaking", hint="invited").gesture
        == GESTURE_SPEAKING
    )
    assert (
        gesture_for(floor="owner_holds", phase="user_speaking", hint="yield_back").gesture
        == GESTURE_YIELD_BACK
    )
    assert (
        gesture_for(floor="owner_holds", phase="user_speaking", hint="owner_cut_in").gesture
        == GESTURE_YIELDING
    )


def test_engine_emits_gesture_on_change_only() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)

    first = _gestures(engine.tick(now_ms=100))
    assert len(first) == 1
    assert first[0].gesture == GESTURE_IDLE
    assert first[0].floor == "idle"

    assert _gestures(engine.tick(now_ms=200)) == []

    engine.push_speech(True, now_ms=300)
    moved = _gestures(engine.tick(now_ms=400))
    assert len(moved) == 1
    assert moved[0].gesture == GESTURE_LISTENING
    assert moved[0].floor == "owner_holds"


def test_engine_owner_cut_in_gesture_yields() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_assistant_speaking(True, now_ms=100)
    engine.tick(now_ms=150)

    engine.push_speech(True, now_ms=200)
    moves = _gestures(engine.tick(now_ms=250))
    assert moves
    assert moves[-1].gesture == GESTURE_YIELDING

    settled = _gestures(engine.tick(now_ms=300))
    assert settled
    assert settled[-1].gesture == GESTURE_LISTENING


def test_engine_eve_cut_in_gesture_marks_urgent_then_yield_back() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_speech(True, now_ms=100)
    engine.tick(now_ms=150)

    assert engine.consider_eve_cut_in(TRIGGER_TIMER_DUE, now_ms=200).allowed
    urgent = _gestures(engine.tick(now_ms=250))
    assert urgent
    assert urgent[-1].gesture == GESTURE_URGENT
    assert urgent[-1].intensity == "high"

    engine.note_eve_cut_in_end(now_ms=300)
    back = _gestures(engine.tick(now_ms=350))
    assert back
    assert back[-1].gesture == GESTURE_YIELD_BACK


def test_engine_invited_cut_in_is_gentle() -> None:
    clock = ManualClock(0)
    engine = LiveEngine(clock_ms=clock)
    engine.push_transcript("stop me if you know", now_ms=50)
    engine.push_speech(True, now_ms=100)
    engine.tick(now_ms=150)

    assert engine.consider_eve_cut_in(TRIGGER_INVITED, now_ms=200).allowed
    moves = _gestures(engine.tick(now_ms=250))
    assert moves
    assert moves[-1].gesture == GESTURE_SPEAKING
    assert moves[-1].intensity == "medium"
