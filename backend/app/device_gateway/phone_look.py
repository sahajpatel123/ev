"""Phone camera frames enter the owner vision path with provenance."""

from __future__ import annotations

import base64
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.camera_runtime import CameraObservation, stash_observation, validate_jpeg
from app.models import Device
from app.utils.text import utcnow

from .sandbox import is_sandbox_device

# Truthful media kinds for phone camera requests. A browser that can only send
# stills must never be recorded as a video.
_PHONE_KIND_BY_ACTION = {
    "look": "frame",
    "look_once": "frame",
    "once": "frame",
    "capture": "frame",
    "observe": "frame",
    "capture_photo": "photo",
    "capture_save": "photo",
    "photo": "photo",
    "record": "burst",
    "record_clip": "burst",
    "record_video": "burst",
}


# A browser burst is a handful of stills over a few seconds; the client sends
# ``captured_at_ms`` offsets, so the timeline is the client's, not a guess.
_BURST_MAX_FRAMES = 6
_BURST_SPAN_MS = 4000


def _phone_media_kind(
    action: str,
    *,
    requested: str | None = None,
    has_clip: bool | None = None,
) -> str:
    """Never claim a kind the phone did not produce (stills stay bursts).

    The client's own ``media_kind`` is honoured only when it is truthful: a
    declared ``video`` without ``has_clip`` is downgraded to a burst.
    """

    declared = (requested or "").strip().lower()
    if declared in {"video", "clip", "movie", "recording"}:
        return declared if has_clip else "burst"
    if declared in {"burst", "frame", "observe", "photo", "image"}:
        return "photo" if declared in {"photo", "image"} else declared
    return _PHONE_KIND_BY_ACTION.get((action or "").strip().lower(), "frame")


async def _read_frame(jpeg: bytes) -> tuple[str | None, list[str], str | None, bool]:
    """Local OCR (+ labels) for one phone frame. Failure is not fatal."""

    try:
        from app.vision.providers import get_vision_provider

        provider = get_vision_provider()
        result = await provider.analyze(data=jpeg, content_type="image/jpeg", filename="phone.jpg")
        ocr = (getattr(result, "ocr_text", None) or "")[:280] or None
        derived = getattr(result, "labels", None) or []
        labels = [str(item)[:48] for item in derived[:8]]
        return (
            ocr,
            labels,
            str(getattr(result, "provider", "") or "") or None,
            bool(getattr(result, "degraded", False)),
        )
    except Exception:
        return None, [], None, True


