"""Phone WebRTC media plane. Evie Core stays the authority; the browser never sees OPENAI_API_KEY.

Signaling (SDP) is proxied through this module. Media (Opus) is a WebRTC track
between the phone and OpenAI. Tools, look, lease, and sandbox stay on Device Gateway HTTP.
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

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.device_gateway.mobile_actions.tool import MOBILE_ACTION_CONTRACT
from app.device_gateway.mobile_voice import (
    MOBILE_ASR_LEXICON,
    MOBILE_CONVERSATION_CONTRACT,
)
from app.device_gateway.sandbox import is_sandbox_device, memory_scope_of
from app.device_gateway.sandbox_tools import sandbox_live_tool_specs
from app.device_gateway.voice import strip_production_memory_from_manifest
from app.models import Device
from app.voice.live.grok_voice import (
    capability_instructions,
    grok_voice_tools,
    openai_realtime_instructions,
)
from app.voice.live.layer import (
    compact_live_tool_json,
    live_for_session,
    register_live,
    unregister_live,
)
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner

LOGGER = logging.getLogger("ev.device_gateway.webrtc")

OPENAI_CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_CALLS = "https://api.openai.com/v1/realtime/calls"

DESIGN_VERSION = "veil-1"
SIGNALING_VERSION = "unified-calls-v1"
SIGNALING_IMPLEMENTATION = "unified_calls"
AUDIO_ARCHITECTURES = ("auto", "webrtc", "webrtc_strict", "pcm_ws", "encoded")
WEBRTC_BACKENDS = frozenset({"webrtc", "webrtc_strict"})
_PROVIDER_SUCCESS = frozenset({200, 201})


def phone_audio_backend_setting() -> str:
    raw = str(getattr(settings, "phone_audio_backend", None) or "webrtc_strict").strip().lower()
    if raw not in AUDIO_ARCHITECTURES:
        return "webrtc_strict"
    return raw


def openai_key_present() -> bool:
    return bool((settings.openai_api_key or "").strip())


def webrtc_possible() -> bool:
    return openai_key_present()


def is_strict_webrtc(backend: str | None) -> bool:
    return (backend or "").strip().lower() == "webrtc_strict"


def resolve_phone_audio_backend(requested: str | None = None) -> str:
    """Choose one media backend. Never run two. Diagnostic default is WebRTC-only."""

    want = (requested or phone_audio_backend_setting() or "webrtc_strict").strip().lower()
    if want not in AUDIO_ARCHITECTURES:
        want = "webrtc_strict"
    if want == "pcm_ws":
        return "pcm_ws"
    if want == "encoded":
        return "encoded"
    if want in {"auto", "webrtc", "webrtc_strict"}:
        if webrtc_possible():
            if want == "webrtc":
                return "webrtc"
            return "webrtc_strict"
        if want in {"webrtc", "webrtc_strict"}:
            raise HTTPException(
                status_code=503,
                detail="WebRTC needs EV_OPENAI_API_KEY on Home Station.",
                headers={"X-Error-Code": "webrtc_unavailable"},
            )
        return "pcm_ws"
    return "pcm_ws"


_OWNER_STATE_CHANNEL_CONTRACT = (
    "OWNER STATE CHANNEL: Projects, goals, commitments, mission-control, "
    "weather, calendar, contacts, notifications/inbox, and visual memory "
    "are answered by Evie Core. When the owner asks about those, call "
    "evie_state_query with their exact words, then speak ONLY the canonical "
    "result it returns. Never invent a forecast, calendar, contact list, or "
    "Health numbers. HealthKit is never sent to a model. Never claim you "
    "lack access to Core data — the canonical result is authoritative."
)


def _evie_state_query_spec() -> dict[str, Any]:
    return {
        "name": "evie_state_query",
        "description": (
            "Authoritative Evie Core lookup for the owner's projects, goals, "
            "commitments, status, recent changes, weather, calendar, contacts, "
            "notifications, or visual memory. Pass the owner's exact words. "
            "Returns the canonical answer to speak. Do not invent those facts."
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
    return {
        "name": "evie_home_action",
        "description": (
            "Route a safe Home Station action: device.echo, mac.notify, "
            "mac.echo, computer.open_calculator, computer.close_calculator. "
            "Never expose shell, credentials, payments, or arbitrary URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "enum": [
                        "device.echo",
                        "device.ping",
                        "mac.notify",
                        "mac.echo",
                        "computer.open_calculator",
                        "computer.close_calculator",
                    ],
                },
                "arguments": {"type": "object"},
            },
            "required": ["capability"],
        },
    }


def phone_webrtc_session(*, device: Device | None = None) -> dict[str, Any]:
    """GA Realtime session for the phone.

    SANDBOX devices: tools stay sandboxed (legacy satellite behavior).
    TRUSTED OWNER devices: OWNER instructions + the single canonical broker
    tool `evie_state_query`, which executes OwnerTurn -> TurnGate -> Core
    server-side. Life-state authority NEVER becomes a model-local tool; the
    model only verbalizes the canonical result (G1 law, PART 7).
    """
    from app.device_gateway.sandbox import is_sandbox_device

    trusted_owner = device is not None and not is_sandbox_device(device)

    if trusted_owner:
        manifest: dict[str, Any] = {"memory_scope": "owner"}
        if device is not None:
            manifest["origin_device_id"] = str(device.id)
            manifest["response_device_id"] = str(device.id)
            manifest["device_role"] = device.role
        # Trusted phones keep evie_state_query as the Core broker and add
        # server-validated phone, perception, and Home Station tools.
        from app.device_gateway.mobile_actions.tool import phone_action_function_spec

        tools = [
            _evie_state_query_spec() | {"type": "function"},
            phone_action_function_spec(device),
            _evie_look_spec() | {"type": "function"},
            _evie_home_action_spec() | {"type": "function"},
        ]
        instructions = (
            openai_realtime_instructions(capability_manifest=manifest)
            + capability_instructions(manifest)
            + "\n"
            + MOBILE_CONVERSATION_CONTRACT
            + "\n"
            + MOBILE_ACTION_CONTRACT
            + "\n"
            + _OWNER_STATE_CHANNEL_CONTRACT
        )
    else:
        specs = sandbox_live_tool_specs(device=device)
        tools = grok_voice_tools(specs)
        manifest = strip_production_memory_from_manifest({"memory_scope": "sandbox"})
        if device is not None:
            manifest["origin_device_id"] = str(device.id)
            manifest["response_device_id"] = str(device.id)
            manifest["device_role"] = device.role
        instructions = (
            openai_realtime_instructions(capability_manifest=manifest)
            + capability_instructions(manifest)
            + "\n"
            + MOBILE_CONVERSATION_CONTRACT
            + "\n"
            + MOBILE_ACTION_CONTRACT
        )
    voice = (settings.openai_realtime_voice or "marin").strip() or "marin"
    model = (settings.openai_realtime_model or "gpt-realtime-2.1-mini").strip()
    asr_model = (getattr(settings, "phone_asr_model", None) or "gpt-4o-transcribe").strip()
    asr_language = (getattr(settings, "phone_asr_language", None) or "en").strip() or "en"
    noise = (getattr(settings, "phone_input_noise_reduction", None) or "near_field").strip()
    inp: dict[str, Any] = {
        "transcription": {
            "model": asr_model,
            "language": asr_language,
            "prompt": MOBILE_ASR_LEXICON,
        },
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 200,
            "silence_duration_ms": 400,
            # Match Mac golden create_response. interrupt_response stays False
            # until barge-in is isolated; overlapping cancel was a duplicate-voice suspect.
            "interrupt_response": False,
            "create_response": True,
        },
    }
    if noise in {"near_field", "far_field"}:
        inp["noise_reduction"] = {"type": noise}
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "include": ["item.input_audio_transcription.logprobs"],
        "audio": {
            "input": inp,
            "output": {"voice": voice},
        },
        "tools": tools,
        "tool_choice": "auto" if tools else "none",
    }


def _extract_ephemeral(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("value")
    if isinstance(value, str) and value.startswith("ek_"):
        return value
    secret = payload.get("client_secret")
    if isinstance(secret, dict):
        nested = secret.get("value")
        if isinstance(nested, str) and nested:
            return nested
    if isinstance(value, str) and value:
        return value
    return ""


def _safety_identifier(device: Device) -> str:
    raw = f"evie-phone|{device.id}|{memory_scope_of(device)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


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
    """Official unified /v1/realtime/calls parts.

    OpenAI requires form *fields* named sdp and session. A file part with a
    filename (httpx default) is ignored, which produced HTTP 400
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


