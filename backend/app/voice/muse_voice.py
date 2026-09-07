"""Muse Voice Transcribe — Meta Model API hearing adapter.

Hearing only: streaming / file transcripts, partials, finals, endpointing.
Does not authenticate the owner. Does not speak. Does not reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.compliance.policy import remote_processing_allowed
from app.gateway.muse import (
    muse_api_key,
    muse_asr_realtime_url,
    muse_base_url,
    muse_voice_model,
    note_asr_audio_bytes,
    note_asr_client_pcm,
    note_asr_final,
    note_asr_keepalive_bytes,
    note_asr_partial,
    note_asr_session_completed,
    note_asr_session_failed,
    note_asr_session_opened,
    note_voice_call,
)
from app.voice.asr import _read_audio, hear_status_message
from app.voice.contracts import Transcript, VoiceError

logger = logging.getLogger("ev.voice.muse")

OnPartial = Callable[[str], Awaitable[None]]
OnFinal = Callable[[str], Awaitable[None]]
OnUnusable = Callable[[VoiceError], Awaitable[None]]
OnSpeechBoundary = Callable[[], Awaitable[None]]

_KEYWORDS = ("Evie", "Eve", "EVIE", "Canary", "OwnerTurn")


def _json_true(value: object) -> bool:
    """JSON boolean true. ``bool("false")`` is True in Python and must not commit."""

    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _as_json_event(message: object) -> dict | None:
    """Parse one Muse Voice WebSocket text/binary JSON frame."""

    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(message, str):
        return None
    try:
        event = json.loads(message)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _event_field(event: dict, *names: str) -> object:
    """Read a field from either the top-level or a common event envelope."""

    for name in names:
        if name in event and event[name] is not None:
            return event[name]
    for envelope_name in ("data", "payload", "event"):
        envelope = event.get(envelope_name)
        if not isinstance(envelope, dict):
            continue
        for name in names:
            if name in envelope and envelope[name] is not None:
                return envelope[name]
    return None


def _event_kind(event: dict) -> str:
    """Normalize the camelCase/snake_case spellings used by stream gateways."""

    raw = _event_field(event, "type", "eventType", "event")
    if isinstance(raw, dict):
        raw = raw.get("type") or raw.get("name")
    value = str(raw or "").strip().replace("-", "_").replace(".", "_").lower()
    return {
        "speechstart": "speech_start",
        "speech_start": "speech_start",
        "speechend": "speech_end",
        "speech_end": "speech_end",
        "speechcomplete": "speech_complete",
        "speech_complete": "speech_complete",
        "speechendpoint": "speech_complete",
        "speech_endpoint": "speech_complete",
        "turncomplete": "speech_complete",
        "turn_complete": "speech_complete",
        "audioprogress": "audio_progress",
        "audio_progress": "audio_progress",
        "transcriptpartial": "transcript",
        "transcript_partial": "transcript",
        "transcriptfinal": "transcript_final",
        "transcript_final": "transcript_final",
        "transcriptcomplete": "speech_complete",
        "transcript_complete": "speech_complete",
        "transcriptcompleted": "speech_complete",
        "transcript_completed": "speech_complete",
        "sessionready": "ready",
        "session_ready": "ready",
        "sessioncreated": "ready",
        "session_created": "ready",
        "sessionconnected": "ready",
        "session_connected": "ready",
    }.get(value, value)


def _sanitize_provider_message(raw: object) -> str:
    text = str(raw or "").strip()[:240]
    low = text.lower()
    if "bearer" in low or "accessToken" in text or "llm_" in low:
        return "provider rejected the request"
    return text


def classify_muse_stream_failure(
    *,
    provider_message: str = "",
    close_code: int | None = None,
    phase: str = "",
) -> tuple[str, str]:
    """Map a Meta stream failure to an owner-safe class and sentence.

    Never returns secrets. The second value is what EV.app may show.
    """

    del phase
    low = (provider_message or "").strip().lower()
    code = int(close_code) if close_code is not None else None
    if code == 1013 or "rate" in low or "quota" in low or "limit" in low and "rate" in low:
        return "asr_rate_limited", "Muse Voice rate/quota limited"
    if (
        "auth" in low
        or "token" in low
        or "unauthorized" in low
        or "forbidden" in low
        or "credential" in low
    ):
        return "asr_auth_failed", "Muse Voice authentication failed"
    if "slower than" in low and ("real" in low or "realtime" in low):
        # Pacing, not encoding: 2.7 ms worklet frames over Tailscale arrived
        # with gaps. The PWA now batches to steady 20 ms realtime frames, so
        # the next VAD turn retries instead of reporting a format rejection.
        return "asr_unusable", "Muse Voice heard gaps in the audio — try again"
    if (
        code == 1008
        or "bad request" in low
        or "backlog" in low
        or "real time" in low
        or "real-time" in low
        or "realtime" in low
        or "encoding" in low
        or "audio format" in low
        or ("invalid" in low and "audio" in low)
    ):
        return "asr_rejected_format", "Muse Voice rejected audio format"
    if "timeout" in low or "idle" in low or "deadline" in low:
        return "asr_timeout", "Muse Voice transcription timed out"
    if code in {1001, 1006, 1011, 1012} or "close" in low or "disconnect" in low:
        return "asr_connection_closed", "Muse Voice connection closed"
    return "asr_unusable", "Muse Voice connection closed"


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
        # File clips are one complete turn (PUSH_TO_TALK). Live WebSocket
        # / LiveAsrFeed use ENDPOINTING plus continuous PCM.
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
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        text = str(data.get("transcript") or data.get("text") or "").strip()
        if not text:
            text = " ".join(
                str(turn.get("transcript") or "").strip()
                for turn in turns
                if isinstance(turn, dict)
            ).strip()
        duration_ms = int(data.get("audioDurationMs") or 0)
        note_voice_call(audio_ms=duration_ms)
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
        mode: str = "ENDPOINTING",
    ) -> None:
        self.abort_live()
        encoding = "PCM_16KHZ" if sample_rate == 16000 else "PCM_24KHZ"
        live_mode = (mode or "ENDPOINTING").strip().upper() or "ENDPOINTING"
        if live_mode not in {"ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"}:
            live_mode = "ENDPOINTING"
        self._live = _MuseLiveSession(
            api_key=self._key(),
            model=self.model,
            encoding=encoding,
            mode=live_mode,
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
    """One Meta realtime WebSocket.

    Audio is queued until the handshake acknowledgement, then flushed
    immediately. Meta clocks ingress from session start; extra sleep made
    live mic audio slower than real-time. ENDPOINTING injects 20 ms of
    silence when the mic pauses so the stream does not starve. A local
    abort closes the socket immediately so a replacement session cannot
    inherit a zombie error event.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        encoding: str,
        on_partial: OnPartial | None,
        on_final: OnFinal | None,
        on_unusable: OnUnusable | None,
        mode: str = "ENDPOINTING",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._encoding = encoding
        self._mode = (mode or "ENDPOINTING").strip().upper() or "ENDPOINTING"
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_unusable = on_unusable
        self._chunks: deque[bytes] = deque()
        self._queued_bytes = 0
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._ended = asyncio.Event()
        self._abort = asyncio.Event()
        self._closed = False
        self._got_final = False
        self._completed_turns: set[object] = set()
        self._audio_ms = 0
        self._bytes_sent = 0
        self._ws = None
        self._phase = "init"
        self._provider_session_id = ""
        self._failed = False
        self._turn_sequence = 0
        self._active_turn_id: object | None = None

    def _bytes_per_sec(self) -> int:
        return 32_000 if self._encoding == "PCM_16KHZ" else 48_000

    def _max_buffer_bytes(self) -> int:
        return self._bytes_per_sec() * 2

    def feed(self, pcm: bytes) -> None:
        if self._closed or self._ended.is_set() or self._abort.is_set() or not pcm:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            return
        rate = 32.0 if self._encoding == "PCM_16KHZ" else 48.0
        self._audio_ms += int(len(pcm) / rate)
        samples = memoryview(pcm).cast("h")
        rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
        note_asr_client_pcm(len(pcm), rms)
        # Coalesce tiny worklet quanta (128 samples ~2.7 ms) into steady
        # 20 ms frames before waking the sender. Without this, 370 tiny
        # WebSocket frames/sec over Tailscale arrive with jitter that Meta
        # rejects as "slower than real-time".
        _FRAME = 640 if self._encoding == "PCM_16KHZ" else 960  # 20 ms
        if not hasattr(self, "_pending_batch"):
            self._pending_batch = bytearray()  # type: ignore[attr-defined]
        pending: bytearray = self._pending_batch  # type: ignore[attr-defined]
        pending.extend(pcm)
        while len(pending) >= _FRAME:
            frame = bytes(pending[:_FRAME])
            del pending[:_FRAME]
            self._chunks.append(frame)
            self._queued_bytes += len(frame)
        # Keep a trailing <20 ms tail in pending_batch; woken sender will
        # flush it via keepalive if needed. Do not queue tiny tails yet.
        overflow = self._queued_bytes - self._max_buffer_bytes()
        while overflow > 0 and self._chunks:
            dropped = self._chunks.popleft()
            self._queued_bytes -= len(dropped)
            overflow -= len(dropped)
        if self._chunks:
            self._wake.set()

    def handshake_payload(self) -> dict:
        """First JSON text frame. Auth is here, not an HTTP Authorization header."""

        return {
            "authorization": {"accessToken": f"Bearer {self._api_key}"},
            "audioEncoding": self._encoding,
            "model": self._model,
            "mode": self._mode,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
            "keywords": list(_KEYWORDS),
            "languageBias": ["English"],
        }

    def end_input(self) -> None:
        if self._ended.is_set():
            return
        self._ended.set()
        self._wake.set()

    def abort(self) -> None:
        self._abort.set()
        self._ended.set()
        self._wake.set()
        ws = self._ws
        if ws is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._force_close(ws), name="ev-muse-voice-abort-close")

    async def _force_close(self, ws) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=1000)

    async def run(self) -> None:
        if not self._api_key:
            await self._fail(
                VoiceError(
                    "Muse Voice Transcribe is unavailable: META_MODEL_API_KEY is missing",
                    status=503,
                    code="asr_auth_failed",
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
        note_asr_session_opened()
        opened = True
        completed = False
        try:
            self._phase = "connect"
            async with websockets.connect(uri, open_timeout=30, max_size=None) as ws:
                self._ws = ws
                if self._abort.is_set():
                    return
                self._phase = "handshake"
                await ws.send(json.dumps(self.handshake_payload()))
                handshake = _as_json_event(await asyncio.wait_for(ws.recv(), timeout=15))
                if self._abort.is_set():
                    return
                handshake_kind = _event_kind(handshake or {})
                if handshake and handshake_kind == "error":
                    await self._fail(self._error_from_provider(handshake, close_code=None))
                    return
                handshake_session_id = _event_field(
                    handshake or {}, "sessionId", "session_id", "session", "id"
                )
                status = str(_event_field(handshake or {}, "status") or "").lower()
                handshake_ready = bool(
                    handshake
                    and (
                        handshake_session_id
                        or handshake_kind in {"ready", "connected", "handshake_ack", "session_ready"}
                        or handshake.get("ok") is True
                        or status in {"ok", "ready", "connected"}
                    )
                )
                if not handshake_ready:
                    await self._fail(
                        VoiceError(
                            "Muse Voice authentication failed"
                            if handshake_kind == "error"
                            else "Muse Voice Transcribe handshake failed",
                            status=503,
                            code="asr_auth_failed" if handshake_kind == "error" else "asr_unusable",
                        )
                    )
                    return
                self._provider_session_id = str(handshake_session_id or session_id)
                self._phase = "ready"
                self._ready.set()
                receiver = asyncio.create_task(self._receive(ws), name="ev-muse-voice-recv")
                sender = asyncio.create_task(self._send(ws), name="ev-muse-voice-send")
                try:
                    done, _pending = await asyncio.wait(
                        {receiver, sender},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Surface worker failures.  In particular, websockets 17
                    # does not expose ``websockets.exceptions`` as a module
                    # attribute; the old cleanup path turned that compatibility
                    # error into a generic ASR failure and hid the real result.
                    for task in done:
                        if task.cancelled():
                            continue
                        error = task.exception()
                        if error is not None:
                            raise error

                    # ``_send`` completes as soon as endStream is written. The
                    # provider's final transcript arrives *after* that frame,
                    # so keep the receiver alive long enough to drain it. The
                    # previous FIRST_COMPLETED cleanup cancelled it and left
                    # every PUSH_TO_TALK turn with no response.
                    if (
                        sender in done
                        and receiver not in done
                        and not self._abort.is_set()
                    ):
                        self._phase = "draining"
                        try:
                            await asyncio.wait_for(receiver, timeout=15)
                        except TimeoutError:
                            await self._fail(
                                VoiceError(
                                    "Muse Voice transcription timed out",
                                    status=503,
                                    code="asr_timeout",
                                )
                            )

                    # ENDPOINTING is a persistent session. If the provider
                    # closes that socket after a usable turn, the next VAD
                    # turn still needs a fresh session; mark the stream dead
                    # without replacing the successful transcript with an
                    # error on the owner's current turn.
                    if (
                        (receiver in done or receiver.done())
                        and self._got_final
                        and not self._ended.is_set()
                        and not self._abort.is_set()
                        and not self._failed
                    ):
                        await self._fail(
                            self._error_from_close(
                                close_code=getattr(ws, "close_code", None),
                                message="provider closed the continuous stream",
                            )
                        )

                    if (
                        not self._got_final
                        and not self._failed
                        and not self._abort.is_set()
                    ):
                        # A clean provider close with no transcript is an
                        # ordinary empty turn, not a stuck live session.
                        await self._fail(
                            VoiceError(
                                "Muse Voice returned no transcript",
                                status=422,
                                code="asr_no_speech",
                            )
                        )
                finally:
                    for task in (receiver, sender):
                        if not task.done():
                            task.cancel()
                            with contextlib.suppress(Exception):
                                await task
                if self._got_final and not self._failed:
                    completed = True
        except Exception as exc:  # noqa: BLE001 - surface as typed ASR failure
            if not self._abort.is_set() and not self._got_final:
                close_code = getattr(exc, "code", None)
                reason = _sanitize_provider_message(getattr(exc, "reason", None) or exc)
                await self._fail(
                    self._error_from_close(close_code=close_code, message=reason)
                )
        finally:
            if opened and completed and not self._failed:
                note_asr_session_completed()
            note_voice_call(audio_ms=self._audio_ms)
            self._closed = True
            self._ws = None

    def _silence_bytes(self, seconds: float) -> bytes:
        n = int(self._bytes_per_sec() * max(0.0, seconds))
        if n % 2:
            n -= 1
        return b"\x00" * n

    async def _send(self, ws) -> None:
        await self._ready.wait()
        if self._abort.is_set():
            return
        self._phase = "sending"
        last_pcm_at = time.monotonic()
        while not self._abort.is_set():
            if self._chunks:
                chunk = self._chunks.popleft()
                self._queued_bytes = max(0, self._queued_bytes - len(chunk))
                await self._send_pcm(ws, chunk)
                last_pcm_at = time.monotonic()
                continue
            # Flush any trailing <20 ms tail as a final frame before idling,
            # otherwise a short utterance ending on a 10 ms tail never sends.
            pending = getattr(self, "_pending_batch", None)
            if isinstance(pending, bytearray) and pending and not self._ended.is_set():
                tail = bytes(pending)
                pending.clear()
                if tail:
                    await self._send_pcm(ws, tail)
                    last_pcm_at = time.monotonic()
                    continue
            if self._ended.is_set() or self._abort.is_set():
                break
            self._wake.clear()
            if self._chunks or self._ended.is_set() or self._abort.is_set():
                continue
            pending2 = getattr(self, "_pending_batch", None)
            if isinstance(pending2, bytearray) and pending2:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.05)
            except TimeoutError:
                idle = time.monotonic() - last_pcm_at
                if (
                    self._mode == "ENDPOINTING"
                    and not self._ended.is_set()
                    and idle >= 0.12
                ):
                    await self._send_keepalive(ws, seconds=min(idle, 0.25))
                    last_pcm_at = time.monotonic()
                continue
        if not self._abort.is_set():
            pending = getattr(self, "_pending_batch", None)
            if isinstance(pending, bytearray) and pending:
                with __import__("contextlib").suppress(Exception):
                    await self._send_pcm(ws, bytes(pending))
                pending.clear()
            self._phase = "finalizing"
            try:
                await ws.send(json.dumps({"type": "endStream"}))
            except ConnectionClosed:
                if not self._abort.is_set():
                    raise

    async def _send_pcm(self, ws, pcm: bytes) -> None:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            return
        try:
            await ws.send(pcm)
        except ConnectionClosed:
            if not self._abort.is_set():
                raise
            return
        self._bytes_sent += len(pcm)
        note_asr_audio_bytes(len(pcm))

    async def _send_keepalive(self, ws, *, seconds: float = 0.12) -> None:
        frame = self._silence_bytes(seconds)
        if not frame:
            return
        await self._send_pcm(ws, frame)
        note_asr_keepalive_bytes(len(frame))

    async def _receive(self, ws) -> None:
        async for message in ws:
            if self._abort.is_set():
                return
            if isinstance(message, bytes) and message.lstrip()[:1] not in {b"{", b"["}:
                continue
            event = _as_json_event(message)
            if event is None:
                continue
            kind = _event_kind(event)
            # Some gateways send an error object without a separate `type`
            # field. Treat that as terminal instead of waiting until the
            # socket closes and reporting a misleading no-speech result.
            if kind == "error" or (
                kind == ""
                and isinstance(event.get("error"), (str, dict))
            ):
                if self._abort.is_set():
                    return
                await self._fail(self._error_from_provider(event, close_code=None))
                self._abort.set()
                return
            if kind == "speech_start":
                turn_id = _event_field(event, "turnId", "turn_id")
                if turn_id is None:
                    self._turn_sequence += 1
                    self._active_turn_id = ("turn", self._turn_sequence)
                else:
                    self._active_turn_id = ("provider", str(turn_id))
                continue
            if kind == "speech_end":
                turn_id = _event_field(event, "turnId", "turn_id")
                if turn_id is not None:
                    self._active_turn_id = ("provider", str(turn_id))
                continue
            if kind in {"audio_progress", "speaker"}:
                continue
            if kind in {"transcript", "transcript_final"}:
                text = str(_event_field(event, "transcript", "text", "value") or "").strip()
                if text:
                    note_asr_partial()
                    if self.on_partial is not None:
                        await self.on_partial(text)
                terminal_transcript = kind == "transcript_final" or (
                    self._mode == "PUSH_TO_TALK"
                    and _json_true(_event_field(event, "final", "isFinal"))
                )
                if terminal_transcript and text:
                    await self._emit_final(text, turn_id=self._turn_key(event))
                    if self._mode == "PUSH_TO_TALK" and self._got_final:
                        return
                continue
            if kind == "speech_complete":
                text = str(_event_field(event, "transcript", "text", "value") or "").strip()
                await self._emit_final(
                    text,
                    turn_id=self._turn_key(event),
                )
                if self._mode == "PUSH_TO_TALK" and self._got_final:
                    return

    def _turn_key(self, event: dict) -> object:
        """Use one stable key for transcript-final/speechComplete duplicates."""

        if self._mode == "PUSH_TO_TALK":
            return "push_to_talk"
        turn_id = _event_field(event, "turnId", "turn_id")
        if turn_id is not None:
            self._active_turn_id = ("provider", str(turn_id))
            return self._active_turn_id
        if self._active_turn_id is None:
            self._turn_sequence += 1
            self._active_turn_id = ("turn", self._turn_sequence)
        return self._active_turn_id

    async def _emit_final(self, text: str, *, turn_id: object = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        key: object = turn_id if turn_id is not None else "_none"
        if key in self._completed_turns:
            return
        self._completed_turns.add(key)
        self._got_final = True
        note_asr_final()
        if self.on_final is not None:
            await self.on_final(text)

    def _error_from_provider(self, event: dict, *, close_code: int | None) -> VoiceError:
        raw_value = _event_field(event, "message", "error", "detail", "reason")
        if isinstance(raw_value, dict):
            raw_value = _event_field(raw_value, "message", "detail", "code", "reason")
        raw = _sanitize_provider_message(raw_value)
        error_class, spoken = classify_muse_stream_failure(
            provider_message=raw,
            close_code=close_code,
            phase=self._phase,
        )
        logger.warning(
            "muse_asr_error class=%s phase=%s close=%s session=%s msg=%s",
            error_class,
            self._phase,
            close_code,
            self._provider_session_id
            or _event_field(event, "sessionId", "session_id")
            or "",
            raw,
        )
        return VoiceError(spoken, status=503, code=error_class)

    def _error_from_close(self, *, close_code: int | None, message: str) -> VoiceError:
        error_class, spoken = classify_muse_stream_failure(
            provider_message=message,
            close_code=close_code,
            phase=self._phase,
        )
        logger.warning(
            "muse_asr_close class=%s phase=%s close=%s session=%s msg=%s",
            error_class,
            self._phase,
            close_code,
            self._provider_session_id,
            message,
        )
        return VoiceError(spoken, status=503, code=error_class)

    async def _fail(self, exc: VoiceError) -> None:
        if self._abort.is_set() or self._failed:
            return
        self._failed = True
        note_asr_session_failed(error_class=getattr(exc, "code", "") or "asr_unusable")
        if self.on_unusable is None:
            return
        with contextlib.suppress(Exception):
            await self.on_unusable(exc)
