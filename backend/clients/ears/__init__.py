"""Ears — the always-on microphone process for EVIE.

Capture → lock-free PCM16 pre-roll ring → streaming VAD → wake word → scene
label → (with explicit consent) deliver the VAD-segmented utterance to Agent 4.
Raw audio is never persisted by default.
"""

from clients.ears.main import EarConfig, EarRunStats, build_config, run_ears

__all__ = ["EarConfig", "EarRunStats", "build_config", "run_ears"]
