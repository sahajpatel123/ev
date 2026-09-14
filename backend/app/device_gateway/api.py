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
from .lease import (
    _when as _lease_when,
    claim_lease,
    current_lease,
    heartbeat_lease,
    lease_belongs,
    lease_public,
    release_lease,
)
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
    attach_phone_control_live,
    close_phone_control_live,
    drain_control_events,
    inject_look_frame,
    is_strict_webrtc,
    mint_ephemeral_secret,
    phone_cognitive_public,
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
    preserve_lease: bool = False
    media_backend: str | None = None
    output_sample_rate: int | None = None
    session_id: str | None = None
    lease_id: str | None = None
    client_generation: int | None = None
    battery_percent: float | None = None
    connectivity: str | None = None
    takeover: bool = False


class ActionCancelRequest(BaseModel):
    action_id: str
    reason: str | None = None


class PresenceGoalCreate(BaseModel):
    objective: str
    normalized_objective: str | None = None
    success_criteria: dict = Field(default_factory=dict)
    constraints: dict = Field(default_factory=dict)
    deadline_at: str | None = None
    priority: str = "NORMAL"
    interruption_policy: str = "NORMAL"
    autonomy_policy: str = "SAFE_DIGITAL"
    activate: bool = True


class PresenceTransition(BaseModel):
    to: str
    reason: str | None = None
    confidence: str | None = None
    evidence: dict = Field(default_factory=dict)


class PresenceWait(BaseModel):
    wait_state: str
    condition: dict = Field(default_factory=dict)
    reason: str | None = None


class PresenceConditionCreate(BaseModel):
    cond_class: str
    payload: dict = Field(default_factory=dict)
    source: str = "event"
    strategy: str = "event"
    frequency_s: int = 60
    ttl_s: int = 86400


class PresenceNodeUpsert(BaseModel):
    node_id: str
    kind: str
    target: str
    status: str = "PENDING"
    effect: str = ""
    risk: str = "R1"
    depends_on: list[str] = Field(default_factory=list)
    verification: str = ""


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
    jpeg_b64: str = ""
    action: str | None = None
    # Burst capture: the browser cannot record video, so it sends timestamped
    # stills. Each entry is ``{jpeg_b64, captured_at_ms}``. Never called a clip.
    frames: list[dict] = Field(default_factory=list)
    media_kind: str | None = None
    has_clip: bool | None = None
    clip_supported: bool | None = None
    captured_at_ms: int | None = None
    note: str | None = Field(default=None, max_length=512)


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


class CaptureNoteRequest(BaseModel):
    text: str = Field(default="", max_length=4000)
    privacy_level: str = Field(default="normal", max_length=32)
    idempotency_key: str = Field(default="", max_length=128)
    request_id: str = Field(default="", max_length=128)


class CaptureAudioRequest(BaseModel):
    audio_b64: str = Field(default="", max_length=10_000_000)
    content_type: str = Field(default="audio/mp4", max_length=128)
    captured_at: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=128)
    request_id: str = Field(default="", max_length=128)


class RoutinesPutRequest(BaseModel):
    enabled: bool = False
    digest_times: list[str] = Field(default_factory=list)
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone: str = "UTC"


class BatteryReportRequest(BaseModel):
    percent: float = Field(default=0, ge=0, le=100)
    charging: bool = False


class OnboardingPutRequest(BaseModel):
    steps_completed: list[str] = Field(default_factory=list)
    camera_role_set: bool = False


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


@router.get("/phone-capabilities")
async def phone_capabilities(
    device: Device = Depends(require_gateway_device),
) -> dict:
    from .phone_mac import PHONE_HOME_CAPABILITY_MANIFEST

    if is_sandbox_device(device) or device.revoked_at is not None:
        return {
            "ok": True,
            "executor": "sandbox",
            "availability": "owner_trust_required",
            "safe_actions": [],
            "blocked": list(PHONE_HOME_CAPABILITY_MANIFEST.get("blocked") or []),
            "trust_state": "REVOKED" if device.revoked_at else "PAIRED_SANDBOX",
        }
    return {
        "ok": True,
        **dict(PHONE_HOME_CAPABILITY_MANIFEST),
        "trust_state": "TRUSTED_OWNER_DEVICE",
    }


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


from time import time as _time

_PAIR_ATTEMPTS: dict[str, list[float]] = {}
_PAIR_LIMIT = 20
_PAIR_WINDOW_SECONDS = 600


def _pair_rate_limited(request: Request) -> bool:
    """Sliding-window failed-pair limiter per client IP. Best-effort and
    in-memory; returns True when the caller must be throttled."""
    client = (request.client.host if request.client else "unknown") + "|" + str(
        request.headers.get("x-forwarded-for", "")
    )[:64]
    now = _time()
    bucket = _PAIR_ATTEMPTS.setdefault(client, [])
    cutoff = now - _PAIR_WINDOW_SECONDS
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _PAIR_LIMIT:
        return True
    bucket.append(now)
    if len(_PAIR_ATTEMPTS) > 512:
        for key in list(_PAIR_ATTEMPTS.keys()):
            if not any(t > cutoff for t in _PAIR_ATTEMPTS[key]):
                del _PAIR_ATTEMPTS[key]
    return False


