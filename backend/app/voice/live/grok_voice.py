"""Grok Voice Think Fast 2.0 — speech-to-speech brain for EV LIVE.

``grok-voice-think-fast-2.0`` is not a chat-completions model. It speaks
the official xAI Realtime API (``wss://api.x.ai/v1/realtime``): PCM in,
PCM + transcript + tool calls out, with server VAD. EV.app still talks
``WS /v1/voice/live``; this module is the upstream socket behind that.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from app.audio.capture import pcm_to_wav_bytes
from app.config import settings
from app.ev.personality import identity_block
from app.voice.live.events import (
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    LiveEvent,
    PartialTranscriptEvent,
    ReplyEvent,
    TtsChunkEvent,
)

logger = logging.getLogger("ev.voice.live.grok")

OnLiveEvent = Callable[[LiveEvent], Awaitable[None]]
OnToolCall = Callable[[str, dict, str], Awaitable[str]]

# First audio chunk leaves as soon as ~80 ms of PCM lands so TTFA stays small.
_FIRST_WAV_BYTES = int(16000 * 2 * 0.08)
_NEXT_WAV_BYTES = int(16000 * 2 * 0.16)

_AUDIO_DELTA_TYPES = frozenset(
    {
        "response.output_audio.delta",
        "response.audio.delta",
    }
)
_TRANSCRIPT_DELTA_TYPES = frozenset(
    {
        "response.output_audio_transcript.delta",
        "response.audio_transcript.delta",
        "response.output_text.delta",
    }
)
_INPUT_TRANSCRIPT_TYPES = frozenset(
    {
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.delta",
        "input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.updated",
    }
)
_SPEECH_STARTED_TYPES = frozenset(
    {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_started.delta",
    }
)

# Life tools Grok Voice is allowed to call. The rest of the registry stays
# on the typed-chat / pipeline path so the realtime session stays snappy.
GROK_VOICE_TOOL_NAMES = (
    "search_memory",
    "search_web",
    "get_weather",
    "set_reminder",
    "send_message",
    "place_call",
    "present",
    "get_upcoming_alerts",
    "list_messages",
    "list_mail",
    "resolve_contact",
    "calculate",
    "whats_on_my_plate",
    "brief_me",
    "where_is",
    "get_person",
)


def grok_voice_enabled() -> bool:
    """True when live conversation should speak through Grok Voice.

    Typed chat stays on ``EV_CHAT_PROVIDER`` (DeepSeek). Spoken live turns
    use Think Fast 2.0 whenever an xAI key is present, unless the owner
    forced ``EV_VOICE_LIVE_BRAIN=pipeline``.
    """

    brain = (settings.voice_live_brain or "auto").strip().lower()
    if brain == "pipeline":
        return False
    if not (settings.xai_api_key or "").strip():
        return False
    return brain in {"auto", "xai"}


def grok_voice_url(*, model: str | None = None, realtime_url: str | None = None) -> str:
    base = (realtime_url or settings.xai_voice_realtime_url).rstrip("/")
    pinned = (model or settings.xai_voice_model).strip() or "grok-voice-think-fast-2.0"
    return f"{base}?{urlencode({'model': pinned})}"


def grok_voice_instructions(*, name: str | None = None, description: str | None = None) -> str:
    block = identity_block(
        name or settings.persona_name,
        description or settings.persona_description,
        compact=True,
    )
    return (
        f"{block}\n"
        "You are in a live spoken conversation. Hear the owner and answer "
        "in your voice. Short sentences. One question at a time. Use tools "
        "when the owner asked you to act (text, call, remind, search, show). "
        "If they only said your name, say Yes? and wait. Do not wait for a "
        "wake word — the app is already open."
    )


def grok_voice_tools(specs: list[dict] | None = None) -> list[dict]:
    """xAI realtime function payloads (flat ``type=function``, not nested)."""

    from app.ev.tools import list_tools

    wanted = set(GROK_VOICE_TOOL_NAMES)
    payload: list[dict] = []
    for spec in specs if specs is not None else list_tools():
        name = spec.get("name")
        if name not in wanted:
            continue
        payload.append(
            {
                "type": "function",
                "name": name,
                "description": spec.get("description") or "",
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return payload


def grok_session_update() -> dict:
    """``session.update`` body for 16 kHz PCM matching EV.app's live mic."""

    return {
        "type": "session.update",
        "session": {
            "instructions": grok_voice_instructions(),
            "voice": settings.xai_voice_voice or "eve",
            "reasoning": {"effort": "none"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(settings.xai_voice_vad_threshold),
                "silence_duration_ms": int(settings.xai_voice_silence_ms),
                "prefix_padding_ms": 280,
            },
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 16000}},
                "output": {"format": {"type": "audio/pcm", "rate": 16000}},
            },
            "tools": grok_voice_tools(),
        },
    }


