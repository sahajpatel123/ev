"""Speech-to-text providers.

Production intent is Whisper-class on-device or local-server transcription with
punctuation and streaming. The dev provider turns an on-device transcript hint
into a typed Transcript so the full voice pipeline is testable end-to-end.
"""

from __future__ import annotations

from app.voice.contracts import Transcriber, Transcript


class EchoTranscriber:
    """Dev/test transcriber: accepts a transcript hint (or returns an error)."""

    name = "echo"

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
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


def default_transcriber() -> Transcriber:
    return EchoTranscriber()
