"""Muse Voice Transcribe — Meta Model API hearing adapter.

Hearing only: streaming / file transcripts, partials, finals, endpointing.
Does not authenticate the owner. Does not speak. Does not reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Awaitable
from typing import Any
from uuid import uuid4

import httpx
import websockets

from app.compliance.policy import remote_processing_allowed
from app.gateway.muse import (
    muse_api_key,
    muse_asr_realtime_url,
    muse_base_url,
    muse_voice_model,
    note_voice_call,
)
from app.voice.asr import _read_audio, hear_status_message
from app.voice.contracts import Transcript, VoiceError

logger = logging.getLogger("ev.voice.muse")

OnPartial = Callable[[str], Awaitable[None]]
OnFinal = Callable[[str], Awaitable[None]]
OnUnusable = Callable[[VoiceError], Awaitable[None]]

_KEYWORDS = ("Evie", "Eve", "EVIE", "Canary", "OwnerTurn")


class MuseVoiceTranscriber:
    """File + live WebSocket ASR for ``muse-voice-transcribe-1.0``."""

    name = "meta_muse_voice"
    native_live_stream = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self.model = (model or "").strip() or muse_voice_model()
        self._client = client
        self._live: _MuseLiveSession | None = None

    def _key(self) -> str:
        return (self._api_key or muse_api_key()).strip()

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
        wake_mode: bool = False,
    ) -> Transcript:
        del text_hint, wake_mode
        if not remote_processing_allowed("voice_asr"):
            raise VoiceError(
                "Remote ASR is denied by regional policy; set EV_ALLOW_REMOTE_ASR=true",
                status=503,
                code="asr_unusable",
            )
        key = self._key()
        if not key:
            raise VoiceError(
                "Muse Voice Transcribe is unavailable: META_MODEL_API_KEY is missing",
                status=503,
                code="asr_unusable",
            )
        audio, filename = await _read_audio(audio_b64, audio_ref)
        session_id = f"ev-asr-{uuid4().hex[:12]}"
        # File clips are one complete turn (PUSH_TO_TALK). Live WebSocket uses
        # ENDPOINTING. Official multipart: request part has no filename.
        request = {
            "mode": "PUSH_TO_TALK",
            "model": self.model,
            "audioEncoding": "WAV",
            "keywords": list(_KEYWORDS),
            "languageBias": ["English"],
        }
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=60)
            close = True
        try:
            resp = await client.post(
                f"{muse_base_url()}/asr/transcribe",
                params={"sessionId": session_id},
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                files={
                    "request": (
                        None,
                        json.dumps(request),
                        "application/json",
                    ),
                    "audio": (filename, audio, "audio/wav"),
                },
            )
        except httpx.TransportError as exc:
            raise VoiceError(
                "Muse Voice Transcribe is unavailable",
                status=503,
                code="asr_unusable",
            ) from exc
        finally:
            if close:
                await client.aclose()
        if resp.status_code in {401, 403}:
            raise VoiceError(
                "Muse Voice Transcribe credential was rejected",
                status=503,
                code="asr_unusable",
            )
        if resp.status_code >= 400:
            raise VoiceError(
                "Muse Voice Transcribe request failed",
                status=503,
                code="asr_unusable",
            )
        data = resp.json() if resp.content else {}
        text = str(data.get("transcript") or "").strip()
        duration_ms = int(data.get("audioDurationMs") or 0)
        note_voice_call(audio_ms=duration_ms)
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        speakers = sorted(
            {
                str(turn.get("speaker") or "")
                for turn in turns
                if isinstance(turn, dict) and turn.get("speaker")
            }
        )
        if not text:
            raise VoiceError(
                hear_status_message("asr_empty_result"),
                status=422,
                code="asr_empty_result",
            )
        return Transcript(
            text=text,
            confidence=1.0,
            language=language or "en",
            provider=self.name,
            details={
                "model": data.get("model") or self.model,
                "session_id": data.get("sessionId") or session_id,
                "audio_duration_ms": duration_ms,
                "turns": turns,
                # Diarization labels are context only. Never owner authentication.
                "diarization_speakers": speakers,
                "diarization_is_owner_auth": False,
            },
        )

    def start_live(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        on_partial: OnPartial | None = None,
        on_final: OnFinal | None = None,
        on_unusable: OnUnusable | None = None,
        sample_rate: int = 16000,
    ) -> None:
        self.abort_live()
        encoding = "PCM_16KHZ" if sample_rate == 16000 else "PCM_24KHZ"
        self._live = _MuseLiveSession(
            api_key=self._key(),
            model=self.model,
            encoding=encoding,
            on_partial=on_partial,
            on_final=on_final,
            on_unusable=on_unusable,
        )
        loop.create_task(self._live.run(), name="ev-muse-voice-live")

    def feed_live(self, pcm: bytes) -> None:
        if self._live is not None and pcm:
            self._live.feed(pcm)

    def end_live(self) -> None:
        if self._live is not None:
            self._live.end_input()

    def abort_live(self) -> None:
        live = self._live
        self._live = None
        if live is not None:
            live.abort()


class _MuseLiveSession:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        encoding: str,
        on_partial: OnPartial | None,
        on_final: OnFinal | None,
        on_unusable: OnUnusable | None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._encoding = encoding
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_unusable = on_unusable
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._ended = asyncio.Event()
        self._abort = asyncio.Event()
        self._closed = False
        self._got_final = False
        self._audio_ms = 0

    def feed(self, pcm: bytes) -> None:
        if self._closed or self._ended.is_set() or not pcm:
            return
        self._audio_ms += int(len(pcm) / 32.0) if self._encoding == "PCM_16KHZ" else int(len(pcm) / 48.0)
        self._queue.put_nowait(pcm)

    def handshake_payload(self) -> dict:
        """First JSON text frame. Auth is here, not an HTTP Authorization header."""

        return {
            "authorization": {"accessToken": f"Bearer {self._api_key}"},
            "audioEncoding": self._encoding,
            "model": self._model,
            "mode": "ENDPOINTING",
            "partialMode": "CUMULATIVE",
            "keywords": list(_KEYWORDS),
            "languageBias": ["English"],
        }

    def end_input(self) -> None:
        if self._ended.is_set():
            return
        self._ended.set()
        self._queue.put_nowait(None)

    def abort(self) -> None:
        self._abort.set()
        self._ended.set()
        with contextlib.suppress(Exception):
            self._queue.put_nowait(None)

    async def run(self) -> None:
        if not self._api_key:
            await self._fail(
                VoiceError(
                    "Muse Voice Transcribe is unavailable: META_MODEL_API_KEY is missing",
                    status=503,
                    code="asr_unusable",
                )
            )
            return
        if not remote_processing_allowed("voice_asr"):
            await self._fail(
                VoiceError(
                    "Remote ASR is denied by regional policy; set EV_ALLOW_REMOTE_ASR=true",
                    status=503,
                    code="asr_unusable",
                )
            )
            return
        session_id = f"ev-live-{uuid4().hex[:12]}"
        uri = f"{muse_asr_realtime_url()}?sessionId={session_id}"
        try:
            async with websockets.connect(uri, open_timeout=30, max_size=None) as ws:
                await ws.send(
                    json.dumps(self.handshake_payload())
                )
                handshake = json.loads(await ws.recv())
                if not isinstance(handshake, dict) or "sessionId" not in handshake:
                    await self._fail(
                        VoiceError(
                            "Muse Voice Transcribe handshake failed",
                            status=503,
                            code="asr_unusable",
                        )
                    )
                    return
                receiver = asyncio.create_task(self._receive(ws), name="ev-muse-voice-recv")
                sender = asyncio.create_task(self._send(ws), name="ev-muse-voice-send")
                try:
                    await asyncio.wait(
                        {receiver, sender},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                finally:
                    for task in (receiver, sender):
                        if not task.done():
                            task.cancel()
                            with contextlib.suppress(Exception):
                                await task
        except Exception as exc:  # noqa: BLE001 - surface as typed ASR failure
            if not self._abort.is_set():
                await self._fail(
                    VoiceError(
                        f"Muse Voice Transcribe stream failed: {type(exc).__name__}",
                        status=503,
                        code="asr_unusable",
                    )
                )
        finally:
            note_voice_call(audio_ms=self._audio_ms)
            self._closed = True

    async def _send(self, ws) -> None:
        while not self._abort.is_set():
            chunk = await self._queue.get()
            if chunk is None:
                break
            await ws.send(chunk)
        if not self._abort.is_set():
            await ws.send(json.dumps({"type": "endStream"}))

    async def _receive(self, ws) -> None:
        async for message in ws:
            if self._abort.is_set():
                return
            if isinstance(message, bytes):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            if kind == "error":
                await self._fail(
                    VoiceError(
                        "Muse Voice Transcribe reported an error",
                        status=503,
                        code="asr_unusable",
                    )
                )
                return
            if kind == "transcript":
                text = str(event.get("transcript") or "").strip()
                if text and self.on_partial is not None:
                    await self.on_partial(text)
                continue
            # ENDPOINTING: speechEnd is a boundary only. The committed turn
            # text is speechComplete (Meta may post-process after speechEnd).
            # PUSH_TO_TALK uses transcript.final on the file endpoint, not here.
            if kind == "speechComplete":
                await self._emit_final(str(event.get("transcript") or "").strip())

    async def _emit_final(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._got_final:
            return
        self._got_final = True
        if self.on_final is not None:
            await self.on_final(text)

    async def _fail(self, exc: VoiceError) -> None:
        if self.on_unusable is None:
            return
        with contextlib.suppress(Exception):
            await self.on_unusable(exc)
