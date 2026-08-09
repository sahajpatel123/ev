"""Wake word engines.

Production intent is an ultra-low-power always-on front end (Sensory/AON1100
class) that wakes a burst processor only on a positive "EVIE" hit. The dev
engines here implement the same contract deterministically so the lifecycle,
privacy, and security logic is fully testable without hardware.
"""

from __future__ import annotations

import re

from app.voice.contracts import WakeDetection, WakeWordEngine


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


class PhraseWakeEngine:
    """Deterministic wake engine for dev/test and on-device text hints.

    A real engine receives raw audio frames; this one accepts an optional
    ``text_hint`` (e.g. a lightweight on-device phrase hypothesis) or raw frames
    containing the wake phrase bytes, and reports the multi-stage power intent.
    """

    name = "phrase"
    power_state = "low_power"

    WAKE_PHRASES = ("evie", "hey evie", "ok evie", "evie wake", "evie wake up", "evi")
    WAKE_TOKEN = re.compile(r"\bevi(?:e|)?\b", re.IGNORECASE)

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        if text_hint is not None:
            normalized = normalize(text_hint)
            triggered = normalized in self.WAKE_PHRASES or bool(self.WAKE_TOKEN.search(normalized))
            confidence = 0.98 if normalized in self.WAKE_PHRASES else 0.55
        elif frames is not None:
            lowered = frames.lower()
            triggered = b"evie" in lowered or b"evi " in lowered
            confidence = 0.9 if triggered else 0.0
        else:
            triggered = False
            confidence = 0.0
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=confidence,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={"engine": self.name, "sample_rate": sample_rate, "audio_ref": audio_ref},
        )


class MultiStageWakeEngine:
    """Composes an always-on front end with a burst classifier.

    The front end runs continuously in a low-power state; only a positive front
    end triggers the burst stage, which is where heavier ASR-grade models run.
    """

    name = "multi-stage"
    power_state = "low_power"

    def __init__(self, front_end: WakeWordEngine, burst: WakeWordEngine) -> None:
        self.front_end = front_end
        self.burst = burst

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        low = await self.front_end.detect(
            audio_ref=audio_ref,
            sample_rate=sample_rate,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
        )
        if not low.triggered:
            return low
        burst = await self.burst.detect(
            audio_ref=audio_ref,
            sample_rate=sample_rate,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
        )
        return WakeDetection(
            triggered=burst.triggered,
            wake_word=burst.wake_word,
            confidence=burst.confidence,
            device_id=device_id,
            stage="burst",
            power_state="burst",
            details={"front_end": low.details, "burst": burst.details},
        )


def default_wake_engine() -> WakeWordEngine:
    return MultiStageWakeEngine(PhraseWakeEngine(), PhraseWakeEngine())
