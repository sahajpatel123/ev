"""Phone camera frames enter the owner vision path with provenance."""

from __future__ import annotations

import base64
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.camera_runtime import CameraObservation, stash_observation, validate_jpeg
from app.models import Device
from app.utils.text import utcnow

from .sandbox import is_sandbox_device


async def ingest_phone_frame(
    session: AsyncSession,
    *,
    device: Device,
    request_id: str,
    jpeg_b64: str,
    action: str = "look",
) -> dict[str, Any]:
    raw = jpeg_b64 or ""
    try:
        jpeg = base64.b64decode(raw)
    except Exception:
        return {
            "ok": False,
            "error_code": "MALFORMED_FRAME",
            "spoken": "The camera frame could not be transferred. I did not see anything.",
        }
    checked = validate_jpeg(jpeg)
    if checked is None:
        return {
            "ok": False,
            "error_code": "MALFORMED_FRAME",
            "spoken": "The camera frame could not be transferred. I did not see anything.",
        }
    jpeg, width, height = checked
    observation = CameraObservation(
        request_id=request_id,
        call_id=request_id,
        jpeg=jpeg,
        width=width,
        height=height,
        camera_name=device.name,
    )
    stash_observation(observation)
    ocr_text = None
    labels: list[str] = []
    try:
        from app.vision.providers import get_vision_provider

        vision = get_vision_provider()
        result = await vision.analyze(data=jpeg, content_type="image/jpeg", filename="phone.jpg")
        ocr_text = (getattr(result, "ocr_text", None) or "")[:280] or None
        derived = getattr(result, "labels", None) or []
        labels = [str(item)[:48] for item in derived[:8]]
    except Exception:
        ocr_text = None

    persisted = False
    spoken = "I have the current camera frame from this iPhone."
    if ocr_text:
        spoken = f"I can read: {ocr_text}"
    elif labels:
        spoken = "I can see " + ", ".join(labels[:4]) + "."
    if not is_sandbox_device(device) and device.revoked_at is None:
        from app.everywhere.sync import emit_everywhere_event

        await emit_everywhere_event(
            session,
            event_type="camera.look",
            actor_label=f"device:{device.name}",
            content={
                "request_id": request_id,
                "device_id": str(device.id),
                "action": action,
                "bytes": len(jpeg),
                "ocr_text": ocr_text,
                "labels": labels,
                "provenance": "phone_camera",
                "observed_at": utcnow().isoformat(),
            },
            device_id=str(device.id),
            privacy_level="normal",
        )
        try:
            from app.memory.visual import persist_visual_observation

            await persist_visual_observation(
                session,
                {
                    "ok": True,
                    "request_id": request_id,
                    "labels": labels,
                    "ocr_text": ocr_text,
                    "spoken": spoken,
                    "media_kind": "frame" if action in {"look", "look_once", "observe"} else action,
                    "visual_facts": "phone_camera",
                },
                actor=f"device:{device.name}",
                device_id=str(device.id),
            )
        except Exception:
            pass
        persisted = True
    return {
        "ok": True,
        "request_id": request_id,
        "target_device_id": str(device.id),
        "ocr_text": ocr_text,
        "labels": labels,
        "observation_id": request_id,
        "persisted_to_memory_os": persisted,
        "provenance": "phone_camera",
        "spoken": spoken,
    }
