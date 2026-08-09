"""Text-to-speech providers.

Production intent is a natural EVIE voice with emotion/urgency modulation and
streaming. The dev provider renders prosody metadata; the OpenAI-compatible
provider talks to any ``/audio/speech`` endpoint and maps urgency/warmth/brevity
to speed and spoken instructions.
"""

from __future__ import annotations

import httpx

from app.config import settings
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


def _tts_instructions(style: SpeechStyle) -> str:
    warmth = "warm and reassuring" if style.warmth >= 0.7 else "steady"
    pacing = "fast and clipped" if style.urgency >= 0.7 else "measured"
    brevity = "Keep the reply short and direct." if style.brevity >= 0.6 else ""
    return (
        f"Speak with a {warmth} tone at a {pacing} pace. "
        f"Match the {style.mode} register. {brevity}".strip()
    )


class OpenAICompatSynthesizer:
    """Natural TTS via any OpenAI-compatible /audio/speech endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        fmt: str = "mp3",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.fmt = fmt
        self._client = client

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        speed = round(
            max(0.5, min(1.5, 0.95 + 0.30 * style.urgency - 0.15 * style.warmth)),
            2,
        )
        payload: dict = {
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "response_format": self.fmt,
            "speed": speed,
        }
        if "tts" in self.model.lower():
            payload["instructions"] = _tts_instructions(style)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        close = False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)
            close = True
        try:
            resp = await self._client.post(
                f"{self.base_url}/audio/speech",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        finally:
            if close:
                await self._client.aclose()
        return SynthesisResult(
            text=text,
            provider=self.name,
            audio=resp.content,
            content_type=f"audio/{self.fmt}",
            duration_ms=max(200, int(len(text) * 60)),
            style=style,
        )


def get_synthesizer() -> Synthesizer:
    if settings.voice_tts_provider == "openai_compat":
        if not settings.voice_tts_base_url:
            raise RuntimeError("EV_VOICE_TTS_BASE_URL is required for openai_compat TTS")
        return OpenAICompatSynthesizer(
            base_url=settings.voice_tts_base_url,
            api_key=settings.voice_tts_api_key,
            model=settings.voice_tts_model,
            voice=settings.voice_tts_voice,
            fmt=settings.voice_tts_format,
        )
    return MetaSynthesizer()
