"""Shared voice utterance pipeline: ASR → chat/intelligence/memory → TTS.

Both the voice-session lifecycle and the centralized 24/7 runtime use this
single path so voice behavior stays provider-agnostic and identical everywhere.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas import ChatRequest
from app.voice.contracts import (
    SpeechStyle,
    SynthesisResult,
    Transcriber,
    Transcript,
    VoiceError,
)
from app.voice.speech import (
    choose_listen_ack,
    choose_voice_filler,
    concat_wav_bytes,
    listen_ack_style,
    owner_facing_speech,
    pop_speakable,
    starts_with_evie,
)
from app.voice.tts import speech_style_from_strategy


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


@dataclass
class TtsChunk:
    index: int
    text: str
    tts: SynthesisResult


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
    if text and audio_b64 is None and audio_ref is None:
        return Transcript(
            text=text,
            confidence=1.0,
            language=language,
            provider="text",
            details={"source": "supplied"},
        )
    try:
        return await asyncio.wait_for(
            transcriber.transcribe(
                audio_ref=audio_ref,
                audio_b64=audio_b64,
                text_hint=text,
                language=language,
            ),
            timeout=settings.voice_asr_timeout_seconds,
        )
    except TimeoutError as exc:
        raise VoiceError(
            "Speech recognition took too long",
            status=504,
            code="asr_timeout",
        ) from exc


async def _synth_sentence(
    synthesizer,
    text: str,
    style: SpeechStyle,
    *,
    timeout: float | None = None,
) -> SynthesisResult:
    speakable = owner_facing_speech(text)
    if not speakable:
        return SynthesisResult(
            text="",
            provider=getattr(synthesizer, "name", "tts"),
            style=style,
            degraded=False,
            details={"reason": "not_speakable"},
        )
    limit = (
        float(timeout)
        if timeout is not None
        else min(8.0, float(settings.voice_tts_timeout_seconds))
    )
    try:
        return await asyncio.wait_for(
            synthesizer.synthesize(speakable, style=style),
            timeout=limit,
        )
    except Exception as exc:  # noqa: BLE001 - speak the words even if TTS dies
        reason = "tts_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        return SynthesisResult(
            text=speakable,
            provider=getattr(synthesizer, "name", "tts"),
            style=style,
            degraded=True,
            details={"reason": reason},
        )


async def synthesize_owner_facing(
    synthesizer,
    text: str,
    *,
    style: SpeechStyle | None = None,
) -> SynthesisResult:
    """Public TTS entry: only the clear owner-facing reply is synthesized."""

    return await _synth_sentence(synthesizer, text, style or SpeechStyle())


async def stream_chat_tts_pipeline(
    session: AsyncSession,
    *,
    actor: str,
    device_id: str | None,
    transcript: Transcript,
    conversation_id=None,
    synthesizer,
    speaker_confidence: float | None = None,
) -> AsyncIterator[tuple[str, object]]:
    """Yield ``tts_chunk`` events as soon as a spoken unit is ready, then outcome."""

    from app.ev.assistant import resolve_live_thread

    thread = await resolve_live_thread(session, conversation_id)
    from app.api.core import run_chat_pipeline
    from app.filter.envelope import SpeakerIdentity

    events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    style = SpeechStyle()

    async def on_delta(text: str) -> None:
        await events.put(("delta", text))

    async def on_turn_ready(strategy) -> None:
        await events.put(("style", speech_style_from_strategy(strategy)))

    async def run_llm() -> None:
        try:
            pipeline = await asyncio.wait_for(
                run_chat_pipeline(
                    ChatRequest(
                        message=transcript.text,
                        conversation_id=thread.id,
                        device_id=device_id,
                        allow_sensitive_tools=True,
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
                    text_delta_callback=on_delta,
                    on_turn_ready=on_turn_ready,
                ),
                timeout=settings.voice_turn_timeout_seconds,
            )
            await events.put(("pipeline", pipeline))
        except BaseException as exc:  # noqa: BLE001 - forwarded to consumer
            await events.put(("error", exc))

    llm_task = asyncio.create_task(run_llm())
    buffer = ""
    index = 0
    wavs: list[bytes] = []
    last_tts: SynthesisResult | None = None
    pipeline: dict[str, Any] | None = None
    # WAV engines stream per sentence (start speaking while the model writes);
    # non-WAV engines (Edge MP3) buffer the text and synthesize once, because
    # MP3 chunks cannot be concatenated without re-encoding.
    streamable = bool(getattr(synthesizer, "streamable_output", True))
    filler_task: asyncio.Task | None = None
    evie_turn = starts_with_evie(transcript.text)
    # Evie-start always speaks a listen-ack first, including Edge/MP3
    # (streamable_output=False). Those engines still cannot concat later
    # sentences, but the ack is its own playable chunk.
    if evie_turn:
        filler_text = choose_listen_ack(transcript.text)
        filler_style = listen_ack_style()
        # Talk only plays tts_chunk.audio_b64. Wait the real engine
        # budget (Edge's EV_VOICE_TTS_TIMEOUT_SECONDS), not a 2s cut
        # that yields a silent first chunk.
        ack_timeout = max(float(settings.voice_tts_timeout_seconds), 8.0)
        filler_task = asyncio.create_task(
            _synth_sentence(
                synthesizer, filler_text, filler_style, timeout=ack_timeout
            )
        )
    elif streamable:
        filler_text = choose_voice_filler(transcript.text)
        filler_style = SpeechStyle(warmth=0.9, brevity=0.95, urgency=0.15)
        filler_task = asyncio.create_task(
            _synth_sentence(synthesizer, filler_text, filler_style)
        )
    try:
        if filler_task is not None:
            filler = await filler_task
            if filler.audio:
                yield ("tts_chunk", TtsChunk(index=0, text=filler_text, tts=filler))
                index = 1
                # Do not concat a non-WAV ack into the final reply audio.
                if streamable:
                    wavs.append(filler.audio)
                    last_tts = filler
        while True:
            kind, payload = await events.get()
            if kind == "style":
                style = payload  # type: ignore[assignment]
                continue
            if kind == "delta":
                buffer += str(payload)
                if not streamable:
                    # Single-shot engine: keep buffering; the full reply is
                    # synthesized once below instead of dropping sentences.
                    continue
                while True:
                    sentence, buffer = pop_speakable(buffer)
                    if not sentence:
                        break
                    chunk = await _synth_sentence(synthesizer, sentence, style)
                    if chunk.audio:
                        wavs.append(chunk.audio)
                        last_tts = chunk
                        yield ("tts_chunk", TtsChunk(index=index, text=sentence, tts=chunk))
                        index += 1
                continue
            if kind == "error":
                from fastapi import HTTPException

                if isinstance(payload, VoiceError):
                    raise payload
                if isinstance(payload, HTTPException):
                    raise VoiceError(
                        str(payload.detail),
                        status=int(payload.status_code or 503),
                        code="chat",
                    ) from payload
                if isinstance(payload, BaseException):
                    raise VoiceError(str(payload), status=503, code="voice_pipeline") from payload
                raise VoiceError(str(payload), status=503, code="voice_pipeline")
            if not isinstance(payload, dict):
                raise VoiceError("Voice reply failed", status=503, code="voice_pipeline")
            pipeline = payload
            break
        if streamable:
            leftover, _ = pop_speakable(buffer, flush=True)
        else:
            # Single-shot engine: the whole buffered reply is one unit.
            leftover = buffer.strip() or None
            buffer = ""
        if leftover:
            chunk = await _synth_sentence(synthesizer, leftover, style)
            if chunk.audio:
                wavs.append(chunk.audio)
                last_tts = chunk
                yield ("tts_chunk", TtsChunk(index=index, text=leftover, tts=chunk))
    finally:
        if not llm_task.done():
            await llm_task

    if pipeline is None:
        raise VoiceError("Voice reply failed", status=503, code="voice_pipeline")

    combined = concat_wav_bytes(wavs)
    if last_tts is not None and combined:
        last_tts.audio = combined
        last_tts.text = pipeline["result"].text
        synthesis = await persist_tts_audio(last_tts)
    else:
        synthesis = await persist_tts_audio(
            await _synth_sentence(synthesizer, pipeline["result"].text, style)
        )

    yield (
        "outcome",
        PipelineOutcome(
            transcript=transcript,
            reply=pipeline["result"].text,
            conversation_id=str(thread.id),
            tts=synthesis,
            style=style,
            model=pipeline["result"].model,
            context_tokens=pipeline["context_tokens"],
            memory_deltas=pipeline["memory_deltas"],
        ),
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

    outcome: PipelineOutcome | None = None
    async for kind, payload in stream_chat_tts_pipeline(
        session,
        actor=actor,
        device_id=device_id,
        transcript=transcript,
        conversation_id=conversation_id,
        synthesizer=synthesizer,
        speaker_confidence=speaker_confidence,
    ):
        if kind == "outcome":
            outcome = payload  # type: ignore[assignment]
    if outcome is None:
        raise VoiceError("Voice reply failed", status=503, code="voice_pipeline")
    return outcome
