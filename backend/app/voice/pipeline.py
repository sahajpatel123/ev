"""Shared voice utterance pipeline: ASR → chat/intelligence/memory → TTS.

Both the voice-session lifecycle and the centralized 24/7 runtime use this
single path so voice behavior stays provider-agnostic and identical everywhere.
"""

from __future__ import annotations

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
) -> PipelineOutcome:
    """Conversation/intelligence-filter/memory/provider pipeline + TTS reply."""
    thread = await conversation.resolve_thread(session, conversation_id)
    from app.api.core import run_chat_pipeline

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
    )
    strategy = pipeline["strategy"]
    from app.voice.tts import speech_style_from_strategy

    style = speech_style_from_strategy(strategy)
    synthesis = await synthesizer.synthesize(pipeline["result"].text, style=style)
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
