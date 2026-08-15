"""EV LIVE — full-duplex conversational voice runtime.

A continuously-operating conversational nervous system: it tracks who is
doing what at all times, treats silence as information, backchannels
naturally, decides turn-taking many times per second, and keeps a
lightweight foreground loop while deeper reasoning (DeepSeek / memory /
tools) runs in the background.

This package is provider-agnostic and offline-deterministic. The HTTP
utterance path in ``app.voice.lifecycle`` stays; ``WS /v1/voice/live`` is
the continuous loop. See ``docs/LIVE_VOICE.md``.
"""

from app.voice.live.backchannel import BackchannelDecision, BackchannelPolicy
from app.voice.live.behavior import (
    BehaviorEnvelope,
    behavior_from_state,
    to_speech_style,
)
from app.voice.live.delegate import needs_deep_work, thinking_filler
from app.voice.live.engine import EngineTick, LiveEngine, ManualClock
from app.voice.live.events import (
    BackchannelEvent,
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    LatencyEvent,
    PartialTranscriptEvent,
    ReadyEvent,
    ReplyEvent,
    StateEvent,
    TtsChunkEvent,
)
from app.voice.live.session import LiveSession
from app.voice.live.state import (
    LiveConversationState,
    phase_label,
    speaking_mode_label,
)
from app.voice.live.turn_taking import (
    TURN_KEEP_LISTENING,
    TURN_RESPOND_NOW,
    TURN_STAY_QUIET,
    TURN_USER_INTERRUPTED,
    TurnDecision,
    TurnTakingPolicy,
)

__all__ = [
    "BackchannelDecision",
    "BackchannelEvent",
    "BackchannelPolicy",
    "BargeInEvent",
    "BehaviorEnvelope",
    "EngineTick",
    "ErrorEvent",
    "FinalTranscriptEvent",
    "LatencyEvent",
    "LiveConversationState",
    "LiveEngine",
    "LiveSession",
    "ManualClock",
    "PartialTranscriptEvent",
    "ReadyEvent",
    "ReplyEvent",
    "StateEvent",
    "TURN_KEEP_LISTENING",
    "TURN_RESPOND_NOW",
    "TURN_STAY_QUIET",
    "TURN_USER_INTERRUPTED",
    "TtsChunkEvent",
    "TurnDecision",
    "TurnTakingPolicy",
    "behavior_from_state",
    "needs_deep_work",
    "phase_label",
    "speaking_mode_label",
    "thinking_filler",
    "to_speech_style",
]
