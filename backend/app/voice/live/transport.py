"""WebSocket transport for EV LIVE.

The socket is a thin mapping: JSON (or raw PCM16) in, ``LiveEvent.as_dict()``
out. Intelligence, ASR, and TTS stay behind ``LiveSession`` so the protocol
can be tested without a real socket.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.auth import ActorContext
from app.config import settings
from app.db import SessionLocal
from app.voice.contracts import Transcript
from app.voice.live.behavior import to_speech_style
from app.voice.live.events import LiveEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.grok_voice import GrokVoiceBridge, grok_voice_enabled
from app.voice.live.session import LiveSession
from app.voice.live.turn_taking import TurnTakingConfig
from app.voice.pipeline import PipelineOutcome, TtsChunk, stream_chat_tts_pipeline
from app.voice.tts import get_synthesizer


def live_turn_config() -> TurnTakingConfig:
    return TurnTakingConfig(
        end_pause_ms=settings.voice_live_end_pause_ms,
        thinking_grace_ms=settings.voice_live_thinking_grace_ms,
        trailing_grace_ms=settings.voice_live_trailing_grace_ms,
        wake_hold_ms=settings.voice_live_wake_hold_ms,
        min_speech_ms=settings.voice_live_min_speech_ms,
        quiet_end_pause_ms=settings.voice_live_quiet_end_pause_ms,
        response_cooldown_ms=settings.voice_live_response_cooldown_ms,
        max_pause_ms=settings.voice_live_max_pause_ms,
    )


def live_transcriber():
    """The ASR engine attached to a live raw-PCM session.

    Never returns ``None`` and never returns a transcriber that silently
    refuses PCM. The echo/dev double is wrapped with a local faster-whisper
    fallback so stock config still hears spoken turns.
    """

    from app.voice.live.asr_feed import resolve_live_transcriber

    return resolve_live_transcriber()


def _style_dict(style) -> dict:
    if style is None:
        return {}
    return {
        "urgency": getattr(style, "urgency", 0.0),
        "warmth": getattr(style, "warmth", 0.0),
        "brevity": getattr(style, "brevity", 0.0),
        "mode": getattr(style, "mode", "casual"),
        "length_target": getattr(style, "length_target", ""),
        "directness": getattr(style, "directness", ""),
    }


def make_pipeline_responder(
    *,
    actor: str,
    device_id: str | None,
    conversation_id,
    synthesizer,
    speaker_confidence: float | None = None,
):
    """Return a ``LiveSession`` respond callback that uses the shared pipeline."""

    async def respond(text: str, envelope) -> AsyncIterator[LiveEvent]:
        transcript = Transcript(
            text=text,
            confidence=1.0,
            language="en",
            provider="live",
            details=dict(source="live_turn"),
        )
        style = to_speech_style(envelope)
        async with SessionLocal() as session:
            async for kind, payload in stream_chat_tts_pipeline(
                session,
                actor=actor,
                device_id=device_id,
                transcript=transcript,
                conversation_id=conversation_id,
                synthesizer=synthesizer,
                speaker_confidence=speaker_confidence,
                skip_listen_ack=True,
                skip_status_filler=True,
            ):
                if kind == "tts_chunk" and isinstance(payload, TtsChunk):
                    audio = getattr(payload.tts, "audio", None)
                    audio_b64 = (
                        base64.b64encode(audio).decode("ascii")
                        if audio and len(audio) <= 1_500_000
                        else None
                    )
                    yield TtsChunkEvent(
                        at_ms=0,
                        index=payload.index,
                        text=payload.text,
                        audio_b64=audio_b64,
                        audio_ref=getattr(payload.tts, "audio_ref", None),
                        content_type=getattr(payload.tts, "content_type", None),
                        duration_ms=getattr(payload.tts, "duration_ms", None),
                        provider=getattr(payload.tts, "provider", "tts"),
                    )
                elif kind == "outcome" and isinstance(payload, PipelineOutcome):
                    yield ReplyEvent(
                        at_ms=0,
                        text=payload.reply,
                        conversation_id=payload.conversation_id,
                        model=payload.model,
                        context_tokens=payload.context_tokens,
                        style=_style_dict(payload.style or style),
                    )
            await session.commit()

    return respond


async def serve_live_websocket(
    websocket: WebSocket,
    *,
    live: LiveSession,
    tick_ms: int | None = None,
    on_heartbeat=None,
) -> None:
    """Pump client frames, engine ticks, and outbound events until disconnect."""

    cadence = (tick_ms if tick_ms is not None else settings.voice_live_tick_ms) / 1000.0
    if on_heartbeat is None:
        on_heartbeat = getattr(live, "on_heartbeat", None)
    await websocket.send_json(live.ready_event().as_dict())
    if getattr(live, "grok_voice", None) is not None:
        started = await live.grok_voice.start()
        if not started:
            live.grok_voice.close()
            live.grok_voice = None
            ensure = getattr(live, "ensure_pipeline", None)
            if callable(ensure):
                ensure()

    async def recv_loop() -> None:
        try:
            while not live._closed:
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    return
                kind = message.get("type")
                if kind in {"websocket.disconnect", "websocket.close"}:
                    return
                if "bytes" in message and message["bytes"] is not None:
                    await live.handle_client(message["bytes"])
                elif "text" in message and message["text"] is not None:
                    try:
                        payload = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        await live.handle_client(payload)
        finally:
            live.close()

    async def tick_loop() -> None:
        last_beat = 0.0
        while not live._closed:
            await asyncio.sleep(max(0.02, cadence))
            if live.grok_voice is None:
                await live.tick()
            now = asyncio.get_running_loop().time()
            if on_heartbeat is not None and now - last_beat >= 30.0:
                last_beat = now
                with contextlib.suppress(Exception):
                    await on_heartbeat()

    async def send_loop() -> None:
        while True:
            try:
                event = await asyncio.wait_for(live.outbound.get(), timeout=max(0.05, cadence))
            except TimeoutError:
                if live._closed and live.outbound.empty():
                    return
                continue
            try:
                await websocket.send_json(event.as_dict())
            except (WebSocketDisconnect, RuntimeError):
                return
            if getattr(event, "fatal", False):
                live.close()
                return

    recv = asyncio.create_task(recv_loop(), name="ev-live-recv")
    tick = asyncio.create_task(tick_loop(), name="ev-live-tick")
    send = asyncio.create_task(send_loop(), name="ev-live-send")
    try:
        done, _pending = await asyncio.wait(
            {recv, tick, send}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        live.close()
        for task in (recv, tick, send):
            task.cancel()
        await asyncio.gather(recv, tick, send, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()


async def bind_live_session(
    *,
    session_id: UUID,
    ctx: ActorContext,
) -> LiveSession:
    """Load the voice session row and build a live runtime around it."""

    from app.voice.lifecycle import VoiceError, VoiceRuntime

    async with SessionLocal() as session:
        runtime = VoiceRuntime(
            session,
            master_key=settings.master_key,
            actor=ctx.actor,
        )
        row = await runtime._get_session(session_id)
        live_states = {"awake", "follow_up", "processing", "responding"}
        if row.ended_at is not None or row.state not in live_states:
            if row.verifier_name in runtime.APP_LIVE_VERIFIERS:
                runtime._reuse_push_to_talk_session(row)
                if row.conversation_id is None:
                    await runtime._bind_live_thread(row)
            else:
                raise VoiceError(
                    "Voice session is not in live conversation — open EV.app to listen",
                    status=428,
                    code="session_not_live",
                )
        conversation_id = row.conversation_id
        device_id = row.device_id
        speaker_confidence = row.speaker_confidence
        await session.commit()

    async def on_sleep(_text: str) -> None:
        from app.voice.lifecycle import VoiceRuntime

        async with SessionLocal() as db:
            runtime = VoiceRuntime(db, master_key=settings.master_key, actor=ctx.actor)
            await runtime.handle_end(session_id, reason="sleep-phrase")
            await db.commit()

    async def on_heartbeat() -> None:
        from app.voice.lifecycle import VoiceRuntime

        async with SessionLocal() as db:
            runtime = VoiceRuntime(db, master_key=settings.master_key, actor=ctx.actor)
            await runtime.refresh_live_lease(session_id)
            await db.commit()

    synthesizer = None
    transcriber = None
    respond = None
    use_grok = grok_voice_enabled()
    if not use_grok:
        synthesizer = get_synthesizer()
        transcriber = live_transcriber()
        respond = make_pipeline_responder(
            actor=ctx.actor,
            device_id=device_id,
            conversation_id=conversation_id,
            synthesizer=synthesizer,
            speaker_confidence=speaker_confidence,
        )

    live_session = LiveSession(
        session_id=str(session_id),
        conversation_id=str(conversation_id) if conversation_id else None,
        synthesizer=synthesizer,
        transcriber=transcriber,
        asr_partial_interval_ms=settings.voice_live_asr_partial_ms,
        respond=respond,
        turn_config=live_turn_config(),
        backchannel_enabled=settings.voice_live_backchannel,
        vad_threshold=settings.voice_live_vad_threshold,
        on_sleep=on_sleep,
    )
    live_session.on_heartbeat = on_heartbeat

    def ensure_pipeline() -> None:
        if live_session._respond is not None:
            return
        synth = get_synthesizer()
        live_session.attach_intelligence(
            transcriber=live_transcriber(),
            synthesizer=synth,
            respond=make_pipeline_responder(
                actor=ctx.actor,
                device_id=device_id,
                conversation_id=conversation_id,
                synthesizer=synth,
                speaker_confidence=speaker_confidence,
            ),
        )

    live_session.ensure_pipeline = ensure_pipeline
    if use_grok:
        live_session.grok_voice = GrokVoiceBridge(
            on_event=live_session.emit,
            on_tool=_grok_tool_runner(actor=ctx.actor, device_id=device_id),
            now_ms=live_session.now,
        )
    return live_session


def _grok_tool_runner(*, actor: str, device_id):
    """Execute EV life tools when Grok Voice asks for a function call."""

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        del call_id
        from app.ev.tools import dispatch

        async with SessionLocal() as db:
            result = await dispatch(
                db,
                name,
                arguments,
                actor=actor,
                allow_sensitive=True,
                device_id=device_id,
            )
            await db.commit()
        payload = {
            "ok": result.ok,
            "name": result.name,
            "result": result.result,
            "error": result.error,
        }
        return json.dumps(payload, default=str)[:4000]

    return on_tool
