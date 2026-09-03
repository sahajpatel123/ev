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

from app.ev.camera_runtime import (
    RECORD_MAX_POSTERS,
    CameraReadiness,
    LookFrame,
    decode_frame_payload,
    log_camera,
    parse_look_frame_meta,
    readiness_from_camera_state,
    validate_jpeg,
)
from app.ev.computer_runtime import (
    ComputerReadiness,
    cancel_computer_task,
    drop_state,
    ensure_state,
    log_computer,
    note_goal,
    readiness_from_computer_state,
    skip_silent_lifecycle_for,
)
from app.ev.computer_strategy import looks_like_computer_task
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
    ComputerRequestEvent,
    ComputerStateEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    HudEvent,
    LatencyEvent,
    LiveEvent,
    PartialTranscriptEvent,
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
    compact_live_tool_json,
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
_CODE_WORKING_SPOKEN = (
    "I'm writing that now. I'll tell you when it's saved and I've run it."
)
_CODE_BUSY_SPOKEN = "I'm still finishing the last coding job."
_CODE_EXEC_CALL_ID = "owner-code-exec"
# Keep the live channel close to real time when a client briefly falls behind.
# Pipeline TTS is paced at its render duration. Speech-to-speech audio is not:
# the provider already streams near real time, and waiting here stalls the
# event pump until a late transcript arrives and used to wipe the reply.
# Only barge-in / provider-reset events discard queued speech. A user
# transcript is the turn she is answering — dropping audio there cuts her
# off mid-sentence.
# Speech-to-speech PCM must not stall behind an 8-event (~0.6 s) ceiling.
# A full outbound queue blocked the provider recv path and punched holes
# in replies after ~20 s. 64 held several seconds but a 30s tool-answer burst
# (provider generates ~3x realtime) still stalled. 96 gives ~7.5s headroom
# while still bounding memory, and HUD/audio prioritization keeps it realtime.
_LIVE_OUTBOUND_MAX_EVENTS = 96
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
    spoken = (
        body.get("spoken")
        or payload.get("spoken")
        or body.get("error")
        or body.get("reason")
        or payload.get("error")
    )
    text = str(spoken or "").strip()
    return text or None


def _is_empty_memory_spoken(text: str) -> bool:
    blob = (text or "").strip().lower()
    return (
        "cannot find that particular record" in blob
        or "no reliable record" in blob
        or "no reliable source" in blob
        or "dedicated memory" in blob
    )


