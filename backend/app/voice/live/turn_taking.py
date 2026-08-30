"""Silence-aware turn-taking for the EV LIVE runtime.

The naive rule — ``if silence > 800ms: assume_user_finished()`` — is exactly
what the GPT-Live-style architecture rejects. Humans pause while thinking,
trail off with "and…", "so…", "maybe…", and emit short sounds ("hmm") that are
not turns.

This policy therefore decides *many times per second* using a combination of:

- voice activity (VAD frame probabilities),
- how long the current silence has lasted,
- what the last ASR partial actually *says* (sentence-final punctuation vs a
  trailing filler vs an incomplete clause),
- whether the user has just started speaking again,
- whether the assistant is currently speaking (interruption detection),
- the listening mode (attentive conversation vs quiet vs passive ambient).

The policy is deterministic and engine-free: the engine feeds it VAD frames
and ASR partials, and asks ``decide()`` on a cadence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.voice.live.state import (
    GEN_BACKGROUND,
    GEN_FOREGROUND,
    GEN_STREAMING,
    LISTEN_PASSIVE,
    LISTEN_QUIET,
    LiveConversationState,
)

#: Decision actions.
TURN_KEEP_LISTENING = "keep_listening"
TURN_RESPOND_NOW = "respond_now"
TURN_STAY_QUIET = "stay_quiet"  # user paused but is likely still thinking
TURN_USER_INTERRUPTED = "user_interrupted"

#: Trailing fillers / discourse markers / incomplete-clause endings that
#: strongly imply "not done yet" ("and…", "so…", "maybe…", "we could…",
#: "give me the…").
_TRAILING = re.compile(
    r"(?:\b(?:and|but|so|because|or|if|when|while|though|although|then|"
    r"like|maybe|actually|basically|however|um|uh|er|hmm|well|also|except|"
    r"could|would|should|will|can|might|may|must|to|for|with|about|"
    r"through|the|a|an|of)\b|,)\s*$",
    re.IGNORECASE,
)
#: Sentence-final punctuation means the thought (probably) ended.
_SENTENCE_FINAL = re.compile(r"[.!?][\"']?\s*$")
#: Spoken questions and short commands are finished turns even without ".".
_COMPLETE_INTENT = re.compile(
    r"\b(?:what(?:'s|s)?|who(?:'s|s)?|where(?:'s|s)?|when(?:'s|s)?|why|how|"
    r"weather|time|date|calendar|remind|text|call|send|show|open|search|"
    r"look(?:\s+up)?|set|tell me|is it|are you)\b",
    re.IGNORECASE,
)
#: Words that are not turns at all: thinking sounds, acknowledgements, "yes".
_NON_TURN = re.compile(
    r"^(?:hmm+|uh+|um+|mm+|mhm|uh huh|yeah|yes|yep|no|nope|ok|okay|right|"
    r"got it|sure|wow|oh|aha|ah)\W*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnDecision:
    """What the runtime should do right now."""

    action: str
    reason: str = ""
    pause_ms: int | None = None
    last_partial: str | None = None

    @property
    def respond(self) -> bool:
        return self.action == TURN_RESPOND_NOW

    @property
    def interrupted(self) -> bool:
        return self.action == TURN_USER_INTERRUPTED


@dataclass
class TurnTakingConfig:
    """Tunable knobs; every knob maps to an ``EV_VOICE_LIVE_*`` setting."""

    #: Silence (after a complete-sentence partial) that ends the turn.
    end_pause_ms: int = 280
    #: Extra wait when the user pauses mid-thought (no final punctuation).
    thinking_grace_ms: int = 700
    #: Extra wait when the last partial trails off ("and…", "so…", "um…").
    trailing_grace_ms: int = 1100
    #: Bare "Evie" — wait for a command before the Yes? ack.
    wake_hold_ms: int = 650
    #: A speech blip shorter than this is noise, not a turn or interruption.
    min_speech_ms: int = 160
    #: Quiet listening mode needs a longer pause before the assistant jumps in.
    quiet_end_pause_ms: int = 1300
    #: Passive listening mode never responds on its own (wake-level only).
    passive_no_self_respond: bool = True
    #: After the assistant has spoken, ignore immediate re-triggers so the
    #: assistant's own audio / residual echo cannot start a new turn.
    response_cooldown_ms: int = 450
    #: Upper bound on any single pause wait before the runtime forces a reply.
    max_pause_ms: int = 2500


def pause_class(text: str | None) -> str:
    """Classify what kind of pause the user is in based on the last partial.

    Returns ``complete`` (sentence-final or a finished question/command),
    ``wake`` (bare Evie — hold for a command), ``trailing`` (ends with a
    filler / comma / incomplete thought), or ``thinking`` (still formulating).
    """

    from app.voice.speech import is_wake_only_name, strip_wake_prefix

    raw = (text or "").strip()
    if not raw:
        return "thinking"
    command = strip_wake_prefix(raw)
    if not command and is_wake_only_name(raw):
        return "wake"
    body = command or raw
    if _SENTENCE_FINAL.search(body):
        return "complete"
    if _TRAILING.search(body):
        return "trailing"
    words = body.split()
    if _COMPLETE_INTENT.search(body) and len(words) >= 2:
        return "complete"
    return "thinking"


def is_non_turn(text: str | None) -> bool:
    """True when the (partial) transcript is a thinking sound, not a turn."""
    return bool(_NON_TURN.match((text or "").strip()))


class TurnTakingPolicy:
    """Tracks who is doing what and decides the next turn action."""

    def __init__(
        self,
        *,
        config: TurnTakingConfig | None = None,
        clock_ms=None,
    ) -> None:
        self.config = config or TurnTakingConfig()
        # ``clock_ms`` is injectable for deterministic tests; defaults to the
        # same monotonic clock used by ``LiveConversationState``.
        self._clock = clock_ms or (lambda: __import__("time").monotonic() * 1000)
        self._last_decide_ms: int | None = None
        self._responded_at_ms: int | None = None
        self._last_speech_start_ms: int | None = None
        self._speech_pending_final: bool = False
        self._last_partial: str | None = None
        self._last_partial_seq: int = 0
        self._speech_ends: list[int] = []

    # ------------------------------------------------------------------ #
    # Signal ingestion
    # ------------------------------------------------------------------ #

    def on_speech_start(self, *, now_ms: int | None = None) -> None:
        """The user started producing audio (VAD crossed its threshold)."""
        now = int(now_ms if now_ms is not None else self._clock())
        self._last_speech_start_ms = now
        self._speech_pending_final = False

    def on_speech_end(self, *, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else self._clock())
        if self._last_speech_start_ms is None:
            self._last_speech_start_ms = now
        duration = now - self._last_speech_start_ms
        if duration >= self.config.min_speech_ms:
            self._speech_ends.append(now)
            self._speech_pending_final = True
        # Micro-blips (coughs, clicks) never end a turn and are dropped here.

    def on_partial(self, text: str, *, seq: int = 0) -> None:
        text = (text or "").strip()
        if text:
            self._last_partial = text
            self._last_partial_seq = seq

    def on_assistant_speech_start(self) -> None:
        self._responded_at_ms = int(self._clock())
        self._speech_pending_final = False

    def on_barge_in(self) -> None:
        self._speech_pending_final = False

    def reset_turn(self) -> None:
        """Clear per-turn bookkeeping after a reply has been spoken."""
        self._last_partial = None
        self._speech_pending_final = False
        self._speech_ends.clear()

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #

    def decide(self, state: LiveConversationState, *, now_ms: int | None = None) -> TurnDecision:
        now = int(now_ms if now_ms is not None else self._clock())
        self._last_decide_ms = now

        # 1. The user just started talking while the assistant is speaking →
        #    this is an interruption, not a new turn.
        if state.user_is_speaking and state.assistant_is_speaking:
            return TurnDecision(
                TURN_USER_INTERRUPTED,
                reason="user speech while assistant speaking",
                pause_ms=0,
                last_partial=self._last_partial,
            )

        # 2. The user is actively speaking → keep listening, never barge in.
        if state.user_is_speaking:
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="user is speaking",
                last_partial=self._last_partial,
            )

        # 2b. A response is already in flight (foreground, delegated, or
        #     streaming). Do not start a second turn from leftover silence.
        if state.response_generation in {GEN_FOREGROUND, GEN_BACKGROUND, GEN_STREAMING}:
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="response already in flight",
                last_partial=self._last_partial,
            )

        # 2c. No recognized content yet — silence after noise is not a turn.
        if not (self._last_partial or "").strip():
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="no user content yet",
                last_partial=self._last_partial,
            )

        # 3. Assistant just finished a reply → cool down before anything else;
        #    residual echo / the user's "thanks" are handled by the echo gate.
        if self._responded_at_ms is not None and self._responded_at_ms >= now - self.config.response_cooldown_ms:
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="response cooldown",
                last_partial=self._last_partial,
            )

        silence = state.silence_ms(now_ms=now)
        if silence is None:
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="no silence baseline yet",
                last_partial=self._last_partial,
            )

        # 4. Passive listening mode: never self-respond; the wake layer decides.
        if state.listening_mode == LISTEN_PASSIVE and self.config.passive_no_self_respond:
            return TurnDecision(
                TURN_STAY_QUIET,
                reason="passive listening mode",
                pause_ms=silence,
                last_partial=self._last_partial,
            )

        # 5. What kind of pause is this? Silence is information.
        klass = pause_class(self._last_partial)
        base = self.config.end_pause_ms
        if klass == "trailing":
            wait = self.config.trailing_grace_ms
            reason = "trailing pause — user likely continuing"
        elif klass == "wake":
            wait = self.config.wake_hold_ms
            reason = "wake-only name — holding for a command"
        elif klass == "thinking":
            wait = self.config.thinking_grace_ms
            reason = "thinking pause — no sentence-final partial yet"
        else:
            wait = base
            reason = "sentence-complete pause"

        if state.listening_mode == LISTEN_QUIET:
            wait = max(wait, self.config.quiet_end_pause_ms)
            reason = f"{reason} (quiet listening mode)"

        wait = min(wait, self.config.max_pause_ms)

        # 6. The user is mid-thought and just stopped for a moment: wait.
        if silence < wait:
            return TurnDecision(
                TURN_STAY_QUIET,
                reason=reason,
                pause_ms=silence,
                last_partial=self._last_partial,
            )

        # 7. A genuine non-turn (hmm / uh / thinking sound) never triggers.
        if is_non_turn(self._last_partial):
            self.reset_turn()
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="non-turn utterance (thinking sound)",
                pause_ms=silence,
                last_partial=self._last_partial,
            )

        # 8. We have waited long enough → the user finished their turn.
        self._responded_at_ms = now
        self._speech_pending_final = False
        return TurnDecision(
            TURN_RESPOND_NOW,
            reason=reason,
            pause_ms=silence,
            last_partial=self._last_partial,
        )

    def commit(self, state: LiveConversationState, *, now_ms: int | None = None) -> TurnDecision:
        """Force end-of-turn (push-to-talk release / explicit commit)."""

        now = int(now_ms if now_ms is not None else self._clock())
        if state.user_is_speaking and state.assistant_is_speaking:
            return TurnDecision(
                TURN_USER_INTERRUPTED,
                reason="commit during assistant speech",
                pause_ms=0,
                last_partial=self._last_partial,
            )
        if not (self._last_partial or "").strip():
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="commit with no user content",
                last_partial=self._last_partial,
            )
        if is_non_turn(self._last_partial):
            return TurnDecision(
                TURN_KEEP_LISTENING,
                reason="commit of non-turn utterance",
                last_partial=self._last_partial,
            )
        self._responded_at_ms = now
        self._speech_pending_final = False
        return TurnDecision(
            TURN_RESPOND_NOW,
            reason="explicit commit",
            pause_ms=state.silence_ms(now_ms=now),
            last_partial=self._last_partial,
        )
