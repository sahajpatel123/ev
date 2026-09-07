"""Device Gateway HTTP API — one Evie, many devices."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.config import settings
from app.db import get_session
from app.models import Device
from app.runtime_identity import runtime_git_sha
from app.utils.text import utcnow
from app.voice.lifecycle import VoiceError, VoiceRuntime

from . import PROTOCOL_VERSION, PWA_BUILD
from .audio_diag import format_truth, known_clean_pcm16, wav_from_pcm16
from .auth import create_pairing_token as issue_pairing_row
from .auth import (
    issue_access_token,
    pair_device,
    require_gateway_device,
)
from .camera import get_frame, put_frame
from .handoff import current_state, state_public
from .health import snapshot as health_snapshot
from .lease import claim_lease, heartbeat_lease, lease_belongs, lease_public, release_lease
from .mobile_actions.engine import status_snapshot as mobile_actions_status
from .mobile_actions.routes import gateway_origin
from .mobile_actions.routes import router as mobile_actions_router
from .mobile_voice import fingerprint_report, remember_diag, transcribe_oracle
from .pipeline import handle_user_text
from .presence import note as note_presence
from .protocol import AUDIO_CONTRACT, protocol_compatible
from .release import (
    STAGE_VERSION_COMPATIBILITY,
    current_web_release,
    evaluate_version_compat,
)
from .sandbox import clear_cross_platform_sandbox, is_sandbox_device, memory_scope_of
from .sandbox_tools import provider_effective_snapshot
from .security import origin_allowed
from .telemetry import emit
from .tickets import mint as mint_ws_ticket
from .voice import close_live_for_device
from .webrtc_live import (
    DESIGN_VERSION,
    SIGNALING_IMPLEMENTATION,
    SIGNALING_VERSION,
    WEBRTC_BACKENDS,
    assert_session_owns,
    attach_phone_control_live,
    close_phone_control_live,
    drain_control_events,
    inject_look_frame,
    is_strict_webrtc,
    mint_ephemeral_secret,
    proxy_phone_sdp,
    public_audio_status,
    resolve_phone_audio_backend,
    run_phone_tool,
)

router = APIRouter(prefix="/v1/device-gateway", tags=["device-gateway"])
router.include_router(mobile_actions_router)


class PairingCreate(BaseModel):
    role: str = "primary_companion"
    display_name: str = "Evie phone"


class PairRequest(BaseModel):
    pairing_token: str
    display_name: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["foreground_voice", "camera", "text"])
    client_version: str | None = None
    protocol_version: str = PROTOCOL_VERSION
    platform: str = "web"
    instance_id: str = ""
    role: str | None = None
    memory_scope: str | None = None
    hardware: dict = Field(default_factory=dict)
    permissions: dict = Field(default_factory=dict)
    native_shell: bool = False


class HelloRequest(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    client_build: str | None = None
    instance_id: str = ""
    capabilities: list[str] = Field(default_factory=list)
    network: str | None = None
    foreground: bool = True
    platform: str | None = None
    hardware: dict = Field(default_factory=dict)
    permissions: dict = Field(default_factory=dict)
    native_shell: bool = False


class TextRequest(BaseModel):
    text: str
    request_id: str | None = None
    instance_id: str = ""
    idempotency_key: str | None = None


class ClaimRequest(BaseModel):
    instance_id: str
    method: str = "manual"
    media_backend: str | None = None
    output_sample_rate: int | None = None
    session_id: str | None = None
    lease_id: str | None = None
    client_generation: int | None = None
    battery_percent: float | None = None
    connectivity: str | None = None


class SdpOffer(BaseModel):
    instance_id: str
    session_id: str
    sdp: str
    attempt_id: str | None = None
    lease_id: str | None = None
    client_generation: int | None = None


class LiveSessionRef(BaseModel):
    instance_id: str
    session_id: str
    attempt_id: str | None = None
    lease_id: str | None = None
    client_generation: int | None = None


class LiveToolRequest(BaseModel):
    instance_id: str
    session_id: str
    name: str
    call_id: str
    arguments: dict = Field(default_factory=dict)
    lease_id: str | None = None
    client_generation: int | None = None


class LookFrameRequest(BaseModel):
    session_id: str
    request_id: str
    jpeg_b64: str | None = None
    error: str | None = None
    permission: str | None = None
    last: bool = True
    instance_id: str = ""
    lease_id: str | None = None
    client_generation: int | None = None
    action: str | None = None


class AudioIncident(BaseModel):
    instance_id: str = ""
    backend: str | None = None
    response_id: str | None = None
    underruns: int = 0
    overflows: int = 0
    context_state: str | None = None
    occupancy: int | None = None
    jitter_p95_ms: float | None = None
    packets_lost: float | None = None
    jitter: float | None = None
    concealed_samples: float | None = None
    runtime: str | None = None


class AsrOracleRequest(BaseModel):
    audio_b64: str
    mime: str = "audio/mp4"
    phrase_hint: str | None = None


class MisheardRequest(BaseModel):
    intended: str = ""
    asr_transcript: str = ""
    independent_asr: str = ""
    model_caption: str = ""
    confidence: float | None = None
    runtime: str | None = None
    stats: dict = Field(default_factory=dict)


class CameraResult(BaseModel):
    request_id: str
    jpeg_b64: str
    action: str | None = None


class TurnReceiptRequest(BaseModel):
    instance_id: str = ""
    session_id: str
    lease_id: str | None = None
    client_generation: int | None = None
    request_id: str
    transcript: str = ""
    provider_item_id: str | None = None
    provider_response_id: str | None = None
    kind: str = "final_transcript"
    action_calls: list[dict] = Field(default_factory=list)


class QueueEnqueueRequest(BaseModel):
    idempotency_key: str
    kind: str = "request"
    payload: dict = Field(default_factory=dict)
    ttl_seconds: int = 86400


class QueueReplayRequest(BaseModel):
    idempotency_key: str


class InboxAckRequest(BaseModel):
    item_id: str


class HealthkitSnapshotRequest(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    captured_at: str | None = None
    available: bool | None = None
    reason: str | None = None


class CalendarSnapshotRequest(BaseModel):
    events: list[dict] = Field(default_factory=list)
    captured_at: str | None = None


class ContactsSnapshotRequest(BaseModel):
    contacts: list[dict] = Field(default_factory=list)
    captured_at: str | None = None


class PushRegisterRequest(BaseModel):
    token: str = ""
    bundle_id: str | None = None
    delivery: str = "apns"
    authorization: str | None = None


class NudgePrefsRequest(BaseModel):
    enabled: bool = True
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"


class WebPushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}


class MarkHomeStationRequest(BaseModel):
    device_id: UUID


class RenameRequest(BaseModel):
    device_id: UUID
    display_name: str


class RevokeRequest(BaseModel):
    device_id: UUID
    reason: str = "owner_revoked"


def _stash_profile(device: Device, key: str, value: dict) -> dict:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    profile[key] = value
    device.endpoint_profile = profile
    return profile


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin_allowed(origin, host):
        raise HTTPException(status_code=403, detail="Origin not allowed", headers={"X-Error-Code": "origin_denied"})


def _device_public(device: Device) -> dict:
    # PART 20 consistency: same explicit auth-state categories everywhere.
    from app.everywhere.devices import public_device

    return public_device(device)


def _device_public_legacy(device: Device) -> dict:
    return {
        "device_id": str(device.id),
        "display_name": device.name,
        "role": device.role or "companion",
        "platform": device.platform,
        "capabilities": device.capabilities or [],
        "memory_scope": memory_scope_of(device),
        "trust_state": "revoked" if device.revoked_at else "paired",
        "client_version": device.client_version,
        "protocol_version": device.protocol_version or PROTOCOL_VERSION,
        "last_seen": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }


@router.get("/health")
async def gateway_health() -> dict:
    snap = health_snapshot()
    snap["production_memory_enabled"] = False if not settings.cross_platform_production_memory else snap["production_memory_enabled"]
    return snap


@router.post("/pairing-tokens")
async def create_pairing_token(
    data: PairingCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    _check_origin(request)
    row, raw = await issue_pairing_row(session, role=data.role, display_name=data.display_name)
    await session.commit()
    return {
        "pairing_token": raw,
        "role": row.role,
        "display_name": row.display_name,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "memory_scope": "sandbox",
    }


@router.post("/pair")
async def pair(
    data: PairRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    if not protocol_compatible(data.protocol_version):
        raise HTTPException(status_code=409, detail="Incompatible protocol_version")
    device, token = await pair_device(
        session,
        pairing_token=data.pairing_token,
        display_name=data.display_name,
        capabilities=data.capabilities,
        client_version=data.client_version or PWA_BUILD,
        protocol_version=data.protocol_version,
        platform=(data.platform or "web")[:32],
    )
    # Client-supplied role/memory_scope never grants owner trust.
    device.memory_scope = "sandbox"
    if data.platform:
        device.platform = data.platform[:32]
    if data.hardware or data.permissions:
        from app.everywhere.endpoint_profile import merge_endpoint_profile

        merge_endpoint_profile(device, hardware=data.hardware, permissions=data.permissions)
    access = issue_access_token(device)
    await session.commit()
    emit("device.paired", device_id=str(device.id), role=device.role)
    return {
        "device": _device_public(device),
        "device_token": token,
        "access_token": access,
        "memory_scope": "sandbox",
        "environment": "SANDBOX",
        "protocol_version": PROTOCOL_VERSION,
        "pwa_build": current_web_release()["web_build"],
        "audio_contract": AUDIO_CONTRACT,
    }


@router.post("/session")
async def refresh_session(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    del session
    from .status import device_status_payload

    return {
        "access_token": issue_access_token(device),
        "device": _device_public(device),
        "memory_scope": memory_scope_of(device),
        "status": device_status_payload(device),
        "auth_revision": int(getattr(device, "auth_revision", 1) or 1),
    }


@router.post("/hello")
async def hello(
    data: HelloRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    release = current_web_release()
    compat = evaluate_version_compat(
        client_build=data.client_build,
        client_protocol=data.protocol_version,
        release=release,
    )
    if not compat["protocol_supported"]:
        # Hard gate ONLY for genuine protocol incompatibility. Build skew is
        # reported as update_recommended below and never blocks auth.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "CLIENT_PROTOCOL_UNSUPPORTED",
                "failed_stage": STAGE_VERSION_COMPATIBILITY,
                "supported_range": [
                    compat and release.get("web_protocol_min"),
                    release.get("web_protocol_max"),
                ],
                "latest_web_build": compat["latest_web_build"],
            },
            headers={
                "X-Error-Code": "protocol_incompatible",
                "X-Evie-Update-Required": "true",
            },
        )
    device.client_version = (data.client_build or device.client_version or "")[:64] or device.client_version
    device.protocol_version = str(data.protocol_version)[:16]
    if data.platform:
        device.platform = data.platform[:32]
    if data.hardware or data.permissions:
        from app.everywhere.endpoint_profile import merge_endpoint_profile

        merge_endpoint_profile(device, hardware=data.hardware, permissions=data.permissions)
    ignored_capabilities: list[str] = []
    if data.capabilities:
        from app.everywhere.capabilities import validate_capabilities

        accepted, ignored = validate_capabilities(data.capabilities)
        # STAGE 19: unknown capability names are IGNORED, never projected.
        if accepted:
            device.capabilities = accepted
        ignored_capabilities = ignored
    note_presence(device.id, instance_id=data.instance_id, state="ready" if data.foreground else "background")
    await session.commit()
    snap = health_snapshot()
    trusted_owner_hello = not is_sandbox_device(device)
    from .status import device_status_payload

    status = device_status_payload(device)
    return {
        "ok": True,
        "device": _device_public(device),
        "status": status,
        "ignored_capabilities": ignored_capabilities,
        "backend_sha": runtime_git_sha(),
        "session_context": {
            "device_id": str(device.id),
            "owner_id": "master" if trusted_owner_hello else f"sandbox:{device.id}",
            "trust_state": (
                "TRUSTED_OWNER_DEVICE"
                if trusted_owner_hello
                else ("REVOKED" if device.revoked_at is not None else "PAIRED_SANDBOX")
            ),
            "scope": "master" if trusted_owner_hello else f"sandbox:{device.id}",
            "auth_revision": int(getattr(device, "auth_revision", 1) or 1),
            "turngate_bound": True,
            "protocol_version": PROTOCOL_VERSION,
            "next_action": status["next_action"],
            "product": "EvieShell+PWA",
        },
        "environment": "SANDBOX" if is_sandbox_device(device) else "OWNER",
        "memory_scope": memory_scope_of(device),
        "home_station": snap.get("home_station"),
        "protocol_version": PROTOCOL_VERSION,
        # Release identity comes from the generated manifest ON DISK, read at
        # request time. A stale backend process can no longer advertise an old
        # build while serving new assets (the 22.21/22.20 outage class).
        "pwa_build": release["web_build"],
        "server_build": release["web_build"],
        "server_release": release["web_build"],
        "latest_web_build": compat["latest_web_build"],
        "web_protocol": release["web_protocol"],
        "web_protocol_min": release["web_protocol_min"],
        "web_protocol_max": release["web_protocol_max"],
        "update_required": compat["update_required"],
        "update_reason": compat["update_reason"],
        "update_recommended": compat["update_recommended"],
        "asset_manifest_hash": release.get("asset_manifest_hash"),
        "design_version": getattr(settings, "pwa_design_version", None) or DESIGN_VERSION,
        "audio_contract": AUDIO_CONTRACT,
        "production_memory_enabled": False,
        "always_ready_voice": False,
        "sandbox_tool_schema_hash": snap.get("sandbox_tool_schema_hash"),
        "tool_schema_generation": snap.get("tool_schema_generation"),
        **public_audio_status(),
        "mobile_actions": mobile_actions_status(
            device_id=str(device.id),
            role=device.role or "companion",
            display_name=device.name or "This iPhone",
        ),
        "states": {
            "tailnet": snap.get("tailscale", {}).get("status"),
            "evie_core": "online",
            "realtime": "idle",
            "home_station": snap.get("home_station"),
        },
    }


@router.get("/status")
async def device_status(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    from .status import device_status_payload

    return {"ok": True, **device_status_payload(device), "device": _device_public(device)}


@router.get("/capabilities")
async def device_capabilities(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """Server-computed answer to "what can THIS iPhone do right now?".

    Pure read: derives the manifest from the same policy code the voice and
    text paths enforce, so what the PWA displays can never drift from what
    turns actually do. No trust, memory, or tool state is mutated here.
    """

    _check_origin(request)
    from .capability_manifest import capability_manifest

    return {"ok": True, **capability_manifest(device)}


@router.post("/heartbeat")
async def heartbeat(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    if data.battery_percent is not None:
        # Cycle 52 — battery awareness: clamp to a sane range, persist so
        # nudges and the capability manifest can respect a dying phone.
        try:
            battery = max(0.0, min(100.0, float(data.battery_percent)))
            device.battery_percent = battery
        except (TypeError, ValueError):
            pass
    note_presence(device.id, instance_id=data.instance_id, state="ready")
    lease = await heartbeat_lease(session, device_id=device.id, instance_id=data.instance_id)
    await session.commit()
    payload: dict[str, Any] = {"ok": True, "lease": lease_public(lease)}
    if lease is not None and not lease_belongs(lease, device_id=device.id, instance_id=data.instance_id):
        payload["conversation_moved"] = True
        payload["response_device_id"] = str(lease.device_id)
        emit("conversation.transferred", device_id=str(device.id), to_device_id=str(lease.device_id))
        from app.everywhere.inbox import push_inbox

        await push_inbox(
            session,
            device_id=device.id,
            kind="conversation_moved",
            title="Conversation moved",
            body="Evie is speaking on another device.",
            payload={"to_device_id": str(lease.device_id)},
        )
        await session.commit()
    return payload


@router.post("/conversation/claim")
async def conversation_claim(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    lease = await claim_lease(session, device_id=device.id, instance_id=data.instance_id, method=data.method)
    note_presence(device.id, instance_id=data.instance_id, state="active")
    await session.commit()
    emit("conversation.claimed", device_id=str(device.id), method=data.method)
    return {"ok": True, "lease": lease_public(lease)}


@router.post("/conversation/release")
async def conversation_release(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    await release_lease(session, device_id=device.id, instance_id=data.instance_id)
    note_presence(device.id, instance_id=data.instance_id, state="ready")
    await session.commit()
    return {"ok": True}


@router.post("/text")
async def user_text(
    data: TextRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    if device.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Device revoked")
    instance = data.instance_id or "default"

    # G2 ONE-EVIE LAW (PART 6/15/16): a TRUSTED endpoint's text turns are
    # canonical owner turns. They enter TurnGate → Evie Core — NEVER the
    # legacy sandbox satellite pipeline. Durable trace events carry device
    # provenance so phone turns are observable like Mac turns.
    if not is_sandbox_device(device):
        from app.device_gateway.pipeline import run_trusted_device_text

        result = await run_trusted_device_text(
            session,
            device=device,
            text=data.text or "",
            idempotency_key=data.request_id or getattr(data, "idempotency_key", None),
        )
        await session.commit()
        return result

    lease = await claim_lease(session, device_id=device.id, instance_id=instance, method="manual")
    note_presence(device.id, instance_id=instance, state="active")
    result = await handle_user_text(
        session,
        device=device,
        text=data.text,
        request_id=data.request_id or data.idempotency_key,
        instance_id=instance,
        origin=gateway_origin(request),
    )
    result["lease"] = lease_public(lease)
    result["handoff"] = state_public(await current_state(session))
    await session.commit()
    return result


@router.post("/live/open")
async def live_open(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Open the existing live voice session without lowering /v1/voice/live/open trust."""

    _check_origin(request)
    lease = await claim_lease(
        session,
        device_id=device.id,
        instance_id=data.instance_id,
        method=data.method,
        client_generation=int(data.client_generation or 0),
    )
    runtime = VoiceRuntime(session, master_key=settings.master_key, actor=f"device:{device.name}")
    try:
        outcome = await runtime.open_live_session(device_id=str(device.id))
    except VoiceError as exc:
        await session.commit()
        raise HTTPException(status_code=exc.status, detail=exc.message, headers={"X-Error-Code": exc.code}) from exc
    if is_sandbox_device(device) and outcome.greeting:
        outcome.greeting = None
    backend = resolve_phone_audio_backend(data.media_backend)
    tools = provider_effective_snapshot()
    trusted_owner = not is_sandbox_device(device)
    payload = {
        "session_id": outcome.session_id,
        "state": outcome.state,
        "live": True,
        # STAGE 13 SESSION CONTEXT CONTRACT: server-owned binding facts.
        "session_context": {
            "device_id": str(device.id),
            "owner_id": "master" if trusted_owner else f"sandbox:{device.id}",
            "trust_state": (
                "TRUSTED_OWNER_DEVICE"
                if trusted_owner
                else ("REVOKED" if device.revoked_at is not None else "PAIRED_SANDBOX")
            ),
            "scope": "master" if trusted_owner else f"sandbox:{device.id}",
            "auth_revision": int(getattr(device, "auth_revision", 1) or 1),
            "turngate_bound": True,
            "protocol_version": PROTOCOL_VERSION,
        },
        "memory_scope": memory_scope_of(device),
        "audio_contract": AUDIO_CONTRACT,
        "media_backend": backend,
        "recommended_backend": backend,
        "strict_webrtc": is_strict_webrtc(backend),
        "pcm_fallback_allowed": not is_strict_webrtc(backend),
        "design_version": getattr(settings, "pwa_design_version", None) or DESIGN_VERSION,
        "response_device_id": str(device.id),
        "sandbox_tool_schema_hash": tools.get("sandbox_tool_schema_hash"),
        "tool_schema_generation": tools.get("tool_schema_generation"),
        "live_cross_platform_tools_ready": tools.get("live_cross_platform_tools_ready"),
        "greeting": "Sandbox pipeline. I'm here." if is_sandbox_device(device) else outcome.greeting,
        "output_sample_rate": data.output_sample_rate,
        "mobile_voice_status": "OWNER FAILURE / CONNECTION CONVERGENCE",
        "lease": lease_public(lease),
        "lease_id": lease.lease_id,
        "client_generation": int(data.client_generation or 0),
        "auth_revision": int(getattr(device, "auth_revision", 1) or 1),
    }
    new_live = None
    if backend in WEBRTC_BACKENDS:
        new_live = attach_phone_control_live(
            device=device,
            session_id=str(outcome.session_id),
            actor=f"device:{device.name}",
            instance_id=data.instance_id,
            gateway_origin=gateway_origin(request),
        )
        # Fence stale sandbox companions but never the just-created authoritative session.
        if is_sandbox_device(device):
            from .live_fence import fence_sandbox_lives

            await fence_sandbox_lives(except_live=new_live)
        from .live_fence import fence_phone_lives

        await fence_phone_lives(except_live=new_live)
        new_live.client_generation = int(data.client_generation or 0)
        new_live.lease_id = lease.lease_id
        payload["sdp_path"] = "/v1/device-gateway/live/webrtc/sdp"
        payload["client_secret_path"] = "/v1/device-gateway/live/webrtc/client-secret"
        payload["control_events_path"] = "/v1/device-gateway/live/events"
        payload["pcm_fallback_ticket"] = not is_strict_webrtc(backend)
        payload["signaling"] = SIGNALING_IMPLEMENTATION
        payload["signaling_version"] = SIGNALING_VERSION
    if not is_strict_webrtc(backend):
        payload["ws_path"] = "/v1/voice/live"
        payload["ws_ticket"] = mint_ws_ticket(
            device_id=device.id,
            session_id=str(outcome.session_id),
            instance_id=data.instance_id,
        )
    lease.session_id = str(outcome.session_id)
    await session.commit()
    return payload


