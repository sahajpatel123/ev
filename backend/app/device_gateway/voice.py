"""Sandbox live responder: TTS only, no Memory OS chat pipeline."""

from __future__ import annotations

import base64
import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

from app.db import SessionLocal
from app.models import Device
from app.voice.contracts import SpeechStyle
from app.voice.live.events import LiveEvent, ReplyEvent, TtsChunkEvent

from .pipeline import handle_user_text
from .sandbox import is_sandbox_device


async def device_is_sandbox(device_id: str | UUID | None) -> bool:
    if device_id is None:
        return False
    try:
        uid = device_id if isinstance(device_id, UUID) else UUID(str(device_id))
    except ValueError:
        return False
    async with SessionLocal() as session:
        device = await session.get(Device, uid)
        return is_sandbox_device(device)


def strip_production_memory_from_manifest(manifest: dict | None) -> dict:
    from .sandbox_tools import (
        SANDBOX_SAFE_LIVE_TOOLS,
        provider_effective_snapshot,
        sandbox_live_tool_specs,
        sandbox_tool_schema_hash,
        tool_schema_generation,
    )

    payload = dict(manifest or {})
    payload.pop("memory_bootstrap", None)
    payload.pop("relationship_card", None)
    payload.pop("relationship_memory", None)
    payload.pop("relationship", None)
    specs = sandbox_live_tool_specs()
    names = [spec["name"] for spec in specs]
    payload["memory_scope"] = "sandbox"
    payload["live_tool_projection"] = specs
    payload["realtime_tools"] = names
    payload["approved_tools"] = names
    payload["executable_tools"] = names
    payload["sandbox_safe_live_tools"] = sorted(SANDBOX_SAFE_LIVE_TOOLS)
    payload["sandbox_tool_schema_hash"] = sandbox_tool_schema_hash(specs)
    payload["tool_schema_generation"] = tool_schema_generation()
    payload["production_memory_enabled"] = False
    payload.update(provider_effective_snapshot())
    return payload


async def close_live_for_device(device_id: str, *, reason: str = "device_revoked") -> bool:
    from app.voice.live.events import ErrorEvent
    from app.voice.live.layer import live_for_device

    live = live_for_device(device_id)
    if live is None:
        return False
    ws = getattr(live, "transport_ws", None)
    with contextlib.suppress(Exception):
        await live.emit(
            ErrorEvent(
                at_ms=getattr(live, "now", lambda: 0)(),
                code=reason,
                message="Device authorization ended.",
                fatal=True,
            )
        )
    live.close()
    if ws is not None:
        with contextlib.suppress(Exception):
            await ws.close(code=4003)
    return True


def make_sandbox_pipeline_responder(
    *,
    device_id: str | None,
    synthesizer,
    tts_device_id: str | None = None,
):
    async def respond(text: str, envelope) -> AsyncIterator[LiveEvent]:
        del envelope
        reply = "Sandbox pipeline heard you."
        async with SessionLocal() as session:
            device = None
            if device_id:
                try:
                    device = await session.get(Device, UUID(str(device_id)))
                except ValueError:
                    device = None
            if device is not None:
                result = await handle_user_text(session, device=device, text=text)
                reply = str(result.get("reply") or reply)
                await session.commit()
        audio_b64 = None
        content_type = None
        duration_ms = None
        try:
            tts = await synthesizer.synthesize(reply, style=SpeechStyle())
            audio = getattr(tts, "audio", None)
            if audio and len(audio) <= 1_500_000:
                audio_b64 = base64.b64encode(audio).decode("ascii")
            content_type = getattr(tts, "content_type", None)
            duration_ms = getattr(tts, "duration_ms", None)
        except Exception:  # noqa: BLE001 - spoken text still returns
            audio_b64 = None
        yield TtsChunkEvent(
            at_ms=0,
            index=0,
            text=reply,
            audio_b64=audio_b64,
            audio_ref=None,
            content_type=content_type,
            duration_ms=duration_ms,
            provider="sandbox-tts",
        )
        yield ReplyEvent(
            at_ms=0,
            text=reply,
            conversation_id=None,
            model="sandbox-pipeline",
            context_tokens=0,
            style={},
            device_id=str(device_id) if device_id else None,
            tts_device_id=str(tts_device_id or device_id) if (tts_device_id or device_id) else None,
        )

    return respond
