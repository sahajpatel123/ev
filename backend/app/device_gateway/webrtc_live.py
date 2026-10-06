"""Phone media plane for the server-side Gemini Live bridge.

Direct browser-to-provider WebRTC was retired with the Gemini Live migration
(Gemini Live is a server-side WebSocket API with no browser-direct
equivalent). Phone media rides pcm_ws to the EV server, which bridges it to
Gemini Live. Evie Core stays the authority; tools, look, lease, and sandbox
stay on Device Gateway HTTP. The retired SDP endpoints answer 503; the
``phone_webrtc_session`` builder now returns the Gemini setup message the
server sends on the phone's server-side socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.device_gateway.mobile_actions.tool import MOBILE_ACTION_CONTRACT
from app.device_gateway.mobile_voice import (
    MOBILE_CONVERSATION_CONTRACT,
    PHONE_SPEECH_COPROCESSOR_CONTRACT,
)
from app.device_gateway.sandbox import is_sandbox_device, memory_scope_of
from app.device_gateway.sandbox_tools import sandbox_live_tool_specs
from app.device_gateway.voice import strip_production_memory_from_manifest
from app.models import Device
from app.voice.live.gemini_live import (
    capability_instructions,
    gemini_live_instructions,
    gemini_live_tools,
)
from app.voice.live.layer import (
    compact_live_tool_json,
    live_for_session,
    register_live,
    unregister_live,
)
from app.voice.live.session import LiveSession
from app.voice.live.transport import _live_tool_runner

LOGGER = logging.getLogger("ev.device_gateway.webrtc")

DESIGN_VERSION = "veil-1"
SIGNALING_VERSION = "unified-calls-v1"
SIGNALING_IMPLEMENTATION = "unified_calls"
AUDIO_ARCHITECTURES = ("auto", "webrtc", "webrtc_strict", "pcm_ws", "encoded")
WEBRTC_BACKENDS = frozenset({"webrtc", "webrtc_strict"})


def phone_audio_backend_setting() -> str:
    raw = str(getattr(settings, "phone_audio_backend", None) or "pcm_ws").strip().lower()
    if raw not in AUDIO_ARCHITECTURES:
        return "pcm_ws"
    return raw


def webrtc_possible() -> bool:
    # Direct browser-to-provider realtime WebRTC was a legacy-vendor transport
    # (ephemeral ek_ mint + /v1/realtime/calls SDP proxy). Gemini Live is a
    # server-side WebSocket API with no browser-direct equivalent, so phone
    # media rides pcm_ws through the EV server bridge. The SDP endpoints stay
    # mounted (contract) but answer 503; see resolve_phone_audio_backend.
    return False


def is_strict_webrtc(backend: str | None) -> bool:
    return (backend or "").strip().lower() == "webrtc_strict"


def resolve_phone_audio_backend(requested: str | None = None) -> str:
    """Choose one media backend. Never run two. pcm_ws is the only path."""

    # Server-side pcm_ws is the only phone media path: it carries the
    # pipeline or the Gemini Live server bridge. A stale client preference
    # must not silently bypass the selected brain.
    want = (requested or phone_audio_backend_setting() or "pcm_ws").strip().lower()
    if want not in AUDIO_ARCHITECTURES:
        want = "pcm_ws"
    if want == "pcm_ws":
        return "pcm_ws"
    if want == "encoded":
        return "encoded"
    if want == "auto":
        return "pcm_ws"
    if want in {"webrtc", "webrtc_strict"}:
        raise HTTPException(
            status_code=503,
            detail=(
                "Direct realtime WebRTC was retired with the Gemini Live "
                "migration (Gemini Live is server-side only). Reconnect with "
                "the pcm_ws backend: the EV server bridges phone media to "
                "Gemini Live."
            ),
            headers={"X-Error-Code": "webrtc_retired"},
        )
    return "pcm_ws"


_OWNER_STATE_CHANNEL_CONTRACT = (
    "OWNER STATE CHANNEL: Their name, the date and time, weather, calendar, "
    "contacts, notifications/inbox, visual memory, projects, goals, "
    "commitments, mission-control, timers, reminders, opening Mac apps, "
    "mail, and messages are answered or executed by Evie Core / Home Station. "
    "When the owner asks about those, call evie_state_query with their exact "
    "words, then speak ONLY the canonical result it returns. Never invent a "
    "name, forecast, calendar, contact list, or Health numbers. HealthKit is "
    "never sent to a model. Never claim you lack access to Core data — the "
    "canonical result is authoritative. Answer day, date, and time from the "
    "Local time line in these instructions when the tool is unnecessary, "
    "and still call evie_state_query if you are unsure."
)


def _evie_state_query_spec() -> dict[str, Any]:
    return {
        "name": "evie_state_query",
        "description": (
            "Authoritative Evie Core lookup AND Home Station action for the "
            "owner's name, date, time, weather, calendar, contacts, "
            "notifications, visual memory, projects, goals, timers, "
            "reminders, opening Mac apps, mail, or recent changes. Pass the "
            "owner's exact words. Returns the canonical answer to speak. "
            "Do not invent those facts. Call this when they ask who they are "
            "or what their name is, and when they want a Mac action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_text": {
                    "type": "string",
                    "description": "The owner's request in their own words.",
                },
                "entity_name": {
                    "type": "string",
                    "description": (
                        "Name of the project/goal/commitment under discussion "
                        "when the owner uses a pronoun or omits the name."
                    ),
                },
            },
            "required": ["query_text"],
        },
    }


def _evie_look_spec() -> dict[str, Any]:
    return {
        "name": "evie_look",
        "description": (
            "Use a trusted iPhone camera. Prefer the owner's best camera. "
            "Actions: look_once, observe, capture_photo, record_clip, ocr. "
            "Never claim success without a server receipt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["look_once", "observe", "capture_photo", "record_clip", "ocr"],
                },
                "query_text": {"type": "string"},
            },
            "required": ["action"],
        },
    }


def _evie_home_action_spec() -> dict[str, Any]:
    from .phone_mac import PHONE_HOME_CAPABILITIES

    capabilities = tuple(
        dict.fromkeys(
            (
                "device.echo",
                "device.ping",
                "mac.notify",
                "mac.echo",
                *PHONE_HOME_CAPABILITIES,
            )
        )
    )
    return {
        "name": "evie_home_action",
        "description": (
            "Run one of the server-validated Home Station actions advertised "
            "in the trusted-phone capability manifest. Safari cannot run "
            "iPhone Clock itself. Never expose shell, credentials, payments, "
            "or arbitrary URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "enum": list(capabilities),
                },
                "arguments": {"type": "object"},
                "text": {"type": "string"},
            },
            "required": ["capability"],
        },
    }


def phone_mini_is_coprocessor() -> bool:
    """True when the kernel brain (MiMo) is the mind and Gemini only speaks."""

    from app.cognitive.mode import mouth_topology_selected

    return mouth_topology_selected()


def phone_cognitive_public() -> dict[str, Any]:
    from app.cognitive.mode import (
        cognitive_mode,
        kernel_mode_active,
        realtime_delegate_active,
    )

    kernel = kernel_mode_active()
    if realtime_delegate_active():
        brain = (settings.gemini_live_model or "gemini-3.8-live-extended-thinking").strip()
    else:
        brain = settings.mimo_model
    return {
        "mode": cognitive_mode(),
        "mimo_kernel": kernel,
        "brain": brain,
        "speech": (settings.gemini_live_model or "gemini-3.8-live-extended-thinking").strip(),
        "realtime_thinks": not kernel,
        "delegated_worker": settings.mimo_model if realtime_delegate_active() else None,
        "delegation_enabled": realtime_delegate_active(),
    }


def phone_webrtc_session(*, device: Device | None = None, owner_name: str | None = None) -> dict[str, Any]:
    """Gemini Live setup for the phone (server-side pcm_ws bridge).

    Direct browser WebRTC is retired; this builds the setup message the EV
    server sends on the phone's server-side Gemini Live socket.
    SANDBOX devices: tools stay sandboxed (legacy satellite behavior).
    TRUSTED OWNER devices: OWNER instructions + the single canonical broker
    tool `evie_state_query`, which executes OwnerTurn -> TurnGate -> Core
    server-side. Life-state authority NEVER becomes a model-local tool; the
    model only verbalizes the canonical result (G1 law, PART 7).
    Cognitive OS V2 (mimo_kernel): Gemini is a speech coprocessor — no tools,
    manual VAD (never auto-answers). MiMo decides via turn receipts.
    """
    from app.cognitive.mode import realtime_delegate_active
    from app.device_gateway.sandbox import is_sandbox_device
    from app.ev.personality import SPEECH_STYLE_INSTRUCTIONS

    delegate_mode = realtime_delegate_active()
    coprocessor = phone_mini_is_coprocessor()
    trusted_owner = (
        device is not None
        and not is_sandbox_device(device)
        and device.revoked_at is None
    )
    identity_line = ""
    saved = (owner_name or "").strip()
    if trusted_owner and saved and not coprocessor and not delegate_mode:
        identity_line = (
            f"\nOWNER IDENTITY: The person you are speaking with is {saved}. "
            "When they ask their name, say it. Still call evie_state_query for "
            "weather, calendar, inbox, memory, and anything you are unsure of.\n"
        )
    if trusted_owner and not coprocessor and not delegate_mode:
        from app.search.live import default_place

        place = default_place()
        if place:
            identity_line += (
                f"HOME PLACE: {place}. Weather still requires evie_state_query; "
                "never invent a forecast.\n"
            )

    if delegate_mode and trusted_owner:
        from app.cognitive.delegation import delegate_task_spec
        from app.ev.personality import spoken_identity

        tools = [delegate_task_spec()]
        # The tool declares routing; the owner-frozen speech contract remains.
        instructions = f"You are {spoken_identity(settings.persona_name)}.\n" + SPEECH_STYLE_INSTRUCTIONS
    elif coprocessor:
        tools = []
        instructions = (
            PHONE_SPEECH_COPROCESSOR_CONTRACT + "\n" + SPEECH_STYLE_INSTRUCTIONS
        )
    elif trusted_owner:
        manifest: dict[str, Any] = {"memory_scope": "owner"}
        if device is not None:
            manifest["origin_device_id"] = str(device.id)
            manifest["response_device_id"] = str(device.id)
            manifest["device_role"] = device.role
        # Trusted phones keep evie_state_query as the Core broker and add
        # server-validated phone, perception, and Home Station tools.
        from app.device_gateway.mobile_actions.tool import phone_action_function_spec

        from .phone_mac import PHONE_HOME_CAPABILITY_MANIFEST

        manifest["home_station_capabilities"] = dict(PHONE_HOME_CAPABILITY_MANIFEST)
        tools = [
            _evie_state_query_spec(),
            phone_action_function_spec(device),
            _evie_look_spec(),
            _evie_home_action_spec(),
        ]
        instructions = (
            gemini_live_instructions(capability_manifest=manifest)
            + capability_instructions(manifest)
            + "\n"
            + MOBILE_CONVERSATION_CONTRACT
            + "\n"
            + MOBILE_ACTION_CONTRACT
            + "\n"
            + _OWNER_STATE_CHANNEL_CONTRACT
            + identity_line
            + "\n"
            + SPEECH_STYLE_INSTRUCTIONS
        )
    else:
        specs = sandbox_live_tool_specs(device=device)
        tools = gemini_live_tools(specs)
        manifest = strip_production_memory_from_manifest({"memory_scope": "sandbox"})
        if device is not None:
            manifest["origin_device_id"] = str(device.id)
            manifest["response_device_id"] = str(device.id)
            manifest["device_role"] = device.role
        instructions = (
            gemini_live_instructions(capability_manifest=manifest)
            + capability_instructions(manifest)
            + "\n"
            + MOBILE_CONVERSATION_CONTRACT
            + "\n"
            + MOBILE_ACTION_CONTRACT
            + "\n"
            + SPEECH_STYLE_INSTRUCTIONS
        )
    # Gemini Live setup has no per-session ASR model / language / lexicon /
    # VAD-threshold knobs: transcription is server-side with server defaults,
    # and the EV bridge owns echo gating. The retired OpenAI knobs
    # (phone_asr_model, turn_detection thresholds, noise reduction) are
    # intentionally not translated; phone_asr_* settings now serve only the
    # diagnostic oracle. Model, voice, and thinking level come from the one
    # shared setup builder; only the phone instructions and the phone tool
    # policy (delegate / coprocessor / owner / sandbox) are phone-specific.
    from app.voice.live.gemini_live import _declaration_from_spec, gemini_live_setup

    declarations: list[dict[str, Any]] = []
    for spec in tools:
        declaration = _declaration_from_spec(spec) if isinstance(spec, dict) else None
        if declaration is not None:
            declarations.append(declaration)
    message = gemini_live_setup(manual_vad=coprocessor)
    setup = message.get("setup")
    setup = setup if isinstance(setup, dict) else {}
    setup["systemInstruction"] = {"parts": [{"text": instructions}]}
    if declarations:
        setup["tools"] = [{"functionDeclarations": declarations}]
    else:
        setup.pop("tools", None)
    return {"setup": setup}


def sha256_sdp(sdp: str) -> str:
    return hashlib.sha256((sdp or "").encode("utf-8")).hexdigest()


def summarize_sdp(sdp: str) -> dict[str, Any]:
    """Structure only. Never log the SDP body."""

    text = sdp or ""
    lower = text.lower()
    direction = "unknown"
    if re.search(r"^a=sendrecv\b", text, re.M):
        direction = "sendrecv"
    elif re.search(r"^a=recvonly\b", text, re.M):
        direction = "recvonly"
    elif re.search(r"^a=sendonly\b", text, re.M):
        direction = "sendonly"
    return {
        "audio_mline": bool(re.search(r"^m=audio\b", text, re.M)),
        "application_mline": bool(re.search(r"^m=application\b", text, re.M)),
        "opus": "opus/48000" in lower,
        "ice": "a=ice-ufrag:" in lower and "a=ice-pwd:" in lower,
        "fingerprint": "a=fingerprint:" in lower,
        "direction": direction,
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
        "sha256": sha256_sdp(text),
    }


def prepare_offer_sdp(offer_sdp: str) -> str:
    """Keep the original offer bytes. Do not strip, wrap, or recode line endings."""

    if not isinstance(offer_sdp, str) or not offer_sdp.lstrip().startswith("v="):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid SDP offer.",
                "failed_stage": "M11",
                "error_code": "invalid_offer",
            },
            headers={"X-Error-Code": "invalid_sdp_offer"},
        )
    return offer_sdp


def unified_call_parts(offer_sdp: str, session_cfg: dict[str, Any]) -> dict[str, tuple[None, bytes, str]]:
    """Multipart parts for the unified realtime-calls SDP exchange.

    The endpoint requires form *fields* named sdp and session. A file part
    with a filename (httpx default) is ignored, which produced HTTP 400
    ``field "sdp" is required but not found`` on Primary iPhone Talk.
    """

    return {
        "sdp": (None, offer_sdp.encode("utf-8"), "application/sdp"),
        "session": (
            None,
            json.dumps(session_cfg, ensure_ascii=False).encode("utf-8"),
            "application/json",
        ),
    }


async def mint_ephemeral_secret(*, device: Device) -> dict[str, Any]:
    """Retired: direct browser WebRTC minting ended with the Gemini migration."""

    raise HTTPException(
        status_code=503,
        detail=(
            "Direct realtime WebRTC was retired with the Gemini Live "
            "migration (Gemini Live is server-side only). Reconnect with the "
            "pcm_ws backend: the EV server bridges phone media to Gemini Live."
        ),
        headers={"X-Error-Code": "webrtc_retired"},
    )


async def create_realtime_call(*, device: Device, offer_sdp: str, attempt_id: str | None = None) -> dict[str, str]:
    """Retired: direct browser WebRTC SDP proxy ended with the Gemini migration."""

    raise HTTPException(
        status_code=503,
        detail=(
            "Direct realtime WebRTC was retired with the Gemini Live "
            "migration (Gemini Live is server-side only). Reconnect with the "
            "pcm_ws backend: the EV server bridges phone media to Gemini Live."
        ),
        headers={"X-Error-Code": "webrtc_retired"},
    )


async def proxy_phone_sdp(
    *,
    device: Device,
    offer_sdp: str,
    attempt_id: str | None = None,
) -> dict[str, str]:
    """Retired with direct WebRTC: delegates to the 503 stub. Kept for the route."""

    return await create_realtime_call(device=device, offer_sdp=offer_sdp, attempt_id=attempt_id)


def attach_phone_control_live(
    *,
    device: Device,
    session_id: str,
    actor: str,
    instance_id: str = "",
    gateway_origin: str = "",
) -> LiveSession:
    existing = live_for_session(session_id)
    if existing is not None:
        # G2 SANDBOX-ESCAPE LAW: a cached LiveSession whose authorization
        # state no longer matches the CURRENT device row must never be
        # reused. The pre-promotion session carried a sandbox capability
        # manifest indefinitely — the exact physical failure of 2026-08-25.
        # Rebind (tear down + rebuild) on any trust/scope/revision change.
        current_revision = int(getattr(device, "auth_revision", 1) or 1)
        bound_revision = int(getattr(existing, "auth_revision", current_revision) or current_revision)
        manifest_scope = str(
            (getattr(existing, "_capability_manifest", None) or {}).get("memory_scope")
            or getattr(existing, "memory_scope", "")
            or ""
        )
        device_scope_now = memory_scope_of(device)
        scope_changed = manifest_scope != device_scope_now and not (
            manifest_scope == "owner" and device_scope_now == "owner"
        )
        if bound_revision != current_revision or scope_changed:
            with contextlib.suppress(Exception):
                existing.close()
            unregister_live(existing)
        else:
            return existing
    device_scope_now = memory_scope_of(device)
    if device_scope_now == "sandbox":
        manifest = strip_production_memory_from_manifest(
            {"memory_scope": "sandbox"}
        )
    else:
        manifest = {"memory_scope": "owner"}
    manifest["origin_device_id"] = str(device.id)
    manifest["response_device_id"] = str(device.id)
    live = LiveSession(
        session_id=session_id,
        device_id=str(device.id),
        tts_device_id=str(device.id),
        capability_manifest=manifest,
    )
    live.memory_scope = "sandbox" if is_sandbox_device(device) else memory_scope_of(device)
    live.auth_revision = int(getattr(device, "auth_revision", 1) or 1)
    live.device_role = device.role or "companion"
    live.device_label = (
        "Primary iPhone"
        if (device.role or "") == "primary_companion"
        else "Secondary iPhone"
        if (device.role or "") == "secondary_companion"
        else (device.name or "This iPhone")
    )
    live.instance_id = instance_id
    live.gateway_origin = gateway_origin
    live.surface = "phone"
    live.client_generation = 0
    live.lease_id = ""
    live.run_live_tool = _live_tool_runner(
        actor=actor,
        device_id=device.id,
        live=live,
        sandbox=is_sandbox_device(device),
    )
    register_live(live)
    return live


def close_phone_control_live(session_id: str | None) -> None:
    live = live_for_session(session_id)
    if live is None:
        return
    unregister_live(live)
    live.close()


async def drain_control_events(session_id: str, *, timeout_s: float = 0.45) -> list[dict[str, Any]]:
    live = live_for_session(session_id)
    if live is None:
        return []
    events: list[dict[str, Any]] = []
    try:
        first = await asyncio.wait_for(live.outbound.get(), timeout=max(0.05, timeout_s))
        events.append(first.as_dict())
    except TimeoutError:
        return []
    while True:
        try:
            nxt = live.outbound.get_nowait()
        except asyncio.QueueEmpty:
            break
        events.append(nxt.as_dict())
    return events


async def inject_look_frame(session_id: str, message: dict[str, Any]) -> None:
    live = live_for_session(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="Live session is not open.")
    await live._handle_look_frame(message)


def _remember_phone_offer(
    *,
    spoken: str,
    owner_text: str,
    action: dict[str, Any] | None,
    kind: str,
) -> None:
    """Keep a question Evie asked on the phone lane answerable next turn.

    The iPhone/PWA realtime lane hands tool results straight to the speech
    provider, so an offer spoken here never passes through the cognitive
    kernel's handle_turn and would otherwise be stored nowhere: the owner's
    next "yes" arrives with no referent and is answered as a topic-free
    greeting. The exchange is always appended so the referent survives even
    if a later offer supersedes it; the offer itself is armed only when the
    line really asks the owner something.
    """

    line = (spoken or "").strip()
    if not line:
        return
    try:
        from app.cognitive.intent import (
            clear_pending_offer,
            looks_like_offer,
            pending_offer,
            remember_exchange,
            set_pending_offer,
        )
        from app.cognitive.session_store import current
        from app.ev.continuity import is_affirmative_reply, is_negative_reply

        session = current()
        said = str(owner_text or "").strip()
        # This lane arms the offer itself, so it must spend it too: nothing
        # here goes through the kernel, whose only clearer would otherwise
        # leave an answered question armed for its whole TTL and let the next
        # bare "yes" re-answer it.
        if pending_offer(session) is not None and (
            is_affirmative_reply(said) or is_negative_reply(said)
        ):
            clear_pending_offer(session)
        remember_exchange(
            session,
            owner=said,
            assistant=line,
            kind=kind,
        )
        if looks_like_offer(line):
            set_pending_offer(session, line, action=action)
    except Exception as exc:  # noqa: BLE001 - continuity bookkeeping, not the action
        # An unreadable session must never turn a completed phone action into
        # a failure for the owner.
        with contextlib.suppress(Exception):
            from app.cognitive import telemetry

            telemetry.note(last_error=f"phone_offer_unavailable:{type(exc).__name__}")


def _live_offer_answer_hint(owner_text: str) -> str | None:
    """Hand the speaker the question a short phone-lane yes/no is answering.

    Realtime tool results go straight to the speech provider, so a bare "yes"
    arrives there carrying no referent of its own: without the live offer's own
    words the provider can only greet. Returns None whenever no live offer
    binds the utterance, which keeps the unbound handback byte-for-byte as it
    was.
    """

    raw = (owner_text or "").strip()
    if not raw:
        return None
    try:
        from app.cognitive.intent import continuation_readout, pending_offer
        from app.cognitive.session_store import current
        from app.ev.continuity import is_affirmative_reply, is_negative_reply

        affirmed = is_affirmative_reply(raw)
        if not (affirmed or is_negative_reply(raw)):
            return None
        session = current()
        offer = pending_offer(session)
        if offer is None:
            return None
        referent = str(offer.get("text") or "").strip()
        if not referent:
            return None
        if not affirmed:
            return (
                f"The owner is answering your own question: \"{referent[:600]}\" "
                f"They said \"{raw}\" — they declined. Drop that offer and "
                "acknowledge briefly; do not ask again."
            )
        action = offer.get("action") if isinstance(offer.get("action"), dict) else None
        carry = " Carry out exactly that offer now."
        if action and action.get("tool"):
            carry = (
                " The offer came from "
                f"{action.get('tool')} with {str(action.get('args') or {})[:400]} — "
                "carry it out with that same tool, changing only what the offer "
                "asked for."
            )
        readout = (
            " Your offer was to read the artifact out: ask for the read-aloud "
            "itself (read it out, the whole thing) — never the same short gist "
            "again."
            if continuation_readout(raw, session)
            else ""
        )
        return (
            f"The owner is answering your own question: \"{referent[:600]}\" "
            f"They said \"{raw}\". Do not greet them and do not invent what "
            f"they meant.{carry}{readout}"
        )
    except Exception:  # noqa: BLE001 - an unreadable ledger keeps today's handback
        return None


async def _delegate_phone_task(
    *, live: LiveSession, arguments: dict[str, Any], call_id: str, owner_item_id: str | None,
) -> str:
    """Accept work under current server authority; deliver only to its origin."""
    from app.cognitive.delegation import dispatch_delegate_control, submit_delegate
    from app.cognitive.mode import realtime_delegate_active
    from app.db import SessionLocal
    from app.everywhere.inbox import push_inbox
    from app.models import PhoneTurnReceipt
    from app.voice.live.events import LiveEvent

    from .cognitive_phone import capture_phone_binding

    if not realtime_delegate_active():
        return compact_live_tool_json({"ok": False, "error_code": "DELEGATION_DISABLED"})
    task = str(arguments.get("task") or "").strip()
    operation = str(arguments.get("operation") or "submit").strip().lower()
    if operation not in {"submit", "status", "cancel", "answer"} or (
        operation == "submit" and (not task or len(task) > 8000 or not (call_id or "").strip())
    ):
        return compact_live_tool_json({"ok": False, "error_code": "INVALID_DELEGATION"})
    async with SessionLocal() as db:
        binding = await capture_phone_binding(
            db, device_id=str(live.device_id), live_session_id=live.session_id,
        )
    if binding is None or binding.live is not live:
        return compact_live_tool_json({"ok": False, "error_code": "PHONE_CONTEXT_CHANGED"})

    if operation == "status":
        return compact_live_tool_json(await dispatch_delegate_control(
            operation=operation, live_session_id=live.session_id,
            device_id=str(live.device_id), actor=f"device:{live.device_id}",
            job_id=arguments.get("job_id"),
        ))

    # A delegated plan is model output. Only the originating durable owner
    # utterance may authorize effects or confirmation, never that plan.
    owner_transcript = ""
    for attempt in range(6):
        if not owner_item_id:
            break
        async with SessionLocal() as db:
            row = await db.scalar(select(PhoneTurnReceipt).where(
                PhoneTurnReceipt.device_id == UUID(str(live.device_id)),
                PhoneTurnReceipt.session_id == live.session_id,
                PhoneTurnReceipt.kind == "final_transcript",
                PhoneTurnReceipt.trusted_owner.is_(True),
            ).order_by(PhoneTurnReceipt.created_at.desc()).limit(1))
            if row is not None and row.provider_item_id == owner_item_id:
                owner_transcript = str(row.transcript or "").strip()
                break
        if attempt < 5:
            await asyncio.sleep(0.1)
    if not owner_transcript:
        return compact_live_tool_json({
            "accepted": False, "status": "needs_repeat", "executed": False,
            "spoken": "I couldn't verify that voice request. Please say it again.",
        })

    if operation in {"cancel", "answer"}:
        async with SessionLocal() as db:
            current = await capture_phone_binding(
                db, device_id=str(live.device_id), live_session_id=live.session_id,
            )
        if current is None or current.live is not live or current.identity != binding.identity:
            return compact_live_tool_json({"ok": False, "error_code": "PHONE_CONTEXT_CHANGED"})
        return compact_live_tool_json(await dispatch_delegate_control(
            operation=operation, live_session_id=live.session_id,
            device_id=str(live.device_id), actor=f"device:{live.device_id}",
            job_id=arguments.get("job_id"), owner_transcript=owner_transcript,
        ))

    async def completed(receipt: dict[str, Any]) -> None:
        spoken = str(receipt.get("spoken") or "").strip()
        if not spoken:
            return
        async with SessionLocal() as db:
            device = await db.get(Device, UUID(str(live.device_id)), populate_existing=True)
            # Re-trust/revocation must not disclose an old result to new authority.
            if device is None or device.revoked_at is not None or is_sandbox_device(device):
                return
            if int(device.auth_revision or 1) != int(binding.identity[1]):
                return
            await push_inbox(
                db, device_id=device.id, kind="delegated_task_result",
                title="Evie task update", body=spoken,
                payload={"job_id": receipt.get("job_id"), "status": receipt.get("status"),
                         "session_id": live.session_id},
            )
            await db.commit()
            current = await capture_phone_binding(
                db, device_id=str(device.id), live_session_id=live.session_id,
            )
        if current is None or current.live is not live or current.identity != binding.identity:
            return
        event = LiveEvent("delegated_task_result", live.now())
        event.__dict__.update(
            job_id=receipt.get("job_id"), status=receipt.get("status"),
            spoken=spoken, device_id=str(live.device_id),
            session_id=live.session_id, client_generation=live.client_generation,
        )
        await live.emit(event)

    receipt = await submit_delegate(
        task=task, request_id=call_id, live_session_id=live.session_id,
        device_id=str(live.device_id), actor=f"device:{live.device_id}",
        on_complete=completed, owner_transcript=owner_transcript, owner_turn_id=owner_item_id,
    )
    return compact_live_tool_json(receipt)


async def run_phone_tool(
    *,
    session_id: str,
    name: str,
    arguments: dict[str, Any] | None,
    call_id: str,
    owner_item_id: str | None = None,
) -> str:
    live = live_for_session(session_id)
    if live is None or live.run_live_tool is None:
        raise HTTPException(status_code=409, detail="Live tools are not attached.")
    args = arguments if isinstance(arguments, dict) else {}
    if name == "delegate_task":
        return await _delegate_phone_task(
            live=live, arguments=args, call_id=call_id, owner_item_id=owner_item_id,
        )
    # G2 ONE-EVIE broker: trusted-owner state questions execute through the
    # canonical control plane (OwnerTurn -> TurnGate -> Core). This is NOT a
    # model-local life tool — the model only verbalizes the canonical result.
    if name == "evie_state_query":
        from app.db import SessionLocal
        from app.models import Device as DeviceRow

        if getattr(live, "memory_scope", "owner") == "sandbox":
            return compact_live_tool_json(
                {
                    "ok": False,
                    "error_code": "DEVICE_NOT_TRUSTED",
                    "spoken": (
                        "This phone is paired, but it hasn't been trusted for "
                        "access to your Evie data yet."
                    ),
                }
            )
        async with SessionLocal() as db:
            drow = (
                await db.execute(
                    select(DeviceRow).where(DeviceRow.id == UUID(str(live.device_id)))
                )
            ).scalars().first()
            if drow is None or drow.revoked_at is not None:
                return compact_live_tool_json(
                    {
                        "ok": False,
                        "error_code": "DEVICE_REVOKED",
                        "spoken": "This device is no longer trusted.",
                    }
                )
            from .pipeline import run_trusted_device_turn

            a = args or {}
            result = await run_trusted_device_turn(
                db,
                device=drow,
                text=str(a.get("query_text") or ""),
                idempotency_key=call_id,
                focus_title=a.get("entity_name"),
                ingest_conversation=True,
            )
            await db.commit()
        if result.get("conversational"):
            # PART 6/11: hand back to the provider's own conversation.
            # F1: turn-scoped recalled history, clearly labeled and bound to
            # this tool result only — never session-persistent.
            history = str(result.get("recalled_history") or "").strip()
            # A short yes/no answers Evie's own live offer, and this lane has no
            # other way to tell the provider which question that was. With no
            # live offer this is exactly the previous hint, byte for byte.
            hint = _live_offer_answer_hint(str(args.get("query_text") or "")) or (
                "No canonical state matched; answer the owner conversationally yourself."
            )
            if history:
                hint = (
                    f"{hint}\n{history}\n"
                    "The bracketed history above is read-only background for "
                    "THIS turn only: use it if the owner's question refers to "
                    "the past, otherwise ignore it. Current canonical state "
                    "always outranks recalled history."
                )
            return compact_live_tool_json(
                {
                    "ok": True,
                    "conversational": True,
                    "spoken": "",
                    "hint": hint,
                }
            )
        spoken = result.get("reply") or (
            "Done." if result.get("ok") else "That didn't complete."
        )
        _remember_phone_offer(
            spoken=spoken,
            owner_text=str(args.get("query_text") or ""),
            action={"tool": "evie_state_query", "args": dict(args)},
            kind="phone_state_query",
        )
        return compact_live_tool_json(
            {
                **result,
                "spoken": spoken,
                "executed": bool(result.get("ok")),
                "verified": bool(result.get("ok")),
            }
        )
    if name == "evie_look":
        from app.db import SessionLocal
        from app.everywhere.endpoint_profile import resolve_camera_target
        from app.models import Device as DeviceRow

        action = str((args or {}).get("action") or "look_once")
        query_text = str((args or {}).get("query_text") or action)
        async with SessionLocal() as db:
            drow = (
                await db.execute(select(DeviceRow).where(DeviceRow.id == UUID(str(live.device_id))))
            ).scalars().first()
            if drow is None or drow.revoked_at is not None:
                return compact_live_tool_json(
                    {"ok": False, "error_code": "DEVICE_REVOKED", "spoken": "This device is no longer trusted."}
                )
            routed = await resolve_camera_target(db, origin=drow, text=query_text)
            target = routed["device"]
            from . import camera as cam

            request_id = cam.new_request(
                origin_device_id=str(drow.id),
                target_device_id=str(target.id),
            )
            same = str(target.id) == str(drow.id)
            if not same:
                from app.everywhere.inbox import push_inbox

                await push_inbox(
                    db,
                    device_id=target.id,
                    kind="camera_request",
                    title="Evie needs this camera",
                    body="Look was routed to this iPhone.",
                    payload={
                        "request_id": request_id,
                        "action": action,
                        "reason": routed.get("reason"),
                        "origin_device_id": str(drow.id),
                        "origin_display_name": drow.name or "The other iPhone",
                        "target_display_name": routed.get("display_name") or target.name,
                    },
                )
            await db.commit()
        spoken = (
            "Looking with this iPhone now."
            if same
            else f"I routed look to {routed.get('display_name') or 'the preferred camera'}."
        )
        return compact_live_tool_json(
            {
                "ok": True,
                "needs_camera": same,
                "camera_request_id": request_id,
                "camera_action": action,
                "action": action,
                "camera_target_device_id": str(target.id),
                "remote": not same,
                "origin_display_name": drow.name or "This iPhone",
                "target_display_name": routed.get("display_name") or target.name,
                "reason": routed.get("reason"),
                "permission": routed.get("permission"),
                "freshness": routed.get("freshness"),
                "provenance": routed.get("provenance"),
                "spoken": spoken,
                "executed": False,
                "verified": False,
            }
        )
    if name == "evie_home_action":
        from app.db import SessionLocal
        from app.everywhere.device_actions import ALLOWED_ROUTED_CAPABILITIES, create_routed_action
        from app.models import Device as DeviceRow

        from .phone_mac import maybe_phone_mac_act, utterance_from_phone_action

        cap = str((args or {}).get("capability") or "")
        extra = (args or {}).get("arguments") if isinstance((args or {}).get("arguments"), dict) else {}
        extra = dict(extra or {})
        if (args or {}).get("text") and not extra.get("text"):
            extra["text"] = (args or {}).get("text")
        bridge = getattr(live, "gemini_live", None)
        transcript = str(getattr(bridge, "_last_input_transcript", "") or extra.get("text") or "").strip()
        direct = {
            "computer.open_calculator": "open calculator",
            "computer.close_calculator": "close calculator",
            "calendar_read": "what's on my calendar",
            "list_mail": "check my mail",
            "list_messages": "check my messages",
            "get_weather": "what's the weather",
            "list_timers": "show my timers",
            "cancel_timer": "cancel my timer",
            "list_reminders": "show my reminders",
            "cancel_reminder": "cancel my reminder",
        }.get(cap)
        if cap == "start_timer":
            minutes = extra.get("minutes") or extra.get("duration_minutes")
            direct = f"set a timer for {minutes} minutes" if minutes else (transcript or "")
        elif cap == "set_reminder":
            title = str(extra.get("text") or extra.get("title") or "").strip()
            direct = f"remind me to {title}" if title else transcript
        elif cap in {"open_app", "close_app"}:
            app = str(extra.get("name") or extra.get("app") or "").strip()
            verb = "open" if cap == "open_app" else "close"
            direct = f"{verb} {app}" if app else transcript
        elif cap == "home_act":
            entity = str(extra.get("entity") or "").strip()
            action = str(extra.get("action") or "").strip()
            direct = f"{action} {entity}" if entity and action else transcript
        elif cap == "resolve_contact":
            name = str(extra.get("name") or extra.get("query") or "").strip()
            direct = f"what is {name}'s phone number" if name else transcript
        utterance = (direct or transcript or utterance_from_phone_action({"operation": cap, **extra}, transcript)).strip()
        use_dispatch = bool(utterance) and (
            cap not in ALLOWED_ROUTED_CAPABILITIES
            or cap.startswith("computer.")
            or cap in {
                "start_timer",
                "set_reminder",
                "open_app",
                "close_app",
                "calendar_read",
                "list_mail",
                "list_messages",
                "get_weather",
                "list_timers",
                "cancel_timer",
                "list_reminders",
                "cancel_reminder",
                "resolve_contact",
                "send_message",
                "place_call",
                "home_act",
            }
        )
        async with SessionLocal() as db:
            drow = (
                await db.execute(select(DeviceRow).where(DeviceRow.id == UUID(str(live.device_id))))
            ).scalars().first()
            if drow is None or drow.revoked_at is not None:
                return compact_live_tool_json(
                    {"ok": False, "error_code": "DEVICE_REVOKED", "spoken": "This device is no longer trusted."}
                )
            if use_dispatch:
                acted = await maybe_phone_mac_act(
                    db,
                    device=drow,
                    text=utterance,
                    idempotency_key=str(call_id or "")[:80] or None,
                )
                await db.commit()
                if acted is not None:
                    acted_spoken = str(acted.get("reply") or "")
                    _remember_phone_offer(
                        spoken=acted_spoken,
                        owner_text=transcript or utterance,
                        action={
                            "tool": "evie_home_action",
                            "args": {"capability": cap, "arguments": extra},
                        },
                        kind="phone_home_action",
                    )
                    return compact_live_tool_json(
                        {
                            **acted,
                            "spoken": acted_spoken,
                            "executed": bool(acted.get("executed")),
                            "verified": bool(acted.get("verified")),
                        }
                    )
            broker = await create_routed_action(
                db,
                requesting_device=drow,
                capability=cap,
                arguments=extra or {"text": cap},
                action_id=f"home-{drow.id}-{call_id}"[:80],
                owner_scope="master",
            )
            await db.commit()
        status = broker.get("status") or ""
        executed = status == "SUCCEEDED"
        queued = bool(broker.get("queued") or status in {"QUEUED", "ROUTED"})
        spoken = broker.get("message") or (
            "Queued for Home Station." if queued else ("Done." if executed else "I could not complete that on the Mac.")
        )
        _remember_phone_offer(
            spoken=spoken,
            owner_text=transcript or utterance,
            action={
                "tool": "evie_home_action",
                "args": {"capability": cap, "arguments": extra},
            },
            kind="phone_home_action",
        )
        return compact_live_tool_json(
            {
                **broker,
                "spoken": spoken,
                "executed": executed,
                "verified": executed,
                "queued": queued,
            }
        )
    if name == "phone_action":
        from app.db import SessionLocal
        from app.device_gateway.mobile_actions.tool import dispatch_phone_action
        from app.models import Device as DeviceRow

        async with SessionLocal() as db:
            drow = (
                await db.execute(
                    select(DeviceRow).where(DeviceRow.id == UUID(str(live.device_id)))
                )
            ).scalars().first()
            if drow is None or drow.revoked_at is not None:
                return json.dumps(
                    {
                        "ok": False,
                        "error_code": "DEVICE_REVOKED",
                        "spoken": "This device is no longer trusted.",
                        "executed": False,
                        "verified": False,
                    }
                )
        bridge = getattr(live, "gemini_live", None)
        transcript = str(getattr(bridge, "_last_input_transcript", "") or "").strip()
        payload = await dispatch_phone_action(
            device_id=str(live.device_id),
            role=str(getattr(live, "device_role", None) or "companion"),
            instance_id=str(getattr(live, "instance_id", None) or ""),
            session_id=session_id,
            origin=str(getattr(live, "gateway_origin", None) or "http://127.0.0.1:8000"),
            arguments=args,
            transcript=transcript,
            device_label=str(getattr(live, "device_label", None) or "This iPhone"),
        )
        return json.dumps(payload)
    tool_result = await live.run_live_tool(name, args, call_id)
    if not isinstance(tool_result, str):
        return json.dumps({"ok": False, "error": "tool_result_invalid"})
    return tool_result


def public_audio_status() -> dict[str, Any]:
    try:
        chosen = resolve_phone_audio_backend(None)
    except HTTPException:
        chosen = "unavailable"
    setting = phone_audio_backend_setting()
    return {
        "phone_audio_backend": setting,
        "recommended_backend": chosen if chosen != "unavailable" else setting,
        "webrtc_available": webrtc_possible(),
        "strict_webrtc": is_strict_webrtc(setting) or is_strict_webrtc(chosen),
        "pcm_fallback_allowed": not is_strict_webrtc(setting) and chosen not in {"unavailable", "webrtc_strict"},
        "design_version": DESIGN_VERSION,
        "sdp_proxy": False,
        "signaling": SIGNALING_IMPLEMENTATION,
        "signaling_version": SIGNALING_VERSION,
        "provider_key_in_browser": False,
        "mobile_runtime_version": SIGNALING_VERSION,
        "mobile_voice_status": "OWNER FAILURE / CONNECTION CONVERGENCE",
    }


def assert_session_owns(*, device: Device, session_id: str) -> LiveSession:
    live = live_for_session(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="Live session is not open.")
    if str(live.device_id) != str(device.id):
        raise HTTPException(status_code=403, detail="Session belongs to another device.")
    return live


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid session.") from exc
