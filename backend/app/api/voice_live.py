"""Hands-free voice transport: one WebSocket carries the always-on stream.

`GET /v1/voice/hands-free/status` answers the only question that matters when EVIE
cannot hear you — *which engine is missing and what installs it* — and
`WS /v1/voice/hands-free` carries the always-on conversation:

* client → server: an ``auth`` frame, then continuous 16 kHz mono PCM16 binary
  frames plus small JSON control frames (``playback_finished``, ``cancel``,
  ``ping``).
* server → client: JSON events from :mod:`app.voice.live` (``state``, ``level``,
  ``wake``, ``partial``, ``transcript``, ``reply``, ``audio``, ``dismissed``,
  ``barge_in``, ``conversation_end``, ``error``).

Auth is the first frame rather than a query parameter so a bearer token never
lands in a URL, a proxy log, or the browser history.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext, require_actor_context
from app.config import settings
from app.db import SessionLocal, get_session
from app.models import Device, VoiceSession
from app.utils.text import sha256_hex, utcnow
from app.voice.contracts import VoiceError
from app.voice.hands_free_loop import (
    LiveConfig,
    LiveEvent,
    LiveReply,
    LiveTurn,
    LiveVoiceLoop,
)
from app.voice.lifecycle import VoiceRuntime, VoiceState
from app.voice.vosk_engine import WakeSignal

LOGGER = logging.getLogger("ev.voice.live.ws")

router = APIRouter(prefix="/v1/voice", tags=["voice"])

# One connection is one microphone; keep the concurrency bounded so a runaway
# client cannot spawn unbounded decoders.
MAX_CONNECTIONS = 4
_connections: set[int] = set()


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def hands_free_status() -> dict:
    """Everything a client needs to explain why hands-free is (not) working."""

    from app.voice.asr import get_transcriber
    from app.voice.tts import get_synthesizer, piper_binary_path, piper_voice_path
    from app.voice.vosk_engine import DEFAULT_WAKE_PHRASES, vosk_status
    from app.voice.wake import default_wake_engine

    wake_engine = default_wake_engine()
    transcriber = get_transcriber()
    synthesizer = get_synthesizer()
    speech = vosk_status()
    phrases = list(settings.voice_wake_phrases or DEFAULT_WAKE_PHRASES)
    config = LiveConfig.from_settings()
    blockers: list[str] = []
    if not speech["ready"]:
        blockers.append(speech["detail"])
    if getattr(transcriber, "name", "") == "echo":
        blockers.append(
            "ASR is the offline 'echo' double, which refuses audio: install the "
            "speech model (see wake_and_asr.detail)"
        )
    return {
        "ready": not blockers,
        "blockers": blockers,
        "wake": {
            "engine": getattr(wake_engine, "name", "unknown"),
            "phrases": phrases,
            "threshold": settings.voice_wake_vosk_threshold,
            "hears_real_audio": getattr(wake_engine, "name", "") not in ("phrase", "multi-stage"),
        },
        "asr": {
            "provider": getattr(transcriber, "name", "unknown"),
            "configured": settings.voice_asr_provider,
            "hears_real_audio": getattr(transcriber, "name", "") != "echo",
        },
        "tts": {
            "provider": getattr(synthesizer, "name", "unknown"),
            "configured": settings.voice_tts_provider,
            "server_audio": getattr(synthesizer, "name", "") != "meta",
            "voice_path": piper_voice_path(),
            "binary": piper_binary_path(),
            "note": (
                "meta provider returns prosody metadata only; clients speak the "
                "reply with the platform voice"
                if getattr(synthesizer, "name", "") == "meta"
                else "server synthesizes reply audio"
            ),
        },
        "wake_and_asr": speech,
        "audio": {
            "sample_rate": config.sample_rate,
            "frame_ms": config.frame_ms,
            "encoding": "pcm_s16le_mono",
        },
        "turn_taking": {
            "endpoint_silence_ms": config.endpoint_silence_ms,
            "wake_grace_ms": config.wake_grace_ms,
            "follow_up_ms": config.follow_up_ms,
            "barge_in_ms": config.barge_in_ms,
            "max_utterance_ms": config.max_utterance_ms,
        },
        "session": {
            "verify_speaker": settings.live_verify_speaker,
            "allow_unenrolled": settings.live_allow_unenrolled,
        },
    }


@router.get("/hands-free/status")
async def live_status(ctx: ActorContext = Depends(require_actor_context)) -> dict:
    return hands_free_status()


@router.get("/hands-free/diagnostics")
async def live_diagnostics(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Readiness plus the owner-enrollment facts that gate a hands-free session."""

    runtime = VoiceRuntime(session, master_key=settings.master_key)
    enrollment = await runtime.enrollment_status()
    status = hands_free_status()
    status["owner"] = enrollment
    return status


