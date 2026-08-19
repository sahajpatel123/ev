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
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.ev.policy import HOLD_LINE
from app.voice.contracts import SpeechStyle, SynthesisResult
from app.voice.live.asr_feed import LiveAsrFeed
from app.voice.live.backchannel import BackchannelPolicy
from app.voice.live.delegate import needs_deep_work, thinking_filler
from app.voice.live.engine import EngineTick, LiveEngine, ManualClock
from app.voice.live.events import (
    BackchannelEvent,
    BargeInEvent,
    CameraRequestEvent,
    CameraStateEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    HudEvent,
    LatencyEvent,
    LiveEvent,
    ReadyEvent,
    ReplyEvent,
    StateEvent,
    TtsChunkEvent,
)
from app.voice.live.layer import (
    CANCEL_SPOKEN,
    CAPABILITY_FALLBACK,
    NO_LIVE_ACTION_TOOLS_SPOKEN,
    PAUSE_SPOKEN,
    RESUME_SPOKEN,
    build_live_capability_manifest,
    classify_live_intent,
    evidence_hud,
    live_for_device,
    proactive_speech_allowed,
    progress_hud,
    protocol_hud_from_payload,
    register_live,
    tool_result_hud,
    tool_result_is_successful,
    unregister_live,
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
_LIFE_ACTION_DEDUP_S = 2.0
# Keep the live channel close to real time when a client briefly falls behind.
# Pipeline TTS is paced at its render duration. Speech-to-speech audio is not:
# the provider already streams near real time, and waiting here stalls the
# event pump until a late transcript arrives and used to wipe the reply.
# Only barge-in / provider-reset events discard queued speech. A user
# transcript is the turn she is answering — dropping audio there cuts her
# off mid-sentence.
_LIVE_OUTBOUND_MAX_EVENTS = 8
_LIVE_DEFAULT_AUDIO_CHUNK_MS = 160
_LIVE_COALESCED_EVENT_TYPES = frozenset(
    {"partial", "state", "latency", "realtime_diagnostics"}
)
_LIVE_DROP_AUDIO_BEFORE_TYPES = frozenset({"barge_in"})
_S2S_TTS_PROVIDERS = frozenset({"grok-voice", "openai-realtime"})
_LIVE_AUDIO_RESET_ERROR_CODES = frozenset(
    {"realtime_disconnect", "realtime_connect"}
)
logger = logging.getLogger(__name__)


def _pcm16(data: bytes) -> array.array:
    n = len(data) - (len(data) % 2)
    return array.array("h", data[:n])


def _spoken_from_tool_json(raw: str) -> str | None:
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_result = payload.get("result")
    body = raw_result if isinstance(raw_result, dict) else payload
    spoken = body.get("spoken") or body.get("error") or body.get("reason") or payload.get("error")
    text = str(spoken or "").strip()
    return text or None


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
        device_id: str | None = None,
        tts_device_id: str | None = None,
        capability_reply=None,
        capability_manifest: dict | None = None,
        camera_state: dict | None = None,
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
        self.outbound: asyncio.Queue[LiveEvent] = asyncio.Queue(
            maxsize=_LIVE_OUTBOUND_MAX_EVENTS
        )
        self._tts_next_emit_at = 0.0
        self._tts_pacing_generation = 0
        self._tts_pacing_wakeup = asyncio.Event()
        self._respond_task: asyncio.Task | None = None
        self._backchannel_task: asyncio.Task | None = None
        self._pcm = array.array("h")
        self._closed = False
        self._vad: Any = None
        self._speech_active = False
        self._authorized_at_ms: int | None = None
        self._pcm_unheard_notified = False
        self._vad_hang_samples = 0
        self.grok_voice = grok_voice
        self.ensure_pipeline: Callable[[], None] | None = None
        self.on_heartbeat: Callable[[], Awaitable[None]] | None = None
        self.run_live_tool: Callable[[str, dict, str], Awaitable[str]] | None = None
        self._life_action_task: asyncio.Task | None = None
        self._last_life_action: tuple[str, str] | None = None
        self._last_life_action_at = 0.0
        self.device_id = str(device_id) if device_id else None
        self.tts_device_id = str(tts_device_id) if tts_device_id else self.device_id
        self._capability_reply = capability_reply
        self._capability_manifest = (
            dict(capability_manifest) if isinstance(capability_manifest, dict) else None
        )
        self._camera_state = self._normalize_camera_state(camera_state)
        self._look_frame_future: asyncio.Future[str] | None = None
        self._paused = False
        self._muted = False
        self._approval_hold: dict | None = None
        self._last_honesty: str | None = None
        self._durable_jobs_cancelled = False
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
        if self.session_id:
            register_live(self)

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
        tts_generation: int | None = None
        is_boundary = self._is_playback_boundary(event)
        if is_boundary:
            self._reset_playback_boundary()
        elif event.type == "final_transcript":
            # Listening cues must not talk over the new turn. The spoken
            # answer for this transcript must keep flowing. Telemetry can
            # go so the transcript itself is not stuck behind a full queue.
            self._cancel_backchannel()
            self._discard_outbound(
                lambda queued: isinstance(queued, BackchannelEvent)
                or queued.type in _LIVE_COALESCED_EVENT_TYPES
            )
        if isinstance(event, TtsChunkEvent):
            tts_generation = self._tts_pacing_generation
            if not await self._pace_tts(event):
                return
        self._prepare_outbound(event)
        try:
            self.outbound.put_nowait(event)
        except asyncio.QueueFull:
            # Continuous telemetry may be skipped while a slow client catches
            # up. Important events wait for a slot so they cannot be starved by
            # a burst of audio.
            if event.type in _LIVE_COALESCED_EVENT_TYPES:
                return
            if (
                tts_generation is not None
                and tts_generation != self._tts_pacing_generation
            ):
                return
            await self.outbound.put(event)
            if (
                tts_generation is not None
                and tts_generation != self._tts_pacing_generation
            ):
                self._discard_outbound(lambda queued: queued is event, first_only=True)
                return
        if isinstance(event, FinalTranscriptEvent):
            from_s2s = event.provider in {"openai-realtime", "grok-voice"}
            await self._maybe_local_intent(event.text, from_grok=from_s2s)

    async def _pace_tts(self, event: TtsChunkEvent) -> bool:
        """Release pipeline audio at speaker speed instead of buffering whole replies."""

        if self.grok_voice is not None or event.provider in _S2S_TTS_PROVIDERS:
            return True
        if not event.audio_b64 and not event.audio_ref:
            return True
        duration_ms = event.duration_ms or _LIVE_DEFAULT_AUDIO_CHUNK_MS
        duration_s = max(0.08, float(duration_ms) / 1000.0)
        loop = asyncio.get_running_loop()
        generation = self._tts_pacing_generation
        now = loop.time()
        target = max(now, self._tts_next_emit_at)
        self._tts_next_emit_at = target + duration_s
        delay = target - now
        if delay <= 0:
            return True
        wakeup = self._tts_pacing_wakeup
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wakeup.wait(), timeout=delay)
        return generation == self._tts_pacing_generation and not self._closed

    def _reset_tts_pacing(self) -> None:
        self._tts_pacing_generation += 1
        self._tts_next_emit_at = 0.0
        wakeup = self._tts_pacing_wakeup
        self._tts_pacing_wakeup = asyncio.Event()
        wakeup.set()

    def _reset_playback_boundary(self) -> None:
        """Stop every queued or in-flight short utterance at a turn boundary."""

        self._cancel_backchannel()
        self._reset_tts_pacing()
        self._discard_outbound(
            lambda queued: isinstance(queued, (TtsChunkEvent, BackchannelEvent))
        )

    def _prepare_outbound(self, event: LiveEvent) -> None:
        """Trim stale realtime output before it can become conversational lag."""

        if self._is_playback_boundary(event):
            self._discard_outbound(
                lambda queued: isinstance(queued, TtsChunkEvent)
                or queued.type in _LIVE_COALESCED_EVENT_TYPES
            )

        if event.type in _LIVE_COALESCED_EVENT_TYPES:
            # Keep the latest snapshot/hypothesis. In normal operation this is
            # a no-op; it matters only when the socket is under pressure.
            if self.outbound.full():
                self._discard_outbound(lambda queued: queued.type == event.type)
        elif isinstance(event, TtsChunkEvent) and self.outbound.full():
            # Preserve every spoken chunk in order.  Telemetry is disposable;
            # audio is not.  If the queue is full of telemetry, make room
            # before applying normal backpressure to the producer.
            self._discard_outbound(
                lambda queued: queued.type in _LIVE_COALESCED_EVENT_TYPES
            )

    @staticmethod
    def _is_playback_boundary(event: LiveEvent) -> bool:
        return event.type in _LIVE_DROP_AUDIO_BEFORE_TYPES or (
            isinstance(event, ErrorEvent)
            and (event.fatal or event.code in _LIVE_AUDIO_RESET_ERROR_CODES)
        )

    def _discard_outbound(self, predicate, *, first_only: bool = False) -> bool:
        """Remove queued disposable events while preserving FIFO order."""

        queue = self.outbound._queue
        kept = []
        removed = 0
        for queued in queue:
            if predicate(queued) and (not first_only or removed == 0):
                removed += 1
            else:
                kept.append(queued)
        if not removed:
            return False
        queue.clear()
        queue.extend(kept)
        for _ in range(removed):
            self.outbound.task_done()
            self.outbound._wakeup_next(self.outbound._putters)
        return True

    async def emit_all(self, events: list[LiveEvent]) -> None:
        for event in events:
            await self.emit(event)

    def _realtime_diagnostics(self) -> dict:
        bridge = self.grok_voice
        if bridge is None:
            return {
                "provider": "pipeline",
                "supports_function_calls": False,
                "tools": [],
                "tool_names": [],
                "upstream_tool_names": [],
                "upstream_session_ready": False,
            }
        tools = getattr(bridge, "advertised_function_tools", [])
        bridge_diagnostics = getattr(bridge, "realtime_diagnostics", {})
        bridge_diagnostics = (
            dict(bridge_diagnostics) if isinstance(bridge_diagnostics, dict) else {}
        )
        return {
            "provider": getattr(bridge, "_provider", None),
            "model": getattr(bridge, "_model", None),
            "bridge_version": getattr(bridge, "bridge_version", None),
            "supports_function_calls": bool(
                getattr(bridge, "supports_function_calls", False)
            ),
            "tools": list(tools) if isinstance(tools, list) else [],
            "tool_names": list(getattr(bridge, "advertised_tool_names", ())),
            "upstream_tool_names": list(
                getattr(bridge, "upstream_tool_names", ())
            ),
            "upstream_session_ready": bool(
                getattr(bridge, "upstream_session_ready", False)
            ),
            "capability_error": getattr(bridge, "_capability_error", None),
            "tool_choice": bridge_diagnostics.get("tool_choice"),
            "tool_schemas": bridge_diagnostics.get("tool_schemas", []),
            "provider_mismatch": bool(bridge_diagnostics.get("provider_mismatch", False)),
            "function_call_error": bool(bridge_diagnostics.get("function_call_error", False)),
            "session_update": getattr(bridge, "session_update_metadata", {}),
            "session_ack": getattr(bridge, "session_ack_metadata", {}),
        }

    def ready_event(self) -> ReadyEvent:
        brain = "pipeline"
        if self.grok_voice is not None:
            brain = (
                "openai-realtime"
                if getattr(self.grok_voice, "_provider", None) == "openai"
                else "grok-voice"
            )
        return ReadyEvent(
            at_ms=self.now(),
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            config={
                "sample_rate": 16000,
                "encoding": "pcm16le",
                "tick_hz": 20,
                "brain": brain,
                "device_id": self.device_id,
                "tts_device_id": self.tts_device_id,
                "paused": self._paused,
                "muted": self._muted,
                "approval_hold": bool(self._approval_hold),
                "capability_manifest": self._capability_manifest,
                "realtime": self._realtime_diagnostics(),
                "camera_state": dict(self._camera_state),
            },
        )

    def interaction_snapshot(self) -> dict:
        snap = self.engine.state.snapshot()
        snap.update(
            {
                "paused": self._paused,
                "muted": self._muted,
                "approval_hold": bool(self._approval_hold),
                "device_id": self.device_id,
                "tts_device_id": self.tts_device_id,
                "conversation_id": self.conversation_id,
                "capability_manifest": self._capability_manifest,
                "realtime": self._realtime_diagnostics(),
                "camera_state": dict(self._camera_state),
            }
        )
        return snap

    def _reset_capture_state(self) -> None:
        """Drop audio that was captured before a pause/mute boundary.

        A long mute must be a clean boundary.  Keeping VAD/ASR state alive
        across it can splice stale pre-roll or a pending final transcript into
        the first utterance after unmute.
        """

        self._speech_active = False
        self._vad_hang_samples = 0
        self._vad = None
        if self.asr_feed is not None:
            self.asr_feed.abort(clear_pre_roll=True)

    async def handle_client(self, message: dict | bytes) -> None:
        """Ingest one client frame and run a decision tick."""

        if self._closed:
            return
        if self._paused or self._muted:
            if await self._handle_while_held(message):
                return
            if isinstance(message, (bytes, bytearray, memoryview)):
                return
            if isinstance(message, dict) and (message.get("type") or "") in {
                "audio",
                "speech",
                "partial",
                "playback",
            }:
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
            if await self._maybe_local_intent(text, from_grok=True):
                return
            if getattr(grok, "_response_active", False) or getattr(
                grok, "_assistant_open", False
            ):
                await grok.cancel()
                await self.emit(BargeInEvent(at_ms=self.now(), reason="text_input"))
            await grok.send_text(text)
            return
        if kind == "speech":
            active = bool(message.get("active"))
            if (
                active
                and not getattr(grok, "_playback_active", False)
                and (
                    getattr(grok, "_assistant_open", False)
                    or getattr(grok, "_response_active", False)
                )
            ):
                await grok.cancel()
                await self.emit(BargeInEvent(at_ms=self.now(), reason="user_speech"))
            await self.emit_all(self.engine.push_speech(active))
            return
        if kind == "playback":
            grok.set_playback(bool(message.get("active")))
            return
        if kind in {"camera", "camera_state"}:
            await self._handle_camera_message(message)
            return
        if kind == "look_frame":
            await self._handle_look_frame(message)
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
            if await self._maybe_local_intent(text, from_grok=False):
                return
            if self.engine.state.assistant_is_speaking or (
                self._respond_task is not None and not self._respond_task.done()
            ):
                self._cancel_respond()
                self.engine.note_barge_in()
                await self.emit(BargeInEvent(at_ms=self.now(), reason="text_input"))
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
            await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))
            return
        if kind in {"camera", "camera_state"}:
            await self._handle_camera_message(message)
            return
        if kind == "look_frame":
            await self._handle_look_frame(message)
            return
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""))
            return
        if kind == "commit":
            tick = self.engine.commit()
            await self._apply_tick(tick)

    async def _handle_control(self, action: str) -> None:
        action = action.strip().lower()
        if action == "wake":
            action = "resume"
        if action == "end":
            self._closed = True
            self._reset_playback_boundary()
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
        if action == "pause":
            self._paused = True
            self._reset_playback_boundary()
            self._cancel_respond()
            self._reset_capture_state()
            if self.grok_voice is not None:
                await self.grok_voice.mute_input()
            self.engine.set_listening_mode("quiet")
            await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))
            return
        if action in {"resume", "unmute"}:
            self._paused = False
            self._muted = False
            self._reset_capture_state()
            self.engine.set_listening_mode("attentive")
            if self.grok_voice is not None:
                await self.grok_voice.resume_input()
            await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))
            return
        if action in {"quiet", "attentive", "passive", "mute"}:
            if action == "mute":
                self._muted = True
                self._reset_playback_boundary()
                self._cancel_respond()
                self._reset_capture_state()
            else:
                self._muted = False
                self.engine.set_listening_mode(action)
                if action == "attentive":
                    self._paused = False
                    self._reset_capture_state()
            await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))
            if action == "attentive" and self.grok_voice is not None:
                await self.grok_voice.resume_input()
            if action in {"quiet", "mute"} and self.grok_voice is not None:
                await self.grok_voice.mute_input()
            return
        if action in {"barge_in", "cancel"}:
            # Cancel in-flight speech only. Durable jobs keep running.
            self._reset_playback_boundary()
            self._cancel_respond()
            if self.grok_voice is not None:
                await self.grok_voice.cancel()
            if self.asr_feed is not None:
                self.asr_feed.abort()
            self._speech_active = False
            self.engine.note_barge_in()
            reason = "client_cancel" if action == "cancel" else "client_barge_in"
            await self.emit(BargeInEvent(at_ms=self.now(), reason=reason))
            return
        if action == "commit":
            tick = self.engine.commit()
            await self._apply_tick(tick)

    async def _handle_camera_message(self, message: dict) -> None:
        """Route explicit camera requests without claiming activation.

        A live voice socket does not own camera capture. It forwards a
        target-bound request to the selected node; only a provider's later
        ``camera_state`` report changes the visible state.
        """

        reported_state = message.get("camera_state")
        if isinstance(reported_state, dict):
            await self.publish_camera_state(reported_state)
            return
        raw_action = str(message.get("action") or "").strip().lower()
        raw_camera = message.get("camera")
        if isinstance(raw_camera, dict) and not raw_action:
            raw_action = str(raw_camera.get("state") or "").strip().lower()
        if raw_action not in {"off", "paused", "active", "capture", "look", "once"}:
            await self.emit(
                ErrorEvent(
                    at_ms=self.now(),
                    code="camera_request_invalid",
                    message="Camera request is invalid; I have not changed the camera.",
                    fatal=False,
                )
            )
            return
        target_id = str(message.get("device_id") or self.device_id or "").strip() or None
        target = live_for_device(target_id) if target_id else self
        if target is not None and target is not self:
            await target.emit(
                CameraRequestEvent(at_ms=target.now(), action=raw_action, device_id=target_id)
            )
            return
        await self.emit(
            CameraRequestEvent(at_ms=self.now(), action=raw_action, device_id=target_id)
        )

    async def request_look_frame(self, *, timeout: float = 12.0) -> str | None:
        """Ask the attached camera client for one frame. Does not mark the camera active."""

        loop = asyncio.get_running_loop()
        previous = self._look_frame_future
        future: asyncio.Future[str] = loop.create_future()
        self._look_frame_future = future
        if previous is not None and not previous.done():
            previous.set_result("")
        await self.emit(
            CameraRequestEvent(
                at_ms=self.now(),
                action="capture",
                device_id=self.device_id,
            )
        )
        try:
            attachment_id = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            if self._look_frame_future is future:
                self._look_frame_future = None
        return attachment_id or None

    async def _handle_look_frame(self, message: dict) -> None:
        attachment_id = str(message.get("attachment_id") or "").strip()
        self._complete_look_frame(attachment_id)

    def _complete_look_frame(self, attachment_id: str) -> None:
        future = self._look_frame_future
        if future is None or future.done():
            return
        future.set_result(attachment_id)

    async def publish_camera_state(self, state: dict) -> None:
        """Publish an Agent 2 provider report to the attached client."""

        self._camera_state = self._normalize_camera_state(state)
        await self.emit(CameraStateEvent(at_ms=self.now(), camera_state=dict(self._camera_state)))
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    @staticmethod
    def _normalize_camera_state(state: dict | None) -> dict:
        raw = dict(state or {})
        value = str(raw.get("state") or "off").strip().lower()
        if value not in {"off", "paused", "active", "denied", "unavailable", "error"}:
            value = "off"
        return {
            "contract_version": str(raw.get("contract_version") or "ev.camera.state.v1"),
            "state": value,
            "visible": bool(raw.get("visible", value == "active")),
            "device_id": str(raw.get("device_id") or "") or None,
            "platform": str(raw.get("platform") or "unknown"),
            "permission_state": str(raw.get("permission_state") or "unknown"),
            "explicit_request": bool(raw.get("explicit_request", False)),
            "paused_reason": raw.get("paused_reason"),
            "consent_state": str(raw.get("consent_state") or "not_granted"),
            "raw_frames_persisted": bool(raw.get("raw_frames_persisted", False)),
            "last_error": raw.get("last_error"),
            "updated_at": raw.get("updated_at"),
        }

    async def _handle_while_held(self, message: dict | bytes) -> bool:
        """Control, resume, and sleep still work while muted or paused."""

        if isinstance(message, (bytes, bytearray, memoryview)):
            return True
        if not isinstance(message, dict):
            return False
        kind = (message.get("type") or "").strip()
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""))
            return True
        if kind in {"text", "transcript"}:
            text = str(message.get("text") or "").strip()
            if not text:
                return True
            if self._is_sleep(text):
                await self._end_sleep(text)
                return True
            if await self._maybe_local_intent(text, from_grok=self.grok_voice is not None):
                return True
            return True
        return False

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
        if self._closed or self._paused or self._muted:
            return
        await self._apply_tick(self.engine.tick())

    async def _apply_tick(self, tick: EngineTick) -> None:
        await self.emit_all(tick.events)
        interrupted = tick.decision.action == TURN_USER_INTERRUPTED or any(
            isinstance(event, BargeInEvent) for event in tick.events
        )
        if interrupted:
            self._cancel_respond()
            self._cancel_backchannel()
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
            self._schedule_backchannel(tick.backchannel.cue)
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

    def _schedule_backchannel(self, cue: str) -> None:
        """Run at most one listening cue, and make it cancelable on new speech."""

        self._cancel_backchannel()
        task = asyncio.create_task(
            self._speak_cue(cue, backchannel=True),
            name="ev-live-backchannel",
        )
        self._backchannel_task = task
        task.add_done_callback(self._backchannel_done)

    def _backchannel_done(self, task: asyncio.Task) -> None:
        if self._backchannel_task is task:
            self._backchannel_task = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    def _cancel_backchannel(self) -> None:
        task = self._backchannel_task
        self._backchannel_task = None
        if task is not None and not task.done():
            task.cancel()

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

    async def _maybe_local_intent(self, text: str, *, from_grok: bool) -> bool:
        """Handle pause/resume/cancel/protocol locally. Never waits for approval."""

        if self._is_sleep(text):
            await self._end_sleep(text)
            return True
        intent = classify_live_intent(text)
        if intent != "none":
            if from_grok and self.grok_voice is not None:
                await self.grok_voice.cancel()
            if intent == "pause":
                await self._handle_control("pause")
                await self.speak_honesty(PAUSE_SPOKEN)
                return True
            if intent == "resume":
                await self._handle_control("resume")
                await self.speak_honesty(RESUME_SPOKEN)
                return True
            if intent == "cancel":
                await self._handle_control("cancel")
                await self.speak_honesty(CANCEL_SPOKEN)
                return True
            if intent in {"capability", "refused"}:
                await self.speak_capability(include_refused=intent == "refused")
                return True
            return False
        # Realtime providers own their function-call protocol. The transcript
        # resolver is pipeline-only so a provider transcript can never cancel
        # Grok, steal TTS, or block the audio pump. Allowlisted Mac open/close
        # still runs the helper in the background without interrupting speech.
        pipeline_intent = (not from_grok) and self.grok_voice is None
        legacy_sidecar = bool(
            from_grok
            and self.grok_voice is not None
            and getattr(self.grok_voice, "_provider", "") == "openai"
            and not getattr(self.grok_voice, "supports_function_calls", False)
        )
        from app.ev.tool_select import DETERMINISTIC_LIVE_ACTIONS, resolve_live_action

        resolved = resolve_live_action(text)
        if (
            from_grok
            and resolved is not None
            and resolved[0] in DETERMINISTIC_LIVE_ACTIONS
            and self.run_live_tool is not None
        ):
            self._schedule_silent_life_action(*resolved)
            return False
        if not ((pipeline_intent or legacy_sidecar) and self.run_live_tool is not None):
            return False
        if resolved is None:
            return False
        name, arguments = resolved
        await self.push_progress(name)
        call_id = "openai-sidecar" if legacy_sidecar else "local-intent"
        raw = await self.run_live_tool(name, arguments, call_id)
        spoken = _spoken_from_tool_json(raw)
        if spoken:
            await self.speak_honesty(spoken)
        return True

    def _schedule_silent_life_action(self, name: str, arguments: dict) -> None:
        """Run Mac open/close without cancelling Grok or blocking audio."""

        key = (name, json.dumps(arguments, sort_keys=True, default=str))
        now = time.monotonic()
        if (
            self._last_life_action == key
            and now - self._last_life_action_at < _LIFE_ACTION_DEDUP_S
        ):
            return
        self._last_life_action = key
        self._last_life_action_at = now
        self._life_action_task = asyncio.create_task(
            self._run_silent_life_action(name, arguments),
            name="ev-live-open-close",
        )

    async def _run_silent_life_action(self, name: str, arguments: dict) -> None:
        if self.run_live_tool is None:
            return
        try:
            await self.run_live_tool(name, arguments, "deterministic-life")
        except Exception:
            logger.exception("macos life action failed name=%s", name)

    async def speak_honesty(self, text: str, *, code: str | None = None, fatal: bool = False) -> None:
        if not text or text == self._last_honesty:
            return
        self._last_honesty = text
        await self._speak_listen_ack(text)
        await self.emit(
            ReplyEvent(
                at_ms=self.now(),
                text=text,
                conversation_id=self.conversation_id,
                device_id=self.device_id,
                tts_device_id=self.tts_device_id,
            )
        )
        if code:
            await self.emit(
                ErrorEvent(at_ms=self.now(), code=code, message=text, fatal=fatal)
            )

    async def speak_proactive(
        self,
        text: str,
        *,
        hud: dict | None = None,
        emergency: bool = False,
        bypass_quiet_hours: bool = False,
    ) -> None:
        if self._closed or self._paused or self._muted:
            return
        if not proactive_speech_allowed(
            emergency=emergency,
            bypass_quiet_hours=bypass_quiet_hours,
        ):
            return
        await self.speak_honesty(text)
        if hud:
            await self.push_hud(hud, kind="callout")

    async def speak_capability(self, *, include_refused: bool = False) -> None:
        payload: dict = {}
        if self._capability_reply is not None:
            try:
                produced = self._capability_reply(include_refused=include_refused)
                payload = await produced if asyncio.iscoroutine(produced) else produced
            except Exception:  # noqa: BLE001
                payload = {}
        if payload:
            self._capability_manifest = build_live_capability_manifest(
                payload,
                device_id=self.device_id,
                tts_device_id=self.tts_device_id,
                provider=(
                    getattr(self.grok_voice, "_provider", None)
                    if self.grok_voice is not None
                    else "pipeline"
                ),
            )
        text = str((payload or {}).get("reply") or CAPABILITY_FALLBACK)
        if self._live_action_tools_available() is False:
            text = NO_LIVE_ACTION_TOOLS_SPOKEN
        await self.speak_honesty(text)
        hud = protocol_hud_from_payload(payload or {})
        if hud:
            await self.push_hud(hud, kind="protocols")

    def _live_action_tools_available(self) -> bool | None:
        """Return the current session's executable-tool state.

        ``None`` means this is a legacy/unit session without a capability
        projection.  Once the runtime has supplied a projection, an empty
        function set is authoritative and must be spoken as unavailable.
        """

        bridge = self.grok_voice
        if bridge is not None:
            names = getattr(bridge, "advertised_tool_names", ())
            if callable(names):
                names = names()
            return bool(names)
        manifest = self._capability_manifest
        if not isinstance(manifest, dict):
            return None
        if manifest.get("capability_projection_present") is False:
            return None
        projection_keys = (
            "realtime_tool_names",
            "realtime_tools",
            "live_tool_projection",
            "executable_tools",
        )
        if not any(key in manifest for key in projection_keys) and not manifest.get(
            "capability_error"
        ):
            return None
        for key in projection_keys:
            value = manifest.get(key)
            if isinstance(value, list) and value:
                return True
        return False

    async def apply_approval_hold(self, payload: dict, *, speak: bool = True) -> None:
        """Speak the hold line and show a HUD card. Do not wait for the tap."""

        self._approval_hold = payload
        self.engine.state.tool_state = "waiting"
        spoken = str(payload.get("spoken") or HOLD_LINE)
        hud = payload.get("hud") if isinstance(payload.get("hud"), dict) else None
        if speak and self.grok_voice is None:
            await self.speak_honesty(spoken)
        if hud:
            await self.push_hud(hud, kind="approval_hold")
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    async def clear_approval_hold(self, *, spoken: str | None = None) -> None:
        self._approval_hold = None
        self.engine.state.tool_state = "none"
        if spoken and not self._closed:
            await self.speak_honesty(spoken)
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    async def complete_approval_hold(
        self,
        name: str,
        result: dict,
        *,
        spoken: str | None = None,
    ) -> None:
        """Deliver the parked result. The owner already confirmed; do not quiet-hours gate."""

        payload = result if isinstance(result, dict) else {}
        if tool_result_is_successful(payload):
            line = spoken or str(payload.get("spoken") or f"{name.replace('_', ' ')} done.")
        else:
            body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            line = str(
                (body or {}).get("spoken")
                or payload.get("spoken")
                or f"I couldn't complete {name.replace('_', ' ')} yet."
            )
        hold = self._approval_hold if isinstance(self._approval_hold, dict) else {}
        call_id = str(hold.get("_realtime_call_id") or "")
        await self.push_evidence(name, payload)
        continued = False
        if call_id and self.grok_voice is not None:
            continue_after_approval = getattr(self.grok_voice, "continue_after_approval", None)
            if callable(continue_after_approval):
                continued = await continue_after_approval(name, payload, call_id=call_id)
        self._approval_hold = None
        self.engine.state.tool_state = "none"
        if not self._closed and line != self._last_honesty and not continued:
            await self.speak_honesty(line)
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    async def push_hud(self, card: dict, *, kind: str = "card") -> None:
        await self.emit(HudEvent(at_ms=self.now(), card=card, kind=kind))

    async def push_progress(self, name: str, *, detail: str | None = None) -> None:
        self.engine.state.tool_state = "running"
        await self.push_hud(progress_hud(name, detail=detail), kind="progress")
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    async def push_evidence(self, name: str, result: dict) -> None:
        self.engine.state.tool_state = "none"
        self._approval_hold = None
        if tool_result_is_successful(result):
            card = evidence_hud(name, result)
            kind = "evidence"
        else:
            card = tool_result_hud(name, result)
            kind = "result"
        await self.push_hud(card, kind=kind)
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    async def push_tool_result(self, name: str, result: dict) -> None:
        """Show a failed/degraded result without styling it as evidence."""

        self.engine.state.tool_state = "none"
        self._approval_hold = None
        await self.push_hud(tool_result_hud(name, result), kind="result")
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

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
        if await self._maybe_local_intent(command or text, from_grok=False):
            self.engine.turns.reset_turn()
            self.engine.finish_response()
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
        self._reset_playback_boundary()
        background = needs_deep_work(text)
        self.engine.begin_response(background=background)
        await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))
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
            await self.emit(StateEvent(at_ms=self.now(), state=self.interaction_snapshot()))

    def _is_sleep(self, text: str) -> bool:
        from app.voice.lifecycle import is_sleep_phrase

        return is_sleep_phrase(text)

    async def _end_sleep(self, text: str) -> None:
        self._closed = True
        self._reset_playback_boundary()
        unregister_live(self)
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
        self._reset_playback_boundary()
        unregister_live(self)
        self._cancel_respond()
        if self.grok_voice is not None:
            closer = getattr(self.grok_voice, "close", None)
            if callable(closer):
                closer()
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
