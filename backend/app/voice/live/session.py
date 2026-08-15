"""Live session: engine + ASR/TTS/intelligence callbacks.

``LiveEngine`` decides *when*. This layer decides *what to do about it*:
synthesize a backchannel, cancel playback, start a reply, or delegate deep
work. Transports (WebSocket, tests) push client messages in and drain
``outbound`` events.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from app.voice.contracts import SpeechStyle, SynthesisResult
from app.voice.live.asr_feed import LiveAsrFeed
from app.voice.live.backchannel import BackchannelPolicy
from app.voice.live.delegate import needs_deep_work, thinking_filler
from app.voice.live.engine import EngineTick, LiveEngine, ManualClock
from app.voice.live.events import (
    BackchannelEvent,
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    LatencyEvent,
    LiveEvent,
    ReadyEvent,
    StateEvent,
    TtsChunkEvent,
)
from app.voice.live.state import SPEAK_FILLER
from app.voice.live.turn_taking import TURN_RESPOND_NOW, TURN_USER_INTERRUPTED, TurnTakingConfig
from app.voice.speech import is_wake_only_name, strip_wake_prefix

RespondFn = Callable[..., Any]

# Glue brief dips between "Evie" and the command so VAD does not end the
# utterance 80 ms into a pause and commit a wake-only Yes?.
_VAD_HANGOVER_SAMPLES = int(16000 * 0.08)
# Far-field "EE-vee" sits under the default EnergyVad floor of 80.
_LIVE_RMS_SPEECH_FLOOR = 48.0


def _pcm16(data: bytes) -> array.array:
    n = len(data) - (len(data) % 2)
    return array.array("h", data[:n])


class LiveSession:
    """One full-duplex conversation attached to a voice session id."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        conversation_id: str | None = None,
        engine: LiveEngine | None = None,
        synthesizer=None,
        respond: RespondFn | None = None,
        transcriber=None,
        asr_partial_interval_ms: int = 600,
        clock: ManualClock | Callable[[], int] | None = None,
        turn_config: TurnTakingConfig | None = None,
        backchannel_enabled: bool = True,
        vad_threshold: float = 0.5,
        on_sleep: Callable[[str], Awaitable[None]] | None = None,
        grok_voice=None,
    ) -> None:
        if engine is not None:
            self.engine = engine
        else:
            clock_fn: Callable[[], int]
            if clock is None:
                from app.voice.live.engine import _wall_clock_ms

                clock_fn = _wall_clock_ms
            elif isinstance(clock, ManualClock):
                clock_fn = clock
            else:
                clock_fn = clock
            self.engine = LiveEngine(
                clock_ms=clock_fn,
                turn_config=turn_config,
                backchannel=BackchannelPolicy() if backchannel_enabled else BackchannelPolicy(
                    min_speech_ms=10**9
                ),
                backchannel_enabled=backchannel_enabled,
            )
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.synthesizer = synthesizer
        self._respond = respond
        self._on_sleep = on_sleep
        self.vad_threshold = vad_threshold
        self.outbound: asyncio.Queue[LiveEvent] = asyncio.Queue()
        self._respond_task: asyncio.Task | None = None
        self._pcm = array.array("h")
        self._closed = False
        self._vad: Any = None
        self._speech_active = False
        self._authorized_at_ms: int | None = None
        self._pcm_unheard_notified = False
        self._vad_hang_samples = 0
        self.grok_voice = grok_voice
        self.ensure_pipeline = None
        self._asr_partial_interval_ms = asr_partial_interval_ms
        self.asr_feed: LiveAsrFeed | None = None
        if transcriber is not None:
            self.asr_feed = LiveAsrFeed(
                transcriber,
                sample_rate=16000,
                partial_interval_ms=asr_partial_interval_ms,
                on_partial=self._feed_partial,
                on_unusable=self._asr_unusable,
            )

    def attach_intelligence(self, *, transcriber=None, synthesizer=None, respond=None) -> None:
        """Lazy DeepSeek ASR+TTS pipeline — only when Grok Voice is not running."""

        if synthesizer is not None:
            self.synthesizer = synthesizer
        if respond is not None:
            self._respond = respond
        if transcriber is not None and self.asr_feed is None:
            self.asr_feed = LiveAsrFeed(
                transcriber,
                sample_rate=16000,
                partial_interval_ms=self._asr_partial_interval_ms,
                on_partial=self._feed_partial,
                on_unusable=self._asr_unusable,
            )

    def now(self) -> int:
        return self.engine.now()

    async def emit(self, event: LiveEvent) -> None:
        await self.outbound.put(event)

    async def emit_all(self, events: list[LiveEvent]) -> None:
        for event in events:
            await self.emit(event)

    def ready_event(self) -> ReadyEvent:
        return ReadyEvent(
            at_ms=self.now(),
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            config={
                "sample_rate": 16000,
                "encoding": "pcm16le",
                "tick_hz": 20,
            },
        )

    async def handle_client(self, message: dict | bytes) -> None:
        """Ingest one client frame and run a decision tick."""

        if self._closed:
            return
        if self.grok_voice is not None:
            await self._handle_grok(message)
            return
        if isinstance(message, (bytes, bytearray, memoryview)):
            await self._handle_pcm(bytes(message))
        elif isinstance(message, dict):
            await self._handle_dict(message)
        await self.tick()

    async def _handle_grok(self, message: dict | bytes) -> None:
        """Grok Voice owns VAD, turn-taking, ASR, and TTS on this channel."""

        grok = self.grok_voice
        if grok is None:
            return
        if isinstance(message, (bytes, bytearray, memoryview)):
            await grok.append_pcm(bytes(message))
            return
        if not isinstance(message, dict):
            return
        kind = (message.get("type") or "").strip()
        if kind == "audio":
            raw = message.get("pcm16_b64") or message.get("audio_b64")
            if not raw:
                return
            try:
                pcm = base64.b64decode(raw, validate=True)
            except Exception:
                await self.emit(
                    ErrorEvent(
                        at_ms=self.now(),
                        code="bad_audio",
                        message="pcm16_b64 must be valid base64",
                    )
                )
                return
            await grok.append_pcm(pcm)
            return
        if kind in {"text", "transcript"}:
            text = str(message.get("text") or "").strip()
            if not text:
                return
            if self._is_sleep(text):
                await self._end_sleep(text)
                return
            await grok.send_text(text)
            return
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""))
            return
        if kind == "commit":
            return

    async def _handle_dict(self, message: dict) -> None:
        kind = (message.get("type") or "").strip()
        if kind == "audio":
            raw = message.get("pcm16_b64") or message.get("audio_b64")
            if raw:
                try:
                    pcm = base64.b64decode(raw, validate=True)
                except Exception:
                    await self.emit(
                        ErrorEvent(
                            at_ms=self.now(),
                            code="bad_audio",
                            message="pcm16_b64 must be valid base64",
                        )
                    )
                    return
                await self._handle_pcm(pcm)
            return
        if kind == "speech":
            active = bool(message.get("active"))
            events = self.engine.push_speech(active)
            self._drive_asr_speech(active)
            await self.emit_all(events)
            if any(isinstance(event, BargeInEvent) for event in events):
                self._cancel_respond()
            return
        if kind == "partial":
            text = str(message.get("text") or "")
            seq = int(message.get("sequence") or 0)
            await self.emit_all(self.engine.push_partial(text, seq=seq))
            return
        if kind in {"text", "transcript"}:
            text = str(message.get("text") or "").strip()
            if not text:
                return
            self.engine.push_transcript(text)
            await self.emit(
                FinalTranscriptEvent(at_ms=self.now(), text=text, confidence=1.0, provider="text")
            )
            if message.get("commit", True):
                tick = self.engine.commit()
                await self._apply_tick(tick)
            return
        if kind == "playback":
            self.engine.push_assistant_speaking(bool(message.get("active")))
            await self.emit(StateEvent(at_ms=self.now(), state=self.engine.state.snapshot()))
            return
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""))
            return
        if kind == "commit":
            tick = self.engine.commit()
            await self._apply_tick(tick)

    async def _handle_control(self, action: str) -> None:
        if action == "end":
            self._closed = True
            self._cancel_respond()
            if self.grok_voice is not None:
                self.grok_voice.close()
            await self.emit(
                ErrorEvent(
                    at_ms=self.now(),
                    code="session_ended",
                    message="Live channel closed",
                    fatal=True,
                )
            )
            return
        if action in {"quiet", "attentive", "passive"}:
            self.engine.set_listening_mode(action)
            await self.emit(StateEvent(at_ms=self.now(), state=self.engine.state.snapshot()))
            return
        if action == "barge_in":
            self._cancel_respond()
            if self.grok_voice is not None:
                await self.grok_voice.cancel()
            if self.asr_feed is not None:
                self.asr_feed.abort()
            self._speech_active = False
            self.engine.note_barge_in()
            await self.emit(BargeInEvent(at_ms=self.now(), reason="client_barge_in"))
            return
        if action == "commit":
            tick = self.engine.commit()
            await self._apply_tick(tick)

    async def _handle_pcm(self, pcm: bytes) -> None:
        samples = _pcm16(pcm)
        if not samples:
            return
        self._pcm.extend(samples)
        # Bounded diagnostic ring (30 s); never let a long session grow this
        # without bound. The ASR feed keeps its own utterance buffer.
        if len(self._pcm) > 16000 * 30:
            del self._pcm[: len(self._pcm) - 16000 * 30]
        speech = await self._vad_speech(samples)
        if speech and self.asr_feed is None:
            await self._note_pcm_unheard()
        self._drive_asr_speech(speech, samples=samples)
        events = self.engine.push_speech(speech)
        await self.emit_all(events)
        if any(isinstance(event, BargeInEvent) for event in events):
            self._cancel_respond()

    def _drive_asr_speech(self, active: bool, samples: array.array | None = None) -> None:
        """Mirror VAD transitions into the incremental ASR feed."""
        feed = self.asr_feed
        if feed is None:
            return
        pcm = bytes(samples) if samples is not None else b""
        if active and not self._speech_active:
            self._speech_active = True
            feed.begin()
        if active:
            feed.feed(pcm)
        else:
            if pcm:
                feed.note_idle(pcm)
            if self._speech_active:
                self._speech_active = False
                feed.end_speech()

    async def _feed_partial(self, text: str) -> None:
        events = self.engine.push_partial(text, seq=0)
        await self.emit_all(events)
        # A late first hypothesis (short "Evie", one PCM packet) must
        # re-enter the turn-taker; otherwise last_partial stays empty
        # and spoken turns never start.
        await self.tick()

    async def _asr_unusable(self, exc) -> None:
        code = getattr(exc, "code", None) or "asr_unusable"
        message = getattr(exc, "message", None) or str(exc)
        await self.emit(
            ErrorEvent(
                at_ms=self.now(),
                code=code,
                message=message[:240],
                fatal=False,
            )
        )

    async def _note_pcm_unheard(self) -> None:
        if self._pcm_unheard_notified:
            return
        self._pcm_unheard_notified = True
        await self.emit(
            ErrorEvent(
                at_ms=self.now(),
                code="asr_unavailable",
                message=(
                    "Live speech arrived but no PCM transcriber is attached; "
                    "send text/partial frames or configure a real ASR engine"
                ),
                fatal=False,
            )
        )

    async def _vad_speech(self, samples: array.array) -> bool:
        if self._vad is None:
            from app.audio.vad import EnergyVad

            self._vad = EnergyVad(rms_speech_floor=_LIVE_RMS_SPEECH_FLOOR)
        probability = await self._vad.block_probability(samples, 16000)
        speaking = float(probability or 0.0) >= self.vad_threshold
        n = len(samples)
        if speaking:
            self._vad_hang_samples = _VAD_HANGOVER_SAMPLES
            return True
        if self._vad_hang_samples > 0:
            self._vad_hang_samples = max(0, self._vad_hang_samples - n)
            return self._vad_hang_samples > 0
        return False

    async def tick(self) -> None:
        if self._closed:
            return
        await self._apply_tick(self.engine.tick())

    async def _apply_tick(self, tick: EngineTick) -> None:
        await self.emit_all(tick.events)
        interrupted = tick.decision.action == TURN_USER_INTERRUPTED or any(
            isinstance(event, BargeInEvent) for event in tick.events
        )
        if interrupted:
            self._cancel_respond()
            # Keep the in-progress utterance. Aborting ASR here used to drop
            # the owner's next words — including the command after "Evie".
            if self.asr_feed is not None and not self.engine.state.user_is_speaking:
                self.asr_feed.abort()
                self._speech_active = False
        if (
            tick.backchannel
            and tick.backchannel.should_backchannel
            and tick.backchannel.cue
            and self.engine.state.speaking_mode == "backchannel"
        ):
            asyncio.create_task(
                self._speak_cue(tick.backchannel.cue, backchannel=True),
                name="ev-live-backchannel",
            )
        if tick.decision.action == TURN_RESPOND_NOW:
            await self._start_respond(tick)

    async def _speak_listen_ack(self, cue: str) -> None:
        """Spoken Yes? that does not hold the floor.

        The owner often continues in the same breath ("Evie, what's the
        weather"). Treating that ack as a held floor made the rest of the
        sentence look like barge-in and aborted ASR.
        """

        self.engine.state.assistant_is_speaking = False
        self.engine.state.speaking_mode = "none"
        if self.synthesizer is None:
            await self.emit(
                TtsChunkEvent(at_ms=self.now(), index=0, text=cue, provider="dev")
            )
            return
        try:
            result = await self.synthesizer.synthesize(
                cue, style=SpeechStyle(warmth=0.95, urgency=0.08, brevity=0.95)
            )
        except Exception:  # noqa: BLE001 - a missed cue must not kill the loop
            await self.emit(
                TtsChunkEvent(at_ms=self.now(), index=0, text=cue, provider="dev")
            )
            return
        await self.emit(self._chunk_from_synthesis(0, cue, result))

    async def _speak_cue(self, cue: str, *, backchannel: bool = True) -> None:
        if backchannel:
            # Listening cues overlap the owner's floor. Never let the brief
            # synthesis window look like the assistant is holding the floor —
            # that would make every tick treat the owner's ongoing speech as
            # an interruption and abort their own ASR feed.
            self.engine.state.assistant_is_speaking = False
            self.engine.state.speaking_mode = "none"
            await self.emit(BackchannelEvent(at_ms=self.now(), text=cue))
        else:
            # Fillers are a held floor: user speech while one is being
            # synthesized/played must barge in and cancel the response task.
            self.engine.push_assistant_speaking(True)
            self.engine.state.speaking_mode = SPEAK_FILLER
        if self.synthesizer is None:
            if not backchannel:
                await self._emit_ttfa()
                await self.emit(
                    TtsChunkEvent(at_ms=self.now(), index=0, text=cue, provider="dev")
                )
                self.engine.push_assistant_speaking(False)
            return
        try:
            result = await self.synthesizer.synthesize(
                cue, style=SpeechStyle(warmth=0.95, urgency=0.08, brevity=0.95)
            )
        except Exception:  # noqa: BLE001 - a missed cue must not kill the loop
            if not backchannel:
                self.engine.push_assistant_speaking(False)
            return
        if not backchannel:
            await self._emit_ttfa()
        await self.emit(self._chunk_from_synthesis(0, cue, result))
        if not backchannel:
            self.engine.push_assistant_speaking(False)

    async def _emit_ttfa(self) -> None:
        authorized = self._authorized_at_ms
        if authorized is None:
            return
        self._authorized_at_ms = None
        now = self.now()
        await self.emit(
            LatencyEvent(
                at_ms=now,
                metric="ttfa",
                ms=max(0, now - authorized),
                authorized_at_ms=authorized,
            )
        )

    def _chunk_from_synthesis(self, index: int, text: str, result: SynthesisResult) -> TtsChunkEvent:
        audio_b64 = None
        if result.audio and len(result.audio) <= 1_500_000:
            audio_b64 = base64.b64encode(result.audio).decode("ascii")
        return TtsChunkEvent(
            at_ms=self.now(),
            index=index,
            text=text,
            audio_b64=audio_b64,
            audio_ref=result.audio_ref,
            content_type=result.content_type,
            duration_ms=result.duration_ms,
            provider=result.provider,
        )

    async def _start_respond(self, tick: EngineTick) -> None:
        if self._respond_task is not None and not self._respond_task.done():
            return
        text = (tick.decision.last_partial or self.engine.state.last_transcript() or "").strip()
        feed_text: str | None = None
        if self.asr_feed is not None:
            # The turn-taker already waited. A long final-ASR wait here is
            # first-word latency. Prefer the partial immediately; give the
            # final only a brief chance to land. Bare "Evie" waits a bit
            # longer in case the rest of the command is still in the buffer.
            from app.voice.live.turn_taking import pause_class

            klass = pause_class(text)
            if klass == "wake" or is_wake_only_name(text):
                wait_ms = 280
            elif klass == "complete":
                wait_ms = 50
            else:
                wait_ms = 120
            feed_text = await self.asr_feed.final_text(timeout_ms=wait_ms)
            if feed_text:
                text = feed_text
        if not text:
            await self.emit(
                ErrorEvent(
                    at_ms=self.now(),
                    code="asr_no_speech",
                    message="I didn't hear any speech in that clip.",
                    fatal=False,
                )
            )
            return
        if feed_text:
            await self.emit(
                FinalTranscriptEvent(
                    at_ms=self.now(),
                    text=text,
                    confidence=1.0,
                    provider="asr",
                )
            )
        command = strip_wake_prefix(text)
        if self._is_sleep(command or text):
            await self._end_sleep(command or text)
            return
        if not command or is_wake_only_name(text):
            # Spoken listen-ack. Do not hold the floor — continued speech
            # is the command, not barge-in.
            await self._speak_listen_ack("Yes?")
            self.engine.turns.reset_turn()
            self.engine.turns.on_assistant_speech_start()
            self.engine.finish_response()
            return
        text = command
        self._authorized_at_ms = self.now()
        background = needs_deep_work(text)
        self.engine.begin_response(background=background)
        await self.emit(StateEvent(at_ms=self.now(), state=self.engine.state.snapshot()))
        filler = thinking_filler(text) if background else None
        envelope = tick.envelope or self.engine.envelope_for(text)
        self._respond_task = asyncio.create_task(
            self._run_respond(text, envelope, filler=filler), name="ev-live-respond"
        )
        self._respond_task.add_done_callback(self._respond_done)

    def _respond_done(self, task: asyncio.Task) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    def _cancel_respond(self) -> None:
        task = self._respond_task
        if task is not None and not task.done():
            task.cancel()
        self._respond_task = None

    async def _run_respond(
        self, text: str, envelope, *, filler: str | None = None
    ) -> None:
        try:
            if filler:
                await self._speak_cue(filler, backchannel=False)
            if self._respond is None:
                await self.emit(
                    ErrorEvent(
                        at_ms=self.now(),
                        code="no_responder",
                        message="Live session has no intelligence callback",
                    )
                )
                self.engine.finish_response()
                return
            self.engine.mark_streaming()
            self.engine.push_assistant_speaking(True)
            produced = self._respond(text, envelope)
            if asyncio.iscoroutine(produced):
                produced = await produced
            first_audio = False
            if hasattr(produced, "__aiter__"):
                async for event in produced:
                    if not first_audio and isinstance(event, TtsChunkEvent):
                        first_audio = True
                        await self._emit_ttfa()
                    await self.emit(event)
            else:
                for event in produced or []:
                    if not first_audio and isinstance(event, TtsChunkEvent):
                        first_audio = True
                        await self._emit_ttfa()
                    await self.emit(event)
        except asyncio.CancelledError:
            self.engine.note_barge_in()
            raise
        except Exception as exc:  # noqa: BLE001 - keep the socket alive
            await self.emit(
                ErrorEvent(at_ms=self.now(), code="voice_pipeline", message=str(exc)[:240])
            )
            self.engine.push_assistant_speaking(False)
            self.engine.finish_response()
        else:
            self.engine.push_assistant_speaking(False)
            self.engine.finish_response()
            await self.emit(StateEvent(at_ms=self.now(), state=self.engine.state.snapshot()))

    def _is_sleep(self, text: str) -> bool:
        from app.voice.lifecycle import is_sleep_phrase

        return is_sleep_phrase(text)

    async def _end_sleep(self, text: str) -> None:
        self._closed = True
        self._cancel_respond()
        if self.grok_voice is not None:
            self.grok_voice.close()
        self.engine.finish_response()
        if self._on_sleep is not None:
            with contextlib.suppress(Exception):
                await self._on_sleep(text)
        lowered = text.strip().lower()
        stop_listening = any(
            phrase in lowered
            for phrase in ("stop listening", "stop evie", "go to sleep", "goodbye evie")
        )
        await self.emit(
            ErrorEvent(
                at_ms=self.now(),
                code="listening_stopped" if stop_listening else "session_ended",
                message="Sleep phrase — live channel closing",
                fatal=True,
            )
        )

    def close(self) -> None:
        self._closed = True
        self._cancel_respond()
        if self.grok_voice is not None:
            self.grok_voice.close()
        if self.asr_feed is not None:
            self.asr_feed.abort()
        with contextlib.suppress(asyncio.QueueFull):
            self.outbound.put_nowait(
                ErrorEvent(
                    at_ms=self.now(),
                    code="closed",
                    message="Live channel closed",
                    fatal=True,
                )
            )