async def _burst_moments(
    frames: list[dict[str, Any]],
    media_kind: str | None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Read every burst frame into a bounded timeline of moments."""

    if len(frames) < 2:
        return [], [], []
    kind = str(media_kind or "").strip().lower()
    if kind in {"video", "clip", "movie", "recording"}:
        # A real clip is processed from its bytes, not from posters.
        return [], [], []
    moments: list[dict[str, Any]] = []
    ocr: list[str] = []
    labels: list[str] = []
    total = min(len(frames), _BURST_MAX_FRAMES)
    for index, frame in enumerate(frames[:total]):
        try:
            payload = base64.b64decode(str(frame.get("jpeg_b64") or ""))
        except Exception:
            continue
        checked = validate_jpeg(payload)
        if checked is None:
            continue
        payload, _, _ = checked
        frame_ocr, frame_labels, _, _ = await _read_frame(payload)
        captured = frame.get("captured_at_ms")
        try:
            start_ms = (
                max(0, int(captured))
                if captured is not None
                else int(_BURST_SPAN_MS * index / max(total, 1))
            )
        except (TypeError, ValueError):
            start_ms = int(_BURST_SPAN_MS * index / max(total, 1))
        end_ms = int(_BURST_SPAN_MS * (index + 1) / max(total, 1))
        moment: dict[str, Any] = {
            "t_start": round(start_ms / 1000.0, 2),
            "t_end": round(max(end_ms, start_ms + 1) / 1000.0, 2),
            "labels": frame_labels,
            "colors": [],
        }
        if frame_ocr:
            moment["ocr_text"] = frame_ocr[:400]
            if frame_ocr not in ocr:
                ocr.append(frame_ocr)
        for name in frame_labels:
            if name not in labels:
                labels.append(name)
        moments.append(moment)
    return moments, ocr, labels


async def ingest_phone_frame(
    session: AsyncSession,
    *,
    device: Device,
    request_id: str,
    jpeg_b64: str,
    action: str = "look",
    frames: list[dict[str, Any]] | None = None,
    media_kind: str | None = None,
    has_clip: bool | None = None,
    note: str | None = None,
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
    ocr_text, labels, engine, degraded = await _read_frame(jpeg)
    moments, extra_ocr, extra_labels = await _burst_moments(frames or [], media_kind)
    printed = [value for value in [ocr_text, *extra_ocr] if value]
    if printed:
        ocr_text = " ".join(dict.fromkeys(printed))[:280]
    for name in extra_labels:
        if name not in labels:
            labels.append(name)

    # The server holds the JPEG: run real local perception (RT-DETR objects +
    # YuNet faces + consented roster match). Never raises into the frame path.
    from app.ev.look import local_perception

    perception = await local_perception(session, jpeg)
    for name in perception["labels"]:
        if name not in labels:
            labels.append(name)
    labels = labels[:8]
    people_matches = list(perception["people_matches"])
    person_names = [
        str(item.get("label"))
        for item in people_matches
        if item.get("label") and not item.get("unknown")
    ]

    persisted = False
    enrolled_names: list[str] = []
    spoken = "I have the current camera frame from this iPhone."
    if ocr_text:
        spoken = f"I can read: {ocr_text}"
    elif labels:
        spoken = "I can see " + ", ".join(labels[:4]) + "."
    if person_names:
        # An enrolled match stays a pending suggestion until the owner confirms.
        spoken = (
            spoken.rstrip()
            + " Possible person match: "
            + ", ".join(person_names[:3])
            + " (pending confirmation)."
        ).strip()
    if action == "remember":
        # Cycle 68 — recognition against the enrolled roster: if the keep
        # note names an enrolled person, say so. No biometrics: the OWNER
        # names who it is; Evie remembers who owns the name.
        wanted = (note or "").strip()
        enrolled_names = []
        if wanted:
            from sqlalchemy import select as _select
            from app.models import Entity as _Entity

            wanted_low = wanted.casefold()
            try:
                rows = (
                    await session.execute(_select(_Entity).where(_Entity.entity_type == "person"))
                ).scalars().all()
                enrolled_names = [r.name for r in rows if r.name and r.name.casefold() == wanted_low]
            except Exception:
                enrolled_names = []
        spoken = "Kept. I'll remember this."
        if enrolled_names:
            spoken = f"Kept — {enrolled_names[0]}. I'll remember this."
    if action != "remember":
        if ocr_text:
            spoken = f"I can read: {ocr_text}"
        elif labels:
            spoken = "I can see " + ", ".join(labels[:4]) + "."
    kind = _phone_media_kind(action, requested=media_kind, has_clip=has_clip)
    stored_attachment_id: str | None = None
    if not is_sandbox_device(device) and device.revoked_at is None:
        from app.everywhere.sync import emit_everywhere_event

        pending_attachment_id = uuid4() if _store_pixels_for(kind) else None
        storage_key: str | None = None
        if pending_attachment_id is not None:
            try:
                from app.storage.object_store import get_object_store

                storage_key = f"attachments/{pending_attachment_id}.bin"
                await get_object_store().put(storage_key, jpeg, "image/jpeg")
            except Exception:
                pending_attachment_id = None
                storage_key = None
        look_event = await emit_everywhere_event(
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
                "media_kind": kind,
                "moment_count": len(moments) or None,
                "attachment_id": str(pending_attachment_id) if pending_attachment_id else None,
                "provenance": "phone_camera",
                "observed_at": utcnow().isoformat(),
            },
            device_id=str(device.id),
            privacy_level="normal",
        )
        if pending_attachment_id is not None and storage_key:
            try:
                from app.models import Attachment
                from app.storage.object_store import sha256_bytes

                await session.flush()
                attachment = Attachment(
                    id=pending_attachment_id,
                    event_id=look_event.id,
                    filename="phone-look.jpg",
                    content_type="image/jpeg",
                    size_bytes=len(jpeg),
                    storage_key=storage_key,
                    sha256=sha256_bytes(jpeg),
                )
                session.add(attachment)
                await session.flush()
                stored_attachment_id = str(attachment.id)
            except Exception:
                stored_attachment_id = None
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
                    "media_kind": kind,
                    "visual_facts": "phone_camera",
                    # The bytes were received here, so this is grounded even when
                    # the on-device classifier found nothing.
                    "encoded_bytes": len(jpeg),
                    "image_ready": True,
                    "frames": max(1, len(frames or [])),
                    "moments": moments,
                    "people_matches": people_matches,
                    "attachment_id": stored_attachment_id,
                    "observed": True,
                    # Cycle 67 — "remember this": an explicit owner keep.
                    "keep_request": (note or "remember this") if action == "remember" else None,
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
        "recognized_person": enrolled_names[0] if enrolled_names else None,
        "ocr_text": ocr_text,
        "labels": labels,
        "observation_id": request_id,
        "media_kind": kind,
        "moments": moments,
        "frame_count": max(1, len(frames or [])),
        "engine": engine,
        "degraded": degraded,
        "persisted_to_memory_os": persisted,
        "provenance": "phone_camera",
        "spoken": spoken,
    }


def _store_pixels_for(kind: str) -> bool:
    """Photos always keep their pixels; looks only when the owner opts in."""

    if kind == "photo":
        return True
    try:
        from app.vision.settings import get_vision_settings

        return bool(get_vision_settings().vision_store_look_pixels)
    except Exception:  # noqa: BLE001 - settings are optional in tooling
        return False
