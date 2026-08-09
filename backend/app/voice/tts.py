"""Text-to-speech providers.

Production intent is a natural EVIE voice with emotion/urgency modulation and
streaming. The dev provider renders prosody controls (rate, pitch, volume,
style) so downstream code and tests can verify the speech style contract.
"""

from __future__ import annotations

from app.ev.interaction import InteractionStrategy
from app.voice.contracts import SpeechStyle, SynthesisResult, Synthesizer


def speech_style_from_strategy(strategy: InteractionStrategy) -> SpeechStyle:
    """Map the intelligence-filter strategy to TTS controls.

    Urgency raises rate and lowers warmth padding; warmth softens delivery;
    brevity compresses length target into short prosody.
    """
    urgency = strategy.urgency
    warmth = 0.9 if strategy.emotional_state in ("excited", "sad") else 0.6
    brevity = 0.9 if strategy.mode in ("emergency", "casual") else 0.3
    return SpeechStyle(
        urgency=round(urgency, 3),
        warmth=round(warmth, 3),
        brevity=round(brevity, 3),
        mode=strategy.mode,
        length_target=strategy.length_target,
        directness=strategy.directness,
    )


class MetaSynthesizer:
    """Dev/test synthesizer: emits SSML-style prosody metadata, no audio."""

    name = "meta"

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        rate = 1.0 + 0.25 * style.urgency - 0.15 * style.warmth
        pitch = 1.0 + 0.08 * style.warmth
        volume = 0.8 + 0.2 * style.urgency
        ssml = (
            f'<speak><prosody rate="{rate:.2f}" pitch="{pitch:.2f}" volume="{volume:.2f}">'
            f"{text}</prosody></speak>"
        )
        return SynthesisResult(
            text=text,
            provider=self.name,
            content_type="text/plain",
            ssml=ssml,
            duration_ms=max(200, int(len(text) * 60)),
            style=style,
        )


def default_synthesizer() -> Synthesizer:
    return MetaSynthesizer()