@router.post("/live/webrtc/sdp")
async def live_webrtc_sdp(
    data: SdpOffer,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Proxy SDP. Browser never receives a provider credential."""

    _check_origin(request)
    from .live_authority import assert_live_authority

    await assert_live_authority(
        session,
        device=device,
        session_id=data.session_id,
        instance_id=data.instance_id,
        lease_id=data.lease_id,
        client_generation=data.client_generation,
    )
    answer = await proxy_phone_sdp(
        device=device,
        offer_sdp=data.sdp,
        attempt_id=data.attempt_id,
    )
    emit(
        "phone.webrtc_sdp",
        device_id=str(device.id),
        session_id=data.session_id[:12],
        attempt_id=(data.attempt_id or "")[:16],
        signaling=SIGNALING_IMPLEMENTATION,
    )
    return {
        "sdp": answer["sdp"],
        "type": "answer",
        "media_backend": "webrtc",
        "signaling": answer.get("signaling") or SIGNALING_IMPLEMENTATION,
        "signaling_version": answer.get("signaling_version") or SIGNALING_VERSION,
        "call_id": answer.get("call_id") or "",
        "provider_status": int(answer["provider_status"]) if str(answer.get("provider_status") or "").isdigit() else answer.get("provider_status"),
        "offer_sha256": answer.get("offer_sha256"),
        "answer_sha256": answer.get("answer_sha256"),
        "attempt_id": data.attempt_id,
    }


@router.post("/live/webrtc/client-secret")
async def live_webrtc_client_secret(
    data: LiveSessionRef,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Option B: short-lived ek_ for direct browser WebRTC. Permanent key stays here."""

    _check_origin(request)
    from .live_authority import assert_live_authority

    await assert_live_authority(
        session,
        device=device,
        session_id=data.session_id,
        instance_id=data.instance_id,
        lease_id=data.lease_id,
        client_generation=data.client_generation,
    )
    minted = await mint_ephemeral_secret(device=device)
    emit("phone.webrtc_client_secret", device_id=str(device.id), session_id=data.session_id[:12])
    return minted


@router.get("/live/events")
async def live_control_events(
    request: Request,
    session_id: str,
    instance_id: str = "",
    lease_id: str | None = None,
    client_generation: int | None = None,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .live_authority import assert_live_authority

    await assert_live_authority(
        session,
        device=device,
        session_id=session_id,
        instance_id=instance_id,
        lease_id=lease_id,
        client_generation=client_generation,
    )
    events = await drain_control_events(session_id)
    return {"events": events}


@router.post("/live/tool")
async def live_tool(
    data: LiveToolRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .live_authority import assert_live_authority

    await assert_live_authority(
        session,
        device=device,
        session_id=data.session_id,
        instance_id=data.instance_id,
        lease_id=data.lease_id,
        client_generation=data.client_generation,
    )
    output = await run_phone_tool(
        session_id=data.session_id,
        name=data.name,
        arguments=data.arguments,
        call_id=data.call_id,
    )
    return {"ok": True, "call_id": data.call_id, "output": output}


@router.post("/live/look-frame")
async def live_look_frame(
    data: LookFrameRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .live_authority import assert_live_authority

    await assert_live_authority(
        session,
        device=device,
        session_id=data.session_id,
        instance_id=data.instance_id,
        lease_id=data.lease_id,
        client_generation=data.client_generation,
    )
    await inject_look_frame(
        data.session_id,
        {
            "request_id": data.request_id,
            "jpeg_b64": data.jpeg_b64,
            "error": data.error,
            "permission": data.permission,
            "last": data.last,
        },
    )
    vision = None
    if data.jpeg_b64 and not is_sandbox_device(device) and device.revoked_at is None:
        from .phone_look import ingest_phone_frame

        vision = await ingest_phone_frame(
            session,
            device=device,
            request_id=data.request_id,
            jpeg_b64=data.jpeg_b64,
            action=data.action or "look",
        )
        await session.commit()
    return {
        "ok": True,
        "vision": vision,
        "persisted_to_memory_os": bool(vision and vision.get("persisted_to_memory_os")),
    }


@router.post("/live/turn-receipt")
async def live_turn_receipt(
    data: TurnReceiptRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .live_authority import assert_live_authority
    from .turn_receipts import record_turn_receipt

    await assert_live_authority(
        session,
        device=device,
        session_id=data.session_id,
        instance_id=data.instance_id,
        lease_id=data.lease_id,
        client_generation=data.client_generation,
    )
    receipt = await record_turn_receipt(
        session,
        device=device,
        idempotency_key=data.request_id,
        transcript=data.transcript,
        session_id=data.session_id,
        lease_id=data.lease_id,
        provider_item_id=data.provider_item_id,
        provider_response_id=data.provider_response_id,
        action_calls=data.action_calls,
        kind=data.kind,
    )
    await session.commit()
    return receipt


@router.get("/inbox")
async def device_inbox(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.inbox import list_inbox

    items = await list_inbox(session, device_id=device.id)
    from .status import _notifications_public

    note = _notifications_public(device)
    public = [
        {
            **item,
            "delivery": "in_app_poll",
            "push_delivery": note["push_delivery"],
        }
        for item in items
    ]
    return {
        "ok": True,
        "items": public,
        "inbox_channel": "in_app_poll",
        "push_delivery": note["push_delivery"],
        "push_registered": note["push_registered"],
    }


@router.post("/inbox/ack")
async def device_inbox_ack(
    data: InboxAckRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.inbox import ack_inbox

    item = await ack_inbox(session, device_id=device.id, item_id=data.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    await session.commit()
    return {"ok": True, "item": item}


@router.post("/queue", response_model=None)
async def offline_enqueue(
    data: QueueEnqueueRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict | JSONResponse:
    _check_origin(request)
    from app.everywhere.offline_queue import enqueue

    result = await enqueue(
        session,
        device=device,
        idempotency_key=data.idempotency_key,
        kind=data.kind,
        payload=data.payload,
        ttl_seconds=data.ttl_seconds,
    )
    await session.commit()
    status = int(result.get("status") or 201)
    if status in {201, 409, 422}:
        return JSONResponse(result, status_code=status)
    return result


@router.get("/queue")
async def offline_list(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.offline_queue import list_pending

    return {"ok": True, "items": await list_pending(session, device_id=device.id)}




@router.post("/healthkit/snapshot")
async def healthkit_snapshot(
    data: HealthkitSnapshotRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    available = data.available if data.available is not None else bool(data.snapshot)
    freshness = "reported" if available else "unavailable"
    profile["healthkit"] = {
        "snapshot": data.snapshot if available else {},
        "captured_at": data.captured_at,
        "freshness": freshness,
        "sent_to_model": False,
        "available": bool(available),
        "reason": data.reason or (None if available else "no_entitlement"),
    }
    device.endpoint_profile = profile
    await session.commit()
    return {"ok": True, "freshness": freshness, "sent_to_model": False, "available": bool(available)}


@router.post("/text/stream")
async def user_text_stream(
    data: TextRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Cycle 54 — streaming STATES for the typed path: an SSE body that
    yields the turn's stages (routing → thinking → reply) so the phone
    shows honest progress instead of a silent wait. Token streaming is a
    later, separate change; this endpoint never fakes it."""

    _check_origin(request)
    if device.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Device revoked")

    from collections.abc import AsyncIterator

    from fastapi.responses import StreamingResponse

    async def events() -> AsyncIterator[str]:
        yield "event: state\ndata: {\"stage\": \"routing\"}\n\n"
        try:
            if not is_sandbox_device(device):
                from app.device_gateway.pipeline import run_trusted_device_text

                yield "event: state\ndata: {\"stage\": \"thinking\"}\n\n"
                result = await run_trusted_device_text(
                    session,
                    device=device,
                    text=data.text or "",
                    idempotency_key=data.request_id or getattr(data, "idempotency_key", None),
                )
                await session.commit()
                payload = {
                    "reply": str(result.get("reply") or ""),
                    "route": result.get("route"),
                    "conversational": bool(result.get("conversational")),
                }
            else:
                lease = await claim_lease(
                    session, device_id=device.id, instance_id=data.instance_id or "default", method="manual"
                )
                note_presence(device.id, instance_id=data.instance_id or "default", state="active")
                result = await handle_user_text(
                    session,
                    device=device,
                    text=data.text,
                    request_id=data.request_id or data.idempotency_key,
                    instance_id=data.instance_id or "default",
                    origin=gateway_origin(request),
                )
                await session.commit()
                payload = {"reply": str(result.get("reply") or result.get("text") or "")}
            import json as _json


            # Cycle 57 — streamed TTS on the typed path: the phone's typed
            # answer gets a voice, sentence by sentence, using the SAME
            # synthesizer the Mac pipeline uses. Skipped silently when the
            # synthesizer has no audio for the text (or synth is degraded).
            reply_text = str(payload.get("reply") or "").strip()
            if reply_text and not getattr(device, "revoked_at", None):
                try:
                    from app.ev.interaction import EMOTION_SPEECH, detect_emotion
                    from app.voice.contracts import SpeechStyle
                    from app.voice.speech import pop_speakable
                    from app.voice.tts import get_synthesizer

                    synth = get_synthesizer()
                    # Cycle 59 — same prosody map as the desk: the owner's
                    # affect (EMOTION_SPEECH) drives warmth/urgency/brevity;
                    # caps respected. The route keys (timers, reads) already
                    # speak their own crisp lines.
                    spec = EMOTION_SPEECH.get(
                        detect_emotion(data.text or ""), EMOTION_SPEECH["neutral"]
                    )
                    urgency = min(
                        1.0,
                        max(0.0, float(spec.get("urgency_boost", 0.0))),
                        float(spec.get("urgency_cap", 1.0)),
                    )
                    style = SpeechStyle(
                        warmth=float(spec.get("warmth", 0.72)),
                        brevity=float(spec.get("brevity", 0.45)),
                        urgency=urgency,
                        mode="casual",
                    )
                    buffer = reply_text
                    index = 0
                    import base64 as _b64

                    from app.voice.pipeline import device_playable_audio

                    while True:
                        sentence, buffer = pop_speakable(buffer)
                        if not sentence:
                            break
                        spoken = await synth.synthesize(sentence, style=style)
                        wav = await device_playable_audio(spoken.audio) if getattr(spoken, "audio", None) else b""
                        if wav:
                            yield (
                                "event: tts\ndata: "
                                + _json.dumps(
                                    {
                                        "index": index,
                                        "audio_b64": _b64.b64encode(wav).decode("ascii"),
                                        "content_type": "audio/wav",
                                    }
                                )
                                + "\n\n"
                            )
                            index += 1
                    leftover, _ = pop_speakable(buffer, flush=True)
                    if leftover:
                        spoken = await synth.synthesize(leftover, style=style)
                        wav = await device_playable_audio(spoken.audio) if getattr(spoken, "audio", None) else b""
                        if wav:
                            yield (
                                "event: tts\ndata: "
                                + _json.dumps(
                                    {
                                        "index": index,
                                        "audio_b64": _b64.b64encode(wav).decode("ascii"),
                                        "content_type": "audio/wav",
                                    }
                                )
                                + "\n\n"
                            )
                except Exception:  # noqa: BLE001 - speech is best-effort on the typed path
                    pass
            yield f"event: reply\ndata: {_json.dumps(payload)}\n\n"
        except Exception as exc:  # noqa: BLE001 - SSE must end with an error event
            yield (
                "event: error\ndata: "
                + _json.dumps({"error_code": "TEXT_STREAM_FAILED", "message": str(exc)[:200]})
                + "\n\n"
            )

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/queue/replay")
async def offline_replay(
    data: QueueReplayRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.offline_queue import replay

    result = await replay(session, device=device, idempotency_key=data.idempotency_key)
    # Cycle 53 — exactly-once execution: for queued VOICE intents on a
    # trusted device the SERVER runs the turn under the queue's idempotency
    # key (the turn gate dedupes on it), so an offline timer or reminder
    # fires exactly once. The reply rides back to the client.
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    text = str((item.get("payload") or {}).get("text") or "").strip()
    if (
        result.get("ok")
        and not result.get("executed")
        and item.get("kind") in {"siri_capture", "voice_intent"}
        and text
        and not is_sandbox_device(device)
        and device.revoked_at is None
    ):
        from app.device_gateway.pipeline import run_trusted_device_turn
        from app.everywhere.offline_queue import mark_executed

        try:
            turn = await run_trusted_device_turn(
                session,
                device=device,
                text=text,
                idempotency_key=item.get("idempotency_key") or data.idempotency_key,
                ingest_conversation=True,
            )
            reply_text = str(turn.get("reply") or turn.get("spoken") or "")
            if turn.get("conversational") and not reply_text:
                reply_text = "Okay — I'll pick this up when you're back."
            await mark_executed(
                session,
                device_id=device.id,
                idempotency_key=item.get("idempotency_key") or data.idempotency_key,
                reply=reply_text,
            )
            result = {
                **result,
                "executed": True,
                "reply": reply_text,
                "item": await _reload_queue_item(
                    session, device_id=device.id, idempotency_key=data.idempotency_key
                ),
            }
        except Exception:  # noqa: BLE001 - execution failure leaves it retryable
            pass
    await session.commit()
    status = int(result.get("status") or 200)
    if status in {404, 422}:
        raise HTTPException(status_code=status, detail=result)
    return result


async def _reload_queue_item(
    session: AsyncSession,
    *,
    device_id: UUID,
    idempotency_key: str,
) -> dict:
    from sqlalchemy import select as _select

    from app.everywhere.offline_queue import OfflineQueueItem, public_item

    row = (
        await session.execute(
            _select(OfflineQueueItem).where(
                OfflineQueueItem.device_id == device_id,
                OfflineQueueItem.idempotency_key == (idempotency_key or "").strip()[:128],
            )
        )
    ).scalar_one_or_none()
    return public_item(row) if row is not None else {}


@router.post("/push/register")
async def push_register(
    data: PushRegisterRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    delivery = (data.delivery or "").strip().lower() or "apns"
    token = (data.token or "").strip()
    if delivery == "poll" or (not token and delivery != "apns"):
        _stash_profile(
            device,
            "notifications",
            {
                "delivery": "poll",
                "authorization": (data.authorization or "granted")[:32],
                "registered_at": utcnow().isoformat(),
            },
        )
        await session.commit()
        return {"ok": True, "registered": False, "delivery": "poll"}
    if len(token) < 8:
        raise HTTPException(status_code=422, detail="Invalid push token")
    device.push_token = token[:4096]
    device.push_bundle_id = (data.bundle_id or "com.ev.evie.shell")[:256]
    from app.utils.text import utcnow as _utcnow

    device.push_token_updated_at = _utcnow()
    _stash_profile(
        device,
        "notifications",
        {
            "delivery": "apns",
            "authorization": (data.authorization or "granted")[:32],
            "registered_at": utcnow().isoformat(),
        },
    )
    await session.commit()
    return {"ok": True, "registered": True, "delivery": "apns"}


@router.get("/vapid-public-key")
async def vapid_public_key(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """applicationServerKey for the PWA's pushManager.subscribe() call."""

    _check_origin(request)
    from app.everywhere.web_push import vapid_configured, vapid_public_key

    return {"ok": True, "application_server_key": vapid_public_key(), "configured": vapid_configured()}


@router.post("/push/web-subscription")
async def push_web_subscription(
    data: WebPushSubscriptionRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store the browser push subscription (endpoint + p256dh/auth keys)."""

    _check_origin(request)
    endpoint = (data.endpoint or "").strip()
    keys = data.keys if isinstance(data.keys, dict) else {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=422, detail="Invalid web push subscription")
    from app.everywhere.web_push import store_web_subscription

    stored = store_web_subscription(device, endpoint=endpoint, keys=keys)
    await session.commit()
    return {"ok": True, "registered": True, "registered_at": stored.get("registered_at")}


@router.get("/nudge-prefs")
async def get_nudge_prefs(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """This phone's nudge policy: enabled + quiet hours (Home Station local)."""

    _check_origin(request)
    from app.everywhere.nudge import in_quiet_hours, nudge_prefs

    prefs = nudge_prefs(device)
    return {"ok": True, **prefs, "quiet_now": in_quiet_hours(prefs)}


@router.post("/nudge-prefs")
async def set_nudge_prefs(
    data: NudgePrefsRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.nudge import store_nudge_prefs

    prefs = store_nudge_prefs(
        device,
        enabled=data.enabled,
        quiet_start=data.quiet_start,
        quiet_end=data.quiet_end,
    )
    await session.commit()
    return {"ok": True, **prefs}



@router.get("/quick-actions")
async def quick_actions(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """Capability-gated one-tap actions for the phone. Each action is an
    utterance the PWA sends through the SAME trusted text path a spoken turn
    would take — no new authority, no client-side tool dispatch."""

    _check_origin(request)
    from .capability_manifest import capability_manifest

    manifest = capability_manifest(device)
    trusted = manifest["trust_state"] == "TRUSTED_OWNER_DEVICE"
    actions: list[dict[str, str]] = []
    if trusted:
        actions.extend(
            [
                {"id": "timer", "label": "5 min timer", "hint": "Home Station runs it", "utterance": "set a timer for five minutes"},
                {"id": "weather", "label": "Weather", "hint": "Local forecast", "utterance": "what's the weather"},
                {"id": "calendar", "label": "Calendar", "hint": "Today's events", "utterance": "what's on my calendar today"},
                {"id": "recall", "label": "What did we say", "hint": "Recall recent context", "utterance": "what did we talk about most recently"},
                {"id": "look", "label": "Look", "hint": "Camera + vision", "utterance": "look"},
            ]
        )
    else:
        actions.extend(
            [
                {"id": "chat", "label": "Catch up", "hint": "Chat only in sandbox", "utterance": "what can you do on this phone right now"},
            ]
        )
    return {"ok": True, "actions": actions, "trust_state": manifest["trust_state"]}


@router.post("/calendar/snapshot")
async def calendar_snapshot(
    data: CalendarSnapshotRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    events = []
    for item in (data.events or [])[:20]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:120]
        if not title:
            continue
        events.append({"title": title, "start": str(item.get("start") or "")[:64]})
    _stash_profile(
        device,
        "calendar",
        {
            "events": events,
            "captured_at": data.captured_at,
            "sent_to_model": False,
        },
    )
    await session.commit()
    return {"ok": True, "count": len(events), "sent_to_model": False}


@router.post("/contacts/snapshot")
async def contacts_snapshot(
    data: ContactsSnapshotRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    people = []
    for item in (data.contacts or [])[:20]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:80]
        if name:
            people.append({"name": name})
    _stash_profile(
        device,
        "contacts",
        {
            "contacts": people,
            "captured_at": data.captured_at,
            "sent_to_model": False,
        },
    )
    await session.commit()
    return {"ok": True, "count": len(people), "sent_to_model": False}


@router.get("/sync/bootstrap")
async def phone_sync_bootstrap(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.sync import bootstrap as bootstrap_snapshot

    from .auth import actor_for

    payload = await bootstrap_snapshot(session, actor_for(device))
    await session.commit()
    return {"ok": True, **payload}


@router.get("/sync/changes")
async def phone_sync_changes(
    request: Request,
    cursor: str | None = Query(default=None),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from app.everywhere.sync import changes as sync_changes

    from .auth import actor_for

    result = await sync_changes(session, actor_for(device), cursor=cursor, limit=50)
    if result.get("ok"):
        await session.commit()
    return result


@router.post("/live/close")
async def live_close(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    await release_lease(session, device_id=device.id, instance_id=data.instance_id)
    if data.session_id:
        close_phone_control_live(data.session_id)
    await session.commit()
    return {"ok": True}


@router.get("/mobile-voice/fingerprint")
async def mobile_voice_fingerprint(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    report = fingerprint_report()
    report["device_role"] = device.role
    report["status"] = "OWNER FAILURE / CONVERGENCE ACTIVE"
    return report


@router.post("/mobile-voice/asr-oracle")
async def mobile_voice_asr_oracle(
    data: AsrOracleRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """Independent transcription of a diagnostic utterance. Audio is not stored."""

    _check_origin(request)
    try:
        raw = base64.b64decode(data.audio_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid audio.") from exc
    try:
        result = await transcribe_oracle(audio=raw, mime=data.mime or "audio/mp4")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Audio too large.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Independent transcription unavailable.") from exc
    remember_diag(
        str(device.id),
        {
            "asr": result.get("transcript"),
            "phrase_hint": (data.phrase_hint or "")[:200],
            "independent": True,
        },
    )
    emit("phone.asr_oracle", device_id=str(device.id), tokens=",".join(result.get("critical_tokens") or []))
    return {"ok": True, **result, "label": "INDEPENDENT_ASR"}


@router.post("/mobile-voice/misheard")
async def mobile_voice_misheard(
    data: MisheardRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    emit(
        "phone.misheard",
        device_id=str(device.id),
        intended=(data.intended or "")[:80],
        asr=(data.asr_transcript or "")[:80],
        independent=(data.independent_asr or "")[:80],
        confidence=data.confidence,
        runtime=data.runtime,
    )
    return {"ok": True, "stored_audio": False, "memory_os": False}


@router.get("/audio-diag/known.pcm")
async def audio_diag_pcm(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> Response:
    _check_origin(request)
    pcm = known_clean_pcm16()
    return Response(
        content=pcm,
        media_type="application/octet-stream",
        headers={"X-Evie-Diag": "d1", "Cache-Control": "no-store"},
    )


@router.get("/audio-diag/known.wav")
async def audio_diag_wav(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> Response:
    _check_origin(request)
    wav = wav_from_pcm16(known_clean_pcm16())
    return Response(content=wav, media_type="audio/wav", headers={"X-Evie-Diag": "d4", "Cache-Control": "no-store"})


@router.get("/audio-diag/truth")
async def audio_diag_truth(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    pcm = known_clean_pcm16()
    return {"ok": True, **format_truth(pcm), "backend": public_audio_status()}


@router.get("/audio-diag/d2")
async def audio_diag_d2(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> StreamingResponse:
    """Stream known-clean PCM as 20 ms tts_chunk JSON over Tailscale HTTPS."""

    _check_origin(request)
    pcm = known_clean_pcm16()
    frame = 16000 * 2 // 50  # 20 ms of int16le

    async def gen():
        for index, off in enumerate(range(0, len(pcm), frame)):
            chunk = pcm[off : off + frame]
            if len(chunk) < 2:
                break
            payload = {
                "type": "tts_chunk",
                "index": index,
                "audio_b64": base64.b64encode(chunk).decode("ascii"),
                "sample_rate": 16000,
                "content_type": "audio/pcm",
                "response_id": "d2-known",
            }
            yield json.dumps(payload) + "\n"
            await asyncio.sleep(0.02)

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@router.post("/audio-diag/incident")
async def audio_diag_incident(
    data: AudioIncident,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    emit(
        "phone.audio_incident",
        device_id=str(device.id),
        backend=(data.backend or "")[:24],
        underruns=data.underruns,
        overflows=data.overflows,
        context_state=(data.context_state or "")[:24],
    )
    return {
        "ok": True,
        "captured": True,
        "report": {
            "pwa_build": current_web_release()["web_build"],
            "design_version": DESIGN_VERSION,
            "backend": data.backend,
            "underruns": data.underruns,
            "overflows": data.overflows,
            "context_state": data.context_state,
            "occupancy": data.occupancy,
            "jitter_p95_ms": data.jitter_p95_ms,
            "memory_scope": memory_scope_of(device),
        },
    }


@router.post("/camera/result")
async def camera_result(
    data: CameraResult,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    try:
        meta = put_frame(data.request_id, device_id=str(device.id), jpeg_b64=data.jpeg_b64)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown camera request") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Camera request belongs to another device") from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    emit("camera.completed", device_id=str(device.id), request_id=data.request_id[:32])
    vision = None
    if not is_sandbox_device(device):
        from .phone_look import ingest_phone_frame

        vision = await ingest_phone_frame(
            session,
            device=device,
            request_id=data.request_id,
            jpeg_b64=data.jpeg_b64,
            action=data.action or "look",
        )
        await session.commit()
    return {
        "ok": True,
        "camera": meta,
        "persisted_to_memory_os": bool(vision and vision.get("persisted_to_memory_os")),
        "vision": vision,
        "ocr_text": (vision or {}).get("ocr_text"),
        "provenance": "phone_camera" if vision else None,
    }


@router.get("/camera/{request_id}")
async def camera_get(
    request_id: str,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _check_origin(request)
    row = get_frame(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown camera request")
    allowed = {row.get("origin_device_id"), row.get("target_device_id")}
    if str(device.id) not in allowed:
        raise HTTPException(status_code=403, detail="Not a party to this camera request")
    return {
        "request_id": request_id,
        "has_frame": bool(row.get("jpeg_b64")),
        "target_device_id": row.get("target_device_id"),
        "persisted_to_memory_os": False,
    }


@router.post("/admin/revoke")
async def admin_revoke(
    data: RevokeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    _check_origin(request)
    device = await session.get(Device, data.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.revoked_at = utcnow()
    device.revoked_reason = data.reason[:256]
    device.auth_revision = int(getattr(device, "auth_revision", 1) or 1) + 1
    await session.commit()
    await close_live_for_device(str(device.id), reason="device_revoked")
    emit("device.revoked", device_id=str(device.id), reason=data.reason[:64])
    return {"ok": True, "device": _device_public(device)}


@router.post("/admin/promote-owner")
async def admin_promote_owner(
    data: RevokeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    """Canonical TRUST PROMOTION (G2 PART 4-9): PAIRED_SANDBOX → TRUSTED_OWNER.

    The master key IS the owner approval factor. Promotion flips the device's
    canonical scope to the owner namespace and closes its live sessions so
    the next reconnect binds OWNER tools/instructions — stale sandbox
    sessions must never persist after a trust transition (symmetric with
    revocation; one authorization state model, no bypasses).
    """
    _check_origin(request)
    device = await session.get(Device, data.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Device is revoked; un-revoke first")
    was_sandbox = is_sandbox_device(device)
    device.memory_scope = None  # owner scope
    device.trust_level = "owner"
    device.paired_at = device.paired_at or utcnow()
    # STAGE 8: bump the authorization generation. Any session opened under
    # the previous generation is stale and must rebind (the transport tick
    # loop enforces this within one 30s window).
    device.auth_revision = int(getattr(device, "auth_revision", 1) or 1) + 1

    from app.everywhere.sync import emit_everywhere_event

    await emit_everywhere_event(
        session,
        event_type="device.trust_promoted",
        actor_label="master",
        content={
            "device_id": str(device.id),
            "display_name": device.name,
            "previous_scope": "sandbox" if was_sandbox else "owner",
        },
        privacy_level="normal",
    )
    await session.commit()
    # Stale-session law: the open socket must re-bind to its new authority.
    await close_live_for_device(str(device.id), reason="trust_promoted")
    emit("device.trust_promoted", device_id=str(device.id))
    return {
        "ok": True,
        "device": _device_public(device),
        "scope_resolved": "master",
        "reconnect_required": True,
    }


@router.get("/devices")
async def list_gateway_devices(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    _check_origin(request)
    rows = list((await session.execute(select(Device).order_by(Device.created_at.asc()))).scalars().all())
    return {"devices": [_device_public(d) for d in rows], "health": health_snapshot()}


@router.post("/admin/mark-home-station")
async def admin_mark_home_station(
    data: MarkHomeStationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    """Label an existing owner Mac as Home Station. Never changes memory_scope."""

    _check_origin(request)
    device = await session.get(Device, data.device_id)
    if device is None or device.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Device not found")
    others = list(
        (await session.execute(select(Device).where(Device.role == "home_station", Device.revoked_at.is_(None)))).scalars().all()
    )
    for row in others:
        if row.id != device.id:
            row.role = "companion"
    device.role = "home_station"
    await session.commit()
    return {"ok": True, "device": _device_public(device), "memory_scope": memory_scope_of(device)}


@router.post("/admin/rename")
async def admin_rename(
    data: RenameRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    _check_origin(request)
    device = await session.get(Device, data.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.name = (data.display_name or device.name)[:128]
    await session.commit()
    return {"ok": True, "device": _device_public(device)}


@router.post("/admin/sandbox/clear")
async def admin_clear_sandbox(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _master: str = Depends(require_master),
) -> dict:
    _check_origin(request)
    deleted = await clear_cross_platform_sandbox(session)
    await session.commit()
    return {"ok": True, "deleted": deleted, "memory_os_untouched": True}