def pcm16_wav(pcm: bytes, *, sample_rate: int = 16000) -> bytes:
    return pcm_to_wav_bytes(pcm, sample_rate=sample_rate)


class GrokVoiceBridge:
    """One upstream Grok Voice realtime socket bound to an EV LIVE session."""

    def __init__(
        self,
        *,
        on_event: OnLiveEvent,
        on_tool: OnToolCall | None = None,
        connect: Callable[..., Awaitable[Any]] | None = None,
        now_ms: Callable[[], int] | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_tool = on_tool
        self._connect = connect or _default_connect
        self._now = now_ms or (lambda: 0)
        self._api_key = api_key if api_key is not None else settings.xai_api_key
        self._model = model or settings.xai_voice_model
        self._ws: Any = None
        self._pump: asyncio.Task | None = None
        self._closed = False
        self._out_pcm = bytearray()
        self._chunk_index = 0
        self._reply_text = ""
        self._first_audio = True
        self._pending_tools = 0
        self._failed = False

    async def start(self) -> bool:
        if self._ws is not None:
            return True
        if self._closed or self._failed:
            return False
        if not (self._api_key or "").strip():
            self._failed = True
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="xai_missing_key",
                    message="EV_XAI_API_KEY is empty — Grok Voice cannot start",
                    fatal=False,
                )
            )
            return False
        url = grok_voice_url(model=self._model)
        try:
            self._ws = await self._connect(
                url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except Exception as exc:  # noqa: BLE001 - surface, keep EV LIVE open
            self._failed = True
            logger.exception("Grok Voice realtime connect failed")
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="xai_voice_connect",
                    message=f"Grok Voice connect failed: {type(exc).__name__}: {exc}"[:240],
                    fatal=False,
                )
            )
            return False
        await self._send(grok_session_update())
        self._pump = asyncio.create_task(self._recv_loop(), name="ev-grok-voice-recv")
        return True

    async def append_pcm(self, pcm: bytes) -> None:
        if not pcm or self._closed:
            return
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def send_text(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw or self._closed:
            return
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": raw}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def cancel(self) -> None:
        self._out_pcm.clear()
        self._first_audio = True
        if self._ws is not None:
            await self._send({"type": "response.cancel"})

    def close(self) -> None:
        self._closed = True
        task = self._pump
        if task is not None and not task.done():
            task.cancel()
        self._pump = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                closer = getattr(ws, "close", None)
                if closer is not None:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        raw = json.dumps(payload)
        try:
            await ws.send(raw)
        except Exception:  # noqa: BLE001 - a dropped frame must not kill LIVE
            logger.debug("Grok Voice send failed", exc_info=True)

    async def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                if self._closed:
                    return
                event = _parse_event(message)
                if event:
                    await self._handle_upstream(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep EV LIVE alive
            logger.exception("Grok Voice recv failed")
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="xai_voice_stream",
                    message=f"Grok Voice stream ended: {type(exc).__name__}: {exc}"[:240],
                    fatal=False,
                )
            )

    async def _handle_upstream(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        if kind in _SPEECH_STARTED_TYPES:
            self._out_pcm.clear()
            self._first_audio = True
            await self._on_event(BargeInEvent(at_ms=self._now(), reason="user_speech"))
            await self._send({"type": "response.cancel"})
            return
        if kind in _INPUT_TRANSCRIPT_TYPES:
            text = _transcript_text(event)
            if text:
                await self._on_event(
                    FinalTranscriptEvent(
                        at_ms=self._now(),
                        text=text,
                        confidence=1.0,
                        provider="grok-voice",
                    )
                    if "completed" in kind
                    else PartialTranscriptEvent(
                        at_ms=self._now(),
                        text=text,
                        sequence=0,
                        stable=False,
                        confidence=0.0,
                    )
                )
            return
        if kind in _TRANSCRIPT_DELTA_TYPES:
            delta = str(event.get("delta") or event.get("text") or "")
            if delta:
                self._reply_text += delta
                await self._on_event(
                    PartialTranscriptEvent(
                        at_ms=self._now(),
                        text=self._reply_text,
                        sequence=self._chunk_index,
                        stable=False,
                        confidence=0.0,
                    )
                )
            return
        if kind in _AUDIO_DELTA_TYPES:
            await self._buffer_audio(event)
            return
        if kind == "response.function_call_arguments.done":
            await self._run_tool(event)
            return
        if kind in {"response.done", "response.output_audio.done", "response.audio.done"}:
            await self._flush_audio(force=True)
            if kind == "response.done" and self._pending_tools <= 0:
                text = (self._reply_text or _transcript_text(event) or "").strip()
                await self._on_event(
                    ReplyEvent(
                        at_ms=self._now(),
                        text=text,
                        model=self._model,
                    )
                )
                self._reply_text = ""
                self._chunk_index = 0
                self._first_audio = True
            return
        if kind == "error":
            message = str(
                event.get("error", {}).get("message")
                if isinstance(event.get("error"), dict)
                else event.get("message") or event.get("error") or "Grok Voice error"
            )
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="xai_voice",
                    message=message[:240],
                    fatal=False,
                )
            )

    async def _buffer_audio(self, event: dict) -> None:
        raw = event.get("delta") or event.get("audio") or ""
        if not raw:
            return
        try:
            pcm = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            return
        self._out_pcm.extend(pcm)
        threshold = _FIRST_WAV_BYTES if self._first_audio else _NEXT_WAV_BYTES
        while len(self._out_pcm) >= threshold:
            chunk = bytes(self._out_pcm[:threshold])
            del self._out_pcm[:threshold]
            await self._emit_wav(chunk)
            self._first_audio = False
            threshold = _NEXT_WAV_BYTES

    async def _flush_audio(self, *, force: bool) -> None:
        if not self._out_pcm:
            return
        if not force and len(self._out_pcm) < _FIRST_WAV_BYTES:
            return
        chunk = bytes(self._out_pcm)
        self._out_pcm.clear()
        await self._emit_wav(chunk)
        self._first_audio = False

    async def _emit_wav(self, pcm: bytes) -> None:
        if not pcm:
            return
        wav = pcm16_wav(pcm, sample_rate=16000)
        audio_b64 = base64.b64encode(wav).decode("ascii")
        text = self._reply_text.strip() if self._chunk_index == 0 else ""
        event = TtsChunkEvent(
            at_ms=self._now(),
            index=self._chunk_index,
            text=text,
            audio_b64=audio_b64,
            content_type="audio/wav",
            duration_ms=int((len(pcm) / 2) / 16.0),
            provider="grok-voice",
        )
        self._chunk_index += 1
        await self._on_event(event)

    async def _run_tool(self, event: dict) -> None:
        name = str(event.get("name") or "")
        call_id = str(event.get("call_id") or event.get("id") or "")
        raw_args = event.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            arguments = {"raw": raw_args}
        self._pending_tools += 1
        output = "{}"
        if self._on_tool is not None and name:
            try:
                output = await self._on_tool(name, arguments, call_id)
            except Exception as exc:  # noqa: BLE001
                output = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        self._pending_tools = max(0, self._pending_tools - 1)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        if self._pending_tools == 0:
            await self._send({"type": "response.create"})


def _parse_event(message: Any) -> dict | None:
    if isinstance(message, dict):
        return message
    if isinstance(message, (bytes, bytearray)):
        try:
            return json.loads(message.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(message, str):
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            return None
    return None


def _transcript_text(event: dict) -> str:
    for key in ("transcript", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    for key in ("transcript", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _default_connect(url: str, additional_headers: dict | None = None):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - env without uvicorn[standard]
        raise RuntimeError(
            "Grok Voice needs the websockets package (install uvicorn[standard])"
        ) from exc
    return await connect(url, additional_headers=additional_headers or {})