def _call_id_from_location(location: str | None) -> str:
    if not location:
        return ""
    return location.rstrip("/").rsplit("/", 1)[-1]


def _provider_result(response: httpx.Response) -> dict[str, Any]:
    text = response.text or ""
    info: dict[str, Any] = {
        "status": response.status_code,
        "ctype": response.headers.get("content-type"),
        "location": response.headers.get("location"),
        "bytes": len(response.content or b""),
        "request_id": response.headers.get("x-request-id") or response.headers.get("openai-request-id"),
        "starts_sdp": text.lstrip().startswith("v="),
        "call_id": _call_id_from_location(response.headers.get("location")),
    }
    if text.lstrip().startswith("{"):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            info["error_type"] = err.get("type")
            info["error_code"] = err.get("code")
            info["error_message"] = str(err.get("message") or "")[:240]
        elif isinstance(payload, dict) and isinstance(payload.get("sdp"), str):
            info["nested_sdp"] = True
    return info


def _signaling_http_exception(*, stage: str, message: str, info: dict[str, Any], status: int = 502) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "message": message,
            "failed_stage": stage,
            "provider_status": info.get("status"),
            "provider_code": info.get("error_code"),
            "provider_message": info.get("error_message"),
            "provider_type": info.get("error_type"),
            "content_type": info.get("ctype"),
            "response_bytes": info.get("bytes"),
            "call_id": (info.get("call_id") or "")[:24],
            "signaling": SIGNALING_IMPLEMENTATION,
            "signaling_version": SIGNALING_VERSION,
        },
        headers={"X-Error-Code": "webrtc_sdp_failed"},
    )


