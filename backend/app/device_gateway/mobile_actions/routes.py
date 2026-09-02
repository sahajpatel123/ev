"""Device Gateway HTTP surface for Evie Mobile Actions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.models import Device

from ..auth import require_gateway_device
from ..sandbox import is_sandbox_device
from ..security import origin_allowed
from . import BRIDGE_NAME, BRIDGE_PROTOCOL, BRIDGE_VERSION
from .bridge import import_url, signed_shortcut
from .engine import apply_confirmation_utterance, confirm_action, native_execute_action, status_snapshot
from .service import (
    apply_handshake,
    cancel_action,
    claim_action,
    client_complete,
    complete_action,
    resolve_action,
)
from .store import consume_download_token, mint_download_token
from .tool import dispatch_phone_action

router = APIRouter(tags=["mobile-actions"])


def gateway_origin(request: Request) -> str:
    host = request.headers.get("host") or "127.0.0.1:8000"
    proto = "https" if request.url.scheme == "https" or host.endswith(".ts.net") else "http"
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded:
        proto = forwarded.split(",")[0].strip() or proto
    return f"{proto}://{host}"


def _label(device: Device) -> str:
    role = (device.role or "").strip().lower()
    if role == "primary_companion":
        return "Primary iPhone"
    if role == "secondary_companion":
        return "Secondary iPhone"
    return device.name or "This iPhone"


class HandshakeBody(BaseModel):
    instance_id: str = ""
    timezone: str | None = None
    locale: str | None = None
    native_shell: bool = False
    broker_version: str | None = None
    os_version: str | None = None
    permissions: dict = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    legacy_bridge: bool = False
    bridge_installed: bool = False
    bridge_version: str | None = None
    protocol: int = BRIDGE_PROTOCOL


class ResolveBody(BaseModel):
    token: str
    device_id: str | None = None
    protocol: int | None = None
    action_id: str | None = None


class ConfirmBody(BaseModel):
    instance_id: str = ""


class ConfirmUtteranceBody(BaseModel):
    text: str = ""
    instance_id: str = ""
    session_id: str | None = None


class CompleteBody(BaseModel):
    completion_token: str = ""
    status: str | None = None
    result: str | None = None
    verified: bool | None = None
    failure: str | None = None
    display_name: str | None = None
    masked_destination: str | None = None
    choices: list[dict] | None = None
    permission: str | None = None
    requires_user_interaction: bool | None = None


class ClaimBody(BaseModel):
    completion_token: str


class PhoneActionBody(BaseModel):
    instance_id: str = ""
    session_id: str | None = None
    operation: str
    arguments: dict = Field(default_factory=dict)


def _require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin_allowed(origin, host):
        raise HTTPException(status_code=403, detail="Origin not allowed")


def _check_origin_or_shortcut(request: Request) -> None:
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin or origin == "null":
        return
    _require_origin(request)


@router.get("/mobile-actions/status")
async def mobile_actions_status(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    snap = status_snapshot(
        device_id=str(device.id),
        role=device.role or "companion",
        display_name=_label(device),
    )
    snap["sandbox"] = is_sandbox_device(device)
    return snap


@router.post("/mobile-actions/handshake")
async def mobile_actions_handshake(
    data: HandshakeBody,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    result = apply_handshake(
        device_id=str(device.id),
        payload=data.model_dump(),
    )
    result["status"] = status_snapshot(
        device_id=str(device.id),
        role=device.role or "companion",
        display_name=_label(device),
    )
    return result


@router.post("/mobile-actions/bridge-link")
async def mobile_actions_bridge_link(
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    origin = gateway_origin(request)
    token = mint_download_token(device_id=str(device.id), origin=origin)
    download = f"{origin}/v1/device-gateway/mobile-actions/bridge.shortcut?dl={token}"
    return {
        "ok": True,
        "bridge_name": BRIDGE_NAME,
        "bridge_version": BRIDGE_VERSION,
        "protocol": BRIDGE_PROTOCOL,
        "download_url": download,
        "import_url": import_url(download),
        "run_name": BRIDGE_NAME,
    }


@router.get("/mobile-actions/bridge.shortcut")
async def mobile_actions_bridge_file(dl: str) -> Response:
    meta = consume_download_token(dl)
    if meta is None:
        raise HTTPException(status_code=404, detail="Install link expired. Open Evie and tap Install again.")
    data, how = signed_shortcut(origin=str(meta["origin"]), device_id=str(meta["device_id"]))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="Evie Mobile Bridge.shortcut"',
            "X-Evie-Bridge-Signing": how,
            "Cache-Control": "no-store",
        },
    )


@router.post("/mobile-actions/resolve")
async def mobile_actions_resolve(data: ResolveBody, request: Request) -> dict:
    _check_origin_or_shortcut(request)
    return resolve_action(token=data.token, device_id=data.device_id)


@router.post("/mobile-actions/{action_id}/claim")
async def mobile_actions_claim(action_id: str, data: ClaimBody, request: Request) -> dict:
    _check_origin_or_shortcut(request)
    return claim_action(action_id=action_id, completion_token=data.completion_token)


@router.post("/mobile-actions/{action_id}/complete")
async def mobile_actions_complete(action_id: str, data: CompleteBody, request: Request) -> dict:
    _check_origin_or_shortcut(request)
    payload = data.model_dump(exclude_none=True)
    payload.pop("completion_token", None)
    result = complete_action(
        action_id=action_id,
        completion_token=data.completion_token,
        payload=payload,
    )
    if result.get("ok") and not result.get("error"):
        row_spoken = result.get("spoken")
        card = {
            "kind": "phone_action",
            "title": "DONE" if result.get("executed") else "UPDATE",
            "status": (result.get("receipt") or {}).get("state"),
            "action_id": action_id,
            "spoken": row_spoken,
            "receipt": result.get("receipt"),
        }
        from .service import _push_live
        from .store import get_action

        row = get_action(action_id)
        await _push_live((row or {}).get("session_id"), card)
        if row:
            from app.db import SessionLocal
            from app.device_gateway.durable_actions import upsert_action

            async with SessionLocal() as db:
                await upsert_action(db, row)
                await db.commit()
    return result


@router.post("/mobile-actions/{action_id}/native-execute")
async def mobile_actions_native_execute(
    action_id: str,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    from .store import get_action

    row = get_action(action_id)
    if row is None:
        from app.db import SessionLocal
        from app.device_gateway.durable_actions import load_action

        async with SessionLocal() as db:
            loaded = await load_action(db, action_id)
        if loaded is not None:
            from .store import restore_action

            restore_action(loaded)
    return native_execute_action(action_id=action_id, device_id=str(device.id))


@router.post("/mobile-actions/confirm-utterance")
async def mobile_actions_confirm_utterance(
    data: ConfirmUtteranceBody,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    result = apply_confirmation_utterance(
        device_id=str(device.id),
        origin=gateway_origin(request),
        text=data.text,
        session_id=data.session_id,
    )
    if result is None:
        return {"ok": False, "unrelated": True, "spoken": None}
    return result


@router.post("/mobile-actions/{action_id}/confirm")
async def mobile_actions_confirm(
    action_id: str,
    data: ConfirmBody,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    result = confirm_action(action_id=action_id, device_id=str(device.id), origin=gateway_origin(request))
    if result.get("ok") and result.get("action_id"):
        from app.db import SessionLocal
        from app.device_gateway.durable_actions import upsert_action

        from .store import get_action

        row = get_action(action_id)
        if row:
            async with SessionLocal() as db:
                await upsert_action(db, row)
                await db.commit()
    return result


@router.post("/mobile-actions/{action_id}/cancel")
async def mobile_actions_cancel(
    action_id: str,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    return cancel_action(action_id=action_id, device_id=str(device.id))


@router.post("/mobile-actions/{action_id}/client-complete")
async def mobile_actions_client_complete(
    action_id: str,
    data: CompleteBody,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    payload = data.model_dump(exclude_none=True)
    payload.pop("completion_token", None)
    return client_complete(action_id=action_id, device_id=str(device.id), payload=payload)


@router.post("/mobile-actions/prepare")
async def mobile_actions_prepare(
    data: PhoneActionBody,
    request: Request,
    device: Device = Depends(require_gateway_device),
) -> dict:
    _require_origin(request)
    args = dict(data.arguments)
    args["operation"] = data.operation
    return await dispatch_phone_action(
        device_id=str(device.id),
        role=device.role or "companion",
        instance_id=data.instance_id,
        session_id=data.session_id,
        origin=gateway_origin(request),
        arguments=args,
        device_label=_label(device),
    )