@router.post("/pair")
async def pair(
    data: PairRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    if not protocol_compatible(data.protocol_version):
        raise HTTPException(status_code=409, detail="Incompatible protocol_version")
    if _pair_rate_limited(request):
        raise HTTPException(
            status_code=429,
            detail="Too many pairing attempts from this network — wait a few minutes.",
            headers={"X-Error-Code": "pair_rate_limited"},
        )
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
    home_station_capabilities = None
    if trusted_owner_hello:
        from .phone_mac import PHONE_HOME_CAPABILITY_MANIFEST

        home_station_capabilities = dict(PHONE_HOME_CAPABILITY_MANIFEST)
    from .status import device_status_payload

    status = device_status_payload(device)
    companions: list[dict] = []
    if trusted_owner_hello:
        from app.everywhere.devices import presence_state as _presence_state

        others = (
            await session.execute(select(Device).where(Device.revoked_at.is_(None)))
        ).scalars().all()
        for row in others:
            role = (row.role or "").strip().lower()
            if role not in {"primary_companion", "secondary_companion", "companion"}:
                continue
            if row.id == device.id:
                continue
            companions.append(
                {
                    "role": role,
                    "display_name": row.name,
                    "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    "presence_state": _presence_state(row),
                }
            )
    return {
        "ok": True,
        "device": _device_public(device),
        "status": status,
        "ignored_capabilities": ignored_capabilities,
        "home_station_capabilities": home_station_capabilities,
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
        "release_generated_at": release.get("generated_at"),
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
        "companions": companions,
        "cognitive": phone_cognitive_public(),
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


async def _refuse_active_lease(session: AsyncSession, existing: Any) -> dict | None:
    """Cycle 77 — refuse a claim on a lease another device holds ACTIVELY.
    Returns the refusal body, or None when the holder went quiet (stale
    lease falls through to a normal claim)."""

    holder = await session.get(Device, existing.device_id)
    holder_name = ((holder.name or "").split() or ["another device"])[0] if holder else "another device"
    return {
        "ok": False,
        "refused": "lease_active",
        "holder": {"device_id": str(existing.device_id), "name": holder_name},
        "spoken": f"Evie is talking with {holder_name}. Tap again to take over here.",
    }


@router.post("/conversation/claim")
async def conversation_claim(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    # Cycle 77 — two-iPhone arbitration: a SECOND phone does not silently
    # rip the conversation from the first. If another device holds an
    # ACTIVE lease, the first claim is refused with who holds it; only an
    # EXPLICIT takeover (second tap) takes the lease.
    existing = await current_lease(session)
    previous_holder_id = existing.device_id if existing is not None else None
    if previous_holder_id is not None and previous_holder_id != device.id and data.takeover is not True:
        refused = await _refuse_active_lease(session, existing)
        if refused is not None:
            return refused
    lease = await claim_lease(session, device_id=device.id, instance_id=data.instance_id, method=data.method)
    note_presence(device.id, instance_id=data.instance_id, state="active")
    await session.commit()
    emit("conversation.claimed", device_id=str(device.id), method=data.method)
    return {"ok": True, "lease": lease_public(lease), "took_over": previous_holder_id is not None and previous_holder_id != device.id}


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
        from app.cognitive.mode import muse_kernel_active

        if muse_kernel_active():
            from .cognitive_text import run_phone_text

            result = await run_phone_text(
                session, device=device, text=data.text or "", instance_id=instance,
                origin=gateway_origin(request),
                request_id=data.request_id or data.idempotency_key,
            )
            await session.commit()
            return result
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


@router.get("/privacy")
async def privacy_stance(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """Cycle 85 — privacy transparency: ONE honest answer to "what does
    Evie keep from this phone?". Server-composed from the same policy the
    surfaces enforce; nothing is stated that is not true."""

    _check_origin(request)
    from app.everywhere.heading_out import heading_out_consent
    from app.everywhere.web_push import vapid_configured, web_subscription

    profile = dict(getattr(device, "endpoint_profile", None) or {})
    hk = profile.get("healthkit") if isinstance(profile.get("healthkit"), dict) else {}
    kept: list[str] = []
    never: list[str] = []
    if not is_sandbox_device(device):
        kept += [
            "Conversation turns (this phone's history, with provenance)",
            "Memories you explicitly kept ('remember this')",
            "Timers, reminders, and their receipts",
            "Your enrolled people roster (names, not biometrics)",
            "An encrypted voiceprint IF you enrolled (raw recordings never stored)",
        ]
        never += [
            "Health numbers (they never reach any model)",
            "Location history (samples are evaluated and dropped)",
            "Raw voice enrollment clips",
            "Mail/Message bodies (read back as short gists for that turn only)",
        ]
    else:
        kept += ["Nothing personal — sandbox devices keep memory off."]
        never += ["Everything personal: history, keeps, roster, voiceprint, health."]
    return {
        "ok": True,
        "environment": "SANDBOX" if is_sandbox_device(device) else "OWNER",
        "health_snapshot_shared": bool(hk.get("available")),
        "push_subscribed": bool(vapid_configured() and web_subscription(device) is not None),
        "heading_out_consent": heading_out_consent(device).get("consent", False),
        "kept": kept,
        "never_kept": never,
        "controls": [
            "EV Sense turns sensors off",
            "Heading out row revokes location consent",
            "Voice row re-enrolls or re-checks; privacy center deletes voiceprints",
            "Privacy center: correct, forget, restore any memory",
        ],
    }


class WakeRequest(BaseModel):
    body: str | None = Field(default=None, max_length=280)


@router.post("/conversation/wake")
async def conversation_wake(
    data: WakeRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 79 — push-to-wake: a doorbell push that opens the live session
    on arrival. The intent is expressed elsewhere (quick action, Home
    Station); the push itself just opens the door with ?wake=1."""

    _check_origin(request)
    from app.everywhere.web_push import send_web_push

    outcome = await send_web_push(
        device,
        title="Evie",
        body=(data.body or "Tap to start talking.")[:280],
        url="/evie/?wake=1",
        wake=True,
    )
    await session.commit()
    return {"ok": True, "push": outcome}


@router.post("/live/open")
async def live_open(
    data: ClaimRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Open the existing live voice session without lowering /v1/voice/live/open trust."""

    _check_origin(request)
    # Cycle 77 — the talk button arbitrates like conversation/claim.
    existing = await current_lease(session)
    previous_holder_id = existing.device_id if existing is not None else None
    if previous_holder_id is not None and previous_holder_id != device.id and data.takeover is not True:
        refused = await _refuse_active_lease(session, existing)
        if refused is not None:
            return refused
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


@router.post("/inbox/ack-all")
async def device_inbox_ack_all(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark every inbox item on this device as read."""
    _check_origin(request)
    from app.models import DeviceInboxItem

    rows = (
        (
            await session.execute(
                select(DeviceInboxItem).where(
                    DeviceInboxItem.device_id == device.id,
                    DeviceInboxItem.read_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    from app.utils.text import utcnow as _utcnow

    now = _utcnow()
    for row in rows:
        row.read_at = now
    await session.commit()
    return {"ok": True, "acked": len(rows)}


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


@router.delete("/queue/{item_id}")
async def offline_drop(
    item_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Drop a pending queued item owned by this device (owner cleanup)."""
    _check_origin(request)
    from app.models import OfflineQueueItem

    row = await session.get(OfflineQueueItem, item_id)
    if row is None or str(row.device_id) != str(device.id):
        raise HTTPException(status_code=404, detail="Queued item not found")
    if row.state in {"executed", "failed"}:
        raise HTTPException(status_code=409, detail="Queued item already terminal")
    if row.state == "rejected" and row.error_code == "DROPPED_BY_OWNER":
        return {"ok": True, "dropped": True, "item_id": str(row.id), "idempotent": True}
    row.state = "rejected"
    row.error_code = "DROPPED_BY_OWNER"
    row.replayed_at = utcnow()
    await session.commit()
    return {"ok": True, "dropped": True, "item_id": str(row.id)}


@router.post("/actions/cancel")
async def action_cancel(
    data: ActionCancelRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Owner cancel for routed phone actions. Idempotent, never fakes."""
    from sqlalchemy import select

    from app.models import PhoneActionRecord

    _check_origin(request)
    action_id = (data.action_id or "").strip()[:80]
    if not action_id:
        raise HTTPException(status_code=422, detail="action_id required")
    cancelled: list[str] = []
    # Phone action trace (owned by this device only).
    trace = (
        await session.execute(
            select(PhoneActionRecord).where(PhoneActionRecord.action_id == action_id)
        )
    ).scalars().first()
    if trace is not None:
        if str(trace.device_id) != str(device.id):
            raise HTTPException(status_code=404, detail="Action not found")
        if str(trace.state or "").lower() not in {"cancelled", "succeeded", "failed"}:
            trace.state = "cancelled"
            cancelled.append("phone_action_record")
    # Cross-device broker (requesting device owns the cancel).
    from app.everywhere.device_actions import TERMINAL_STATUSES, get_action

    broker_row = await get_action(session, action_id, owner_scope="master")
    broker_state: str | None = None
    if broker_row is not None and str(broker_row.requesting_device_id) == str(device.id):
        broker_state = str(broker_row.status or "")
        if broker_state not in TERMINAL_STATUSES:
            broker_row.status = "CANCELLED"
            cancelled.append("broker")
    if trace is None and broker_row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    await session.commit()
    try:
        from .telemetry import emit as _cancel_emit

        _cancel_emit(
            "mobile.cancel",
            device_id=str(device.id),
            action_id=action_id,
            cancelled=",".join(cancelled) if cancelled else "already_terminal",
        )
    except Exception:
        pass
    return {
        "ok": True,
        "action_id": action_id,
        "cancelled": cancelled,
        "already_terminal": not cancelled,
        "action_result": "BLOCKED",
    }


@router.post("/presence/goals")
async def presence_create_goal(
    data: PresenceGoalCreate,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a durable GoalContract. Orchestration over Core, never a chat object."""
    from datetime import datetime

    from app.presence.contract import public_contract
    from app.presence.service import create_contract

    _check_origin(request)
    if not (data.objective or "").strip():
        raise HTTPException(status_code=422, detail="objective required")
    deadline = None
    if data.deadline_at:
        try:
            deadline = datetime.fromisoformat(data.deadline_at)
        except ValueError:
            raise HTTPException(status_code=422, detail="deadline_at must be ISO-8601")
    row = await create_contract(
        session,
        objective=data.objective,
        origin_device_id=device.id,
        normalized_objective=data.normalized_objective or "",
        success_criteria=data.success_criteria,
        constraints=data.constraints,
        deadline_at=deadline,
        priority=data.priority,
        interruption_policy=data.interruption_policy,
        autonomy_policy=data.autonomy_policy,
        activate=data.activate,
    )
    await session.commit()
    return {"ok": True, "goal": public_contract(row)}


@router.get("/presence/goals")
async def presence_list_goals(
    request: Request,
    states: str | None = None,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.contract import public_contract
    from app.presence.service import list_contracts

    _check_origin(request)
    wanted = [s.strip().upper() for s in (states or "").split(",") if s.strip()] or None
    rows = await list_contracts(session, states=wanted)
    return {"ok": True, "goals": [public_contract(r) for r in rows]}


@router.post("/presence/goals/{goal_id}/transition")
async def presence_transition(
    goal_id: UUID,
    data: PresenceTransition,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.contract import public_contract
    from app.presence.service import get_contract, transition

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        if str(data.to or "").upper() == "CANCELLED":
            from app.presence.runner import cancel_contract

            summary = await cancel_contract(session, row, data.reason or "")
            await session.commit()
            return {"ok": True, "goal": public_contract(row), "cancelled": summary}
        row = await transition(
            session, row, data.to, reason=data.reason or "",
            confidence=data.confidence or "", evidence=data.evidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return {"ok": True, "goal": public_contract(row)}


@router.post("/presence/goals/{goal_id}/wait")
async def presence_wait(
    goal_id: UUID,
    data: PresenceWait,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.contract import public_contract
    from app.presence.service import get_contract, set_wait

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        row = await set_wait(
            session, row, wait_state=data.wait_state,
            condition=data.condition, reason=data.reason or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return {"ok": True, "goal": public_contract(row)}


@router.post("/presence/goals/{goal_id}/conditions")
async def presence_add_condition(
    goal_id: UUID,
    data: PresenceConditionCreate,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.service import add_condition, get_contract

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        cond = await add_condition(
            session, row, cond_class=data.cond_class, payload=data.payload,
            source=data.source, strategy=data.strategy,
            frequency_s=data.frequency_s, ttl_s=data.ttl_s,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await session.commit()
    return {"ok": True, "condition_id": str(cond.id), "state": cond.state}


@router.post("/presence/goals/{goal_id}/resume")
async def presence_resume(
    goal_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.contract import public_contract
    from app.presence.service import get_contract, resume_if_ready

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    resumed = await resume_if_ready(session, row)
    await session.commit()
    return {"ok": True, "resumed": resumed, "goal": public_contract(row)}


@router.post("/presence/goals/{goal_id}/nodes")
async def presence_upsert_node(
    goal_id: UUID,
    data: PresenceNodeUpsert,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.contract import NodeKind, NodeStatus, NodeTarget
    from app.presence.service import get_contract, upsert_node

    _check_origin(request)
    for value, enum in (
        (data.kind, NodeKind), (data.target, NodeTarget), (data.status, NodeStatus),
    ):
        try:
            enum(value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown graph value: {value}")
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    node = await upsert_node(
        session, row, node_id=data.node_id, kind=data.kind, target=data.target,
        status=data.status, effect=data.effect, risk=data.risk,
        depends_on=data.depends_on, verification=data.verification,
    )
    await session.commit()
    return {"ok": True, "node": node}


@router.get("/presence/situation")
async def presence_situation(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.service import situation

    _check_origin(request)
    return {"ok": True, "situation": await situation(session, device_id=device.id)}


@router.post("/presence/goals/{goal_id}/teleport")
async def presence_teleport(
    goal_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.everywhere.inbox import push_inbox
    from app.presence.service import continuation_capsule, get_contract, task_capsule

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    capsule = await task_capsule(session, row)
    continued = await continuation_capsule(session, row)
    try:
        await push_inbox(
            session,
            device_id=device.id,
            kind="handoff_ready",
            title="Task moved",
            body=f"“{row.objective[:120]}” is ready on the other device.",
            payload={"goal_id": str(row.id), "capsule": "task"},
        )
    except Exception:
        pass
    await session.commit()
    return {"ok": True, "task_capsule": capsule, "continuation": continued}


@router.get("/presence/what-changed")
async def presence_what_changed(
    request: Request,
    since_hours: int = 24,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.service import what_changed

    _check_origin(request)
    return {"ok": True, **await what_changed(session, since_hours=since_hours)}


@router.post("/presence/simulate")
async def presence_simulate(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.service import simulate

    _check_origin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    scenario = str((body or {}).get("scenario") or "")
    if not scenario:
        raise HTTPException(status_code=422, detail="scenario required")
    return {"ok": True, "simulation": await simulate(session, scenario=scenario)}


@router.post("/presence/goals/{goal_id}/advance")
async def presence_advance(
    goal_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Advance one contract through its runnable nodes. Bounded, idempotent."""
    from app.presence.service import get_contract

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        from app.presence.runner import advance

        out = await advance(session, goal_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"advance failed: {type(exc).__name__}") from exc
    await session.commit()
    return {"ok": True, "advance": out}


@router.post("/presence/goals/{goal_id}/compile")
async def presence_compile(
    goal_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Compile a contract into a validated graph via Muse Spark. Fail-closed."""
    from app.presence.compiler import SparkUnavailable, compile_graph
    from app.presence.service import get_contract

    _check_origin(request)
    row = await get_contract(session, goal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        out = await compile_graph(
            session, row, context=dict((body or {}).get("context") or {}),
            budget_s=max(5.0, min(float((body or {}).get("budget_s") or 20.0), 120.0)),
        )
    except SparkUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await session.commit()
    return {"ok": True, "graph": out}


@router.get("/presence/mission")
async def presence_mission(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.presence.service import mission_status, work_graph

    _check_origin(request)
    return {"ok": True, "mission": await mission_status(session),
            "work_graph": await work_graph(session)}


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


@router.get("/brief")
async def morning_brief(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 60 — the morning brief, server-computed from the SAME
    deterministic surfaces the phone already reads (clock, calendar
    snapshot, inbox, name) — no model call, nothing new leaves the house.
    The PWA renders it as the Today card; the desk voice summary is a
    later, separate lane."""

    _check_origin(request)
    if device.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Device revoked")
    from datetime import datetime as _dt

    profile = dict(getattr(device, "endpoint_profile", None) or {})
    cal = profile.get("calendar") if isinstance(profile.get("calendar"), dict) else {}
    events = cal.get("events") if isinstance(cal.get("events"), list) else []
    today = _dt.now()
    today_events = []
    for item in events[:20]:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "")
        if start[:10] == today.strftime("%Y-%m-%d"):
            today_events.append(
                {
                    "title": str(item.get("title") or "Event")[:120],
                    "start": start,
                }
            )
    from app.everywhere.inbox import list_inbox

    inbox = await list_inbox(session, device_id=device.id, limit=20)
    unread = [item for item in inbox if item.get("unread")]
    from .sandbox import is_sandbox_device

    return {
        "ok": True,
        "trust_state": "TRUSTED_OWNER_DEVICE" if not is_sandbox_device(device) else "PAIRED_SANDBOX",
        "date": today.strftime("%A, %B %-d"),
        "greeting_name": (device.name or "").split()[0] if (device.name or "").strip() else "",
        "calendar_today": today_events[:8],
        "inbox_unread": len(unread),
        "battery_percent": device.battery_percent,
        "nudges": [
            {"title": item.get("title") or "", "body": (item.get("body") or "")[:140]}
            for item in unread[:3]
        ],
    }


class PersonEnrollRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    relation: str = Field(default="other", max_length=64)
    note: str | None = Field(default=None, max_length=512)


@router.get("/people")
async def people_roster(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The enrolled-people roster (owner graph, no biometrics)."""

    _check_origin(request)
    from app.life.people import list_relationships

    return {"ok": True, "people": await list_relationships(session)}


@router.post("/people/enroll")
async def people_enroll(
    data: PersonEnrollRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enroll a person into the owner's roster. Explicit only; relation
    vocabulary comes from the existing G1 set."""

    _check_origin(request)
    from app.life.people import set_relationship

    result = await set_relationship(
        session,
        actor=f"device:{device.name}",
        person_name=data.name.strip()[:256],
        relation=data.relation,
        note=data.note,
        device_id=str(device.id),
    )
    await session.commit()
    return {"ok": result.get("ok", False), **result}


class HeadingOutRequest(BaseModel):
    consent: bool | None = None
    radius_meters: float | None = Field(default=None, ge=50, le=5000)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


@router.get("/heading-out")
async def heading_out_state(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    """Consent state + whether a home anchor exists (nothing else)."""

    _check_origin(request)
    from app.everywhere.heading_out import heading_out_consent
    from app.search.live import home_coords

    state = heading_out_consent(device)
    home = home_coords()
    return {
        "ok": True,
        **state,
        "home_anchor": home is not None,
    }


@router.post("/heading-out")
async def heading_out_update(
    data: HeadingOutRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Consent toggle and/or one consented position sample."""

    _check_origin(request)
    from app.everywhere.heading_out import evaluate_heading_out, heading_out_consent, set_heading_out_consent

    if data.consent is not None:
        set_heading_out_consent(device, consent=data.consent, radius_meters=data.radius_meters)
        await session.commit()
    if data.lat is None or data.lng is None:
        state = heading_out_consent(device)
        return {"ok": True, **state, "transition": None}
    outcome = evaluate_heading_out(device, lat=data.lat, lng=data.lng)
    await session.commit()
    if outcome.get("transition") == "heading_out":
        from app.everywhere.nudge import send_nudge

        await send_nudge(
            session,
            device,
            kind="heading_out",
            title="Heading out",
            body="You've left home. Ask me for anything you need on the way.",
        )
    await session.commit()
    return {"ok": True, **heading_out_consent(device), **outcome}


@router.get("/sense")
async def ev_sense(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 65 — EV Sense: the phone's CONSENTED sensor surface, stated
    honestly. Everything here is already-reported device state (healthkit
    snapshot availability, battery, storage, camera role, nudge policy);
    nothing is inferred, nothing new is collected, and health numbers stay
    off the model (sent_to_model is always False)."""

    _check_origin(request)
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    hk = profile.get("healthkit") if isinstance(profile.get("healthkit"), dict) else {}
    from app.everywhere.heading_out import heading_out_consent
    from app.everywhere.nudge import in_quiet_hours, nudge_prefs
    from app.life.people import list_relationships
    prefs = nudge_prefs(device)
    from app.models import VoiceEnrollment as _VoiceEnrollment
    from sqlalchemy import select as _select

    current_enrollment = (
        await session.execute(
            _select(_VoiceEnrollment).where(_VoiceEnrollment.is_current.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    return {
        "ok": True,
        "healthkit": {
            "available": bool(hk.get("available")),
            "freshness": str(hk.get("freshness") or "unavailable"),
            "sent_to_model": False,
            "captured_at": hk.get("captured_at"),
        },
        "battery_percent": device.battery_percent,
        "storage_free_bytes": device.storage_free_bytes,
        "voice_enrolled": bool(current_enrollment),
        "camera_capability": "camera" in (device.capabilities or []),
        "push_delivery": str((profile.get("notifications") or {}).get("delivery") or "poll"),
        "nudges": {**prefs, "quiet_now": in_quiet_hours(prefs)},
        "people_count": len(await list_relationships(session)),
        "heading_out": heading_out_consent(device),
        "never_to_model": ["health_numbers", "location_history"],
    }


class PhoneVoiceEnrollRequest(BaseModel):
    # Cycle 84 — hardening: at most 20 clips, each ≤ 2 MB of base64, and a
    # bounded reason. Enrollment audio is never stored, but a giant payload
    # still costs decode memory before that refusal.
    samples: list[str] = Field(max_length=20)
    consent: bool = False
    reason: str | None = Field(default=None, max_length=512)


@router.post("/voice/enroll")
async def phone_voice_enroll(
    data: PhoneVoiceEnrollRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 69 — voice enrollment from the phone. Same runtime, same
    consent law, same encrypted voiceprint as the owner-trust API; the
    phone PWA just provides the recording surface. Explicit consent is
    required IN THIS REQUEST; raw audio is never stored."""

    _check_origin(request)
    if is_sandbox_device(device):
        raise HTTPException(status_code=403, detail="Voice enrollment is an owner surface")
    if not data.consent:
        raise HTTPException(status_code=403, detail="Voice enrollment needs explicit consent in this request")
    if len(data.samples) < 5:
        raise HTTPException(status_code=422, detail="Enrollment needs at least 5 voice samples")
    from app.api.voice import _runtime

    runtime = _runtime(session)
    try:
        row = await runtime.enroll(
            [{"audio_b64": sample, "liveness_proof": "live"} for sample in data.samples[:20]],
            reason=data.reason or f"phone-enroll:{device.name}",
        )
    except Exception as exc:
        from app.voice.contracts import VoiceError as _VoiceError

        if isinstance(exc, _VoiceError):
            await session.commit()
            raise HTTPException(status_code=exc.status, detail=exc.message, headers={"X-Error-Code": exc.code}) from exc
        raise
    from app.identity.service import identity_service as _identity

    owner = await _identity.get_owner(session)
    if owner is not None:
        row.owner_id = owner.id
    await session.commit()
    return {
        "ok": True,
        "enrollment_id": str(row.id),
        "version": row.version,
        "sample_count": row.sample_count,
        "algorithm": row.algorithm,
        "raw_samples_stored": False,
    }




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



@router.get("/history")
async def phone_history(
    request: Request,
    limit: int = 20,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 72 — the phone's own recent turns with provenance chips.
    Read from PhoneTurnReceipt (the durable record the text/voice paths
    already write); nothing is re-derived, nothing invented."""

    _check_origin(request)
    from sqlalchemy import select as _select
    from app.models import PhoneTurnReceipt as _Receipt

    rows = (
        await session.execute(
            _select(_Receipt)
            .where(_Receipt.device_id == device.id)
            .order_by(_Receipt.created_at.desc())
            .limit(max(1, min(int(limit or 20), 50)))
        )
    ).scalars().all()
    turns = []
    for row in rows:
        actions = row.action_calls if isinstance(row.action_calls, list) else []
        turns.append(
            {
                "at": row.created_at.isoformat() if row.created_at else None,
                "kind": row.kind,
                "text": (row.transcript or "")[:240],
                "life_mutation": bool(row.life_mutation),
                "trusted_owner": bool(row.trusted_owner),
                "chips": [
                    {
                        "tool": str(action.get("name") or action.get("tool") or ""),
                        "route": str(action.get("route") or action.get("provenance") or ""),
                        "executed": bool(action.get("executed", action.get("ok"))),
                    }
                    for action in actions[:6]
                    if isinstance(action, dict)
                ],
            }
        )
    return {"ok": True, "turns": turns}


@router.get("/memory")
async def memory_browser(
    request: Request,
    limit: int = 25,
    kind: str | None = None,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 73 — READ-ONLY memory browser for the trusted phone. Recent
    memories with type/importance; no edit verbs exist on this surface
    (correct/forget/restore stay in the privacy center)."""

    _check_origin(request)
    if is_sandbox_device(device):
        return {"ok": True, "sandbox": True, "memories": [], "note": "Personal memory is off on this device."}
    from sqlalchemy import select as _select
    from app.models import Memory as _Memory

    query = _select(_Memory).order_by(_Memory.created_time.desc()).limit(max(1, min(int(limit or 25), 60)))
    rows = (await session.execute(query)).scalars().all()
    memories = [
        {
            "id": str(row.id),
            "kind": row.memory_type,
            "text": (row.text or "")[:280],
            "importance": round(float(row.importance or 0), 2),
            "provenance": row.source_type,
            "at": row.created_time.isoformat() if row.created_time else None,
        }
        for row in rows
    ]
    return {"ok": True, "count": len(memories), "memories": memories}



@router.get("/tactical")
async def tactical_brief(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 74 — READ-ONLY tactical brief page data: what's true RIGHT
    NOW across the system (timers, inbox, nudges, devices, voice lease,
    heading-out state). Server-composed; the phone only renders."""

    _check_origin(request)
    from sqlalchemy import select as _select, func as _func
    from app.models import Device as _Device
    from app.ev.timers import list_timers
    timers = await list_timers(session)
    from app.everywhere.inbox import list_inbox
    inbox_items = await list_inbox(session, device_id=device.id, limit=50)
    devices = (await session.execute(_select(_Device))).scalars().all()
    online = [d for d in devices if d.revoked_at is None and d.last_seen_at is not None]
    from app.everywhere.heading_out import heading_out_consent
    from app.device_gateway.lease import current_lease

    lease = None
    try:
        lease = await current_lease(session)
    except Exception:
        lease = None
    from app.everywhere.nudge import in_quiet_hours, nudge_prefs

    prefs = nudge_prefs(device)
    return {
        "ok": True,
        "at": utcnow().isoformat(),
        "timers": [
            {"id": str(t.get("id") or ""), "label": str(t.get("label") or t.get("title") or ""), "fire_at": str(t.get("fire_at") or "")}
            for t in (timers.get("timers") or [])[:6]
        ],
        "inbox_unread": len([i for i in inbox_items if i.get("unread")]),
        "nudges": {**prefs, "quiet_now": in_quiet_hours(prefs)},
        "devices_online": len(online),
        "devices_total": len([d for d in devices if d.revoked_at is None]),
        "heading_out": heading_out_consent(device).get("state") or "unknown",
        "voice_lease": bool(lease),
    }


class VoiceVerifyRequest(BaseModel):
    audio_b64: str = Field(max_length=2_800_000)  # ≈2 MB of audio


@router.post("/voice/verify")
async def phone_voice_verify(
    data: VoiceVerifyRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cycle 70 — spoken challenge against the enrolled voiceprint. Success
    opens the 120 s speaker-verified window for consequential sends."""

    _check_origin(request)
    if is_sandbox_device(device):
        raise HTTPException(status_code=403, detail="Speaker verification is an owner surface")
    from app.api.voice import _runtime

    outcome = await _runtime(session).verify_samples([{"audio_b64": data.audio_b64, "liveness_proof": "live"}])
    await session.commit()
    if not outcome.get("accepted"):
        return {"ok": False, **outcome, "spoken": "That didn't match. Try again."}
    from app.everywhere.speaker_verify import mark_speaker_verified

    mark_speaker_verified(device)
    await session.commit()
    return {"ok": True, **outcome, "spoken": "It's you. Go ahead."}


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
    if delivery in {"poll", "web_notification"} or (not token and delivery != "apns"):
        chosen = "web_notification" if delivery == "web_notification" else "poll"
        _stash_profile(
            device,
            "notifications",
            {
                "delivery": chosen,
                "authorization": (data.authorization or "granted")[:32],
                "registered_at": utcnow().isoformat(),
            },
        )
        await session.commit()
        return {"ok": True, "registered": chosen == "web_notification", "delivery": chosen}
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


@router.get("/today")
async def device_today(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One-call phone dashboard: HUD card, health, calendar, reminders,
    memory highlights, pending inbox. Additive and phone-scoped; Mac clients
    and existing endpoints are untouched."""
    _check_origin(request)
    from app.ev.alert_radar import list_alerts
    from app.ev.hud import status_card
    from app.ev.workbench import last_hud_payload
    from app.everywhere.inbox import list_inbox
    from app.models import Memory
    from app.utils.text import utcnow as _utcnow

    memory_enabled = not is_sandbox_device(device)
    profile = dict(getattr(device, "endpoint_profile", None) or {})

    card: dict | None = None
    last = await last_hud_payload(session)
    if last and last.get("schema_version") == "ev.hud.card.v1":
        card = {
            "schema_version": "ev.hud.card.v1",
            "generated_at": last.get("generated_at"),
            "title": last.get("title") or "EV",
            "body": last.get("body") or "",
            "priority": last.get("priority") or 0.0,
            "meta": last.get("meta") if isinstance(last.get("meta"), dict) else {},
        }
    else:
        try:
            sc = await status_card(session)
            card = sc.model_dump() if hasattr(sc, "model_dump") else dict(sc)
        except Exception:
            card = None

    healthkit = profile.get("healthkit") or {}
    calendar = profile.get("calendar") or {}
    reminders = await list_alerts(session, status="pending", kind="reminder", limit=20)
    memories: list[dict] = []
    if memory_enabled:
        rows = (
            (
                await session.execute(
                    select(Memory)
                    .where(Memory.is_current.is_(True))
                    .order_by(Memory.updated_time.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        memories = [
            {
                "id": str(m.id),
                "memory_type": m.memory_type,
                "text": str(m.text or "")[:280],
                "importance": m.importance,
                "updated_time": m.updated_time.isoformat() if m.updated_time else None,
            }
            for m in rows
        ]
    inbox_items = await list_inbox(session, device_id=device.id, limit=50)
    from .phone_routines import normalize

    quiet_state: dict = {"active": False, "window": None}
    routines = normalize(profile.get("routines") or {})
    if routines.get("quiet_hours_start") and routines.get("quiet_hours_end"):
        from .phone_routines import in_quiet_hours

        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(routines.get("timezone") or "UTC")
            local_now = _utcnow().astimezone(tz)
            quiet_state = {
                "active": bool(in_quiet_hours(local_now, routines)),
                "window": {
                    "start": routines.get("quiet_hours_start"),
                    "end": routines.get("quiet_hours_end"),
                    "timezone": routines.get("timezone") or "UTC",
                },
            }
        except Exception:  # noqa: BLE001 - display-only state
            quiet_state = {"active": False, "window": None}
    return {
        "ok": True,
        "generated_at": _utcnow().isoformat(),
        "device": {
            "id": str(device.id),
            "role": device.role or "companion",
            "display_name": device.name or "This iPhone",
        },
        "memory_enabled": memory_enabled,
        "memory_scope": memory_scope_of(device),
        "quiet_hours": quiet_state,
        "hud": card,
        "health": {
            "available": bool(healthkit.get("available")),
            "freshness": healthkit.get("freshness") or "unavailable",
            "captured_at": healthkit.get("captured_at"),
            "metrics": healthkit.get("snapshot") if isinstance(healthkit.get("snapshot"), dict) else {},
            "sent_to_model": False,
        },
        "calendar": {
            "events": calendar.get("events") or [],
            "captured_at": calendar.get("captured_at"),
            "sent_to_model": False,
        },
        "reminders": [
            {
                "id": str(row.id),
                "text": str(row.body or row.title or "untitled"),
                "status": str(row.status or "pending"),
            }
            for row in reminders
        ],
        "memories": memories,
        "inbox_pending": sum(1 for item in inbox_items if item.get("unread")),
    }


@router.get("/memories")
async def device_memories(
    request: Request,
    q: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Phone-scoped memory browser. Sandbox devices get an explicit off state;
    owner devices get current memories with optional semantic search."""
    _check_origin(request)
    if is_sandbox_device(device):
        return {"ok": True, "memory_enabled": False, "memories": [], "total": 0}
    from app.memory.retrieval import Retriever
    from app.models import Memory

    stmt = select(Memory).where(Memory.is_current.is_(True))
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    rows = list((await session.execute(stmt)).scalars().all())
    if q:
        retriever = Retriever(session)
        hits = await retriever.search(
            q,
            k=limit,
            access="master",
            memory_types=[memory_type] if memory_type else None,
        )
        hit_ids = {UUID(h.memory_id) for h in hits}
        rows = [m for m in rows if m.id in hit_ids]
        rows.sort(key=lambda m: next(h.score for h in hits if h.memory_id == str(m.id)), reverse=True)
    rows = rows[:limit]
    return {
        "ok": True,
        "memory_enabled": True,
        "memories": [_phone_memory(m) for m in rows],
        "total": len(rows),
        "query": q,
    }


@router.get("/memories/{memory_id}")
async def device_memory_detail(
    memory_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One memory with its source-event provenance for the phone surface."""
    _check_origin(request)
    if is_sandbox_device(device):
        return {"ok": True, "memory_enabled": False, "memory": None}
    from app.models import Event, Memory, MemoryEvent

    memory = await session.get(Memory, memory_id)
    if memory is None or not memory.is_current:
        raise HTTPException(status_code=404, detail="Memory not found")
    source_rows = (
        (
            await session.execute(
                select(Event)
                .join(MemoryEvent, MemoryEvent.event_id == Event.id)
                .where(MemoryEvent.memory_id == memory.id)
                .order_by(Event.occurred_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "ok": True,
        "memory_enabled": True,
        "memory": _phone_memory(memory),
        "sources": [
            {
                "id": str(ev.id),
                "kind": ev.event_type,
                "text": str((ev.content or {}).get("text") or "")[:400],
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            }
            for ev in source_rows
        ],
    }


@router.get("/memories/{memory_id}/provenance")
async def device_memory_provenance(
    memory_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Memory provenance: the version chain of this memory plus its source
    events — what Evie knew, when it changed, and why."""
    _check_origin(request)
    if is_sandbox_device(device):
        return {"ok": True, "memory_enabled": False, "memory": None, "versions": [], "sources": []}
    from app.models import Event, Memory, MemoryEvent

    memory = await session.get(Memory, memory_id)
    if memory is None or not memory.is_current:
        raise HTTPException(status_code=404, detail="Memory not found")
    group_rows = (
        (
            await session.execute(
                select(Memory)
                .where(Memory.version_group == memory.version_group)
                .order_by(Memory.version.asc())
            )
        )
        .scalars()
        .all()
    )
    source_rows = (
        (
            await session.execute(
                select(Event)
                .join(MemoryEvent, MemoryEvent.event_id == Event.id)
                .where(MemoryEvent.memory_id == memory.id)
                .order_by(Event.occurred_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "ok": True,
        "memory_enabled": True,
        "memory": _phone_memory(memory),
        "versions": [
            {
                "version": m.version,
                "text": str(m.text or "")[:400],
                "reason_for_change": m.reason_for_change,
                "updated_time": m.updated_time.isoformat() if m.updated_time else None,
                "is_current": bool(m.is_current),
                "supersedes_id": str(m.supersedes_id) if m.supersedes_id else None,
            }
            for m in group_rows
        ],
        "sources": [
            {
                "id": str(ev.id),
                "kind": ev.event_type,
                "text": str((ev.content or {}).get("text") or "")[:400],
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            }
            for ev in source_rows
        ],
    }


def _phone_memory(memory: Memory) -> dict:
    return {
        "id": str(memory.id),
        "memory_type": memory.memory_type,
        "text": str(memory.text or "")[:2000],
        "importance": memory.importance,
        "confidence": memory.confidence,
        "source_type": memory.source_type,
        "privacy_level": memory.privacy_level,
        "event_time": memory.event_time.isoformat() if memory.event_time else None,
        "updated_time": memory.updated_time.isoformat() if memory.updated_time else None,
    }


@router.get("/search")
async def device_search(
    request: Request,
    q: str = Query(default="", min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=60),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One-call phone search across memories, recent events, pending
    reminders, and the phone's own contacts snapshot. Sandbox devices see
    reminders/contacts only — personal memory stays off until promotion."""
    _check_origin(request)
    needle = q.strip().lower()
    memory_enabled = not is_sandbox_device(device)
    profile = dict(getattr(device, "endpoint_profile", None) or {})

    memories: list[dict] = []
    events: list[dict] = []
    if memory_enabled and needle:
        from app.memory.retrieval import Retriever
        from app.models import Event, Memory

        mem_rows = list(
            (
                await session.execute(
                    select(Memory).where(Memory.is_current.is_(True)).limit(2000)
                )
            )
            .scalars()
            .all()
        )
        try:
            retriever = Retriever(session)
            hits = await retriever.search(q, k=limit, access="master")
            hit_ids = {UUID(h.memory_id) for h in hits}
            ranked = [m for m in mem_rows if m.id in hit_ids]
            ranked.sort(
                key=lambda m: next(h.score for h in hits if h.memory_id == str(m.id)),
                reverse=True,
            )
            memories = [_phone_memory(m) for m in ranked[:limit]]
        except Exception:
            memories = []

        if len(memories) < limit:
            recent = (
                (
                    await session.execute(
                        select(Event)
                        .where(Event.tombstoned_at.is_(None))
                        .order_by(Event.occurred_at.desc())
                        .limit(600)
                    )
                )
                .scalars()
                .all()
            )
            for ev in recent:
                text = str((ev.content or {}).get("text") or "")
                if needle in text.lower():
                    events.append(
                        {
                            "id": str(ev.id),
                            "kind": ev.event_type,
                            "text": text[:400],
                            "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                        }
                    )
                    if len(events) >= max(1, limit - len(memories)):
                        break

    from app.ev.alert_radar import list_alerts

    reminders = []
    for row in await list_alerts(session, status="pending", kind="reminder", limit=60):
        text = str(row.body or row.title or "")
        if needle in text.lower():
            reminders.append({"id": str(row.id), "text": text})
    contacts = []
    for item in (profile.get("contacts") or {}).get("contacts") or []:
        name = str(item.get("name") or "")
        if needle in name.lower():
            contacts.append({"name": name})
    return {
        "ok": True,
        "query": q,
        "memory_enabled": memory_enabled,
        "memories": memories,
        "events": events,
        "reminders": reminders[:limit],
        "contacts": contacts[:limit],
    }


@router.post("/capture", response_model=None)
async def gateway_capture(
    data: CaptureNoteRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Phone-scoped note capture into the owner event stream.

    Trusted owner devices write a note event exactly like /v1/events does
    (idempotent, processed, curated). Sandbox devices cannot write owner
    memory — honest 403 instead of a fake capture."""
    _check_origin(request)
    if is_sandbox_device(device):
        raise HTTPException(
            status_code=403,
            detail="Capture requires Mac promotion (TRUSTED_OWNER_DEVICE).",
            headers={"X-Error-Code": "capture_requires_owner"},
        )
    text = (data.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Capture text is empty")
    privacy = data.privacy_level or "normal"
    if privacy not in {"normal", "private", "sensitive", "never_send_to_model"}:
        raise HTTPException(status_code=422, detail="Unknown privacy level")
    key = (data.idempotency_key or "").strip()[:128]
    from uuid import uuid4

    from app.models import Event
    from app.schemas import EventCreate
    from app.services.event_service import EventService
    from app.services.processor import ensure_processed
    from app.utils.text import sha256_hex

    if key:
        existing = (
            (
                await session.execute(
                    select(Event).where(Event.idempotency_key_hash == sha256_hex(key))
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            await session.commit()
            return {
                "ok": True,
                "duplicate": True,
                "event_id": str(existing.id),
                "kind": existing.event_type,
            }
    service = EventService(session, actor="master")
    event = await service.create(
        EventCreate(
            source="owner.phone",
            event_type="note",
            text=text[:2000],
            device_id=str(device.id),
            privacy_level=privacy,
        ),
        request_id=data.request_id or str(uuid4()),
        idempotency_key=key or None,
    )
    from app.routines.service import consider_event

    await consider_event(session, event=event)
    await session.commit()
    deltas = await ensure_processed(event.id)
    try:
        schedule_curation(limit=1)
    except Exception:  # noqa: BLE001 - capture already succeeded
        pass
    return {
        "ok": True,
        "duplicate": False,
        "event_id": str(event.id),
        "kind": event.event_type,
        "memory_delta": [{"id": d.get("id"), "memory_type": d.get("memory_type")} for d in deltas],
    }


@router.post("/capture/audio", response_model=None)
async def gateway_audio_capture(
    data: CaptureAudioRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Phone-scoped voice-note capture: base64 audio -> attachment event.

    Trusted owner devices only (same honesty gate as /capture). The audio
    is stored through the object store and linked to a voice_note event;
    transcription is a later stage and is never fabricated here."""
    _check_origin(request)
    if is_sandbox_device(device):
        raise HTTPException(
            status_code=403,
            detail="Capture requires Mac promotion (TRUSTED_OWNER_DEVICE).",
            headers={"X-Error-Code": "capture_requires_owner"},
        )
    import base64 as _b64

    try:
        raw = _b64.b64decode((data.audio_b64 or "").encode("ascii"), validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="audio_b64 is not valid base64") from None
    if not raw:
        raise HTTPException(status_code=422, detail="Audio payload is empty")
    if len(raw) > 6 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio payload exceeds 6 MB")

    from uuid import uuid4 as _uuid4

    from app.models import Attachment as AttachmentRow
    from app.schemas import EventCreate
    from app.services.event_service import EventService
    from app.services.processor import ensure_processed
    from app.storage.object_store import get_object_store, sha256_bytes

    content_type = (data.content_type or "audio/mp4").strip()[:128] or "audio/mp4"
    store = get_object_store()
    storage_key = f"attachments/{_uuid4()}.bin"
    await store.put(storage_key, raw, content_type)
    key = (data.idempotency_key or "").strip()[:128]
    service = EventService(session, actor="master")
    event = await service.create(
        EventCreate(
            source="owner.phone",
            event_type="voice_note",
            content={
                "filename": f"voice-note-{data.captured_at or 'now'}.m4a",
                "content_type": content_type,
                "size_bytes": len(raw),
                "storage_key": storage_key,
                "transcript": None,
            },
            metadata={"capture": "voice_note"},
            device_id=str(device.id),
            privacy_level="normal",
        ),
        request_id=data.request_id or str(_uuid4()),
        idempotency_key=key or None,
    )
    row = AttachmentRow(
        event_id=event.id,
        filename=f"voice-note-{_uuid4().hex[:8]}.m4a",
        content_type=content_type,
        size_bytes=len(raw),
        storage_key=storage_key,
        sha256=sha256_bytes(raw),
    )
    session.add(row)
    await session.commit()
    await ensure_processed(event.id)
    return {
        "ok": True,
        "event_id": str(event.id),
        "attachment_id": str(row.id),
        "kind": event.event_type,
        "size_bytes": len(raw),
    }


@router.get("/routines")
async def get_phone_routines(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .phone_routines import normalize

    profile = dict(getattr(device, "endpoint_profile", None) or {})
    return {"ok": True, "routines": normalize(profile.get("routines"))}


@router.put("/routines")
async def put_phone_routines(
    data: RoutinesPutRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    from .phone_routines import normalize

    candidate = normalize(
        {
            "enabled": data.enabled,
            "digest_times": data.digest_times,
            "quiet_hours_start": data.quiet_hours_start,
            "quiet_hours_end": data.quiet_hours_end,
            "timezone": data.timezone,
            "updated_at": utcnow().isoformat(),
        }
    )
    if data.enabled and not candidate["digest_times"]:
        raise HTTPException(status_code=422, detail="Enable requires at least one digest time")
    _stash_profile(device, "routines", candidate)
    await session.commit()
    return {"ok": True, "routines": candidate}


@router.get("/contacts")
async def device_contacts(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Names only. Safari cannot read the iPhone address book; Home Station
    names come from memory, mail, and message events. Numbers stay off this
    JSON and are resolved only for an explicit Call.
    """
    _check_origin(request)
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    contacts = profile.get("contacts") or {}
    from .phone_people import list_phone_people, public_contact_rows

    snapshot = public_contact_rows(contacts.get("contacts") or [])
    home: list[dict] = []
    if not is_sandbox_device(device):
        try:
            home = await list_phone_people(session, limit=40)
        except Exception:
            home = []
    return {
        "ok": True,
        "contacts": snapshot,
        "home": [{"name": row["name"], "channels": row.get("channels") or [], "callable": bool(row.get("callable"))} for row in home],
        "captured_at": contacts.get("captured_at"),
        "sent_to_model": False,
    }


@router.get("/looks")
async def device_look_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=60),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Recent camera look events (this phone's vision history) with any
    derived memory text. Sandbox phones see nothing — looks write owner
    memory."""
    _check_origin(request)
    if is_sandbox_device(device):
        return {"ok": True, "memory_enabled": False, "looks": []}
    from app.models import Event

    rows = (
        (
            await session.execute(
                select(Event)
                .where(
                    Event.event_type == "camera.look",
                    Event.tombstoned_at.is_(None),
                )
                .order_by(Event.occurred_at.desc())
                .limit(min(limit, 60))
            )
        )
        .scalars()
        .all()
    )
    looks = []
    for ev in rows:
        content = ev.content or {}
        attachment_id = str(content.get("attachment_id") or "").strip() or None
        media_kind = str(content.get("media_kind") or "").strip().lower() or None
        looks.append(
            {
                "id": str(ev.id),
                "summary": str(content.get("summary") or content.get("text") or "")[:400],
                "scene": str(content.get("scene") or "")[:200],
                "device_id": ev.device_id,
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                # Playback affordance: only ever true for media this look
                # actually stored, and only for a trusted device.
                "has_media": bool(attachment_id),
                "media_kind": media_kind,
                "duration_s": content.get("duration_s"),
                "moment_count": content.get("moment_count") or None,
                "transcript": (str(content.get("transcript") or "")[:400] or None),
                "media_url": (
                    f"/v1/device-gateway/looks/{ev.id}/media" if attachment_id else None
                ),
            }
        )
    return {"ok": True, "memory_enabled": True, "looks": looks}


@router.get("/looks/{look_id}/media")
async def device_look_media(
    look_id: UUID,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Play back the media a camera observation stored (clip or kept frame).

    Scoped twice over: the caller must be a trusted (non-sandbox) device, and
    only the attachment referenced by that exact observation is served — never
    an arbitrary attachment id.
    """

    _check_origin(request)
    if is_sandbox_device(device):
        raise HTTPException(status_code=403, detail="Sandbox devices cannot read owner media")
    from app.models import Attachment, Event

    ev = await session.get(Event, look_id)
    if ev is None or ev.event_type != "camera.observation" or ev.tombstoned_at is not None:
        raise HTTPException(status_code=404, detail="No such camera observation")
    raw_id = str((ev.content or {}).get("attachment_id") or "").strip()
    if not raw_id:
        raise HTTPException(status_code=404, detail="This look stored no media")
    try:
        attachment = await session.get(Attachment, UUID(raw_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="This look stored no media") from None
    if attachment is None:
        raise HTTPException(
            status_code=410,
            detail="The stored media was deleted by the retention policy",
        )
    from app.storage.object_store import get_object_store

    try:
        data = await get_object_store().get(attachment.storage_key)
    except Exception:  # noqa: BLE001 - a missing blob is an honest 410
        raise HTTPException(status_code=410, detail="The stored media is no longer available") from None
    return Response(
        content=data,
        media_type=attachment.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.post("/battery")
async def device_battery_report(
    data: BatteryReportRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Phone power state, persisted for gear/power surfaces. Honest values
    only: percent must be 0-100 and charging is a boolean, never guessed."""
    _check_origin(request)
    percent = round(float(data.percent), 1)
    if not (0 <= percent <= 100):
        raise HTTPException(status_code=422, detail="percent must be 0-100")
    device.battery_percent = percent
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    hardware = dict(profile.get("hardware") or {})
    hardware["battery_percent"] = percent
    hardware["charging"] = bool(data.charging)
    hardware["battery_reported_at"] = utcnow().isoformat()
    profile["hardware"] = hardware
    device.endpoint_profile = profile
    await session.commit()
    return {"ok": True, "percent": percent, "charging": bool(data.charging)}


@router.get("/vitals")
async def device_health_series(
    request: Request,
    limit: int = Query(default=14, ge=1, le=90),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Phone health dashboard data: the phone's own snapshot plus the
    owner vitals series (readiness band + core metrics) written by the
    native HealthKit path."""
    _check_origin(request)
    from app.models import HealthSnapshot

    profile = dict(getattr(device, "endpoint_profile", None) or {})
    healthkit = profile.get("healthkit") or {}
    series = []
    if not is_sandbox_device(device):
        rows = (
            (
                await session.execute(
                    select(HealthSnapshot)
                    .order_by(HealthSnapshot.occurred_at.desc())
                    .limit(min(limit, 90))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            metrics = dict(row.metrics or {})
            keep = {k: v for k, v in metrics.items() if isinstance(v, (int, float))}
            series.append(
                {
                    "id": str(row.id),
                    "source": row.source,
                    "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                    "readiness": row.readiness,
                    "band": row.band,
                    "metrics": keep,
                    "units": row.units or {},
                }
            )
    return {
        "ok": True,
        "phone_snapshot": {
            "available": bool(healthkit.get("available")),
            "freshness": healthkit.get("freshness") or "unavailable",
            "captured_at": healthkit.get("captured_at"),
            "metrics": healthkit.get("snapshot") if isinstance(healthkit.get("snapshot"), dict) else {},
            "sent_to_model": False,
        },
        "series": series,
        "memory_enabled": not is_sandbox_device(device),
    }


@router.get("/weather")
async def device_weather(
    request: Request,
    place: str | None = Query(default=None, max_length=120),
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Structured weather for the phone surface. Uses Home Station's live
    providers; honest error codes on timeout/unavailability."""
    _check_origin(request)
    import asyncio as _asyncio

    from app.search.live import default_place, home_coords, weather_results

    requested = (place or "").strip() or None
    if requested is None and home_coords() is None and not default_place():
        return {"ok": True, "status": "no_place", "forecast": None, "error_code": "NO_PLACE"}
    try:
        results = await _asyncio.wait_for(weather_results(requested or "home", limit=2), timeout=8)
    except TimeoutError:
        return {"ok": True, "status": "unavailable", "forecast": None, "error_code": "WEATHER_TIMEOUT", "retryable": True}
    except Exception:
        return {"ok": True, "status": "unavailable", "forecast": None, "error_code": "WEATHER_UNAVAILABLE", "retryable": True}
    if not results:
        return {"ok": True, "status": "unavailable", "forecast": None, "error_code": "WEATHER_UNAVAILABLE", "retryable": True}
    first = results[0]
    forecast = {
        "title": str(getattr(first, "title", None) or "")[:160],
        "snippet": str(getattr(first, "snippet", None) or "")[:800],
        "place": requested or default_place() or "home",
    }
    return {"ok": True, "status": "ok", "forecast": forecast, "error_code": None}


@router.get("/capabilities")
async def device_capabilities(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What this phone can actually do right now, derived from trust state,
    declared capabilities, and reported snapshots — the client never infers."""
    _check_origin(request)
    trusted = not is_sandbox_device(device)
    declared = set(device.capabilities or [])
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    has_contacts = bool((profile.get("contacts") or {}).get("contacts"))
    has_health = bool((profile.get("healthkit") or {}).get("available"))
    has_calendar = bool((profile.get("calendar") or {}).get("events"))
    home_people = False
    series_health = False
    if trusted:
        try:
            from .phone_people import list_phone_people

            home_people = bool(await list_phone_people(session, limit=1))
        except Exception:
            home_people = False
        try:
            from app.models import HealthSnapshot

            series_health = (
                await session.execute(select(HealthSnapshot).limit(1))
            ).scalars().first() is not None
        except Exception:
            series_health = False

    def cap(available: bool, reason: str | None = None) -> dict:
        return {"available": bool(available), "reason": reason}

    return {
        "ok": True,
        "trust_state": "TRUSTED_OWNER_DEVICE" if trusted else "PAIRED_SANDBOX",
        "capabilities": {
            "voice": cap("foreground_voice" in declared or "voice" in declared),
            "camera": cap("camera" in declared),
            "text": cap("text" in declared or "foreground_voice" in declared),
            "memory": cap(trusted, None if trusted else "promote_on_mac"),
            "capture_note": cap(trusted, None if trusted else "promote_on_mac"),
            "capture_voice": cap(trusted, None if trusted else "promote_on_mac"),
            "search": cap(trusted, None if trusted else "promote_on_mac"),
            "looks": cap(trusted, None if trusted else "promote_on_mac"),
            "today": cap(True),
            "inbox": cap(True),
            "queue": cap(True),
            "weather": cap(True),
            "people": cap(
                has_contacts or home_people,
                None if (has_contacts or home_people) else "no_contacts_snapshot",
            ),
            "health": cap(
                has_health or series_health,
                None if (has_health or series_health) else "no_healthkit_snapshot",
            ),
            "calendar": cap(has_calendar, None if has_calendar else "no_calendar_snapshot"),
            "routines": cap(True),
        },
    }


@router.get("/onboarding")
async def get_phone_onboarding(
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    onboarding = profile.get("onboarding")
    if not isinstance(onboarding, dict):
        onboarding = {}
    return {
        "ok": True,
        "onboarding": {
            "steps_completed": list(onboarding.get("steps_completed") or []),
            "camera_role_set": bool(onboarding.get("camera_role_set")),
            "updated_at": onboarding.get("updated_at"),
        },
    }


@router.put("/onboarding")
async def put_phone_onboarding(
    data: OnboardingPutRequest,
    request: Request,
    device: Device = Depends(require_gateway_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_origin(request)
    allowed = {
        "paired",
        "camera_role",
        "promoted",
        "first_turn",
        "digest_configured",
    }
    steps = [s for s in (data.steps_completed or []) if s in allowed]
    onboarding = {
        "steps_completed": sorted(set(steps)),
        "camera_role_set": bool(data.camera_role_set),
        "updated_at": utcnow().isoformat(),
    }
    _stash_profile(device, "onboarding", onboarding)
    await session.commit()
    return {"ok": True, "onboarding": onboarding}


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
    if not data.preserve_lease:
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
    frames = [
        frame
        for frame in (data.frames or [])
        if isinstance(frame, dict) and str(frame.get("jpeg_b64") or "").strip()
    ][:8]
    primary = data.jpeg_b64 or (str(frames[-1].get("jpeg_b64") or "") if frames else "")
    if not primary:
        raise HTTPException(status_code=422, detail="No camera frame supplied")
    try:
        meta = put_frame(data.request_id, device_id=str(device.id), jpeg_b64=primary)
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

        burst: list[dict] = []
        for index, frame in enumerate(frames):
            entry = {
                "jpeg_b64": str(frame.get("jpeg_b64") or ""),
                "captured_at_ms": frame.get("captured_at_ms"),
                "sequence": frame.get("sequence", index),
            }
            burst.append(entry)
        vision = await ingest_phone_frame(
            session,
            device=device,
            request_id=data.request_id,
            jpeg_b64=primary,
            action=data.action or "look",
            frames=burst or None,
            media_kind=data.media_kind,
            has_clip=data.has_clip,
            note=data.note,
        )
        await session.commit()
    return {
        "ok": True,
        "camera": meta,
        "persisted_to_memory_os": bool(vision and vision.get("persisted_to_memory_os")),
        "vision": vision,
        "ocr_text": (vision or {}).get("ocr_text"),
        "moments": (vision or {}).get("moments") or [],
        "media_kind": (vision or {}).get("media_kind"),
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
        "created_at": row.get("created_at"),
        "received_at": row.get("received_at"),
        "expires_at": row.get("expires_at"),
        "expired": False,
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
