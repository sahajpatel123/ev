"""Backchanneling for the EV LIVE runtime.

"Mhm", "yeah", "right", "got it" are not random — they signal "I'm listening".
A human listener backchannels continuously so the speaker feels engaged.
But an assistant that backchannels at the wrong moment (during an emotional
story, inside an urgent request, or twice in the same sentence) feels
robotic. This policy decides *when* to backchannel and *which* cue to use.

The decision is pure: it reads the live conversation state and returns a cue
(or ``None`` to stay quiet). The engine turns a cue into a short TTS
synthesis that plays under the user's speech.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.voice.live.state import (
    LISTEN_ATTENTIVE,
    LiveConversationState,
)

#: Cues we may speak. ``None`` = stay quiet.
BACKCHANNELS = ("Mhm.", "Yeah.", "Right.", "Got it.", "Okay.")

#: Emotional contexts where a verbal cue would feel tone-deaf. Stay quiet and
#: give the speaker space.
_QUIET_EMOTIONS = {"frustrated", "sad", "urgent"}

#: Cue selection by how long the user has been holding the floor.
_EARLY = ("Mhm.", "Yeah.", "Okay.")
_MID = ("Mhm.", "Yeah.", "Right.")
_LONG = ("Right.", "Got it.", "Yeah.")


@dataclass
class BackchannelDecision:
    cue: str | None = None
    reason: str = ""

    @property
    def should_backchannel(self) -> bool:
        return self.cue is not None


class BackchannelPolicy:
    """Decides whether and what to backchannel on a given decision tick."""

    def __init__(
        self,
        *,
        min_speech_ms: int = 1800,
        max_interval_ms: int = 5000,
        max_per_turn: int = 3,
        long_narration_ms: int = 20_000,
        fast_cadence_ms: int = 2500,
    ) -> None:
        #: The user must have held the floor this long before the first cue.
        self.min_speech_ms = min_speech_ms
        #: Minimum spacing between cues during normal conversation.
        self.max_interval_ms = max_interval_ms
        #: Hard cap of cues per user turn.
        self.max_per_turn = max_per_turn
        #: After this much continuous narration, the speaker needs a steady
        #: stream of cues to feel heard — switch to the faster cadence.
        self.long_narration_ms = long_narration_ms
        #: Minimum spacing between cues during long continuous narration.
        self.fast_cadence_ms = fast_cadence_ms

    def decide(
        self,
        state: LiveConversationState,
        *,
        now_ms: int | None = None,
        speech_started_ms: int | None = None,
    ) -> BackchannelDecision:
        # Only while the user actually holds the floor.
        if not state.user_is_speaking:
            return BackchannelDecision(reason="user is not speaking")
        if state.assistant_is_speaking:
            return BackchannelDecision(reason="assistant is speaking")
        if state.listening_mode != LISTEN_ATTENTIVE:
            return BackchannelDecision(
                reason=f"listening mode is {state.listening_mode!r}, not attentive"
            )
        if state.emotional_context in _QUIET_EMOTIONS:
            return BackchannelDecision(
                reason=f"emotional context {state.emotional_context!r} → stay quiet"
            )
        if state.user_intent == "urgent_request":
            return BackchannelDecision(reason="urgent request → stay quiet")
        if state.backchannel_count >= self.max_per_turn:
            return BackchannelDecision(reason="per-turn cap reached")

        now = now_ms
        if now is None:
            import time

            now = int(time.monotonic() * 1000)

        elapsed = (
            now - state.last_user_speech_start_ms
            if state.last_user_speech_start_ms is not None
            else 0
        )
        if elapsed < self.min_speech_ms:
            return BackchannelDecision(reason="user just started speaking")

        since_last = state.time_since_backchannel(now_ms=now)
        if since_last is not None:
            spacing = (
                self.fast_cadence_ms
                if elapsed >= self.long_narration_ms
                else self.max_interval_ms
            )
            if since_last < spacing:
                return BackchannelDecision(reason="backchanneled too recently")

        if elapsed >= 24_000:
            cue = _LONG[state.backchannel_count % len(_LONG)]
        elif elapsed >= 12_000:
            cue = _MID[state.backchannel_count % len(_MID)]
        else:
            cue = _EARLY[state.backchannel_count % len(_EARLY)]
        return BackchannelDecision(cue=cue, reason="holding-floor cue")
