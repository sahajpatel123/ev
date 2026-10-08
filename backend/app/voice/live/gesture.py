"""Conversational gesture envelope for the EV LIVE runtime.

Speech carries words; gestures carry presence. The envelope maps the
authoritative conversation state (floor, phase, emotion, plus one-shot
interruption hints) onto a small shared gesture vocabulary that every
client renders natively:

- Mac orb: energy modulation of the existing renderer (no new visuals).
- iPhone PWA presence: motion/energy modulation of living glass + haptics.
- Any future surface: same ``gesture`` events, same vocabulary.

Vocabulary (mirrored in ``GestureEnergy.swift`` and ``presence.js``):

- ``idle``: at rest, nothing happening.
- ``listening``: owner holds the floor; Eve is attending.
- ``thinking``: Eve holds the floor but has no audio yet.
- ``speaking``: Eve is audibly speaking.
- ``yielding``: Eve was cut off and is giving the floor up.
- ``yield_back``: Eve finished her own cut-in and returns the floor
  ("sorry — go on").
- ``contested``: overlap neither side has won yet.
- ``urgent``: a safety/timer cut-in is active; needs attention now.

Intensity (``low`` | ``medium`` | ``high``) scales the render, never the
meaning. This module is pure: no I/O, bare-install safe.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Gesture vocabulary (protocol enum values on ``gesture`` events).
GESTURE_IDLE = "idle"
GESTURE_LISTENING = "listening"
GESTURE_THINKING = "thinking"
GESTURE_SPEAKING = "speaking"
GESTURE_YIELDING = "yielding"
GESTURE_YIELD_BACK = "yield_back"
GESTURE_CONTESTED = "contested"
GESTURE_URGENT = "urgent"

GESTURES = frozenset(
    {
        GESTURE_IDLE,
        GESTURE_LISTENING,
        GESTURE_THINKING,
        GESTURE_SPEAKING,
        GESTURE_YIELDING,
        GESTURE_YIELD_BACK,
        GESTURE_CONTESTED,
        GESTURE_URGENT,
    }
)

#: Render intensity.
INTENSITY_LOW = "low"
INTENSITY_MEDIUM = "medium"
INTENSITY_HIGH = "high"

#: Eve cut-in triggers that render as urgent (mirrors eve_cutin.py names
#: without importing it, so this module stays dependency-free).
_URGENT_TRIGGERS = frozenset({"safety", "timer_due", "urgent_alert"})
_EVE_CUT_IN_TRIGGERS = frozenset(
    {"safety", "timer_due", "urgent_alert", "invited", "known_answer"}
)

#: Emotions where Eve gives the owner space (low listening energy).
_SPACE_EMOTIONS = frozenset({"sad", "frustrated"})


@dataclass(frozen=True)
class Gesture:
    """One resolved gesture + render intensity."""

    gesture: str
    intensity: str = INTENSITY_MEDIUM


def gesture_for(
    *,
    floor: str,
    phase: str,
    emotion: str = "neutral",
    hint: str | None = None,
) -> Gesture:
    """Resolve the current gesture from authoritative live state.

    ``hint`` is a one-shot set by interruption transitions (a cut-in
    trigger name, ``"owner_cut_in"``, or ``"yield_back"``); the caller
    consumes it after one drain so transient gestures never stick.
    """

    if hint in _URGENT_TRIGGERS:
        return Gesture(GESTURE_URGENT, INTENSITY_HIGH)
    if hint in _EVE_CUT_IN_TRIGGERS:
        return Gesture(GESTURE_SPEAKING, INTENSITY_MEDIUM)
    if hint == "yield_back" and floor == "owner_holds":
        return Gesture(GESTURE_YIELD_BACK, INTENSITY_LOW)
    if hint == "owner_cut_in" and floor == "owner_holds":
        return Gesture(GESTURE_YIELDING, INTENSITY_MEDIUM)
    if floor == "contested":
        return Gesture(GESTURE_CONTESTED, INTENSITY_MEDIUM)
    if floor == "yielding":
        return Gesture(GESTURE_YIELDING, INTENSITY_MEDIUM)
    if floor == "eve_holds":
        if phase == "thinking":
            return Gesture(GESTURE_THINKING, INTENSITY_MEDIUM)
        if emotion == "urgent":
            return Gesture(GESTURE_SPEAKING, INTENSITY_HIGH)
        if emotion in _SPACE_EMOTIONS:
            return Gesture(GESTURE_SPEAKING, INTENSITY_LOW)
        return Gesture(GESTURE_SPEAKING, INTENSITY_MEDIUM)
    if floor == "owner_holds":
        if emotion in _SPACE_EMOTIONS:
            return Gesture(GESTURE_LISTENING, INTENSITY_LOW)
        if emotion == "urgent":
            return Gesture(GESTURE_LISTENING, INTENSITY_HIGH)
        return Gesture(GESTURE_LISTENING, INTENSITY_MEDIUM)
    if phase == "thinking":
        return Gesture(GESTURE_THINKING, INTENSITY_MEDIUM)
    return Gesture(GESTURE_IDLE, INTENSITY_LOW)