# --------------------------------------------------------------------------- #
# Responder: bridges the audio loop to the voice lifecycle
# --------------------------------------------------------------------------- #


class LifecycleResponder:
    """Runs each hands-free turn through the normal voice lifecycle.

    A fresh database session per turn keeps the long-lived socket from pinning a
    connection, and every gate (consent, sensitive-command re-verification,
    memory writes, TTS) behaves exactly as it does over HTTP.
    """

    def __init__(self, *, device_id: str, ctx: ActorContext, actor: str) -> None:
        self.device_id = device_id
        self.ctx = ctx
        self.actor = actor
        self.session_id: UUID | None = None
        self.conversation_id: UUID | None = None
        self._interrupted = asyncio.Event()

    def _runtime(self, session: AsyncSession) -> VoiceRuntime:
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            actor=self.actor,
            follow_up_seconds=max(1, int(settings.live_follow_up_ms / 1000)),
        )

    async def open_session(self, *, wake: WakeSignal, wav: bytes) -> dict:
        async with SessionLocal() as session:
            runtime = self._runtime(session)
            try:
                outcome = await runtime.handle_hands_free_wake(
                    device_id=self.device_id,
                    wake_word=wake.phrase or "evie",
                    wake_confidence=wake.confidence,
                    wake_audio_b64=base64.b64encode(wav).decode("ascii") if wav else None,
                )
            except VoiceError:
                await session.commit()
                raise
            await session.commit()
        if outcome.session_id is None:
            raise VoiceError(
                outcome.message or "Hands-free wake refused",
                status=403,
                code="hands_free_refused",
            )
        self.session_id = UUID(outcome.session_id)
        return {
            "session_id": outcome.session_id,
            "state": outcome.state,
            "owner_enrolled": outcome.owner_enrolled,
            "message": outcome.message,
        }

    async def respond(self, turn: LiveTurn) -> LiveReply:
        self._interrupted.clear()
        if self.session_id is None:
            raise VoiceError("No hands-free session", status=409, code="no_session")
        async with SessionLocal() as session:
            runtime = self._runtime(session)
            row = await session.get(VoiceSession, self.session_id)
            if row is None or row.ended_at is not None:
                raise VoiceError(
                    "Hands-free session ended", status=428, code="session_ended"
                )
            # The lifecycle decides which transition is legal; the audio loop
            # does not get to assert it.
            follow_up = row.state == VoiceState.FOLLOW_UP
            try:
                outcome = await runtime.handle_utterance(
                    session_id=self.session_id,
                    transcript=turn.transcript,
                    ctx=self.ctx,
                    conversation_id=self.conversation_id,
                    follow_up=follow_up,
                )
            except VoiceError:
                await session.commit()
                raise
            await session.commit()
        if outcome.conversation_id:
            self.conversation_id = UUID(outcome.conversation_id)
        tts = outcome.tts
        return LiveReply(
            text=outcome.reply,
            audio=tts.audio if tts else None,
            content_type=tts.content_type if tts else None,
            audio_ref=tts.audio_ref if tts else None,
            duration_ms=tts.duration_ms if tts else None,
            session_id=str(self.session_id),
            provider=tts.provider if tts else None,
            details={"model": outcome.model} if outcome.model else {},
        )

    async def interrupt(self) -> None:
        self._interrupted.set()
        if self.session_id is None:
            return
        async with SessionLocal() as session:
            runtime = self._runtime(session)
            with contextlib.suppress(VoiceError):
                await runtime.handle_barge_in(self.session_id)
            await session.commit()

    async def close(self, *, reason: str) -> None:
        if self.session_id is None:
            return
        session_id, self.session_id = self.session_id, None
        self.conversation_id = None
        async with SessionLocal() as session:
            runtime = self._runtime(session)
            with contextlib.suppress(VoiceError):
                await runtime.handle_end(session_id, reason=reason)
            await session.commit()


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


