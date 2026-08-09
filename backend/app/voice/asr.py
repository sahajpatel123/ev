"""Speech-to-text providers.

Production intent is Whisper-class on-device or local-server transcription with
punctuation and streaming. The dev provider turns an on-device transcript hint
into a typed Transcript; the OpenAI-compatible provider talks to any
``/audio/transcriptions`` endpoint so ASR never couples to one platform.
"""

from __future__ import annotations

import base64

import httpx

from app.config import settings
from app.voice.contracts import Transcriber, Transcript


class EchoTranscriber:
    """Dev/test transcriber: accepts a transcript hint (or returns an error)."""

    name = "echo"

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript:
        if text_hint is None or not text_hint.strip():
            raise ValueError("ASR requires an audio sample or a transcript hint (dev provider)")
        return Transcript(
            text=text_hint.strip(),
            confidence=0.97,
            language=language,
            provider=self.name,
            duration_ms=None,
            audio_ref=audio_ref,
        )


class OpenAICompatTranscriber:
    """Whisper-class ASR via any OpenAI-compatible /audio/transcriptions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str = "whisper-1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript:
        if audio_b64 is None:
            if text_hint is None or not text_hint.strip():
                raise ValueError("ASR requires audio_b64 or a transcript hint")
            return Transcript(
                text=text_hint.strip(),
                confidence=0.97,
                language=language,
                provider=self.name,
                audio_ref=audio_ref,
            )
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise ValueError("audio_b64 must be valid base64") from exc
        if not audio:
            raise ValueError("audio_b64 must not be empty")

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        close = False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
            close = True
        try:
            resp = await self._client.post(
                f"{self.base_url}/audio/transcriptions",
                headers=headers,
                data={"model": self.model, "language": language},
                files={"file": ("voice.wav", audio, "audio/wav")},
            )
            resp.raise_for_status()
        finally:
            if close:
                await self._client.aclose()
        data = resp.json()
        text = (data.get("text") or "").strip()
        if not text:
            raise ValueError("ASR returned an empty transcript")
        raw_confidence = data.get("confidence")
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float))
            else 0.95
        )
        return Transcript(
            text=text,
            confidence=round(confidence, 4),
            language=language,
            provider=self.name,
            audio_ref=audio_ref,
        )


def get_transcriber() -> Transcriber:
    if settings.voice_asr_provider == "openai_compat":
        if not settings.voice_asr_base_url:
            raise RuntimeError("EV_VOICE_ASR_BASE_URL is required for openai_compat ASR")
        return OpenAICompatTranscriber(
            base_url=settings.voice_asr_base_url,
            api_key=settings.voice_asr_api_key,
            model=settings.voice_asr_model,
        )
    return EchoTranscriber()
