"""Semantic content vs conversational behavior for EV LIVE.

The LLM (or a deterministic strategy layer) decides *what* to say and *how
the interaction should feel*. The voice renderer then realizes that intent
acoustically. We never stuff fake speech tags like ``[sad][slow]`` into the
reply text and hope TTS interprets them.

The envelope is the contract between those layers. ``to_speech_style`` maps
it onto the existing ``SpeechStyle`` so Kokoro / Edge / meta TTS keep working
unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.voice.contracts import SpeechStyle
from app.voice.live.state import LiveConversationState

#: interaction_mode → SpeechStyle.mode
_MODE = {
    "empathetic": "casual",
    "direct": "command",
    "excited": "casual",
    "calm": "casual",
    "urgent": "emergency",
    "neutral": "casual",
    "analytical": "analytical",
}

#: emotional_context / interaction_mode → (energy, pace, interruptibility, pause_ms)
_AFFECT = {
    "neutral": ("medium", "normal", "high", 80),
    "calm": ("low", "slow", "high", 180),
    "sad": ("low", "slow", "high", 240),
    "tired": ("low", "slow", "high", 220),
    "frustrated": ("low", "normal", "medium", 160),
    "stressed": ("medium", "fast", "medium", 60),
    "excited": ("high", "fast", "medium", 40),
    "uncertain": ("low", "slow", "high", 200),
    "urgent": ("high", "fast", "low", 0),
}

#: Text-only nudges used when no strategy / emotion is attached yet, so a bare
#: ``behavior_from_state(state, text=...)`` still behaves sensibly.
_URGENT_TEXT = re.compile(
    r"\b(hurry|asap|quick|right now|urgent|emergency|immediately|fast)\b",
    re.IGNORECASE,
)
_SAD_TEXT = re.compile(
    r"\b(sad|sorry|upset|hurt|lost|cried|crying|miss|missed|died|death|"
    r"struggl|fail|failed|tough day|rough day)\b",
    re.IGNORECASE,
)
_EXCITED_TEXT = re.compile(
    r"\b(awesome|amazing|great|excited|yay|finally|won|won!|love it|love this|"
    r"incredible|fantastic|perfect)\b",
    re.IGNORECASE,
)


@dataclass
class BehaviorEnvelope:
    """How this utterance should be realized, separate from its words."""

    semantic_content: str
    interaction_mode: str = "neutral"
    energy: str = "medium"  # low | medium | high
    pace: str = "normal"  # slow | normal | fast
    interruptibility: str = "high"  # high | medium | low
    pause_before_response_ms: int = 80
    emotional_context: str = "neutral"
    verbosity: str = "one to two sentences"
    directness: str = "medium"

    def as_dict(self) -> dict:
        return {
            "semantic_content": self.semantic_content,
            "interaction_mode": self.interaction_mode,
            "energy": self.energy,
            "pace": self.pace,
            "interruptibility": self.interruptibility,
            "pause_before_response_ms": self.pause_before_response_ms,
            "emotional_context": self.emotional_context,
            "verbosity": self.verbosity,
            "directness": self.directness,
        }


def _text_nudge(text: str, envelope: BehaviorEnvelope) -> None:
    """Lightweight text heuristics when no strategy/emotion is attached yet.

    The live engine normally feeds detected emotion/intent from the ASR text;
    these nudges keep the envelope sane when the transport calls the behavior
    layer directly with raw text.
    """

    raw = (text or "").strip()
    if not raw or envelope.emotional_context not in {"", "neutral"}:
        return
    if _URGENT_TEXT.search(raw):
        envelope.interaction_mode = "urgent"
        envelope.energy, envelope.pace, envelope.interruptibility, envelope.pause_before_response_ms = _AFFECT[
            "urgent"
        ]
        envelope.verbosity = "short and direct"
        envelope.directness = "high"
    elif _SAD_TEXT.search(raw):
        envelope.interaction_mode = "empathetic"
        envelope.energy, envelope.pace, envelope.interruptibility, envelope.pause_before_response_ms = _AFFECT[
            "sad"
        ]
        envelope.directness = "low"
    elif _EXCITED_TEXT.search(raw):
        envelope.interaction_mode = "excited"
        envelope.energy, envelope.pace, envelope.interruptibility, envelope.pause_before_response_ms = _AFFECT[
            "excited"
        ]


def behavior_from_state(
    state: LiveConversationState,
    text: str = "",
    *,
    strategy=None,
    transcript: str | None = None,
) -> BehaviorEnvelope:
    """Build an envelope from live state, optional InteractionStrategy, and text.

    ``transcript`` is accepted as an explicit keyword alias for ``text`` so
    callers can be self-documenting; the engine passes ``text`` positionally.
    """

    if transcript is not None and not text:
        text = transcript
    emotion = getattr(strategy, "emotional_state", None) or state.emotional_context or "neutral"
    intent = getattr(strategy, "intent", None) or state.user_intent or ""
    energy, pace, interruptibility, pause_ms = _AFFECT.get(emotion, _AFFECT["neutral"])

    mode = "neutral"
    if emotion in {"sad", "tired"}:
        mode = "empathetic"
    elif emotion in {"frustrated", "stressed"}:
        mode = "direct"
    elif emotion == "excited":
        mode = "excited"
    elif emotion == "urgent" or intent == "urgent_request":
        mode = "urgent"
        energy, pace, interruptibility, pause_ms = _AFFECT["urgent"]
    elif getattr(strategy, "mode", None) in {"analytical", "technical"}:
        mode = "analytical"

    envelope = BehaviorEnvelope(
        semantic_content=text,
        interaction_mode=mode,
        energy=energy,
        pace=pace,
        interruptibility=interruptibility,
        pause_before_response_ms=pause_ms,
        emotional_context=emotion,
    )

    # Verbosity / directness follow the mode so TTS + reply length stay aligned.
    if mode == "urgent":
        envelope.verbosity = "short and direct"
        envelope.directness = "high"
    elif mode == "direct":
        envelope.directness = "high"
        envelope.verbosity = "short and direct"
    elif mode == "empathetic":
        envelope.directness = "low"
    elif mode == "analytical":
        envelope.verbosity = "structured and precise"
        envelope.directness = "medium"

    if state.listening_mode == "quiet":
        pause_ms = max(pause_ms, 180)
        envelope.pause_before_response_ms = pause_ms
        envelope.interruptibility = "high"

    _text_nudge(text, envelope)
    return envelope


def to_speech_style(envelope: BehaviorEnvelope, *, strategy=None) -> SpeechStyle:
    """Map a behavior envelope onto the TTS ``SpeechStyle`` contract.

    When an ``InteractionStrategy`` is supplied, its existing mapping is the
    base (so emotion → warmth/urgency/brevity stays consistent with chat),
    then energy / pace overlay rate-adjacent knobs.
    """

    if strategy is not None:
        from app.voice.tts import speech_style_from_strategy

        style = speech_style_from_strategy(strategy)
    else:
        warmth = {"low": 0.9, "medium": 0.72, "high": 0.55}[envelope.energy]
        urgency = {"slow": 0.08, "normal": 0.28, "fast": 0.55}[envelope.pace]
        if envelope.energy == "high":
            urgency = max(urgency, 0.45)
        if envelope.energy == "low":
            urgency = min(urgency, 0.18)
        brevity = 0.82 if envelope.interaction_mode in {"direct", "urgent"} else 0.45
        style = SpeechStyle(
            urgency=urgency,
            warmth=warmth,
            brevity=brevity,
            mode=_MODE.get(envelope.interaction_mode, "casual"),
            length_target=envelope.verbosity,
            directness=envelope.directness,
        )

    if envelope.energy == "low":
        style.warmth = min(1.0, max(style.warmth, 0.85))
        style.urgency = min(style.urgency, 0.2)
    elif envelope.energy == "high":
        style.urgency = max(style.urgency, 0.4)
    if envelope.pace == "slow":
        style.urgency = min(style.urgency, 0.22)
    elif envelope.pace == "fast":
        style.urgency = max(style.urgency, 0.4)
    if envelope.interaction_mode == "urgent":
        style.mode = "emergency"
        style.brevity = max(style.brevity, 0.85)
        style.urgency = max(style.urgency, 0.75)
    return style
