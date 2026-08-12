"""Shared voice utterance pipeline: ASR → chat/intelligence/memory → TTS.

Both the voice-session lifecycle and the centralized 24/7 runtime use this
single path so voice behavior stays provider-agnostic and identical everywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import conversation
from app.schemas import ChatRequest
from app.voice.contracts import (
    SpeechStyle,
    SynthesisResult,
    Transcriber,
    Transcript,
)


@dataclass
class PipelineOutcome:
    transcript: Transcript
    reply: str
    conversation_id: str
    tts: SynthesisResult
    style: SpeechStyle
    model: str | None
    context_tokens: int
    memory_deltas: list[dict]


def _content_extension(content_type: str | None) -> str:
    return {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mp3": "mp3",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "audio/flac": "flac",
    }.get((content_type or "").lower().split(";")[0].strip(), "bin")


async def persist_tts_audio(result: SynthesisResult) -> SynthesisResult:
    """Persist synthesized bytes to the object store and populate ``audio_ref``.

    Content-addressed (sha256) so identical replies dedupe on disk. The ref
    uses the ``ev://`` scheme; the streaming endpoint resolves it.
    """

    if result.audio is None or result.audio_ref:
        return result
    digest = hashlib.sha256(result.audio).hexdigest()
    extension = _content_extension(result.content_type)
    key = f"voice/tts/{digest[:2]}/{digest}.{extension}"
    from app.storage.object_store import get_object_store

    await get_object_store().put(key, result.audio, result.content_type or "audio/wav")
    result.audio_ref = f"ev://{key}"
    return result


async def transcribe_input(
    transcriber: Transcriber,
    *,
    text: str | None = None,
    audio_b64: str | None = None,
    audio_ref: str | None = None,
    language: str = "en",
) -> Transcript:
    return await transcriber.transcribe(
        audio_ref=audio_ref,
        audio_b64=audio_b64,
        text_hint=text,
        language=language,
    )


async def run_chat_tts_pipeline(
    session: AsyncSession,
    *,
    actor: str,
    device_id: str | None,
    transcript: Transcript,
    conversation_id=None,
    synthesizer,
    speaker_confidence: float | None = None,
) -> PipelineOutcome:
    """Conversation/intelligence-filter/memory/provider pipeline + TTS reply."""
    thread = await conversation.resolve_thread(session, conversation_id)
    from app.api.core import run_chat_pipeline
    from app.filter.envelope import SpeakerIdentity

    pipeline = await run_chat_pipeline(
        ChatRequest(
            message=transcript.text,
            conversation_id=thread.id,
            device_id=device_id,
        ),
        session,
        actor,
        thread_id=thread.id,
        source="voice",
        user_event_type="voice.transcript",
        event_privacy="sensitive",
        speaker=SpeakerIdentity(
            actor_id=actor,
            verified=True,
            confidence=speaker_confidence if speaker_confidence is not None else 1.0,
            method="voiceprint",
        ),
    )
    strategy = pipeline["strategy"]
    from app.voice.tts import speech_style_from_strategy

    style = speech_style_from_strategy(strategy)
    synthesis = await synthesizer.synthesize(pipeline["result"].text, style=style)
    synthesis = await persist_tts_audio(synthesis)
    return PipelineOutcome(
        transcript=transcript,
        reply=pipeline["result"].text,
        conversation_id=str(thread.id),
        tts=synthesis,
        style=style,
        model=pipeline["result"].model,
        context_tokens=pipeline["context_tokens"],
        memory_deltas=pipeline["memory_deltas"],
    )
