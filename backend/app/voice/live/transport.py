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
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.auth import ActorContext
from app.config import settings
from app.db import SessionLocal
from app.device_gateway.sandbox import is_sandbox_device
from app.device_gateway.voice import (
    make_sandbox_pipeline_responder,
    strip_production_memory_from_manifest,
)
from app.voice.contracts import Transcript
from app.voice.live.behavior import to_speech_style
from app.voice.live.events import ErrorEvent, LiveEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.grok_voice import (
    GrokVoiceBridge,
    approved_live_tool_specs,
    grok_voice_enabled,
    live_realtime_provider,
)
from app.voice.live.layer import (
    build_live_capability_manifest,
    compact_live_tool_json,
    register_live,
    tool_result_is_successful,
    unregister_live,
)
from app.voice.live.session import LiveSession
from app.voice.live.turn_taking import TurnTakingConfig
from app.voice.pipeline import PipelineOutcome, TtsChunk, stream_chat_tts_pipeline
from app.voice.tts import get_synthesizer

logger = logging.getLogger("ev.voice.live.transport")


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
    tts_device_id: str | None = None,
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
                        device_id=str(device_id) if device_id else None,
                        tts_device_id=(
                            str(tts_device_id)
                            if tts_device_id
                            else (str(device_id) if device_id else None)
                        ),
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
    live.transport_ws = websocket
    register_live(live)
    if getattr(live, "grok_voice", None) is not None:
        await live.grok_voice.start()
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
                    await _handle_client_frame(live, message["bytes"])
                elif "text" in message and message["text"] is not None:
                    try:
                        payload = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        await _handle_client_frame(live, payload)
        finally:
            live.note_client_gone()

    async def tick_loop() -> None:
        last_beat = 0.0
        last_mail = 0.0
        last_trust_check = 0.0
        while not live._closed:
            await asyncio.sleep(max(0.02, cadence))
            if live.grok_voice is None:
                await live.tick()
            now = asyncio.get_running_loop().time()
            if now - last_mail >= 1.0:
                last_mail = now
                with contextlib.suppress(Exception):
                    from app.voice.live.layer import drain_live_mail

                    await drain_live_mail(live)
                with contextlib.suppress(Exception):
                    from app.ev.timers import sweep_due_timers

                    await sweep_due_timers()
            # STAGE 16 TRUST LAW: a revoked device must not retain an open
            # live channel. Bounded check (30s) closes the session with a
            # fatal, explicit outcome — the socket never outlives trust.
            if now - last_trust_check >= 30.0:
                last_trust_check = now
                dev_id = getattr(live, "device_id", None)
                if dev_id:
                    with contextlib.suppress(Exception):
                        from sqlalchemy import select

                        from app.models import Device

                        async with SessionLocal() as db:
                            drow = (
                                await db.execute(
                                    select(Device).where(Device.id == UUID(str(dev_id)))
                                )
                            ).scalars().first()
                        if drow is not None and drow.revoked_at is not None:
                            from app.voice.live.events import ErrorEvent

                            await live.emit(
                                ErrorEvent(
                                    at_ms=live.now(),
                                    code="device_revoked",
                                    message="This device was revoked; disconnecting.",
                                    fatal=True,
                                )
                            )
                            return
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
                await asyncio.wait_for(
                    websocket.send_json(event.as_dict()), timeout=2.0
                )
            except (TimeoutError, WebSocketDisconnect, RuntimeError):
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
        sandbox_live = getattr(live, "memory_scope", "owner") == "sandbox"
        if not sandbox_live:
            with contextlib.suppress(Exception):
                await live.drain_durable_voice_memory()
        unregister_live(live)
        if not sandbox_live:
            with contextlib.suppress(Exception):
                from app.voice.live.voice_memory import PERSIST_FLUSH_TIMEOUT_S

                await live.flush_relationship_turns(timeout_s=PERSIST_FLUSH_TIMEOUT_S)
        live.close()
        for task in (recv, tick, send):
            task.cancel()
        await asyncio.gather(recv, tick, send, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _handle_client_frame(live: Any, payload: dict | bytes) -> None:
    """One client frame == one bounded decision tick.

    FAILURE CONTAINMENT LAW: an exception inside an optional feature's
    handler (a malformed control, a listener-presence message, a tool
    envelope) must degrade to a rejected frame — it must NEVER propagate out
    of the ASGI websocket handler, because that terminates the whole voice
    transport (P0 2026-08-22: a stale _handle_control signature killed every
    client socket that sent any control).
    """

    try:
        await live.handle_client(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "realtime_trace event=client_frame.rejected error_type=%s",
            type(payload).__name__,
        )
        with contextlib.suppress(Exception):
            from app.voice.live.events import ErrorEvent

            await live.emit(
                ErrorEvent(
                    at_ms=getattr(live, "now", lambda: 0)(),
                    code="control_rejected",
                    message="That control was rejected; the conversation continues.",
                    fatal=False,
                )
            )


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
        tts_device_id = str(device_id) if device_id else None
        sandbox = False
        if device_id:
            from app.models import Device as DeviceRow

            try:
                drow = await session.get(DeviceRow, UUID(str(device_id)))
            except (ValueError, TypeError):
                drow = None
            sandbox = is_sandbox_device(drow)
        if not sandbox:
            try:
                from app.ev.fleet import resolve_registry_device, tts_playback_device

                prefer = None
                resolved = await resolve_registry_device(
                    session, str(device_id) if device_id else None
                )
                if resolved is not None:
                    prefer = resolved.id
                    tts_device_id = str(resolved.id)
                elif device_id:
                    try:
                        prefer = UUID(str(device_id))
                    except ValueError:
                        prefer = None
                target = await tts_playback_device(session, prefer_device_id=prefer)
                if target is not None:
                    tts_device_id = str(target.id)
            except Exception:  # noqa: BLE001 - routing metadata must not block live
                pass
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

    synthesizer = get_synthesizer()
    transcriber = None
    respond = None
    use_grok = grok_voice_enabled()
    provider = live_realtime_provider() if use_grok else "pipeline"
    if not use_grok:
        transcriber = live_transcriber()
        if sandbox:
            respond = make_sandbox_pipeline_responder(
                device_id=str(device_id) if device_id else None,
                synthesizer=synthesizer,
                tts_device_id=tts_device_id,
            )
        else:
            respond = make_pipeline_responder(
                actor=ctx.actor,
                device_id=device_id,
                conversation_id=conversation_id,
                synthesizer=synthesizer,
                speaker_confidence=speaker_confidence,
                tts_device_id=tts_device_id,
            )

    async def capability_reply(*, include_refused: bool = False):
        from app.ev.protocols import capability_reply as protocol_reply

        async with SessionLocal() as db:
            payload = await protocol_reply(
                db,
                include_refused=include_refused,
                actor=ctx.actor,
                device_id=device_id,
                realtime_provider=provider,
                channel="voice" if use_grok else "action",
            )
            await db.commit()
        if sandbox:
            return strip_production_memory_from_manifest(payload)
        return payload

    async def approved_live_tools():
        """Load the current policy capability projection for Realtime.

        The voice bridge receives an explicit empty list when the projection
        cannot approve any live function. It never falls back to the full
        registry at runtime; each call is still authorized by dispatch.
        """

        if sandbox:
            from app.device_gateway.sandbox_tools import sandbox_live_tool_specs

            return sandbox_live_tool_specs()
        payload = await capability_reply()
        projection = payload.get("live_tool_projection")
        if not isinstance(projection, list):
            return []
        return approved_live_tool_specs({"capabilities": projection})

    capability_manifest = None
    approved_tool_specs: list[dict] = []
    initial_capabilities: dict = {}
    capability_error: str | None = None
    try:
        initial_capabilities = await capability_reply()
        capability_manifest = build_live_capability_manifest(
            initial_capabilities,
            device_id=str(device_id) if device_id else None,
            tts_device_id=tts_device_id,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed but expose the reason
        capability_error = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception(
            "live capability projection failed session=%s provider=%s",
            session_id,
            provider,
        )
        capability_manifest = build_live_capability_manifest(
            {
                "active_providers": {"realtime": provider, "fallback": "pipeline"},
                "live_tool_projection": [],
                "realtime_tools": [],
                "approved_tools": [],
                "executable_tools": [],
                "capability_error": capability_error,
            },
            device_id=str(device_id) if device_id else None,
            tts_device_id=tts_device_id,
            provider=provider,
        )
    if sandbox:
        from app.device_gateway.sandbox_tools import sandbox_live_tool_specs

        approved_tool_specs = sandbox_live_tool_specs()
        if isinstance(capability_manifest, dict):
            capability_manifest = strip_production_memory_from_manifest(capability_manifest)
            capability_manifest["origin_device_id"] = str(device_id) if device_id else None
            capability_manifest["response_device_id"] = str(device_id) if device_id else None
    elif use_grok:
        try:
            projection = initial_capabilities.get("live_tool_projection")
            approved_tool_specs = (
                approved_live_tool_specs({"capabilities": projection})
                if isinstance(projection, list)
                else []
            )
        except Exception:  # noqa: BLE001 - fail closed for remote function exposure
            approved_tool_specs = []

    if not sandbox:
        try:
            from app.memory.relationship import attach_relationship_memory

            async with SessionLocal() as memory_db:
                capability_manifest = await attach_relationship_memory(
                    memory_db, capability_manifest
                )
        except Exception:  # noqa: BLE001 - live audio still starts without the card
            logger.exception("relationship memory attach failed session=%s", session_id)
    elif isinstance(capability_manifest, dict):
        capability_manifest = strip_production_memory_from_manifest(capability_manifest)

    from app.ev.computer_runtime import drop_state

    drop_state(str(session_id))
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
        device_id=str(device_id) if device_id else None,
        tts_device_id=tts_device_id,
        capability_reply=capability_reply,
        capability_manifest=capability_manifest,
    )
    if capability_error:
        await live_session.emit(
            ErrorEvent(
                at_ms=live_session.now(),
                code="live_capabilities_unavailable",
                message=(
                    "Live function tools are unavailable: " + capability_error
                )[:240],
                fatal=False,
            )
        )
    live_session.on_heartbeat = on_heartbeat
    live_session.memory_scope = "sandbox" if sandbox else "owner"

    def ensure_pipeline() -> None:
        if live_session._respond is not None:
            return
        synth = get_synthesizer()
        respond_cb = (
            make_sandbox_pipeline_responder(
                device_id=str(device_id) if device_id else None,
                synthesizer=synth,
                tts_device_id=tts_device_id,
            )
            if sandbox
            else make_pipeline_responder(
                actor=ctx.actor,
                device_id=device_id,
                conversation_id=conversation_id,
                synthesizer=synth,
                speaker_confidence=speaker_confidence,
                tts_device_id=tts_device_id,
            )
        )
        live_session.attach_intelligence(
            transcriber=live_transcriber(),
            synthesizer=synth,
            respond=respond_cb,
        )

    live_session.ensure_pipeline = ensure_pipeline
    tool_runner = _grok_tool_runner(
        actor=ctx.actor, device_id=device_id, live=live_session, sandbox=sandbox
    )
    live_session.run_live_tool = tool_runner
    if use_grok:
        live_session.grok_voice = GrokVoiceBridge(
            on_event=live_session.emit,
            on_tool=tool_runner,
            now_ms=live_session.now,
            provider=provider,
            capability_manifest=capability_manifest,
            capability_manifest_loader=capability_reply,
            approved_tool_specs=approved_tool_specs,
            tool_specs_loader=approved_live_tools,
            turn_authority_v2=settings.turn_authority_v2_enabled,
            long_form_diagnostic=settings.long_form_diagnostic,
        )
    return live_session


def _grok_tool_runner(*, actor: str, device_id, live: LiveSession, sandbox: bool = False):
    """Execute EV life tools when a live model or the pipeline intent resolver asks.

    Confirmation never blocks this callback. The audio loop stays alive and
    the hold line / HUD card are emitted immediately.
    """

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        from app.ev.computer_runtime import COMPUTER_TOOLS
        from app.ev.tools import dispatch

        if sandbox or getattr(live, "memory_scope", "owner") == "sandbox":
            from app.device_gateway.sandbox_tools import is_sandbox_safe_tool

            if not is_sandbox_safe_tool(name):
                return compact_live_tool_json(
                    {
                        "ok": False,
                        "executed": False,
                        "verified": False,
                        "error": "sandbox_tools_blocked",
                        "spoken": "That capability is not available in sandbox.",
                    }
                )
            if name == "phone_action":
                from app.device_gateway.mobile_actions.tool import dispatch_phone_action

                grok = getattr(live, "grok_voice", None)
                transcript = str(getattr(grok, "_last_input_transcript", "") or "").strip()
                payload = await dispatch_phone_action(
                    device_id=str(device_id),
                    role=str(getattr(live, "device_role", None) or "companion"),
                    instance_id=str(getattr(live, "instance_id", None) or ""),
                    session_id=live.session_id,
                    origin=str(getattr(live, "gateway_origin", None) or "http://127.0.0.1:8000"),
                    arguments=args,
                    transcript=transcript,
                    device_label=str(getattr(live, "device_label", None) or "This iPhone"),
                )
                return compact_live_tool_json(payload)

        # Realtime has already enforced the advertised name/schema boundary.
        # Do not run a second sync policy preview here: it lacks the async
        # provider, device, and delegate-scope context. ``dispatch`` is the
        # canonical authorization/execution boundary and rechecks all of it.
        await live.push_progress(name)
        args = dict(arguments or {})
        grok = getattr(live, "grok_voice", None)
        transcript = str(getattr(grok, "_last_input_transcript", "") or "").strip()
        if name in COMPUTER_TOOLS:
            from app.ev.computer_runtime import (
                allowed_computer_arguments,
                ensure_state,
                note_goal,
            )

            note_goal(ensure_state(live.session_id), transcript or args.get("goal"))
            args = allowed_computer_arguments(name, args)
        elif name == "calculate":
            from app.ev.computer_runtime import state_for

            computer_state = state_for(live.session_id)
            apps = list(
                (computer_state.goal.target_apps if computer_state and computer_state.goal else [])
                or []
            )
            if any(str(app).lower() == "calculator" for app in apps):
                payload = {
                    "ok": False,
                    "name": name,
                    "executed": False,
                    "verified": False,
                    "must_continue": True,
                    "completion_claim_allowed": False,
                    "error": "use_computer_control",
                    "spoken": "Use the Calculator app with computer tools, not the calculate function.",
                    "suggested_fallbacks": ["open_app", "inspect_ui", "ui_action"],
                }
                return compact_live_tool_json(payload)
        async with SessionLocal() as db:
            result = await dispatch(
                db,
                name,
                args,
                actor=actor,
                allow_sensitive=True,
                request_id=call_id,
                device_id=device_id,
                channel="voice",
                live_session_id=live.session_id,
            )
            await db.commit()
        body = result.result if isinstance(result.result, dict) else {}
        if name in COMPUTER_TOOLS:
            successful = (
                bool(result.ok)
                and body.get("ok") is not False
                and not body.get("degraded")
            )
        else:
            successful = bool(result.ok and tool_result_is_successful(body))
        if result.error == "confirmation_required" or body.get("confirmation_required"):
            hold = dict(body)
            if not hold:
                hold = {
                    "ok": False,
                    "error": "confirmation_required",
                    "confirmation_required": True,
                    "hold": True,
                }
            hold["_realtime_call_id"] = call_id
            await live.apply_approval_hold(hold, speak=False)
        elif successful and body:
            await live.push_evidence(name, body)
        elif body:
            await live.push_tool_result(name, body)
        payload = {
            "ok": successful,
            "name": result.name,
            "result": result.result,
            "evidence": body.get("evidence"),
            "spoken": body.get("spoken"),
            "error": result.error or (body.get("error") if not successful else None),
            "executed": body.get("executed"),
            "verified": body.get("verified"),
            "must_continue": body.get("must_continue"),
            "completion_claim_allowed": body.get("completion_claim_allowed"),
            "goal": body.get("goal"),
            "control": body.get("control"),
            "suggested_fallbacks": body.get("suggested_fallbacks"),
            "goal_complete": body.get("goal_complete"),
            "confirmation_required": bool(
                result.error == "confirmation_required" or body.get("confirmation_required")
            ),
        }
        return compact_live_tool_json(payload)

    return on_tool
