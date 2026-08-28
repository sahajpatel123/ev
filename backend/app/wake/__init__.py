"""Always-available wake foundation: local cascade (W1-W4).

Stages (small, measured, added only when solving a failure):
 MIC → Ring(10s) → Stage1(high-recall) → Stage2(high-precision) → Speaker fast
 → Arbitration → Realtime handoff (pre-roll 1-2s + live PCM) → Full-utterance speaker
 + Directed-speech / false-trigger check.

See docs/WAKE_W0_AUDIT.md and PROJECT-HEAD directive 28 sections.
"""

from .arbitration import WakeArbitration
from .directed import DirectedSpeechChecker, DirectedResult
from .speaker_stage import SpeakerStage

__all__ = ["DirectedSpeechChecker", "DirectedResult", "SpeakerStage", "WakeArbitration"]