async def mint_ephemeral_secret(*, device: Device) -> dict[str, Any]:
    """Mint a 60s ek_ credential. Permanent key never leaves Home Station."""

    key = (settings.openai_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Live speech isn't connected.")
    session_cfg = phone_webrtc_session(device=device)
    body = {
        "expires_after": {"anchor": "created_at", "seconds": 60},
        "session": session_cfg,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "OpenAI-Safety-Identifier": _safety_identifier(device),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(OPENAI_CLIENT_SECRETS, headers=headers, json=body)
    if response.status_code >= 400:
        info = _provider_result(response)
        LOGGER.warning("client_secrets failed status=%s code=%s", info.get("status"), info.get("error_code"))
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not mint a Realtime client credential.",
                "failed_stage": "M10",
                "provider_status": info.get("status"),
                "provider_code": info.get("error_code"),
                "signaling": "ephemeral_direct",
            },
            headers={"X-Error-Code": "webrtc_secret_failed"},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Could not mint a Realtime client credential.") from exc
    ephemeral = _extract_ephemeral(payload)
    if not ephemeral:
        raise HTTPException(status_code=502, detail="Could not mint a Realtime client credential.")
    expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
    return {
        "value": ephemeral,
        "expires_at": expires_at,
        "expires_in": 60,
        "calls_url": OPENAI_CALLS,
        "signaling": "ephemeral_direct",
        "signaling_version": SIGNALING_VERSION,
        "provider_key_kind": "ephemeral",
    }


async def create_realtime_call(*, device: Device, offer_sdp: str, attempt_id: str | None = None) -> dict[str, str]:
    """Official unified interface: server API key + multipart sdp/session fields."""

    key = (settings.openai_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Live speech isn't connected.")
    offer = prepare_offer_sdp(offer_sdp)
    session_cfg = phone_webrtc_session(device=device)
    offer_meta = summarize_sdp(offer)
    LOGGER.info(
        "realtime/calls start attempt=%s audio=%s app=%s opus=%s ice=%s fp=%s dir=%s bytes=%s",
        (attempt_id or "")[:16],
        offer_meta["audio_mline"],
        offer_meta["application_mline"],
        offer_meta["opus"],
        offer_meta["ice"],
        offer_meta["fingerprint"],
        offer_meta["direction"],
        offer_meta["bytes"],
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "OpenAI-Safety-Identifier": _safety_identifier(device),
        "Accept": "application/sdp",
    }
    files = unified_call_parts(offer, session_cfg)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(OPENAI_CALLS, headers=headers, files=files)
    except httpx.HTTPError as exc:
        LOGGER.warning("realtime/calls transport failed attempt=%s err=%s", (attempt_id or "")[:16], type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Realtime signaling request failed.",
                "failed_stage": "M09",
                "error_name": type(exc).__name__,
                "signaling": SIGNALING_IMPLEMENTATION,
            },
            headers={"X-Error-Code": "webrtc_sdp_failed"},
        ) from exc
    info = _provider_result(response)
    if response.status_code not in _PROVIDER_SUCCESS:
        LOGGER.warning(
            "realtime/calls failed attempt=%s status=%s code=%s ctype=%s bytes=%s",
            (attempt_id or "")[:16],
            info.get("status"),
            info.get("error_code"),
            info.get("ctype"),
            info.get("bytes"),
        )
        raise _signaling_http_exception(
            stage="M10",
            message="Realtime signaling failed.",
            info=info,
        )
    text = response.text or ""
    if not text.lstrip().startswith("v="):
        LOGGER.warning(
            "realtime/calls non-sdp attempt=%s status=%s ctype=%s bytes=%s",
            (attempt_id or "")[:16],
            info.get("status"),
            info.get("ctype"),
            info.get("bytes"),
        )
        raise _signaling_http_exception(
            stage="M11",
            message="Realtime SDP answer was not valid.",
            info=info,
        )
    answer_meta = summarize_sdp(text)
    LOGGER.info(
        "realtime/calls ok attempt=%s status=%s call=%s answer_sha=%s offer_sha=%s",
        (attempt_id or "")[:16],
        info.get("status"),
        (info.get("call_id") or "")[:20],
        answer_meta["sha256"][:16],
        offer_meta["sha256"][:16],
    )
    return {
        "sdp": text,
        "type": "answer",
        "call_id": info.get("call_id") or "",
        "provider_status": str(info.get("status") or ""),
        "provider_content_type": str(info.get("ctype") or ""),
        "offer_sha256": offer_meta["sha256"],
        "answer_sha256": answer_meta["sha256"],
        "signaling": SIGNALING_IMPLEMENTATION,
        "signaling_version": SIGNALING_VERSION,
    }


async def proxy_phone_sdp(
    *,
    device: Device,
    offer_sdp: str,
    attempt_id: str | None = None,
) -> dict[str, str]:
    """Proxy SDP through Home Station. The PWA never receives OPENAI_API_KEY."""

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
    live.run_live_tool = _grok_tool_runner(
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


async def run_phone_tool(
    *,
    session_id: str,
    name: str,
    arguments: dict[str, Any] | None,
    call_id: str,
) -> str:
    live = live_for_session(session_id)
    if live is None or live.run_live_tool is None:
        raise HTTPException(status_code=409, detail="Live tools are not attached.")
    args = arguments if isinstance(arguments, dict) else {}
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
            )
        if result.get("conversational"):
            # PART 6/11: hand back to the provider's own conversation.
            # F1: turn-scoped recalled history, clearly labeled and bound to
            # this tool result only — never session-persistent.
            history = str(result.get("recalled_history") or "").strip()
            hint = "No canonical state matched; answer the owner conversationally yourself."
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
                    payload={"request_id": request_id, "action": action},
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
        from app.everywhere.device_actions import create_routed_action
        from app.models import Device as DeviceRow

        cap = str((args or {}).get("capability") or "")
        extra = (args or {}).get("arguments") if isinstance((args or {}).get("arguments"), dict) else {}
        async with SessionLocal() as db:
            drow = (
                await db.execute(select(DeviceRow).where(DeviceRow.id == UUID(str(live.device_id))))
            ).scalars().first()
            if drow is None or drow.revoked_at is not None:
                return compact_live_tool_json(
                    {"ok": False, "error_code": "DEVICE_REVOKED", "spoken": "This device is no longer trusted."}
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
        from app.device_gateway.mobile_actions.tool import dispatch_phone_action

        grok = getattr(live, "grok_voice", None)
        transcript = str(getattr(grok, "_last_input_transcript", "") or "").strip()
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
        "sdp_proxy": True,
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
