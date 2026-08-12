"""Offline wake fallback for the ears process (no heavy voice imports).

``PhraseWakeEngine`` (app/voice/wake.py) is the canonical dev double. The
ears process uses this lightweight mirror for the default offline path so that
importing ``app.voice`` (whose package init eagerly loads the full lifecycle
stack) does not blow the ≤ 60 MB RSS budget. The rules are kept identical to
the canonical engine; when a real openWakeWord model is configured the ears
process imports the real engine instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WAKE_PHRASES = ("evie", "hey evie", "ok evie", "evie wake", "evie wake up", "evi")
WAKE_TOKEN = re.compile(r"\bevi(?:e|)?\b", re.IGNORECASE)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


@dataclass
class WakeDetectionLike:
    """Minimal mirror of app.voice.contracts.WakeDetection.

    Kept local so the ears default path never imports ``app.voice`` (whose
    package init eagerly loads the lifecycle stack, +42 MB RSS). Only the
    fields the ears loop consumes are defined; the real engine returns the
    canonical contract type.
    """

    triggered: bool
    confidence: float
    wake_word: str = "evie"
    device_id: str | None = None
    stage: str = "low_power"
    power_state: str = "low_power"
    details: dict = field(default_factory=dict)


class PhraseFallbackWake:
    """Deterministic text/frame wake double mirroring PhraseWakeEngine."""

    name = "phrase-fallback"
    power_state = "low_power"

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetectionLike:
        if text_hint is not None:
            normalized = normalize(text_hint)
            triggered = normalized in WAKE_PHRASES or bool(WAKE_TOKEN.search(normalized))
            confidence = 0.98 if normalized in WAKE_PHRASES else 0.55
        elif frames is not None:
            lowered = frames.lower()
            triggered = b"evie" in lowered or b"evi " in lowered
            confidence = 0.9 if triggered else 0.0
        else:
            triggered = False
            confidence = 0.0
        return WakeDetectionLike(
            triggered=triggered,
            wake_word="evie",
            confidence=confidence,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={"engine": self.name, "sample_rate": sample_rate, "audio_ref": audio_ref},
        )