async def _authenticate(token: str) -> ActorContext | None:
    """Resolve a bearer token to an actor without FastAPI's header plumbing."""

    if not token:
        return None
    if secrets.compare_digest(token, settings.master_key):
        return ActorContext(actor="master", is_master=True)
    async with SessionLocal() as session:
        result = await session.execute(
            select(Device).where(
                Device.token_hash == sha256_hex(token),
                Device.revoked_at.is_(None),
            )
        )
        device = result.scalar_one_or_none()
        if device is None:
            return None
        device.last_seen_at = utcnow()
        await session.commit()
        return ActorContext(
            actor=f"device:{device.name}",
            device_id=device.id,
            is_master=False,
            device=device,
        )


def build_loop(
    *,
    responder: Any,
    emit: Any,
    device_id: str,
    config: LiveConfig | None = None,
) -> LiveVoiceLoop:
    """Wire the real engines into the hands-free loop."""

    from app.audio.vad import default_vad_engine
    from app.voice.vosk_engine import VoskStreamingRecognizer, VoskWakeSpotter

    resolved = config or LiveConfig.from_settings()
    return LiveVoiceLoop(
        responder=responder,
        emit=emit,
        spotter=VoskWakeSpotter(sample_rate=resolved.sample_rate),
        recognizer_factory=lambda: VoskStreamingRecognizer(sample_rate=resolved.sample_rate),
        vad=default_vad_engine(),
        config=resolved,
        device_id=device_id,
    )


@router.websocket("/hands-free")
async def live_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    key = id(websocket)
    if len(_connections) >= MAX_CONNECTIONS:
        await websocket.send_json(
            {
                "type": "error",
                "data": {"code": "too_many_streams", "message": "Too many live streams"},
            }
        )
        await websocket.close(code=1013)
        return
    _connections.add(key)
    loop: LiveVoiceLoop | None = None
    try:
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=1008)
            return
        ctx = await _authenticate(str(hello.get("token") or ""))
        if ctx is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "data": {"code": "unauthorized", "message": "Invalid bearer token"},
                }
            )
            await websocket.close(code=1008)
            return
        device_id = str(
            ctx.device_id if ctx.is_device else (hello.get("device_id") or "web-hands-free")
        )
        status = hands_free_status()
        await websocket.send_json({"type": "ready", "data": status})
        if not status["ready"]:
            # Never pretend to listen: say what is missing and hang up.
            await websocket.send_json(
                {
                    "type": "error",
                    "data": {
                        "code": "engines_unavailable",
                        "message": " ".join(status["blockers"]),
                    },
                }
            )
            await websocket.close(code=1011)
            return

        async def emit(event: LiveEvent) -> None:
            await websocket.send_json({"type": event.type, "data": event.data})

        responder = LifecycleResponder(
            device_id=device_id, ctx=ctx, actor=ctx.actor if ctx.is_device else "voice"
        )
        loop = build_loop(responder=responder, emit=emit, device_id=device_id)
        LOGGER.info("hands-free stream open device=%s actor=%s", device_id, ctx.actor)

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload:
                await loop.feed(payload)
                continue
            text = message.get("text")
            if not text:
                continue
            await _handle_control(websocket, loop, text)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a socket failure must not take down the app
        LOGGER.exception("hands-free stream failed")
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "error",
                    "data": {"code": "stream_failed", "message": "Live stream failed"},
                }
            )
    finally:
        _connections.discard(key)
        if loop is not None:
            with contextlib.suppress(Exception):
                await loop.close(reason="disconnect")
        with contextlib.suppress(Exception):
            await websocket.close()


async def _handle_control(websocket: WebSocket, loop: LiveVoiceLoop, text: str) -> None:
    import json

    try:
        frame = json.loads(text)
    except ValueError:
        return
    kind = frame.get("type")
    if kind == "playback_finished":
        await loop.playback_finished()
    elif kind == "cancel":
        await loop.cancel(reason=str(frame.get("reason") or "client_cancel"))
    elif kind == "ping":
        await websocket.send_json({"type": "pong", "data": {}})
