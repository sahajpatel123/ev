"""Always-on hands-free voice loop — the Siri interaction model for EVIE.

One continuous 16 kHz mono PCM stream per device goes in; conversation events
come out. The loop never asks the human to press anything:

1. **Idle** — the grammar-restricted wake spotter listens continuously. Nothing
   is transcribed, stored, or sent anywhere.
2. **Wake** — "EVIE" (or "hey EVIE", "ok EVIE", ...) lights the session up the
   moment it appears in a partial hypothesis, so the client can show that EVIE
   is listening within a few hundred milliseconds. The hit must still be
   *confirmed* when the decoder closes the segment; an unconfirmed hit discards
   the captured audio and nothing reaches the model.
3. **Listening** — everything after the wake phrase is transcribed with live
   partials and endpointed by silence, exactly like "Hey Siri, what's the
   weather" in one breath. A bare "EVIE" instead waits ``wake_grace`` for the
   command.
4. **Thinking / speaking** — the responder answers; sustained speech during
   playback is a barge-in that cuts EVIE off and starts a new turn.
5. **Follow-up** — the mic stays open for ``follow_up`` seconds with no wake word
   required. Speech that is not addressed to EVIE (acknowledgements, "never
   mind", a stray "mm-hmm") is dropped without a turn, and the window closes
   itself in silence.

All timing is measured in *audio samples consumed*, never wall clock, so a test
that feeds a WAV file sees exactly the behavior a live microphone produces.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.audio.ring import PCM16RingBuffer, pcm16_bytes
from app.config import settings
from app.voice.contracts import Transcript
from app.voice.vosk_engine import WakeSignal

LOGGER = logging.getLogger("ev.voice.live")


class LiveState:
    IDLE = "idle"  # always-on, waiting for the wake phrase
    WAKING = "waking"  # wake heard, capturing, confirmation pending
    LISTENING = "listening"  # capturing the command
    THINKING = "thinking"  # responder in flight
    SPEAKING = "speaking"  # reply audio playing on the client
    FOLLOW_UP = "follow_up"  # open mic, no wake word needed
    CLOSED = "closed"


# Speech in the follow-up window that is not a request. Kept deliberately small:
# a false "not addressed" is worse than answering an extra acknowledgement.
_ACK_ONLY = re.compile(
    r"^(?:uh|um|mm|hmm|mhm|uh huh|yeah|yep|yes|no|nope|ok|okay|oh|ah|"
    r"right|sure|cool|nice|thanks|thank you|hm)+$"
)
_DISMISS_PHRASE = (
    r"that'?s all|that is all|never ?mind|forget it|stop(?: listening)?|be quiet|"
    r"shut up|go to sleep|good ?bye|bye|good ?night|nothing|cancel|"
    r"thanks|thank you|we'?re done|i'?m done|done"
)
# One or more dismissal phrases in a row ("never mind, stop", "thanks evie").
_DISMISS = re.compile(
    rf"^(?:evie\s+)?(?:{_DISMISS_PHRASE})(?:[,.!\s]+(?:{_DISMISS_PHRASE}|evie))*$"
)
_WAKE_PREFIX = re.compile(
    r"^(?:hey|hi|hello|ok|okay|yo)?\s*evie[\s,.!]*", re.IGNORECASE
)


def strip_wake_prefix(text: str) -> str:
    """Drop a leading wake phrase so the model sees only the request."""

    return _WAKE_PREFIX.sub("", (text or "").strip(), count=1).strip()


def classify_turn(text: str) -> str:
    """``request`` | ``acknowledgement`` | ``dismissal`` | ``empty``."""

    normalized = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "empty"
    if _DISMISS.fullmatch(normalized):
        return "dismissal"
    if _ACK_ONLY.fullmatch(normalized.replace(" ", "")) or _ACK_ONLY.fullmatch(normalized):
        return "acknowledgement"
    return "request"


# --------------------------------------------------------------------------- #
# Events and collaborators
# --------------------------------------------------------------------------- #


@dataclass
class LiveEvent:
    type: str
    data: dict = field(default_factory=dict)


@dataclass
class LiveTurn:
    """One command captured from the stream, ready for the responder."""

    transcript: Transcript
    wav: bytes
    follow_up: bool
    wake: WakeSignal | None = None


@dataclass
class LiveReply:
    text: str
    audio: bytes | None = None
    content_type: str | None = None
    audio_ref: str | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    provider: str | None = None
    details: dict = field(default_factory=dict)


class LiveResponder(Protocol):
    """Turns a captured command into a spoken answer."""

    async def open_session(self, *, wake: WakeSignal, wav: bytes) -> dict:
        """Start (or reuse) a voice session for a confirmed wake. Returns info."""

    async def respond(self, turn: LiveTurn) -> LiveReply: ...

    async def interrupt(self) -> None:
        """Barge-in: stop the current answer."""

    async def close(self, *, reason: str) -> None: ...


class Recognizer(Protocol):
    """Streaming command recognizer (see VoskStreamingRecognizer)."""

    def feed(self, pcm: bytes) -> str | None: ...

    def final(self) -> Any: ...


class Spotter(Protocol):
    def feed(self, pcm: bytes) -> list[WakeSignal]: ...

    def flush(self) -> list[WakeSignal]:
        """Close the open segment so a pending hit is decided immediately."""

    def reset(self) -> None: ...


class Vad(Protocol):
    name: str

    async def block_probability(self, samples, sample_rate: int) -> float: ...


@dataclass
class LiveConfig:
    sample_rate: int = 16000
    frame_ms: int = 20
    endpoint_silence_ms: int = 900
    min_speech_ms: int = 240
    max_utterance_ms: int = 20_000
    wake_grace_ms: int = 7_000
    follow_up_ms: int = 12_000
    barge_in_ms: int = 400
    speech_threshold: float = 0.5
    ring_seconds: float = 30.0
    # Audio kept before the wake word ends, so a command that starts in the same
    # breath is never clipped.
    wake_backtrack_ms: int = 60
    # Cadence of mic-level updates (so a client can show that audio is arriving).
    level_interval_ms: int = 120
    # Safety net when a client never reports that playback finished.
    playback_grace_ms: int = 3_000

    @classmethod
    def from_settings(cls) -> LiveConfig:
        return cls(
            sample_rate=settings.ears_sample_rate,
            frame_ms=settings.live_frame_ms,
            endpoint_silence_ms=settings.live_endpoint_silence_ms,
            min_speech_ms=settings.live_min_speech_ms,
            max_utterance_ms=settings.live_max_utterance_ms,
            wake_grace_ms=settings.live_wake_grace_ms,
            follow_up_ms=settings.live_follow_up_ms,
            barge_in_ms=settings.live_barge_in_ms,
            speech_threshold=settings.live_speech_threshold,
            ring_seconds=settings.live_ring_seconds,
        )

    def samples(self, ms: int) -> int:
        return int(self.sample_rate * ms / 1000)


def wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


class LiveVoiceLoop:
    """Stateful hands-free conversation over one audio stream."""

    def __init__(
        self,
        *,
        responder: LiveResponder,
        emit: Callable[[LiveEvent], Awaitable[None]],
        spotter: Spotter,
        recognizer_factory: Callable[[], Recognizer],
        vad: Vad,
        config: LiveConfig | None = None,
        device_id: str = "live",
    ) -> None:
        self.config = config or LiveConfig.from_settings()
        self.responder = responder
        self.emit = emit
        self.spotter = spotter
        self.recognizer_factory = recognizer_factory
        self.vad = vad
        self.device_id = device_id

        self.state = LiveState.IDLE
        self.consumed = 0  # total samples seen
        self._ring = PCM16RingBuffer(int(self.config.sample_rate * self.config.ring_seconds))
        self._recognizer: Recognizer | None = None
        self._capture_start: int | None = None  # stream offset of the command audio
        self._speech_samples = 0
        self._silence_samples = 0
        self._speaking_speech = 0
        self._state_entered = 0
        self._wake: WakeSignal | None = None
        self._wake_confirmed = False
        self._turn_index = 0
        self._task: asyncio.Task | None = None
        self._partial = ""
        self._session: dict | None = None
        self._closed = False
        self._level_at = 0
        self._level_peak = 0.0
        self._speaking_budget = 0

    # -- helpers --------------------------------------------------------- #

    @property
    def _elapsed_in_state(self) -> int:
        return self.consumed - self._state_entered

    async def _set_state(self, state: str, **data: Any) -> None:
        if state == self.state and not data:
            return
        self.state = state
        self._state_entered = self.consumed
        await self.emit(
            LiveEvent(
                "state",
                {
                    "state": state,
                    "device_id": self.device_id,
                    "audio_seconds": round(self.consumed / self.config.sample_rate, 2),
                    **data,
                },
            )
        )

    def _slice_from(self, start_offset: int) -> bytes:
        """PCM from ``start_offset`` to now, as far back as the ring reaches."""

        wanted = max(0, self.consumed - start_offset)
        available = min(wanted, len(self._ring))
        if available <= 0:
            return b""
        return pcm16_bytes(self._ring.read_last(available))

    def _reset_capture(self) -> None:
        self._recognizer = None
        self._capture_start = None
        self._speech_samples = 0
        self._silence_samples = 0
        self._partial = ""

    def _start_capture(self, start_offset: int, *, keep_endpoint_state: bool = False) -> None:
        """(Re)start command decoding at ``start_offset`` from the ring buffer.

        ``keep_endpoint_state`` re-decodes the same utterance from a more precise
        offset (once the wake phrase's exact end is known) without restarting the
        silence/speech accounting that endpoints the turn.
        """

        self._recognizer = self.recognizer_factory()
        self._capture_start = start_offset
        if not keep_endpoint_state:
            self._speech_samples = 0
            self._silence_samples = 0
        self._partial = ""
        backlog = self._slice_from(start_offset)
        if backlog and self._recognizer is not None:
            self._recognizer.feed(backlog)

    # -- audio ingestion ------------------------------------------------- #

    async def feed(self, pcm: bytes) -> None:
        """Consume one block of mono PCM16 audio (any size)."""

        if self._closed or not pcm:
            return
        frame = self.config.samples(self.config.frame_ms) * 2
        for offset in range(0, len(pcm), frame):
            await self._feed_block(pcm[offset : offset + frame])

    async def _feed_block(self, block: bytes) -> None:
        samples = array.array("h")
        samples.frombytes(block[: len(block) // 2 * 2])
        if not samples:
            return
        self._ring.write(samples)
        self.consumed += len(samples)

        try:
            probability = await self.vad.block_probability(samples, self.config.sample_rate)
        except Exception as exc:  # a VAD failure must not deafen the loop
            LOGGER.warning("VAD error, treating block as silence: %s", exc)
            probability = 0.0
        speech = probability >= self.config.speech_threshold
        await self._report_level(samples, speech=speech)

        signals = self.spotter.feed(block)
        for signal in signals:
            await self._on_wake_signal(signal)

        if self._recognizer is not None and self.state in (LiveState.WAKING, LiveState.LISTENING):
            hypothesis = self._recognizer.feed(block)
            if hypothesis and hypothesis != self._partial:
                self._partial = hypothesis
                await self.emit(
                    LiveEvent(
                        "partial",
                        {"text": strip_wake_prefix(hypothesis), "raw": hypothesis},
                    )
                )

        await self._advance(speech=speech, samples=len(samples))

    async def _report_level(self, samples: array.array, *, speech: bool) -> None:
        """Publish a throttled RMS level so clients can prove the mic is live."""

        total = 0
        for value in samples:
            total += value * value
        rms = math.sqrt(total / len(samples)) if samples else 0.0
        # 0..1 on a dBFS-ish curve: 32768 full scale, floor at -60 dB.
        if rms > 0:
            db = 20 * math.log10(rms / 32768.0)
            normalized = max(0.0, min(1.0, (db + 60.0) / 60.0))
        else:
            normalized = 0.0
        self._level_peak = max(self._level_peak, normalized)
        interval = self.config.samples(self.config.level_interval_ms)
        if self.consumed - self._level_at < interval:
            return
        self._level_at = self.consumed
        await self.emit(
            LiveEvent(
                "level",
                {
                    "level": round(self._level_peak, 3),
                    "speech": speech,
                    "state": self.state,
                },
            )
        )
        self._level_peak = 0.0

    async def _on_wake_signal(self, signal: WakeSignal) -> None:
        if signal.kind == "pending":
            if self.state in (LiveState.IDLE, LiveState.FOLLOW_UP):
                self._wake = signal
                self._wake_confirmed = False
                await self.emit(
                    LiveEvent(
                        "wake",
                        {
                            "phrase": signal.phrase,
                            "stage": "pending",
                            "confidence": signal.confidence,
                        },
                    )
                )
                start = max(
                    0,
                    signal.end_offset - self.config.samples(self.config.wake_backtrack_ms),
                )
                self._start_capture(start)
                await self._set_state(LiveState.WAKING, phrase=signal.phrase)
            return
        if signal.kind == "confirmed":
            self._wake = signal
            self._wake_confirmed = True
            if self.state in (LiveState.IDLE, LiveState.FOLLOW_UP):
                # Wake and command arrived in one breath and the decoder only
                # closed the segment now: capture from just after the phrase.
                start = max(
                    0,
                    signal.end_offset - self.config.samples(self.config.wake_backtrack_ms),
                )
                self._start_capture(start)
                await self._set_state(LiveState.LISTENING, phrase=signal.phrase)
            elif self.state == LiveState.WAKING:
                # The pending hit started capture at "wherever we were"; now the
                # decoder has told us exactly where the phrase ended, so
                # re-decode from there and keep the wake word out of the request.
                precise = max(
                    0,
                    signal.end_offset - self.config.samples(self.config.wake_backtrack_ms),
                )
                if self._capture_start is not None and precise > self._capture_start:
                    self._start_capture(precise, keep_endpoint_state=True)
                await self._set_state(LiveState.LISTENING, phrase=signal.phrase)
            await self.emit(
                LiveEvent(
                    "wake",
                    {
                        "phrase": signal.phrase,
                        "stage": "confirmed",
                        "confidence": signal.confidence,
                    },
                )
            )
            return
        if (
            signal.kind == "rejected"
            and not self._wake_confirmed
            and self.state in (LiveState.WAKING, LiveState.LISTENING)
        ):
            await self.emit(
                LiveEvent("dismissed", {"reason": "wake_not_confirmed", "heard": signal.text})
            )
            self._reset_capture()
            self._wake = None
            await self._set_state(LiveState.IDLE)

    # -- state machine --------------------------------------------------- #

    async def _advance(self, *, speech: bool, samples: int) -> None:
        if self.state in (LiveState.WAKING, LiveState.LISTENING):
            if speech:
                self._speech_samples += samples
                self._silence_samples = 0
            else:
                self._silence_samples += samples
            await self._advance_capture()
            return
        if self.state == LiveState.SPEAKING:
            if speech:
                self._speaking_speech += samples
                if self._speaking_speech >= self.config.samples(self.config.barge_in_ms):
                    await self._barge_in()
                    return
            else:
                self._speaking_speech = 0
            if self._speaking_budget and self._elapsed_in_state >= self._speaking_budget:
                # The client never told us playback finished; open the mic anyway
                # rather than stranding the conversation.
                await self.playback_finished()
            return
        if self.state == LiveState.FOLLOW_UP:
            if speech:
                self._speech_samples += samples
                if self._speech_samples >= self.config.samples(self.config.min_speech_ms):
                    # Continued conversation: no wake word needed.
                    start = self.consumed - self._speech_samples - self.config.samples(200)
                    self._start_capture(max(0, start))
                    self._speech_samples = self.config.samples(self.config.min_speech_ms)
                    await self._set_state(LiveState.LISTENING, reason="follow_up")
                return
            self._speech_samples = 0
            if self._elapsed_in_state >= self.config.samples(self.config.follow_up_ms):
                await self._end_conversation("follow_up_timeout")

    async def _advance_capture(self) -> None:
        config = self.config
        spoke_enough = self._speech_samples >= config.samples(config.min_speech_ms)
        silence = self._silence_samples
        if self.state == LiveState.WAKING and not spoke_enough:
            # Bare "EVIE": wait for the command, but not forever.
            if self._elapsed_in_state >= config.samples(config.wake_grace_ms):
                await self.emit(
                    LiveEvent("dismissed", {"reason": "no_command_after_wake"})
                )
                self._reset_capture()
                await self._end_conversation("no_command")
            return
        endpointed = spoke_enough and silence >= config.samples(config.endpoint_silence_ms)
        overlong = (
            self._capture_start is not None
            and self.consumed - self._capture_start >= config.samples(config.max_utterance_ms)
        )
        if not (endpointed or overlong):
            return
        if not self._wake_confirmed and self._turn_index == 0:
            # The human stopped talking, so close the spotter's segment now and
            # take its verdict instead of waiting for the decoder's own
            # endpointer. This is what turns a "pending" hit into a decision.
            for signal in self.spotter.flush():
                await self._on_wake_signal(signal)
            if self.state == LiveState.IDLE:
                return  # the rejection path already reported and reset
            if not self._wake_confirmed:
                await self.emit(LiveEvent("dismissed", {"reason": "wake_not_confirmed"}))
                self._reset_capture()
                self._wake = None
                await self._set_state(LiveState.IDLE)
                return
        await self._dispatch_turn(reason="max_length" if overlong else "endpoint")

    # -- turns ----------------------------------------------------------- #

    async def _dispatch_turn(self, *, reason: str) -> None:
        recognizer = self._recognizer
        start = self._capture_start
        follow_up = self._turn_index > 0
        self._reset_capture()
        if recognizer is None or start is None:
            await self._set_state(LiveState.IDLE)
            return
        if not self._wake_confirmed and not follow_up:
            await self.emit(LiveEvent("dismissed", {"reason": "wake_not_confirmed"}))
            await self._set_state(LiveState.IDLE)
            return
        pcm = self._slice_from(start)
        result = recognizer.final()
        text = strip_wake_prefix(getattr(result, "text", "") or "")
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        kind = classify_turn(text)
        if kind == "empty":
            await self.emit(LiveEvent("dismissed", {"reason": "no_speech_recognized"}))
            await self._reopen_or_idle()
            return
        if kind == "dismissal":
            await self.emit(LiveEvent("dismissed", {"reason": "dismissed_by_user", "text": text}))
            await self._end_conversation("user_dismissed")
            return
        if kind == "acknowledgement" and follow_up:
            await self.emit(
                LiveEvent("dismissed", {"reason": "not_addressed_to_evie", "text": text})
            )
            await self._reopen_or_idle()
            return
        transcript = Transcript(
            text=text,
            confidence=confidence,
            provider=getattr(recognizer, "name", "vosk"),
            duration_ms=int(len(pcm) / 2 / self.config.sample_rate * 1000),
            details={"endpoint_reason": reason, "hands_free": True},
        )
        await self.emit(
            LiveEvent(
                "transcript",
                {
                    "text": text,
                    "confidence": confidence,
                    "follow_up": follow_up,
                    "endpoint_reason": reason,
                },
            )
        )
        await self._set_state(LiveState.THINKING)
        turn = LiveTurn(
            transcript=transcript,
            wav=wav_bytes(pcm, self.config.sample_rate),
            follow_up=follow_up,
            wake=self._wake,
        )
        self._task = asyncio.create_task(self._respond(turn))

    async def _respond(self, turn: LiveTurn) -> None:
        try:
            if self._session is None:
                self._session = await self.responder.open_session(
                    wake=self._wake
                    or WakeSignal(kind="confirmed", phrase="evie", confidence=1.0),
                    wav=turn.wav,
                )
                await self.emit(LiveEvent("session", dict(self._session)))
            reply = await self.responder.respond(turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface, never crash the stream
            LOGGER.exception("hands-free turn failed")
            await self.emit(
                LiveEvent(
                    "error",
                    {
                        "code": getattr(exc, "code", "turn_failed"),
                        "message": getattr(exc, "message", str(exc)),
                    },
                )
            )
            await self._reopen_or_idle()
            return
        self._turn_index += 1
        await self.emit(
            LiveEvent(
                "reply",
                {
                    "text": reply.text,
                    "audio_ref": reply.audio_ref,
                    "content_type": reply.content_type,
                    "duration_ms": reply.duration_ms,
                    "session_id": reply.session_id,
                    "tts_provider": reply.provider,
                    "speak_locally": reply.audio is None,
                    **({"details": reply.details} if reply.details else {}),
                },
            )
        )
        if reply.audio:
            await self.emit(
                LiveEvent(
                    "audio",
                    {
                        "content_type": reply.content_type or "audio/wav",
                        "bytes": len(reply.audio),
                        "audio_b64": base64.b64encode(reply.audio).decode("ascii"),
                    },
                )
            )
        self._speaking_speech = 0
        self._speaking_budget = self._playback_budget(reply)
        await self._set_state(LiveState.SPEAKING)

    def _playback_budget(self, reply: LiveReply) -> int:
        """How long to stay in ``speaking`` if the client never reports back."""

        if reply.duration_ms:
            duration_ms = reply.duration_ms
        else:
            words = max(1, len(reply.text.split()))
            duration_ms = int(words / 165 * 60_000)  # ~165 wpm of speech
        return self.config.samples(duration_ms + self.config.playback_grace_ms)

    async def _barge_in(self) -> None:
        await self.emit(LiveEvent("barge_in", {"reason": "owner_spoke"}))
        with contextlib.suppress(Exception):
            await self.responder.interrupt()
        self._speaking_speech = 0
        start = self.consumed - self.config.samples(self.config.barge_in_ms + 200)
        self._start_capture(max(0, start))
        self._speech_samples = self.config.samples(self.config.barge_in_ms)
        self._silence_samples = 0
        await self._set_state(LiveState.LISTENING, reason="barge_in")

    async def playback_finished(self) -> None:
        """Client reports the reply finished playing: open the follow-up mic."""

        if self.state != LiveState.SPEAKING:
            return
        self._speech_samples = 0
        self._speaking_budget = 0
        await self._set_state(LiveState.FOLLOW_UP, seconds=self.config.follow_up_ms / 1000)

    async def _reopen_or_idle(self) -> None:
        if self._turn_index > 0:
            self._speech_samples = 0
            await self._set_state(LiveState.FOLLOW_UP, seconds=self.config.follow_up_ms / 1000)
        else:
            await self._set_state(LiveState.IDLE)

    async def _end_conversation(self, reason: str) -> None:
        self._reset_capture()
        self._wake = None
        self._wake_confirmed = False
        had_session = self._session is not None
        self._session = None
        self._turn_index = 0
        self.spotter.reset()
        if had_session:
            with contextlib.suppress(Exception):
                await self.responder.close(reason=reason)
        await self.emit(LiveEvent("conversation_end", {"reason": reason}))
        await self._set_state(LiveState.IDLE)

    # -- control --------------------------------------------------------- #

    async def cancel(self, *, reason: str = "client_cancel") -> None:
        """Stop the current turn/playback and return to always-on listening."""

        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        with contextlib.suppress(Exception):
            await self.responder.interrupt()
        await self._end_conversation(reason)

    async def close(self, *, reason: str = "disconnect") -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self.responder.close(reason=reason)
            self._session = None
        self.state = LiveState.CLOSED