def _owner_memory_live_action(text: str) -> tuple[str, dict] | None:
    """Transcript → keep/look or recall when Mini will hedge instead of calling it."""

    from app.ev.laptop_files import is_system_confirmation
    from app.ev.tool_select import resolve_live_action
    from app.memory.visual import wants_keep_visible, is_keep_recall_query, is_visual_recall_query

    if is_system_confirmation(text):
        return None
    if is_keep_recall_query(text) or is_visual_recall_query(text):
        return "search_memory", {"query": text[:400]}
    if wants_keep_visible(text):
        return "look", {"prompt": text[:400], "focus": "auto"}
    resolved = resolve_live_action(text)
    if resolved is None:
        return None
    if resolved[0] == "look" and wants_keep_visible(text):
        return resolved
    if resolved[0] in {"search_memory", "recall", "recall_history"}:
        return resolved
    return None


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
        self._client_gone = False
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
        self._owner_text_task: asyncio.Task | None = None
        self._code_job_task: asyncio.Task | None = None
        self._code_job_announce_progress = False
        self._last_life_action: tuple[str, str] | None = None
        self._last_life_action_at = 0.0
        self._last_code_job: dict[str, Any] | None = None
        self.device_id = str(device_id) if device_id else None
        self.tts_device_id = str(tts_device_id) if tts_device_id else self.device_id
        self._capability_reply = capability_reply
        self._capability_manifest = (
            dict(capability_manifest) if isinstance(capability_manifest, dict) else None
        )
        self._camera_state = self._normalize_camera_state(camera_state)
        self._look_frame_queues: dict[str, asyncio.Queue] = {}
        self._look_frame_order: list[str] = []
        self._last_capture_status: str | None = None
        self._computer_state: dict[str, Any] = {}
        self._computer_queues: dict[str, asyncio.Queue] = {}
        self._computer_order: list[str] = []
        self._last_computer_status: str | None = None
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

    def _schedule_relationship_turn(
        self,
        role: str,
        text: str | None,
        *,
        transcript_source: str | None = None,
        extra_metadata: dict | None = None,
    ) -> None:
        from app.memory.turns import schedule_live_turn

        schedule_live_turn(
            text=text or "",
            role=role,
            conversation_id=self.conversation_id,
            device_id=self.device_id,
            live_session_id=self.session_id,
            transcript_source=transcript_source,
            extra_metadata=extra_metadata,
        )

    def _schedule_turn_gate(self, event) -> None:
        """G1.6 TurnGate: schedule authoritative handling of final owner transcript."""
        import asyncio

        async def _run_gate():
            try:
                from app.db import SessionLocal
                from app.ev.owner_turn import create_owner_turn
                from app.ev.turn_gate import (
                    create_realtime_response_payload,
                    handle_owner_turn,
                )
                from app.utils.text import utcnow

                # Create canonical OwnerTurn from FinalTranscriptEvent
                # Provider item id is not directly on event; use text hash + session as fallback
                # For live, the provider_item_id is available via GrokVoiceBridge's UserAudioTurn
                provider_item_id = getattr(event, "provider_item_id", None) or getattr(event, "item_id", None)
                turn = create_owner_turn(
                    live_session_id=self.session_id,
                    provider_item_id=provider_item_id,
                    owner_id="master",  # resolved via session's actor (master for owner)
                    device_id=str(self.device_id) if self.device_id else None,
                    transcript=event.text,
                    transcript_source=getattr(event, "transcript_source", None),
                    confidence=getattr(event, "confidence", None),
                    committed_at=utcnow(),
                    transcription_completed_at=utcnow(),
                )
                note_owner_turn = getattr(self.grok_voice, "note_owner_turn", None)
                if callable(note_owner_turn):
                    note_owner_turn(turn_id=turn.turn_id)
                note_turn_gate = getattr(self.grok_voice, "note_turn_gate", None)
                if callable(note_turn_gate):
                    note_turn_gate(turn_id=turn.turn_id)
                async with SessionLocal() as session:
                    result = await handle_owner_turn(session, turn)
                    # G1.11 repair: the live voice path OWNS its transaction.
                    # Services only flush; without this commit the context exit
                    # ROLLED BACK every voice mutation while TurnResult still
                    # reported ok=true (owner-proven commitment failure).
                    await session.commit()
                    note_turn_result = getattr(self.grok_voice, "note_turn_result", None)
                    if callable(note_turn_result):
                        note_turn_result(ok=bool(result.ok))
                    # OWNER LATENCY LAW: server VAD auto-creates the spoken
                    # response the moment speech ends. The gate's canonical
                    # recording above still runs in parallel — but the gate
                    # must NOT send its own response.create, or the model
                    # would answer the owner twice (two takes, two angles).
                    # Observability trace (no custom provider event — GA-safe)
                    import logging as _log

                    _log.getLogger("ev.turn_gate").warning(
                        "turn_gate recorded turn_id=%s route=%s op=%s ok=%s (response owned by server VAD)",
                        turn.turn_id,
                        result.route,
                        result.operation,
                        result.ok,
                    )
            except Exception as e:
                import logging

                logging.getLogger("ev.turn_gate").exception("turn_gate failed for %s: %s", getattr(event, "text", "")[:40], e)

        # Schedule without blocking emit
        try:
            asyncio.create_task(_run_gate())
        except RuntimeError:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(_run_gate())

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
        persist_user = isinstance(event, FinalTranscriptEvent)
        persist_assistant = (
            isinstance(event, ReplyEvent)
            and self.grok_voice is not None
            and event.model
        )
        if isinstance(event, PartialTranscriptEvent) and getattr(event, "role", "user") != "assistant":
            await self._preempt_memory_hedge(event.text)
        if persist_user:
            from_s2s = event.provider in {"openai-realtime", "grok-voice"}
            from app.ev.laptop_files import is_system_confirmation
            from app.memory.visual import is_camera_prompt_echo, is_memory_hedge_scene

            if not is_system_confirmation(event.text) and not is_camera_prompt_echo(
                event.text
            ):
                await self._maybe_local_intent(event.text, from_grok=from_s2s)
            # Injected speak_ack / speak_life_record prompts echo as user
            # transcripts. Storing them poisons owner history and camera looks.
            if (
                not is_system_confirmation(event.text)
                and not is_camera_prompt_echo(event.text)
                and not is_memory_hedge_scene(event.text)
                and (self.grok_voice is not None or from_s2s)
            ):
                self._schedule_relationship_turn(
                    "user",
                    event.text,
                    transcript_source=getattr(event, "transcript_source", None),
                )
            # G1.6 TurnGate: authoritative control plane (shadow until cutover, then direct)
            from app.config import settings as _gate_settings
            if getattr(_gate_settings, "turn_gate_enabled", False):
                # Schedule gate handling without blocking emit
                self._schedule_turn_gate(event)
        self._prepare_outbound(event)
        try:
            self.outbound.put_nowait(event)
        except asyncio.QueueFull:
            # Continuous telemetry may be skipped while a slow client catches
            # up. Important events wait for a slot so they cannot be starved by
            # a burst of audio.
            if event.type in _LIVE_COALESCED_EVENT_TYPES:
                return
            if persist_user and self._client_gone:
                return
            if isinstance(event, (FinalTranscriptEvent, ReplyEvent)):
                self._discard_outbound(
                    lambda queued: queued.type in _LIVE_COALESCED_EVENT_TYPES
                    or queued.type == "partial"
                )
                try:
                    self.outbound.put_nowait(event)
                except asyncio.QueueFull:
                    if persist_user and self._client_gone:
                        return
                    await self.outbound.put(event)
            elif (
                tts_generation is not None
                and tts_generation != self._tts_pacing_generation
            ):
                return
            else:
                await self.outbound.put(event)
                if (
                    tts_generation is not None
                    and tts_generation != self._tts_pacing_generation
                ):
                    self._discard_outbound(lambda queued: queued is event, first_only=True)
                    return
        if persist_assistant:
            extra = None
            if getattr(event, "interrupted", False):
                from app.voice.live.barge_in import interrupt_metadata

                extra = interrupt_metadata(
                    reason=getattr(event, "interruption_reason", None) or "user_barge_in",
                    provider_response_id=getattr(event, "provider_response_id", None),
                    audio_played_ms=getattr(event, "audio_played_ms", None),
                    generated_duration_ms=getattr(event, "generated_duration_ms", None),
                    generated_text=getattr(event, "generated_text", None) or event.text,
                )
            from app.ev.laptop_files import is_system_confirmation
            from app.memory.visual import is_memory_hedge_scene

            if not is_system_confirmation(event.text) and not is_memory_hedge_scene(event.text):
                self._schedule_relationship_turn(
                    "assistant", event.text, extra_metadata=extra
                )

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
        elif (
            isinstance(event, (TtsChunkEvent, ReplyEvent, FinalTranscriptEvent))
            and self.outbound.full()
        ):
            # Preserve spoken audio, transcripts, and replies. Telemetry is
            # disposable if the client is behind. During tool execution a burst
            # of HUD/progress cards can fill the queue and delay audio → glitch.
            # Prioritize audio over HUD/state coalescing.
            self._discard_outbound(
                lambda queued: queued.type in _LIVE_COALESCED_EVENT_TYPES
            )
            if self.outbound.full():
                # Still full → HUD cards are next most disposable (keep newest).
                self._discard_outbound(lambda queued: queued.type == "hud")

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
            "computer_tool_schema_hash": bridge_diagnostics.get("computer_tool_schema_hash"),
            "tool_schema_match": bridge_diagnostics.get("tool_schema_match"),
            "provider_tools_confirmed": bridge_diagnostics.get("provider_tools_confirmed"),
            "computer_control_ready": bridge_diagnostics.get("computer_control_ready"),
            "tool_schema_generation": bridge_diagnostics.get("tool_schema_generation"),
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
                "camera": self.camera_readiness().as_dict(),
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
                "camera": self.camera_readiness().as_dict(),
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

        if self._closed or self._client_gone:
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

    def _schedule_owner_text(
        self, text: str, *, from_grok: bool, commit: bool = True
    ) -> None:
        """Run owner text off the websocket receive coroutine."""

        self._owner_text_task = asyncio.create_task(
            self._dispatch_owner_text(text, from_grok=from_grok, commit=commit),
            name="ev-live-owner-text",
        )

    async def _dispatch_owner_text(
        self, text: str, *, from_grok: bool, commit: bool = True
    ) -> None:
        del commit
        try:
            if self._closed or self._client_gone:
                return
            if self._is_sleep(text):
                await self._end_sleep(text)
                return
            grok = self.grok_voice
            from app.ev.laptop_files import is_system_confirmation

            if grok is not None and not is_system_confirmation(text):
                grok._last_input_transcript = text
                grok._last_input_transcript_at = time.monotonic()
            if await self._maybe_local_intent(text, from_grok=from_grok):
                return
            grok = self.grok_voice
            if grok is None:
                return
            if looks_like_computer_task(text):
                note_goal(ensure_state(self.session_id), text)
            if getattr(grok, "_response_active", False) or getattr(
                grok, "_assistant_open", False
            ):
                await grok.cancel()
                await self.emit(BargeInEvent(at_ms=self.now(), reason="text_input"))
            await grok.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live owner text dispatch failed")

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
        if kind == "keepalive":
            return
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
            # File/code brokers await Mac computer_result. That result arrives
            # on this same websocket. Do not hold the receive coroutine.
            self._schedule_owner_text(text, from_grok=True)
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
        if kind in {"computer", "computer_state"}:
            await self._handle_computer_state_message(message)
            return
        if kind == "computer_result":
            await self._handle_computer_result(message)
            return
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""), message)
            return
        if kind == "commit":
            return

    async def _handle_dict(self, message: dict) -> None:
        kind = (message.get("type") or "").strip()
        if kind == "keepalive":
            return
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
        if kind in {"computer", "computer_state"}:
            await self._handle_computer_state_message(message)
            return
        if kind == "computer_result":
            await self._handle_computer_result(message)
            return
        if kind == "control":
            await self._handle_control(str(message.get("action") or ""), message)
            return
        if kind == "commit":
            tick = self.engine.commit()
            await self._apply_tick(tick)

    async def _handle_control(self, action: str, message: dict | None = None) -> None:
        action = action.strip().lower()
        if action == "wake":
            action = "resume"
        if action == "listener_presence":
            # OWNER DECISION 2026-08-23: Listener Presence is CANCELLED.
            # Accepted-and-ignored for old clients so the control can never
            # affect audio state again.
            return
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
            # Cancel in-flight speech only. Durable Mac jobs keep running
            # through echo / barge-in so Talk does not report a fake deny
            # while Chrome is still searching. Explicit cancel/stop is the
            # only client control that aborts computer waiters.
            from app.voice.live.barge_in import parse_interrupt_request

            request = parse_interrupt_request(message)
            reason = "client_cancel" if action == "cancel" else request.reason
            self._reset_playback_boundary()
            self._cancel_respond()
            self._fail_look_futures(LookFrame(request_id="", error="cancelled", last=True))
            if action == "cancel":
                self._fail_computer_futures({"ok": False, "error": "cancelled", "spoken": "Stopped."})
            await self.emit(
                CameraRequestEvent(
                    at_ms=self.now(),
                    action="observe_stop",
                    device_id=self.device_id,
                )
            )
            if self.grok_voice is not None:
                await self.grok_voice.interrupt_for_user(
                    reason=reason,
                    audio_played_ms=request.audio_played_ms,
                    confidence=request.confidence,
                    preroll_ms=request.preroll_ms,
                )
            if self.asr_feed is not None:
                self.asr_feed.abort()
            self._speech_active = False
            self.engine.note_barge_in()
            await self.emit(
                BargeInEvent(
                    at_ms=self.now(),
                    reason=reason,
                    audio_played_ms=request.audio_played_ms,
                    confidence=request.confidence,
                    preroll_ms=request.preroll_ms,
                    provider_response_id=request.provider_response_id,
                )
            )
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
        if raw_action not in {
            "off",
            "paused",
            "active",
            "capture",
            "look",
            "once",
            "capture_save",
            "observe",
            "observe_stop",
            "record",
            "record_stop",
        }:
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
        request_id = str(message.get("request_id") or "").strip() or None
        target = live_for_device(target_id) if target_id else self
        event = CameraRequestEvent(
            at_ms=(target or self).now(),
            action=raw_action,
            device_id=target_id,
            request_id=request_id,
        )
        if target is not None and target is not self:
            await target.emit(event)
            return
        await self.emit(event)

    def camera_readiness(self) -> CameraReadiness:
        provider = None
        if self.grok_voice is not None:
            provider = getattr(self.grok_voice, "_provider", None)
        elif isinstance(self._capability_manifest, dict):
            provider = (
                self._capability_manifest.get("current_provider")
                or (self._capability_manifest.get("active_providers") or {}).get("realtime")
            )
        ready = readiness_from_camera_state(
            self._camera_state,
            client_connected=not self._closed,
            realtime_provider=str(provider or ""),
            device_id=self.device_id,
            session_id=self.session_id,
            connecting_device=bool(self.device_id),
        )
        ready.last_capture_status = self._last_capture_status
        return ready

    def computer_readiness(self) -> ComputerReadiness:
        helper_ready = False
        if isinstance(self._capability_manifest, dict):
            computer = self._capability_manifest.get("computer_control")
            if isinstance(computer, dict):
                helper_ready = bool(computer.get("app_lifecycle_ready") and not computer.get("mac_client_connected"))
        provider = None
        if self.grok_voice is not None:
            provider = getattr(self.grok_voice, "_provider", None)
        ready = readiness_from_computer_state(
            self._computer_state,
            client_connected=not self._closed,
            helper_ready=helper_ready,
            realtime_provider=str(provider or ""),
            device_id=self.device_id,
            session_id=self.session_id,
        )
        ready.last_error = self._last_computer_status
        return ready

    def _ensure_look_queue(self, request_id: str) -> asyncio.Queue:
        queue = self._look_frame_queues.get(request_id)
        if queue is None:
            queue = asyncio.Queue()
            self._look_frame_queues[request_id] = queue
            self._look_frame_order.append(request_id)
        return queue

    def _drop_look_queue(self, request_id: str | None) -> None:
        if not request_id:
            return
        self._look_frame_queues.pop(request_id, None)
        if request_id in self._look_frame_order:
            self._look_frame_order.remove(request_id)

    def _fail_look_futures(self, frame: LookFrame) -> None:
        for request_id in list(self._look_frame_order):
            queue = self._look_frame_queues.get(request_id)
            if queue is not None:
                failed = LookFrame(
                    request_id=request_id,
                    error=frame.error,
                    permission=frame.permission,
                    last=True,
                )
                if queue.empty():
                    queue.put_nowait(failed)
        self._look_frame_queues.clear()
        self._look_frame_order.clear()

    async def request_look_frame(
        self,
        *,
        timeout: float = 12.0,
        request_id: str | None = None,
        detail: str | None = None,
        action: str = "capture",
    ) -> LookFrame | None:
        """Ask the attached camera client for one frame. Does not keep the camera on."""

        rid = str(request_id or "").strip() or f"look-{self.now()}"
        emit_action = action if action in {"capture", "look", "once", "capture_save"} else "capture"
        log_camera(
            "camera.request_sent",
            request_id=rid,
            extra={"device_id": self.device_id, "action": emit_action},
        )
        queue = self._ensure_look_queue(rid)
        await self.emit(
            CameraRequestEvent(
                at_ms=self.now(),
                action=emit_action,
                device_id=self.device_id,
                request_id=rid,
                detail=detail,
            )
        )
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            self._last_capture_status = "timeout"
            return LookFrame(request_id=rid, error="timeout", last=True)
        except asyncio.CancelledError:
            raise
        finally:
            self._drop_look_queue(rid)
        return frame

    async def request_observe_frames(
        self,
        *,
        duration_s: float,
        interval_s: float,
        max_frames: int,
        timeout: float,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> list[LookFrame]:
        """Bounded temporal capture. The client stops at last=true, timeout, or cancel."""

        rid = str(request_id or "").strip() or f"observe-{self.now()}"
        log_camera(
            "camera.request_sent",
            request_id=rid,
            extra={
                "device_id": self.device_id,
                "action": "observe",
                "duration_s": duration_s,
                "max_frames": max_frames,
            },
        )
        queue = self._ensure_look_queue(rid)
        await self.emit(
            CameraRequestEvent(
                at_ms=self.now(),
                action="observe",
                device_id=self.device_id,
                request_id=rid,
                duration_ms=int(duration_s * 1000),
                interval_ms=int(interval_s * 1000),
                max_frames=max_frames,
                detail=detail,
            )
        )
        frames: list[LookFrame] = []
        deadline = time.monotonic() + max(timeout, duration_s + 2.0)
        try:
            while time.monotonic() < deadline and len(frames) < max_frames:
                remaining = deadline - time.monotonic()
                frame = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
                frames.append(frame)
                if frame.last or frame.error:
                    break
        except TimeoutError:
            if not frames:
                self._last_capture_status = "timeout"
                return [LookFrame(request_id=rid, error="timeout", last=True)]
        except asyncio.CancelledError:
            await self.emit(
                CameraRequestEvent(
                    at_ms=self.now(),
                    action="observe_stop",
                    device_id=self.device_id,
                    request_id=rid,
                )
            )
            raise
        finally:
            self._drop_look_queue(rid)
        return frames

    async def request_record_clip(
        self,
        *,
        duration_s: float,
        timeout: float,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> list[LookFrame]:
        """Ask the attached camera client to record one bounded video clip.

        The client may send several poster frames (start/mid/end). Collect until
        last=true so the live model can describe the clip, not one freeze-frame.
        A single last=true payload still completes immediately.
        """

        rid = str(request_id or "").strip() or f"record-{self.now()}"
        log_camera(
            "camera.request_sent",
            request_id=rid,
            extra={
                "device_id": self.device_id,
                "action": "record",
                "duration_s": duration_s,
            },
        )
        queue = self._ensure_look_queue(rid)
        await self.emit(
            CameraRequestEvent(
                at_ms=self.now(),
                action="record",
                device_id=self.device_id,
                request_id=rid,
                duration_ms=int(duration_s * 1000),
                detail=detail,
            )
        )
        frames: list[LookFrame] = []
        deadline = time.monotonic() + max(timeout, duration_s + 2.0)
        try:
            while time.monotonic() < deadline and len(frames) < RECORD_MAX_POSTERS:
                remaining = deadline - time.monotonic()
                frame = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
                frames.append(frame)
                if frame.last or frame.error:
                    break
        except TimeoutError:
            if not frames:
                self._last_capture_status = "timeout"
                await self.emit(
                    CameraRequestEvent(
                        at_ms=self.now(),
                        action="record_stop",
                        device_id=self.device_id,
                        request_id=rid,
                    )
                )
                return [LookFrame(request_id=rid, error="timeout", last=True, media_kind="video")]
        except asyncio.CancelledError:
            await self.emit(
                CameraRequestEvent(
                    at_ms=self.now(),
                    action="record_stop",
                    device_id=self.device_id,
                    request_id=rid,
                )
            )
            raise
        finally:
            self._drop_look_queue(rid)
        return frames

    async def cancel_camera_requests(self, *, reason: str = "cancelled") -> None:
        self._fail_look_futures(LookFrame(request_id="", error=reason, last=True))

    async def _handle_look_frame(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "").strip()
        attachment_id = str(message.get("attachment_id") or "").strip() or None
        permission = str(message.get("permission") or message.get("permission_state") or "") or None
        error = str(message.get("error") or "").strip() or None
        jpeg = decode_frame_payload(
            message.get("jpeg_b64") or message.get("image_b64") or message.get("image")
        )
        width = message.get("width")
        height = message.get("height")
        if jpeg:
            validated = validate_jpeg(jpeg)
            if validated is None:
                jpeg = None
                error = error or "malformed_image"
            else:
                jpeg, parsed_w, parsed_h = validated
                width = width or parsed_w
                height = height or parsed_h
        if jpeg is None and not attachment_id and not error:
            preview = parse_look_frame_meta(message)
            if not preview.get("saved_path"):
                error = "empty_frame"
        try:
            sequence = int(message.get("sequence") or 0)
        except (TypeError, ValueError):
            sequence = 0
        last_raw = message.get("last")
        last = True if last_raw is None else bool(last_raw)
        try:
            parsed_width = int(width) if width is not None else None
        except (TypeError, ValueError):
            parsed_width = None
        try:
            parsed_height = int(height) if height is not None else None
        except (TypeError, ValueError):
            parsed_height = None
        meta = parse_look_frame_meta(message)
        frame = LookFrame(
            request_id=request_id,
            jpeg=jpeg,
            attachment_id=attachment_id,
            width=parsed_width,
            height=parsed_height,
            error=error,
            permission=permission,
            camera_name=str(message.get("camera_name") or "") or None,
            sequence=sequence,
            last=last,
            encoded_bytes=len(jpeg) if jpeg else 0,
            labels=meta.get("labels") or None,
            ocr_text=meta.get("ocr_text"),
            luminance=meta.get("luminance"),
            face_count=meta.get("face_count"),
            person_count=meta.get("person_count"),
            lighting=meta.get("lighting"),
            colors=meta.get("colors") or None,
            saved_path=meta.get("saved_path"),
            media_kind=meta.get("media_kind"),
            duration_ms=meta.get("duration_ms"),
        )
        if permission:
            self._camera_state["permission_state"] = permission
        if error:
            self._last_capture_status = error
        elif jpeg or attachment_id or frame.saved_path:
            self._last_capture_status = "success"
        queue = self._look_frame_queues.get(request_id) if request_id else None
        if queue is None and self._look_frame_order:
            queue = self._look_frame_queues.get(self._look_frame_order[0])
        if queue is None:
            return
        queue.put_nowait(frame)

    def _ensure_computer_queue(self, request_id: str) -> asyncio.Queue:
        queue = self._computer_queues.get(request_id)
        if queue is None:
            queue = asyncio.Queue()
            self._computer_queues[request_id] = queue
            self._computer_order.append(request_id)
        return queue

    def _drop_computer_queue(self, request_id: str | None) -> None:
        if not request_id:
            return
        self._computer_queues.pop(request_id, None)
        if request_id in self._computer_order:
            self._computer_order.remove(request_id)

    def _fail_computer_futures(self, payload: dict) -> None:
        for request_id in list(self._computer_order):
            queue = self._computer_queues.get(request_id)
            if queue is not None and queue.empty():
                queue.put_nowait({**payload, "request_id": request_id})
        self._computer_queues.clear()
        self._computer_order.clear()

    async def request_computer(
        self,
        command: str,
        arguments: dict | None = None,
        *,
        timeout: float = 12.0,
        request_id: str | None = None,
    ) -> dict:
        """Ask the attached Mac client to perform one structured computer action."""

        rid = str(request_id or "").strip() or f"computer-{self.now()}"
        queue = self._ensure_computer_queue(rid)
        await self.emit(
            ComputerRequestEvent(
                at_ms=self.now(),
                command=command,
                request_id=rid,
                arguments=dict(arguments or {}),
                device_id=self.device_id,
            )
        )
        try:
            result = await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            self._last_computer_status = "timeout"
            return {
                "ok": False,
                "error": "timeout",
                "spoken": "The Mac did not complete that action in time.",
                "request_id": rid,
                "command": command,
            }
        except asyncio.CancelledError:
            raise
        finally:
            self._drop_computer_queue(rid)
        if isinstance(result, dict):
            result.setdefault("request_id", rid)
            result.setdefault("command", command)
            return result
        return {"ok": False, "error": "invalid_result", "request_id": rid, "command": command}

    async def cancel_computer_requests(self, *, reason: str = "cancelled") -> None:
        self._fail_computer_futures({"ok": False, "error": reason, "spoken": "Stopped."})
        await self.emit(
            ComputerRequestEvent(
                at_ms=self.now(),
                command="cancel",
                request_id=f"cancel-{self.now()}",
                arguments={"reason": reason},
                device_id=self.device_id,
            )
        )

    async def _handle_computer_state_message(self, message: dict) -> None:
        raw = message.get("computer_state")
        if not isinstance(raw, dict):
            raw = {key: value for key, value in message.items() if key != "type"}
        previous = dict(self._computer_state)
        self._computer_state = dict(raw)
        await self.emit(ComputerStateEvent(at_ms=self.now(), computer_state=dict(self._computer_state)))
        prev_ready = previous.get("generic_ui_control_ready")
        now_ready = self._computer_state.get("generic_ui_control_ready")
        prev_ax = previous.get("accessibility_permission")
        now_ax = self._computer_state.get("accessibility_permission")
        if (prev_ready, prev_ax) != (now_ready, now_ax) and self.grok_voice is not None:
            refresher = getattr(self.grok_voice, "refresh_live_instructions", None)
            if callable(refresher):
                try:
                    await refresher()
                except Exception:  # noqa: BLE001 - instruction refresh must not kill audio
                    log_computer("computer.replan", extra={"reason": "instruction_refresh_failed"})

    async def _handle_computer_result(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "").strip()
        payload = dict(message)
        payload.pop("type", None)
        if payload.get("ok") is False:
            self._last_computer_status = str(payload.get("error") or "failed")
        else:
            self._last_computer_status = "success"
        permissions = {
            key: payload.get(key)
            for key in (
                "accessibility_permission",
                "screen_capture_permission",
                "foreground_app",
                "foreground_bundle_id",
                "generic_ui_control_ready",
                "accessibility_ready",
                "accessibility_probe",
            )
            if payload.get(key) is not None
        }
        if permissions:
            self._computer_state.update(permissions)
        queue = self._computer_queues.get(request_id) if request_id else None
        if queue is None:
            return
        queue.put_nowait(payload)

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
            "camera_name": str(raw.get("camera_name") or "") or None,
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
            if self.grok_voice is not None:
                self._schedule_owner_text(text, from_grok=True)
                return True
            if await self._maybe_local_intent(text, from_grok=False):
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
        # OWNER DECISION 2026-08-23: no server listener speech — the
        # backchannel lane is dead. tick.backchannel is ignored.
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
                await self.cancel_computer_requests(reason="owner_stop")
                cancel_computer_task(self.session_id, reason="owner_stop")
                await self.speak_honesty(CANCEL_SPOKEN)
                return True
            if intent in {"capability", "refused"}:
                await self.speak_capability(include_refused=intent == "refused")
                return True
            return False
        # Realtime providers own their function-call protocol. The transcript
        # resolver is pipeline-only so a provider transcript can never cancel
        # Grok, steal TTS, or block the audio pump — except owner laptop-file
        # and coding commands, which Mini often will not execute, and owner
        # memory recall / memorize-from-sight, which Mini hedges instead of
        # calling search_memory. Those cancel the S2S reply, run the broker,
        # and speak the verified receipt.
        # Allowlisted Mac open/close still runs the helper in the background
        # without interrupting speech.
        pipeline_intent = (not from_grok) and self.grok_voice is None
        legacy_sidecar = bool(
            from_grok
            and self.grok_voice is not None
            and getattr(self.grok_voice, "_provider", "") == "openai"
            and not getattr(self.grok_voice, "supports_function_calls", False)
        )
        from app.ev.tool_select import DETERMINISTIC_LIVE_ACTIONS, resolve_live_action
        from app.ev.laptop_files import is_system_confirmation, looks_like_file_task
        from app.ev.computer_runtime import state_for
        from app.memory.visual import is_camera_prompt_echo

        last_path = str(getattr(state_for(self.session_id), "last_file_path", None) or "").strip() or None
        if not last_path:
            from app.ev.desk_scene import referent_file_path

            found = referent_file_path()
            if found is not None:
                last_path = str(found)
        resolved = resolve_live_action(text)
        if is_system_confirmation(text) or is_camera_prompt_echo(text):
            # Injected speak_ack / speak_life_record prompts echo back as
            # user transcripts. Swallow them so we do not recall again or
            # send_text the prompt into Mini.
            return True
        if resolved is not None and resolved[0] == "code" and self.run_live_tool is not None:
            return await self._run_owner_transcript_broker(
                resolved, call_id="owner-code", from_grok=from_grok
            )
        if await self._speak_last_code_followup(text, from_grok=from_grok):
            return True
        from app.ev.luna_code import last_code_job, looks_like_code_continue

        last_job = last_code_job(str(self.session_id or "")) or self._last_code_job
        if (
            last_job
            and looks_like_code_continue(text)
            and self.run_live_tool is not None
        ):
            return await self._run_owner_transcript_broker(
                ("code", {"goal": text[:4000]}),
                call_id="owner-code",
                from_grok=from_grok,
            )
        owner_memory = _owner_memory_live_action(text)
        if owner_memory is not None and self.run_live_tool is not None:
            if from_grok and self._provider_tool_in_flight():
                # Provider already committed to a function call for this
                # turn: its continuation owns the single spoken reply.
                # Brokering here would double-speak over it → glitch.
                return False
            call_id = "owner-keep" if owner_memory[0] == "look" else "owner-memory"
            return await self._run_owner_transcript_broker(
                owner_memory, call_id=call_id, from_grok=from_grok
            )
        if looks_like_file_task(text, last_path=last_path) and self.run_live_tool is not None:
            args = {"goal": text[:500], "session_id": str(self.session_id or "")}
            if last_path:
                args["last_path"] = last_path
            if resolved is not None and resolved[0] == "computer" and isinstance(resolved[1], dict):
                args = {**resolved[1], **args}
            return await self._run_owner_transcript_broker(
                ("computer", args), call_id="owner-file", from_grok=from_grok
            )
        if (
            from_grok
            and resolved is not None
            and resolved[0] in DETERMINISTIC_LIVE_ACTIONS
            and self.run_live_tool is not None
            and not skip_silent_lifecycle_for(text)
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

    def _provider_owns_live_turn(self) -> bool:
        """True when the realtime provider owns this turn via function calls.

        Cancelling an active function-capable response to run the transcript
        broker creates a SECOND overlapping spoken response: the provider's
        tool continuation and the broker's speak_life_record/speak_honesty
        both send response.create and their PCM interleaves on the client —
        heard as breaking/glitching on every tool turn. Single-speech-lane
        law: when the provider is actively handling the turn, the broker
        must stand down.
        """

        grok = self.grok_voice
        if grok is None:
            return False
        if not getattr(grok, "supports_function_calls", False):
            return False
        return bool(
            getattr(grok, "_response_active", False)
            or getattr(grok, "_assistant_open", False)
            or getattr(grok, "_pending_tools", 0)
            or getattr(grok, "_tool_boundary_pending", False)
        )

    def _provider_tool_in_flight(self) -> bool:
        """True when the provider already committed to a function call.

        Narrower than _provider_owns_live_turn: a merely-speaking response
        with no tool yet may still be a no-record hedge the broker must
        preempt (single lane: cancel hedge, speak pack). But once a tool is
        pending or the tool boundary arrived, the provider's continuation
        owns the single spoken reply and the broker must stand down —
        otherwise both speak over each other on every tool turn.
        """

        grok = self.grok_voice
        if grok is None:
            return False
        if not getattr(grok, "supports_function_calls", False):
            return False
        return bool(
            getattr(grok, "_pending_tools", 0)
            or getattr(grok, "_tool_boundary_pending", False)
        )

    async def _preempt_memory_hedge(self, text: str) -> None:
        """Stop Mini from answering a memory question before the broker runs.

        Shadow ``response.create`` and an early S2S hedge both speak the
        no-record line. The final transcript still owns recall. This only
        cancels that hedge. Playback, VAD, and reconnect stay untouched.
        """

        grok = self.grok_voice
        if grok is None or self.run_live_tool is None:
            return
        if self._provider_tool_in_flight():
            # A provider function call is already in flight: its continuation
            # owns the single spoken reply. Preempt-cancel here would chop
            # that reply and the broker would overlap it → glitch.
            return
        from app.ev.laptop_files import is_system_confirmation

        if is_system_confirmation(text):
            return
        if not (
            getattr(grok, "_response_active", False)
            or getattr(grok, "_assistant_open", False)
        ):
            return
        if _owner_memory_live_action(text) is None:
            return
        turn_id = getattr(grok, "_open_turn_id", None)
        if turn_id:
            grok._shadow_response_for_turn = turn_id
        with contextlib.suppress(Exception):
            await grok.cancel()

    async def _run_owner_transcript_broker(
        self,
        resolved: tuple[str, dict],
        *,
        call_id: str,
        from_grok: bool,
    ) -> bool:
        """Run file/code from the owner transcript when Mini will not call it.

        Cancels the S2S reply so it cannot invent success. Does not change
        playback, VAD, or reconnect.
        """

        if self.run_live_tool is None:
            return False
        if (
            from_grok
            and resolved[0] in {"recall", "recall_history", "search_memory", "look"}
            and self._provider_tool_in_flight()
        ):
            return False
        key = (call_id, json.dumps(resolved[1], sort_keys=True, default=str))
        now = time.monotonic()
        if (
            self._last_life_action == key
            and now - self._last_life_action_at < _LIFE_ACTION_DEDUP_S
        ):
            return True
        self._last_life_action = key
        self._last_life_action_at = now
        if from_grok and self.grok_voice is not None:
            await self.grok_voice.cancel()
            # Shadow mode answers after the transcript. This turn already has
            # a verified receipt; do not let Mini also invent success.
            turn_id = getattr(self.grok_voice, "_open_turn_id", None)
            if turn_id:
                self.grok_voice._shadow_response_for_turn = turn_id
        name, arguments = resolved
        await self.push_progress(name)
        if name == "code":
            await self.begin_background_code_job(arguments, call_id)
            return True
        raw = await self.run_live_tool(name, arguments, call_id)
        spoken = _spoken_from_tool_json(raw)
        if spoken:
            self._last_honesty = ""
            grok = self.grok_voice
            use_life_record = (
                name in {"recall", "recall_history", "search_memory", "look"}
                and grok is not None
                and hasattr(grok, "speak_life_record")
                and not _is_empty_memory_spoken(spoken)
            )
            logger.warning(
                "realtime_trace event=owner-memory tool=%s spoken_chars=%s life_record=%s",
                name,
                len(spoken),
                use_life_record,
            )
            if use_life_record:
                try:
                    if await grok.speak_life_record(spoken):
                        await self.emit(
                            ReplyEvent(
                                at_ms=self.now(),
                                text=spoken,
                                conversation_id=self.conversation_id,
                                device_id=self.device_id,
                                tts_device_id=self.tts_device_id,
                            )
                        )
                        return True
                except Exception:  # noqa: BLE001 - memory speech must not kill the session
                    logger.exception("realtime speak_life_record failed; falling back")
            await self.speak_honesty(spoken)
        return True

    def _code_job_busy(self) -> bool:
        task = self._code_job_task
        return task is not None and not task.done()

    async def drain_code_job(self) -> None:
        """Wait until a background live coding job has spoken its receipt."""

        task = self._code_job_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def begin_background_code_job(self, arguments: dict, call_id: str) -> str:
        """Start Luna without blocking Realtime pings. Speak the receipt later."""

        pending = {
            "ok": True,
            "name": "code",
            "pending": True,
            "spoken": _CODE_BUSY_SPOKEN if self._code_job_busy() else _CODE_WORKING_SPOKEN,
            "executed": False,
            "verified": False,
            "must_continue": True,
            "completion_claim_allowed": False,
        }
        if self._code_job_busy():
            if call_id.startswith("owner-code") and call_id != _CODE_EXEC_CALL_ID:
                await self._speak_code_receipt(_CODE_BUSY_SPOKEN)
            return compact_live_tool_json(pending)
        self._code_job_announce_progress = (
            call_id.startswith("owner-code") and call_id != _CODE_EXEC_CALL_ID
        )
        self._code_job_task = asyncio.create_task(
            self._complete_owner_code_job(dict(arguments or {}), call_id),
            name="ev-live-code-job",
        )
        pending["spoken"] = _CODE_WORKING_SPOKEN
        return compact_live_tool_json(pending)

    async def _complete_owner_code_job(self, arguments: dict, origin_call_id: str) -> None:
        if self.run_live_tool is None:
            return
        progress: asyncio.Task | None = None
        if self._code_job_announce_progress:
            progress = asyncio.create_task(self._speak_code_progress_if_slow())
        try:
            raw = await self.run_live_tool("code", arguments, _CODE_EXEC_CALL_ID)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - coding must fail honest, not kill live
            logger.exception("live code job failed")
            if not self._closed:
                await self._speak_code_receipt("I couldn't finish that coding job honestly.")
            return
        finally:
            if progress is not None:
                progress.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress
        if self._closed:
            return
        self._remember_code_tool_json(raw)
        spoken = _spoken_from_tool_json(raw)
        if spoken:
            self._last_honesty = ""
            await self._speak_code_receipt(spoken)

    async def _speak_code_progress_if_slow(self) -> None:
        await asyncio.sleep(1.2)
        if self._closed or not self._code_job_busy():
            return
        await self._speak_code_receipt(_CODE_WORKING_SPOKEN)

    def _remember_code_tool_json(self, raw: str) -> None:
        from app.ev.luna_code import remember_code_job

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(body, dict):
            return
        remember_code_job(body, session_key=str(self.session_id or "owner"))
        self._last_code_job = {
            "workspace": str(body.get("workspace") or ""),
            "files": [str(item) for item in (body.get("files_changed") or []) if item],
            "spoken": str(body.get("spoken") or payload.get("spoken") or ""),
            "ok": bool(body.get("ok", payload.get("ok"))),
            "goal": str(body.get("goal") or ""),
            "runs": list(body.get("runs") or []),
        }

    async def _speak_last_code_followup(self, text: str, *, from_grok: bool) -> bool:
        from app.ev.luna_code import last_code_job, looks_like_code_followup, spoken_code_followup

        if not looks_like_code_followup(text):
            return False
        if self._code_job_busy():
            if from_grok and self.grok_voice is not None:
                await self.grok_voice.cancel()
                turn_id = getattr(self.grok_voice, "_open_turn_id", None)
                if turn_id:
                    self.grok_voice._shadow_response_for_turn = turn_id
            self._last_honesty = ""
            await self._speak_code_receipt(
                "I'm still writing that. I'll tell you when it's saved."
            )
            return True
        job = last_code_job(str(self.session_id or "")) or self._last_code_job
        if not job:
            return False
        spoken = spoken_code_followup(text, job)
        if not spoken:
            return False
        if from_grok and self.grok_voice is not None:
            await self.grok_voice.cancel()
            turn_id = getattr(self.grok_voice, "_open_turn_id", None)
            if turn_id:
                self.grok_voice._shadow_response_for_turn = turn_id
        self._last_honesty = ""
        await self._speak_code_receipt(spoken)
        return True

    async def _speak_code_receipt(self, spoken: str) -> None:
        """Speak coding evidence as a short record, not a one-word ack."""

        grok = self.grok_voice
        if grok is not None and hasattr(grok, "speak_life_record"):
            try:
                if await grok.speak_life_record(spoken):
                    await self.emit(
                        ReplyEvent(
                            at_ms=self.now(),
                            text=spoken,
                            conversation_id=self.conversation_id,
                            device_id=self.device_id,
                            tts_device_id=self.tts_device_id,
                        )
                    )
                    return
            except Exception:  # noqa: BLE001 - coding speech must not kill the session
                logger.exception("realtime speak_life_record failed; falling back")
        await self.speak_honesty(spoken)

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
        # ONE VOICE LAW: when the realtime S2S session is attached, control
        # acknowledgments and callouts must come from the SAME spoken voice
        # as normal answers — never from the pipeline synthesizer.
        grok = self.grok_voice
        if grok is not None and hasattr(grok, "speak_ack"):
            try:
                if await grok.speak_ack(text):
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
                    return
            except Exception:  # noqa: BLE001 - ack must not kill the session
                logger.exception("realtime speak_ack failed; falling back to synthesizer")
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
        # P0-adjacent diagnostics: log WHY a sleep-stop fired without ever
        # recording owner words (length + phrase-match only).
        import logging as _log

        from app.voice.lifecycle import is_sleep_phrase

        _log.getLogger("ev.turn_gate").warning(
            "realtime_trace event=sleep_stop_triggered text_len=%s exact_phrase=%s",
            len((text or "").strip()),
            is_sleep_phrase(text),
        )
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
        self._fail_look_futures(LookFrame(request_id="", error="client_disconnected"))
        self._fail_computer_futures({"ok": False, "error": "client_disconnected"})
        drop_state(self.session_id)
        self._reset_playback_boundary()
        unregister_live(self)
        self._cancel_respond()
        task = self._code_job_task
        if task is not None and not task.done():
            task.cancel()
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

    def note_client_gone(self) -> None:
        """Client websocket dropped. Do not close the provider until drain completes."""

        self._client_gone = True

    async def drain_durable_voice_memory(self, *, timeout_s: float | None = None) -> None:
        grok = self.grok_voice
        if grok is None:
            return
        drain = getattr(grok, "drain_voice_memory", None)
        if callable(drain):
            await drain(timeout_s=timeout_s)

    async def flush_relationship_turns(self, *, timeout_s: float = 4.0) -> None:
        from app.memory.turns import flush_live_turns

        await flush_live_turns(timeout_s=timeout_s)
