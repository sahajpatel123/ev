"""Incremental ASR for the EV LIVE runtime.

The live loop receives raw PCM and needs "what the user is saying right now"
without waiting for a complete turn. ``LiveAsrFeed`` wraps any configured
``Transcriber`` behind a small incremental feed:

* while the user is speaking, it re-transcribes the growing speech buffer on
  a cadence (``partial_interval_ms``), emitting a partial hypothesis each time;
* when VAD says the user stopped, it starts a final transcription of the full
  utterance so the turn-taker can reply on the accurate text, not a stale
  mid-thought hypothesis;
* ``abort()`` cancels any in-flight work the instant the user barges in.

The feed is engine-agnostic: Parakeet (streaming-capable), faster-whisper,
and hosted OpenAI-compatible ASR all work through ``transcribe()``. A
transcriber that cannot take audio (the ``echo`` dev double) is wrapped by
``resolve_live_transcriber`` so a local fallback still produces partials.
If even that fails, the feed notifies ``on_unusable`` — the socket is never
silently mute.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import time
import wave
from collections.abc import Awaitable, Callable

from app.voice.contracts import Transcript, VoiceError

OnPartial = Callable[[str], Awaitable[None]]
OnUnusable = Callable[[VoiceError], Awaitable[None]]

# First hypothesis needs a real slice of speech. 80 ms of PCM is silence to
# faster-whisper (wake_mode=False raises asr_empty_result) and must not be
# scored — that used to permanently kill the live fallback.
_FIRST_PARTIAL_MS = 300

_ECHO_REFUSAL_CODES = frozenset({"asr_echo_no_audio", "asr_audio_required"})
_TRANSIENT_ASR_CODES = frozenset({"asr_empty_result", "asr_no_speech"})
_PERMANENT_ASR_CODES = frozenset(
    {
        "asr_echo_no_audio",
        "asr_audio_required",
        "asr_unusable",
        "asr_audio_ref_denied",
    }
)


def _wav_bytes(pcm: bytes, sample_rate: int = 16000) -> str:
    """Wrap a PCM16 buffer as a WAV and return its base64 payload."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _pad_live_audio_kwargs(kwargs: dict, *, min_seconds: float = 1.0) -> dict:
    """Pad short live clips so faster-whisper does not treat them as no-speech."""

    audio_b64 = kwargs.get("audio_b64")
    if not audio_b64:
        return kwargs
    try:
        raw = base64.b64decode(audio_b64)
        with wave.open(io.BytesIO(raw), "rb") as wav:
            rate = wav.getframerate() or 16000
            pcm = wav.readframes(wav.getnframes())
        need = int(min_seconds * rate) * 2
        if len(pcm) >= need:
            return kwargs
        pcm = pcm + b"\x00\x00" * ((need - len(pcm)) // 2)
        padded = dict(kwargs)
        padded["audio_b64"] = _wav_bytes(pcm, rate)
        return padded
    except Exception:  # noqa: BLE001 - leave the original payload alone
        return kwargs


def transcriber_refuses_pcm(transcriber) -> bool:
    """True for the echo/dev double that raises on any audio payload."""

    return getattr(transcriber, "name", None) == "echo"


class LivePcmTranscriber:
    """Live ASR that never refuses PCM silently.

    Wraps the configured engine. If that engine cannot take audio (the echo
    double) or returns a degraded empty transcript, a local faster-whisper
    fallback is used so EV.app's raw PCM path still produces partials. When
    even the fallback cannot run, ``transcribe`` raises ``asr_unusable`` so
    the session can emit a visible error instead of dropping speech.
    """

    name = "live-pcm"

    def __init__(self, primary, *, fallback=None, fallback_factory=None) -> None:
        self.primary = primary
        self._fallback = fallback
        self._fallback_factory = fallback_factory
        self._primary_unusable = transcriber_refuses_pcm(primary)
        self._fallback_unusable = False
        self.last_error: VoiceError | None = None

    @property
    def fallback(self):
        if self._fallback is None and self._fallback_factory is not None:
            try:
                self._fallback = self._fallback_factory()
            except Exception as exc:  # noqa: BLE001 - degrade visibly
                self._fallback_unusable = True
                self.last_error = VoiceError(
                    f"live ASR fallback failed to construct: {type(exc).__name__}: {exc}",
                    status=503,
                    code="asr_unusable",
                )
                self._fallback_factory = None
        return self._fallback

    def _empty(self, *, language: str = "en") -> Transcript:
        return Transcript(
            text="",
            confidence=0.0,
            language=language,
            provider=self.name,
        )

    def _weights_missing(self, result: Transcript) -> bool:
        reason = str((getattr(result, "details", None) or {}).get("reason") or "").lower()
        return "unavailable" in reason or "weights" in reason

    async def transcribe(self, **kwargs) -> Transcript:
        language = str(kwargs.get("language") or "en")
        if not self._primary_unusable:
            try:
                result = await self.primary.transcribe(**kwargs)
            except VoiceError as exc:
                if exc.code in _ECHO_REFUSAL_CODES or exc.code in _PERMANENT_ASR_CODES:
                    self._primary_unusable = True
                    self.last_error = exc
                elif exc.code in _TRANSIENT_ASR_CODES:
                    self.last_error = exc
                    result = None
                else:
                    raise
            else:
                if not getattr(result, "degraded", False) and (result.text or "").strip():
                    return result
                if getattr(result, "degraded", False) and self._weights_missing(result):
                    self._primary_unusable = True
                    self.last_error = VoiceError(
                        str((result.details or {}).get("reason") or "primary ASR degraded"),
                        status=503,
                        code="asr_unusable",
                    )
                else:
                    result = None
        else:
            result = None
        engine = self.fallback
        if engine is not None and not self._fallback_unusable:
            try:
                result = await engine.transcribe(**_pad_live_audio_kwargs(kwargs))
            except VoiceError as exc:
                self.last_error = exc
                if exc.code in _PERMANENT_ASR_CODES:
                    self._fallback_unusable = True
                else:
                    # Empty / no-speech / one-shot engine errors are this clip
                    # only. A later, longer utterance must still be heard.
                    return self._empty(language=language)
            except Exception as exc:  # noqa: BLE001 - keep the live loop alive
                self.last_error = VoiceError(
                    f"live ASR fallback failed: {type(exc).__name__}: {exc}",
                    status=503,
                    code="asr_unusable",
                )
                return self._empty(language=language)
            else:
                if not getattr(result, "degraded", False) and (result.text or "").strip():
                    return result
                if getattr(result, "degraded", False) and self._weights_missing(result):
                    self._fallback_unusable = True
                    self.last_error = VoiceError(
                        str((result.details or {}).get("reason") or "fallback ASR degraded"),
                        status=503,
                        code="asr_unusable",
                    )
                else:
                    return result if result is not None else self._empty(language=language)
        if self._fallback_unusable or engine is None:
            raise VoiceError(
                "live ASR cannot take audio; send text/partial frames or configure "
                "a real engine (EV_VOICE_ASR_PROVIDER=faster_whisper)",
                status=422,
                code="asr_unusable",
            )
        return self._empty(language=language)


def _faster_whisper_live_fallback():
    from app.config import settings
    from app.voice.asr import FasterWhisperTranscriber

    model = (settings.ears_wake_asr_model or "tiny").strip() or "tiny"
    return FasterWhisperTranscriber(model=model, vad_filter=False)


def resolve_live_transcriber(configured=None):
    """Return a transcriber the live PCM path can actually feed.

    The echo/dev double refuses audio. On that (or any missing) engine we
    attach a local faster-whisper fallback so stock config still hears
    spoken turns instead of silently dropping the socket.
    """

    from app.voice.asr import get_transcriber

    primary = configured if configured is not None else get_transcriber()
    if not transcriber_refuses_pcm(primary):
        return primary
    return LivePcmTranscriber(primary, fallback_factory=_faster_whisper_live_fallback)


class LiveAsrFeed:
    """Incremental speech-to-text feed for one continuous live conversation.

    Usage pattern (driven by VAD block decisions):

    * VAD crosses up  → ``begin()``
    * speech blocks   → ``feed(pcm_bytes)`` (called on every mic block)
    * VAD crosses down → ``end_speech()`` (final transcription starts now,
      in parallel with the silence/pause turn-taker)
    * turn committed   → ``final_text(timeout_ms=...)`` for the reply text
    * user barged in   → ``abort()`` and start over
    """

    def __init__(
        self,
        transcriber,
        *,
        sample_rate: int = 16000,
        partial_interval_ms: int = 600,
        max_buffer_seconds: float = 20.0,
        prefix_padding_ms: int = 300,
        on_partial: OnPartial | None = None,
        on_unusable: OnUnusable | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.transcriber = transcriber
        self.sample_rate = sample_rate
        self.partial_interval_ms = max(50, int(partial_interval_ms))
        # The stream is PCM16, so the byte bound is two bytes per sample.
        self.max_buffer_bytes = int(sample_rate * max(1.0, max_buffer_seconds) * 2)
        self.prefix_padding_bytes = int(sample_rate * max(0, prefix_padding_ms) / 1000) * 2
        self.on_partial = on_partial
        self.on_unusable = on_unusable
        self._loop = loop or asyncio.get_event_loop()
        self._clock = clock or time.monotonic
        self._buffer = bytearray()
        self._pre_roll = bytearray()
        self._speech_active = False
        self._partial_task: asyncio.Task | None = None
        self._final_task: asyncio.Task | None = None
        self._last_partial: str = ""
        self._final_text: str | None = None
        self._final_ready = asyncio.Event()
        self._last_partial_at: float = 0.0
        self._unusable_notified = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def begin(self) -> None:
        """VAD crossed up: start a fresh utterance (no partials of the old)."""

        self._abort_workers()
        self._buffer = bytearray(self._pre_roll)
        self._pre_roll.clear()
        self._last_partial = ""
        self._final_text = None
        self._final_ready.clear()
        self._speech_active = True
        self._last_partial_at = self._clock()

    def note_idle(self, pcm: bytes) -> None:
        """Keep a short pre-speech ring so word onsets are not clipped."""

        if self._speech_active or not pcm or self.prefix_padding_bytes <= 0:
            return
        self._pre_roll.extend(pcm)
        overflow = len(self._pre_roll) - self.prefix_padding_bytes
        if overflow > 0:
            del self._pre_roll[:overflow]

    def feed(self, pcm: bytes) -> None:
        """New PCM16 samples arrived while the user is speaking."""

        if not self._speech_active or not pcm:
            return
        remaining = self.max_buffer_bytes - len(self._buffer)
        if remaining > 0:
            self._buffer.extend(pcm[:remaining])
        if self._final_task is not None and not self._final_task.done():
            # Speech resumed after a final transcription started (VAD jitter):
            # the final may be stale; drop it, we will emit fresh partials.
            with contextlib.suppress(Exception):
                self._final_task.cancel()
            self._final_task = None
            self._final_text = None
            self._final_ready.clear()
        if self._partial_task is None or self._partial_task.done():
            elapsed = self._clock() - self._last_partial_at
            audio_ms = (len(self._buffer) / max(1, self.sample_rate * 2)) * 1000.0
            if not self._last_partial:
                # Need enough *samples*, not just wall time — a single 20 ms
                # frame after 80 ms of clock is still unusable to Whisper.
                if audio_ms >= _FIRST_PARTIAL_MS:
                    self._schedule_partial()
            elif elapsed * 1000.0 >= self.partial_interval_ms:
                self._schedule_partial()

    def end_speech(self) -> None:
        """VAD crossed down: kick off the final transcription immediately.

        The turn-taker still applies thinking/complete-pause logic while this
        runs, so the accurate transcript is usually ready before the reply is
        due — that is what makes the reply text match what was actually said.
        """

        self._speech_active = False
        if not self._buffer:
            return
        if self._partial_task is not None and not self._partial_task.done():
            with contextlib.suppress(Exception):
                self._partial_task.cancel()
            self._partial_task = None
        if self._final_task is None or self._final_task.done():
            self._final_ready.clear()
            self._final_task = self._loop.create_task(
                self._transcribe(bytes(self._buffer), final=True),
                name="ev-live-asr-final",
            )

    def abort(self) -> None:
        """The user interrupted / a new turn started: drop in-flight work."""

        self._abort_workers()
        self._buffer.clear()
        self._speech_active = False
        self._last_partial = ""

    def _abort_workers(self) -> None:
        for task in (self._partial_task, self._final_task):
            if task is not None and not task.done():
                with contextlib.suppress(Exception):
                    task.cancel()
        self._partial_task = None
        self._final_task = None
        self._final_text = None
        self._final_ready.clear()

    # ------------------------------------------------------------------ #
    # Async surface
    # ------------------------------------------------------------------ #

    async def final_text(self, *, timeout_ms: int | None = None) -> str | None:
        """The accurate transcript for the finished utterance.

        Returns the final transcription when it is ready within ``timeout_ms``
        (default: no wait — whatever is already there), else the latest
        partial, else ``None``. The live loop must never stall the reply on
        ASR; the partial is always a valid fallback.
        """

        if self._final_text is not None:
            return self._final_text
        if self._final_task is None:
            return self._last_partial or None
        if timeout_ms:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._final_ready.wait(), timeout=max(0.0, timeout_ms / 1000.0)
                )
        if self._final_text is not None:
            return self._final_text
        return self._last_partial or None

    def latest_partial(self) -> str:
        return self._last_partial

    def reset(self) -> None:
        """Called after a reply lands: back to the empty state."""
        self.abort()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _schedule_partial(self) -> None:
        self._last_partial_at = self._clock()
        self._partial_task = self._loop.create_task(
            self._transcribe(bytes(self._buffer), final=False),
            name="ev-live-asr-partial",
        )

    async def _notify_unusable(self, exc: VoiceError) -> None:
        if self._unusable_notified:
            return
        self._unusable_notified = True
        if self.on_unusable is None:
            return
        with contextlib.suppress(Exception):
            await self.on_unusable(exc)

    async def _transcribe(self, payload: bytes, *, final: bool) -> None:
        audio_ms = (len(payload) / max(1, self.sample_rate * 2)) * 1000.0
        # Incremental / short finals use wake_mode so faster-whisper returns
        # an empty transcript instead of raising asr_empty_result.
        use_wake_mode = (not final) or audio_ms < 1000.0
        audio_b64 = _wav_bytes(payload, self.sample_rate)
        try:
            transcribe = self.transcriber.transcribe
            try:
                transcript = await transcribe(
                    audio_b64=audio_b64,
                    language="en",
                    wake_mode=use_wake_mode,
                )
            except TypeError:
                transcript = await transcribe(
                    audio_b64=audio_b64,
                    language="en",
                )
        except VoiceError as exc:
            if getattr(exc, "code", None) in _TRANSIENT_ASR_CODES:
                if final:
                    # Finished clip, no words: complete the turn as empty so
                    # the session can emit asr_no_speech instead of hanging mute.
                    self._final_text = ""
                    self._final_ready.set()
                return
            # Echo / missing-weights must not swallow the owner's speech.
            # Notify once so the session can emit a visible error; the
            # client can still inject text/partial frames.
            await self._notify_unusable(exc)
            return
        except Exception as exc:  # noqa: BLE001 - a failed hypothesis must not kill the loop
            await self._notify_unusable(
                VoiceError(
                    f"live ASR failed: {type(exc).__name__}: {exc}",
                    status=503,
                    code="asr_unusable",
                )
            )
            return
        text = (getattr(transcript, "text", "") or "").strip()
        if getattr(transcript, "degraded", False) or not text:
            if getattr(transcript, "degraded", False):
                await self._notify_unusable(
                    VoiceError(
                        str((getattr(transcript, "details", {}) or {}).get("reason") or "ASR degraded"),
                        status=503,
                        code="asr_unusable",
                    )
                )
            return
        if final:
            self._final_text = text
            self._final_ready.set()
        self._last_partial = text
        if self.on_partial is not None:
            with contextlib.suppress(Exception):
                await self.on_partial(text)
