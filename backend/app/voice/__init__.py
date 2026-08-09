"""EVIE voice & speech subsystem.

Provider-agnostic wake detection, owner speaker verification, anti-spoofing,
ASR, TTS, and the wake → verify → listen → act → reply → follow-up → idle
lifecycle. Voice behavior never couples to a specific model or platform.
"""

from app.voice.contracts import (
    SpeakerDecision,
    SpeechStyle,
    SynthesisResult,
    Transcript,
    WakeDetection,
)
from app.voice.lifecycle import VoiceRuntime, VoiceState

__all__ = [
    "SpeakerDecision",
    "SpeechStyle",
    "SynthesisResult",
    "Transcript",
    "VoiceRuntime",
    "VoiceState",
    "WakeDetection",
]
