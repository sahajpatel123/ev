"""Camera look: capture a real frame and hand pixels to the live model.

Live voice uses the connected Mac camera. The Realtime model must receive the
JPEG itself. On-device OCR is optional metadata, never a substitute for vision.
Raw frames are not persisted unless the owner already supplied an attachment.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatMessage, RequestEnvelope
from app.ev.camera_runtime import (
    OBSERVE_DEFAULT_INTERVAL,
    OBSERVE_MAX_FRAMES,
    CameraObservation,
    clamp_observe_duration,
    clamp_record_duration,
    lighting_from_luminance,
    log_camera,
    looks_like_dark_excuse,
    now_mono,
    stash_observation,
    validate_jpeg,
)
from app.ev.workbench import hud_card
from app.gateway.providers import get_chat_provider
from app.gateway.service import ModelGateway
from app.models import Attachment, Entity, OwnerObject, RecognitionLog
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.storage.object_store import get_object_store, sha256_bytes
from app.vision.camera import CameraPermissionDeniedError, capture_once
from app.vision.providers import VisionBinaryError, VisionEngineError, VisionProviderError

logger = logging.getLogger("ev.look")

LOOK_TIMEOUT_SECONDS = 12.0
KEEP_ANALYZE_TIMEOUT_SECONDS = 14.0
OCR_SNIPPET = 280
MAX_LABELS = 8

UNAVAILABLE_SPOKEN = (
    "I can't see a camera frame right now. No camera source is currently connected."
)
DENIED_SPOKEN = (
    "I can't access the camera because macOS hasn't granted EV camera access."
)
MALFORMED_SPOKEN = "The camera frame could not be transferred. I did not see anything."
TIMEOUT_SPOKEN = "The camera did not return a frame in time. I did not see anything."
LIVE_CAPTURED_SPOKEN = (
    "A current camera image is attached. Describe what you actually see in "
    "two or three natural sentences: people, clothing and its colors, pose, "
    "held objects, and the setting. Name garment and object colors from the "
    "image itself. Do not hedge when a person, garment, or object is visible. "
    "Labels may miss objects; the image is the source of truth. If they asked "
    "you to remember a name or title, read printed text on what they are showing. "
    "Mention printed text when it is visible. Do not name people unless "
    "enrolled."
)
OBSERVE_CAPTURED_SPOKEN = (
    "Bounded camera images are attached. Describe people, clothing, objects, "
    "colors, and what changed across those frames in natural speech. Name "
    "garment and object colors from the images. Listed colors are scene "
    "hints, not a reason to refuse."
)
RECORD_TIMEOUT_SPOKEN = (
    "The camera did not finish recording a clip in time, so I have no video "
    "to work with."
)
FOLLOW_UP_HINT = (
    "Later questions about this image or clip should keep describing people, "
    "clothing, objects, colors, and the scene from the attached frames. "
    "Missing printed text is not a defect. Do not say the image is dark or "
    "unreadable when people, objects, or colors are visible. Do not reduce "
    "the answer to 'I saved a file'. This look is stored as memory. If they "
    "asked you to remember what they showed, say you will remember it. Never "
    "say you cannot guarantee future recall."
)
KEEP_STORED_HINT = (
    "This look is stored as durable memory across app restarts. Tell the "
    "owner you will remember what they showed. Never say you cannot guarantee "
    "future recall, cannot memorize a glance, or that you have no reliable "
    "record of what they just asked you to remember."
)
DEFAULT_LOOK_PROMPT = (
    "Describe visible people, objects, colors, and the scene. Mention printed "
    "text only if it is actually readable. Do not name people unless enrolled."
)
KEEP_LOOK_PROMPT = (
    "Name the main thing they are showing in two short sentences: what it is, "
    "its colors, any printed text, and one distinctive detail. Do not say you "
    "can see it now, do not repeat their request, and do not name people "
    "unless enrolled."
)
KEEP_CAPTURED_SPOKEN = (
    "A current camera image is attached. Name the main thing they are showing "
    "in two short sentences: what it is, colors, any printed text, and one "
    "distinctive detail. Do not say you can see it now. Do not repeat their "
    "request. Do not name people unless enrolled."
)


def _card(spoken: str, meta: dict[str, Any]) -> dict:
    return hud_card("Look", spoken, meta, priority=0.7)


async def store_frame_attachment(
    session: AsyncSession,
    data: bytes,
    *,
    actor: str,
    device_id: str | None = None,
    filename: str = "look.jpg",
    content_type: str = "image/jpeg",
) -> Attachment:
    """Persist one look frame as a normal attachment. Raw pixels stay in object storage."""

    store = get_object_store()
    storage_key = f"attachments/{uuid4()}.bin"
    await store.put(storage_key, data, content_type)
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="camera",
            event_type="camera.look",
            content={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
                "storage_key": storage_key,
            },
            metadata={"look": True, "persist_raw_default": False},
            device_id=device_id,
            privacy_level="normal",
        )
    )
    attachment = Attachment(
        event_id=event.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=storage_key,
        sha256=sha256_bytes(data),
    )
    session.add(attachment)
    await session.flush()
    return attachment


async def _capture_local_frame(
    session: AsyncSession,
    *,
    actor: str,
    device_id: str | None,
) -> tuple[Attachment | None, str | None]:
    """Best-effort same-machine capture through the evvision helper."""

    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return None, None
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        result = await capture_once(
            persist=True,
            output_path=tmp_path,
            consent_reason="look tool explicit request",
        )
        if not result.captured or not tmp_path:
            return None, UNAVAILABLE_SPOKEN
        data = Path(tmp_path).read_bytes()
        if not data:
            return None, UNAVAILABLE_SPOKEN
        attachment = await store_frame_attachment(
            session,
            data,
            actor=actor,
            device_id=device_id,
            filename="look-local.jpg",
            content_type="image/jpeg",
        )
        return attachment, None
    except CameraPermissionDeniedError:
        return None, DENIED_SPOKEN
    except (VisionBinaryError, VisionEngineError, VisionProviderError, OSError) as exc:
        logger.info("local look capture unavailable: %s", exc)
        return None, None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


async def _wait_for_live_frame(
    *,
    live_session_id: str | None,
    device_id: str | None,
    timeout: float = LOOK_TIMEOUT_SECONDS,
    request_id: str | None = None,
    detail: str | None = None,
    action: str = "capture",
):
    from app.ev.camera_runtime import LookFrame
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(live_session_id) or live_for_device(device_id)
    if live is None:
        return None, None
    try:
        frame = await live.request_look_frame(
            timeout=timeout,
            request_id=request_id,
            detail=detail,
            action=action,
        )
        return live, frame
    except Exception:  # noqa: BLE001 - live capture is optional
        logger.info("live look frame request failed", exc_info=True)
        return live, LookFrame(request_id=request_id or "", error="capture_failed", last=True)


def _spoken_for_capture_error(
    error: str | None,
    permission: str | None = None,
    *,
    purpose: str = "look",
) -> str:
    raw = str(error or "").strip().lower()
    perm = str(permission or "").strip().lower()
    if raw in {"denied", "permission_denied"} or perm in {"denied", "restricted"}:
        return DENIED_SPOKEN
    if raw in {"timeout"}:
        if purpose == "record":
            return RECORD_TIMEOUT_SPOKEN
        return TIMEOUT_SPOKEN
    if raw in {"malformed_image", "empty_frame"}:
        if purpose == "record":
            return "The camera did not return a saved clip."
        return MALFORMED_SPOKEN
    if raw in {"client_disconnected", "disconnected"}:
        return "No camera source is currently connected."
    if raw in {"unavailable", "no_camera"}:
        return "No camera is available on the connected Mac."
    if purpose == "record":
        return "I could not record a video with the camera just now."
    return UNAVAILABLE_SPOKEN


def _frame_labels(frame: Any, extra: list[str] | None = None) -> list[str]:
    names: list[str] = []
    for source in (getattr(frame, "labels", None) or [], extra or []):
        for item in source:
            name = str(item or "").strip()
            if name and name.lower() not in {value.lower() for value in names}:
                names.append(name)
    return names[:8]


def _frame_colors(frame: Any, extra: list[str] | None = None) -> list[str]:
    names: list[str] = []
    for source in (getattr(frame, "colors", None) or [], extra or []):
        for item in source:
            name = str(item or "").strip()
            if name and name.lower() not in {value.lower() for value in names}:
                names.append(name)
    return names[:4]


def _visual_facts(
    *,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
    ocr_text: str | None = None,
    person_count: int | None = None,
    face_count: int | None = None,
) -> str:
    bits: list[str] = []
    people = person_count or 0
    faces = face_count or 0
    if people > 0:
        bits.append("a person" if people == 1 else f"{people} people")
    elif faces > 0:
        bits.append("a person" if faces == 1 else f"{faces} people")
    for name in labels or []:
        lowered = name.lower()
        if lowered in {"person", "people", "human"} and bits:
            continue
        if name:
            bits.append(name)
    if colors:
        bits.append("colors: " + ", ".join(colors[:4]))
    ocr = " ".join(str(ocr_text or "").split())
    if ocr:
        bits.append("text: " + ocr[:120])
    return "; ".join(bits[:8])


def _spoken_from_frame(
    *,
    purpose: str,
    frame: Any | None = None,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
    ocr_text: str | None = None,
    lighting: str | None = None,
    saved_path: str | None = None,
    duration_s: float | None = None,
    width: int | None = None,
    height: int | None = None,
    people: int | None = None,
    faces: int | None = None,
    keep_request: str | None = None,
) -> str:
    """Ask the live model to describe attached frames; facts are grounding only."""

    labels = [name for name in (labels or []) if name]
    colors = [name for name in (colors or _frame_colors(frame)) if name]
    ocr = " ".join(str(ocr_text or "").split())[:OCR_SNIPPET]
    lighting_value = str(lighting or "").strip() or None
    face_count = (
        faces if faces is not None else (getattr(frame, "face_count", None) if frame is not None else None)
    )
    person_count = (
        people
        if people is not None
        else (getattr(frame, "person_count", None) if frame is not None else None)
    )
    luminance = getattr(frame, "luminance", None) if frame is not None else None
    if lighting_value is None:
        lighting_value = lighting_from_luminance(luminance)
    facts = _visual_facts(
        labels=labels,
        colors=colors,
        ocr_text=ocr,
        person_count=person_count,
        face_count=face_count,
    )
    grounding = f" Grounding: {facts}." if facts else ""
    if purpose == "capture":
        save = (
            f" After the description, mention the photo is saved to {saved_path}."
            if saved_path
            else " After the description, mention the photo is saved."
        )
        spoken = (
            "A photo you just took is attached. Describe it in two or three "
            "natural sentences: people, clothing, pose, objects, colors, and "
            "the setting. Do not only list labels or say that you saved a file."
            + grounding
            + save
        )
    elif purpose == "record":
        seconds = duration_s if duration_s and duration_s > 0 else None
        if saved_path and seconds:
            save = (
                f" After the description, mention the {seconds:.0f}-second "
                f"clip is saved to {saved_path}."
            )
        elif saved_path:
            save = f" After the description, mention the recorded clip is saved to {saved_path}."
        else:
            save = " After the description, mention the recorded clip is saved."
        spoken = (
            "Frames from the video you just recorded are attached. Describe "
            "the clip in two or three natural sentences: who is in it, "
            "clothing, objects, colors, and what they are doing. Do not only "
            "say that you saved a file."
            + grounding
            + save
        )
    elif purpose == "observe":
        spoken = OBSERVE_CAPTURED_SPOKEN + grounding
        if width and height:
            spoken += f" Image {width} by {height}."
    else:
        from app.memory.visual import wants_keep_visible

        spoken = (
            KEEP_CAPTURED_SPOKEN if wants_keep_visible(str(keep_request or "")) else LIVE_CAPTURED_SPOKEN
        ) + grounding
        if width and height:
            spoken += f" Image {width} by {height}."
    spoken = spoken.strip()
    if looks_like_dark_excuse(spoken) and lighting_value and lighting_value != "dim":
        spoken = spoken.replace("too dark", lighting_value).replace("too dim", lighting_value)
    return spoken[:1100] or LIVE_CAPTURED_SPOKEN


def _observe_spoken(summaries: list[dict[str, Any]], *, duration_s: float) -> str:
    """Compare bounded frames instead of collapsing them into one last guess."""

    if not summaries:
        return OBSERVE_CAPTURED_SPOKEN
    if len(summaries) == 1:
        item = summaries[0]
        return _spoken_from_frame(
            purpose="observe",
            labels=list(item.get("labels") or []),
            colors=list(item.get("colors") or []),
            ocr_text=item.get("ocr_text"),
            width=item.get("width"),
            height=item.get("height"),
        )
    first = summaries[0]
    last = summaries[-1]
    parts = [f"I watched {len(summaries)} frames over {duration_s:.0f} seconds."]

    def _clause(title: str, item: dict[str, Any]) -> str:
        bits: list[str] = []
        labels = [name for name in (item.get("labels") or []) if name]
        colors = [name for name in (item.get("colors") or []) if name]
        people = int(item.get("person_count") or item.get("face_count") or 0)
        if people > 0:
            bits.append("a person" if people == 1 else f"{people} people")
        bits.extend(labels[:4])
        if colors:
            bits.append("colors " + ", ".join(colors[:3]))
        ocr = " ".join(str(item.get("ocr_text") or "").split())
        if ocr:
            bits.append("text " + ocr[:80])
        if not bits:
            return f"{title}: scene attached."
        return f"{title}: " + ", ".join(bits) + "."

    parts.append(_clause("First", first))
    parts.append(_clause("Last", last))
    first_colors = [name.lower() for name in (first.get("colors") or [])]
    last_colors = [name.lower() for name in (last.get("colors") or [])]
    first_labels = {name.lower() for name in (first.get("labels") or [])}
    last_labels = {name.lower() for name in (last.get("labels") or [])}
    changes: list[str] = []
    if first_colors and last_colors and first_colors != last_colors:
        changes.append(
            "colors from " + ", ".join(first.get("colors") or [])
            + " to "
            + ", ".join(last.get("colors") or [])
        )
    added = [name for name in last_labels if name not in first_labels]
    removed = [name for name in first_labels if name not in last_labels]
    if added:
        changes.append("now also " + ", ".join(sorted(added)[:4]))
    if removed:
        changes.append("no longer " + ", ".join(sorted(removed)[:4]))
    if changes:
        parts.append("Changed: " + "; ".join(changes) + ".")
    else:
        parts.append("The scene stayed similar across those frames.")
    return " ".join(parts)[:800]


def _stash_frame(
    *,
    call_id: str | None,
    request_id: str,
    jpeg: bytes,
    width: int | None,
    height: int | None,
    camera_name: str | None,
    detail: str,
    t0: float,
    sequence: int = 0,
) -> None:
    if not call_id or not jpeg:
        return
    stash_observation(
        CameraObservation(
            request_id=request_id,
            call_id=str(call_id),
            jpeg=jpeg,
            width=width,
            height=height,
            detail=detail if detail in {"auto", "low", "high"} else "high",
            camera_name=camera_name,
            sequence=sequence,
            t0=t0,
            t4=now_mono(),
        )
    )


async def _jpeg_from_attachment(session: AsyncSession, attachment: Attachment) -> bytes | None:
    try:
        data = await get_object_store().get(attachment.storage_key)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    if attachment.content_type == "image/jpeg" or data.startswith(b"\xff\xd8"):
        validated = validate_jpeg(data)
        return validated[0] if validated else data
    return data


async def _resolve_attachment(
    session: AsyncSession,
    attachment_id: UUID,
) -> Attachment:
    row = await session.get(Attachment, attachment_id)
    if row is None:
        raise KeyError(f"Attachment {attachment_id} not found")
    return row


def _label_names(labels: list[dict]) -> list[str]:
    names: list[str] = []
    for item in labels:
        name = str(item.get("label") or "").strip()
        if name and name.lower() not in {value.lower() for value in names}:
            names.append(name)
    return names[:MAX_LABELS]


async def _enrolled_object_matches(
    session: AsyncSession,
    *,
    labels: list[str],
    ocr_text: str,
) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.execute(
                select(OwnerObject).where(
                    OwnerObject.status == "active",
                    OwnerObject.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    haystack = " ".join([ocr_text, *labels]).lower()
    matches: list[dict[str, Any]] = []
    for row in rows:
        name = (row.name or "").strip()
        if len(name) < 2:
            continue
        needle = name.lower()
        if needle in haystack or any(needle in label.lower() for label in labels):
            matches.append(
                {
                    "id": str(row.id),
                    "name": name,
                    "object_type": row.object_type,
                    "matched": "label_or_text",
                }
            )
    return matches[:MAX_LABELS]


async def _confirmed_recognition_matches(
    session: AsyncSession,
    *,
    labels: list[str],
    ocr_text: str,
) -> list[str]:
    rows = list(
        (
            await session.execute(
                select(RecognitionLog).where(RecognitionLog.source == "user").limit(80)
            )
        )
        .scalars()
        .all()
    )
    haystack = " ".join([ocr_text, *labels]).lower()
    found: list[str] = []
    for row in rows:
        label = (row.label or "").strip()
        if len(label) < 2:
            continue
        if label.lower() in haystack and label not in found:
            found.append(label)
    return found[:MAX_LABELS]


async def _roster_text_matches(
    session: AsyncSession,
    ocr_text: str,
) -> list[str]:
    if not ocr_text.strip():
        return []
    people = list(
        (
            await session.execute(
                select(Entity).where(Entity.entity_type == "person").limit(40)
            )
        )
        .scalars()
        .all()
    )
    haystack = ocr_text.lower()
    names: list[str] = []
    for person in people:
        name = (person.name or "").strip()
        if len(name) < 2:
            continue
        if name.lower() in haystack and name not in names:
            names.append(name)
    return names[:MAX_LABELS]


async def _match_enrolled_faces(
    session: AsyncSession,
    data: bytes,
    *,
    attachment_id: UUID,
) -> list[dict[str, Any]]:
    """Match detected crops against the consented roster. Unknown stays unknown."""

    from app.vision.face import aligned_crop, create_face_detector

    detector = create_face_detector()
    detection = await detector.detect(data, "image/jpeg")
    if detection.degraded or not detection.faces:
        return [
            {
                "label": None,
                "unknown": True,
                "count": len(detection.faces),
                "degraded": detection.degraded,
                "engine": detection.engine,
            }
        ] if detection.faces else []

    try:
        from app.people.face_embed import FaceCrop
        from app.people.resolver import FaceResolver
    except Exception:  # noqa: BLE001 - roster is optional
        return [{"label": None, "unknown": True, "count": len(detection.faces)}]

    matches: list[dict[str, Any]] = []
    try:
        resolver = FaceResolver(session, master_key=settings.master_key)
        for face in detection.faces[:4]:
            crop_bytes = aligned_crop(data, face)
            if not crop_bytes:
                continue
            crop = FaceCrop(
                image_b64=base64.b64encode(crop_bytes).decode("ascii"),
                confidence=float(face.get("score") or 0.0),
                source="camera.look",
                attachment_id=str(attachment_id),
            )
            result = await resolver.recognize(crop, write_log=True)
            if result.resolved and result.label and not result.degraded:
                matches.append(
                    {
                        "label": result.label,
                        "unknown": False,
                        "confidence": result.confidence,
                        "entity_id": str(result.entity_id) if result.entity_id else None,
                    }
                )
            else:
                matches.append(
                    {
                        "label": None,
                        "unknown": True,
                        "confidence": result.confidence,
                        "degraded": result.degraded,
                    }
                )
    except Exception:  # noqa: BLE001 - never fail a look on roster errors
        logger.info("enrolled face match skipped", exc_info=True)
        return [{"label": None, "unknown": True, "count": len(detection.faces)}]
    return matches or [{"label": None, "unknown": True, "count": len(detection.faces)}]


def _compose_spoken(
    *,
    summary: str,
    ocr_text: str,
    labels: list[str],
    things: list[dict[str, Any]],
    people: list[dict[str, Any]],
    roster_text: list[str],
    confirmed: list[str],
    focus: str,
    keep: bool = False,
) -> str:
    from app.memory.visual import is_empty_visual_scene, is_memory_hedge_scene

    parts: list[str] = []
    named_people = [item["label"] for item in people if item.get("label")]
    unknown_people = sum(1 for item in people if item.get("unknown") and not item.get("label"))
    thing_names = [item["name"] for item in things if item.get("name")]
    cleaned = (summary or "").strip()
    lowered = cleaned.lower()
    usable_summary = bool(
        cleaned
        and "blocked" not in lowered
        and "without a provider" not in lowered
        and "describe the people, objects" not in lowered
        and not is_empty_visual_scene(cleaned)
        and not is_memory_hedge_scene(cleaned)
    )
    if usable_summary:
        parts.append(cleaned.split("\n")[0][:400].rstrip("."))
    elif keep and thing_names:
        parts.append("I can see " + ", ".join(thing_names))
    elif keep and labels:
        useful = [
            name
            for name in labels[:5]
            if name.lower() not in {"person", "people", "human", "adult", "structure"}
        ]
        if useful:
            parts.append("I can see " + ", ".join(useful))
    elif focus in {"auto", "objects"} and thing_names:
        parts.append("I can see " + ", ".join(thing_names) + ".")
    elif focus in {"auto", "objects"} and labels:
        parts.append("Visible: " + ", ".join(labels[:5]) + ".")

    if focus in {"auto", "text"} and ocr_text.strip():
        snippet = " ".join(ocr_text.split())[:OCR_SNIPPET]
        if not any(snippet.lower() in part.lower() for part in parts):
            parts.append(f"Text reads: {snippet}.")

    if focus in {"auto", "people"} and not keep:
        if named_people:
            parts.append("Enrolled match: " + ", ".join(named_people) + ".")
        elif roster_text:
            parts.append("The text mentions " + ", ".join(roster_text) + ".")
        elif unknown_people == 1:
            parts.append("I see a person. I will not name them without an enrolled match.")
        elif unknown_people > 1:
            parts.append(
                f"I see {unknown_people} people. Unenrolled faces stay unnamed."
            )

    if confirmed and focus in {"auto", "objects", "text"}:
        extra = [name for name in confirmed if name not in thing_names]
        if extra:
            parts.append("Previously confirmed: " + ", ".join(extra[:4]) + ".")

    if not parts:
        if usable_summary:
            parts.append(cleaned.split("\n")[0][:400])
        else:
            parts.append(
                "I looked at the image. Describe the people, objects, colors, "
                "and scene. Missing printed text is not a failure."
            )
    line = ". ".join(part.rstrip(".") for part in parts if part).strip()
    if not line.endswith("."):
        line += "."
    return line[:800]


async def _polish_spoken(draft: str, payload: dict[str, Any]) -> str:
    """Optional DeepSeek wording pass over derived facts. Never sends pixels."""

    provider = get_chat_provider()
    if getattr(provider, "name", "") in {"echo", "mock"} or not getattr(provider, "api_key", True):
        return draft
    if provider.name == "deepseek" and not settings.deepseek_api_key:
        return draft
    facts = {
        "ocr_text": (payload.get("ocr_text") or "")[:OCR_SNIPPET],
        "labels": payload.get("labels") or [],
        "things": payload.get("things") or [],
        "people": payload.get("people") or [],
    }
    try:
        envelope = RequestEnvelope(
            request_id=str(uuid4()),
            strategy={"mode": "look"},
            metadata={"privacy_level": "normal", "raw_sent": False},
        )
        call = await ModelGateway(provider).chat(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "You are EV's camera look layer. Rewrite the draft into one or two "
                        "short spoken sentences. Use only the supplied facts. Do not invent "
                        "people, brands, or locations. Do not name a person unless they are "
                        "listed as an enrolled match. Do not mention DeepSeek, Grok, or OpenAI."
                        + (
                            " Name the main object, its colors, printed text, and one "
                            "distinctive detail. Do not say you can see it now or repeat "
                            "the owner's request."
                            if payload.get("keep")
                            else ""
                        )
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=f"Draft: {draft}\nFacts: {facts}",
                ),
            ],
            envelope=envelope,
        )
        text = (call.result.text or "").strip().split("\n")[0].strip()
        return text[:800] if text else draft
    except Exception:  # noqa: BLE001 - wording polish is optional
        logger.info("look wording polish skipped", exc_info=True)
        return draft


def _live_image_result(
    *,
    request_id: str,
    source: str,
    width: int | None,
    height: int | None,
    encoded_bytes: int,
    camera_name: str | None,
    focus: str,
    frames: int = 1,
    ocr_text: str | None = None,
    labels: list[str] | None = None,
    observe: bool = False,
    spoken: str | None = None,
    lighting: str | None = None,
    saved_path: str | None = None,
    persist_raw: bool = False,
    attachment_id: str | None = None,
    media_kind: str | None = None,
    duration_s: float | None = None,
    luminance: float | None = None,
    face_count: int | None = None,
    person_count: int | None = None,
    colors: list[str] | None = None,
    frames_summary: list[dict[str, Any]] | None = None,
    keep_request: str | None = None,
) -> dict[str, Any]:
    text = (spoken or (OBSERVE_CAPTURED_SPOKEN if observe else LIVE_CAPTURED_SPOKEN)).strip()
    facts = _visual_facts(
        labels=labels,
        colors=colors,
        ocr_text=ocr_text,
        person_count=person_count,
        face_count=face_count,
    )
    summary = facts or text
    if saved_path:
        summary = f"{facts}. Saved to {saved_path}." if facts else f"Saved to {saved_path}."
    result = {
        "ok": True,
        "spoken": text,
        "summary": summary,
        "image_ready": True,
        "model_image_delivered": False,
        "persist_raw": persist_raw,
        "raw_sent": False,
        "request_id": request_id,
        "source": source,
        "width": width,
        "height": height,
        "encoded_bytes": encoded_bytes,
        "camera_name": camera_name,
        "focus": focus,
        "frames": frames,
        "labels": labels or [],
        "colors": colors or [],
        "visual_facts": facts or None,
        "describe_attached": True,
        "follow_up": FOLLOW_UP_HINT,
        "lighting": lighting,
        "luminance": luminance,
        "face_count": face_count,
        "person_count": person_count,
        "saved_path": saved_path,
        "media_kind": media_kind,
        "observe": observe,
        "duration_s": duration_s,
        "keep_request": (keep_request or "").strip()[:400] or None,
        "hud": _card(
            summary,
            {
                "ok": True,
                "source": source,
                "request_id": request_id,
                "width": width,
                "height": height,
                "frames": frames,
                "saved_path": saved_path,
            },
        ),
    }
    ocr = (ocr_text or "").strip()
    if ocr:
        result["local_ocr"] = ocr[:OCR_SNIPPET]
        result["ocr_text"] = ocr[:2000]
        try:
            from app.ev.desk_scene import bind_visible_text

            bind_visible_text(ocr)
        except Exception:
            pass
    if frames_summary:
        result["frames_summary"] = frames_summary
    if attachment_id:
        result["attachment_id"] = attachment_id
    hud_meta = result["hud"].get("meta") if isinstance(result.get("hud"), dict) else None
    if isinstance(hud_meta, dict):
        hud_meta["visor"] = True
        hud_meta["media_kind"] = media_kind
        hud_meta["saved_path"] = saved_path
    return result


def live_owner_transcript(
    live_session_id: str | None = None,
    device_id: str | None = None,
) -> str:
    """Owner's latest live utterance, even if message.user is not committed yet."""

    try:
        from app.voice.live.layer import live_for_device, live_for_session

        live = live_for_session(live_session_id) or live_for_device(
            str(device_id) if device_id else None
        )
    except Exception:
        return ""
    grok = getattr(live, "grok_voice", None) if live is not None else None
    return str(getattr(grok, "_last_input_transcript", "") or "").strip()[:400]


def _frame_was_delivered(result: dict[str, Any] | None) -> bool:
    """True when a real JPEG/attachment made it through, not a blank look."""

    body = result or {}
    try:
        encoded = int(body.get("encoded_bytes") or 0)
    except (TypeError, ValueError):
        encoded = 0
    return bool(
        body.get("attachment_id")
        or encoded > 0
        or body.get("image_ready")
        or body.get("image_delivered")
        or body.get("model_image_delivered")
    )


def resolve_keep_request(
    prompt: str | None,
    *,
    live_session_id: str | None = None,
    device_id: str | None = None,
) -> str:
    """Bind a keep-from-sight request to this look, not a later DB read."""

    for text in (
        " ".join(str(prompt or "").split()).strip(),
        live_owner_transcript(live_session_id, device_id),
    ):
        if not text:
            continue
        if text.lower().startswith("describe visible people"):
            continue
        return text[:400]
    return ""


def _look_vision_prompt(keep_request: str) -> str:
    from app.memory.visual import keep_topic, wants_keep_visible

    if not wants_keep_visible(keep_request):
        return DEFAULT_LOOK_PROMPT
    named = keep_topic(keep_request)
    if named and named.lower() not in {"this", "that", "it", "you"}:
        return (
            f"Name the {named} they are showing in two short sentences: what "
            "it is, its colors, any printed text, and one distinctive detail. "
            "Do not say you can see it now. Do not repeat their request. "
            "Do not name people unless enrolled."
        )
    return KEEP_LOOK_PROMPT


async def _finish_vision_result(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    actor: str,
    device_id: str | None = None,
    keep_request: str | None = None,
) -> dict[str, Any]:
    """Remember what she saw, then return the live camera result."""

    asked = " ".join(str(keep_request or result.get("keep_request") or "").split()).strip()
    if asked:
        result["keep_request"] = asked[:400]
    if result.get("ok"):
        try:
            from app.memory.visual import persist_visual_observation

            await persist_visual_observation(
                session, result, actor=actor, device_id=device_id
            )
        except Exception:  # noqa: BLE001
            logger.warning("visual memory persist skipped", exc_info=True)
    if result.get("kept"):
        from app.memory.visual import keep_owner_spoken

        result["spoken"] = keep_owner_spoken(
            scene=result.get("spoken"),
            ocr=result.get("ocr_text") or result.get("local_ocr"),
            labels=list(result.get("labels") or []),
            colors=list(result.get("colors") or []),
            keep_request=asked,
            frame_ok=_frame_was_delivered(result),
        )
        result["summary"] = result["spoken"]
        result["follow_up"] = KEEP_STORED_HINT
        result["memory_note"] = KEEP_STORED_HINT
    return result


async def look_now(
    session: AsyncSession,
    *,
    actor: str = "owner",
    prompt: str | None = None,
    attachment_id: str | None = None,
    focus: str = "auto",
    live_session_id: str | None = None,
    device_id: str | None = None,
    request_id: str | None = None,
    detail: str = "high",
) -> dict[str, Any]:
    """Capture or accept one frame. Live voice stashes JPEG for Realtime injection."""

    t0 = now_mono()
    focus_value = (focus or "auto").strip().lower()
    if focus_value not in {"auto", "text", "objects", "people"}:
        focus_value = "auto"
    detail_value = (detail or "high").strip().lower()
    if detail_value not in {"auto", "low", "high"}:
        detail_value = "high"
    call_id = str(request_id or "").strip() or None
    capture_id = call_id or str(uuid4())
    source = "none"
    attachment: Attachment | None = None
    spoken_error: str | None = None
    live_connected = False
    captured_ocr = ""
    captured_labels: list[str] = []
    log_camera("camera.tool_called", request_id=capture_id, extra={"tool": "look"})
    keep_request = resolve_keep_request(
        prompt, live_session_id=live_session_id, device_id=device_id
    )

    if attachment_id:
        try:
            attachment = await _resolve_attachment(session, UUID(str(attachment_id)))
            source = "attachment"
        except (KeyError, ValueError):
            return {
                "ok": False,
                "spoken": "I don't have that photo.",
                "error": "not_found",
                "hud": _card("I don't have that photo.", {"ok": False}),
            }

    if attachment is None:
        live, frame = await _wait_for_live_frame(
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=capture_id,
            detail=detail_value,
        )
        live_connected = live is not None
        if frame is not None and (frame.jpeg or frame.attachment_id) and not frame.error:
            source = "live_camera"
            if frame.jpeg:
                labels = _frame_labels(frame)
                colors = _frame_colors(frame)
                ocr = getattr(frame, "ocr_text", None)
                lighting = getattr(frame, "lighting", None) or lighting_from_luminance(
                    getattr(frame, "luminance", None)
                )
                spoken = _spoken_from_frame(
                    purpose="look",
                    frame=frame,
                    labels=labels,
                    colors=colors,
                    ocr_text=ocr,
                    lighting=lighting,
                    width=frame.width,
                    height=frame.height,
                    keep_request=keep_request,
                )
                _stash_frame(
                    call_id=call_id,
                    request_id=frame.request_id or capture_id,
                    jpeg=frame.jpeg,
                    width=frame.width,
                    height=frame.height,
                    camera_name=frame.camera_name,
                    detail=detail_value,
                    t0=t0,
                )
                from app.memory.visual import wants_keep_visible

                captured_ocr = " ".join(str(ocr or "").split()).strip()
                captured_labels = list(labels)

                live_image = _live_image_result(
                    request_id=frame.request_id or capture_id,
                    source=source,
                    width=frame.width,
                    height=frame.height,
                    encoded_bytes=len(frame.jpeg),
                    camera_name=frame.camera_name,
                    focus=focus_value,
                    ocr_text=ocr,
                    labels=labels,
                    spoken=spoken,
                    lighting=lighting,
                    luminance=getattr(frame, "luminance", None),
                    face_count=getattr(frame, "face_count", None),
                    person_count=getattr(frame, "person_count", None),
                    colors=colors,
                    media_kind=getattr(frame, "media_kind", None) or "frame",
                    keep_request=keep_request,
                )
                if wants_keep_visible(keep_request):
                    try:
                        attachment = await store_frame_attachment(
                            session,
                            frame.jpeg,
                            actor=actor,
                            device_id=device_id,
                            filename="look-keep.jpg",
                        )
                    except Exception:  # noqa: BLE001 - still persist Mac OCR
                        logger.warning("keep frame store skipped", exc_info=True)
                        attachment = None
                    if attachment is None:
                        return await _finish_vision_result(
                            session,
                            live_image,
                            actor=actor,
                            device_id=device_id,
                            keep_request=keep_request,
                        )
                    # Read the frame here. Mini is cancelled on memorize, so
                    # the injection prompt is not a stored scene.
                else:
                    return await _finish_vision_result(
                        session,
                        live_image,
                        actor=actor,
                        device_id=device_id,
                        keep_request=keep_request,
                    )
            elif frame.attachment_id:
                try:
                    attachment = await _resolve_attachment(session, UUID(str(frame.attachment_id)))
                except (KeyError, ValueError):
                    attachment = None
        elif frame is not None and frame.error:
            spoken = _spoken_for_capture_error(frame.error, frame.permission)
            return {
                "ok": False,
                "spoken": spoken,
                "error": frame.error,
                "degraded": True,
                "source": "live_camera",
                "request_id": frame.request_id or capture_id,
                "model_image_delivered": False,
                "hud": _card(spoken, {"ok": False, "error": frame.error}),
            }

    if attachment is None and not live_connected:
        local, spoken_error = await _capture_local_frame(
            session, actor=actor, device_id=device_id
        )
        if local is not None:
            attachment = local
            source = "local_helper"

    if attachment is None:
        spoken = spoken_error or UNAVAILABLE_SPOKEN
        error = "macos_permission_denied" if spoken == DENIED_SPOKEN else "not_connected"
        return {
            "ok": False,
            "spoken": spoken,
            "error": error,
            "degraded": True,
            "source": source,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "source": source}),
        }

    if live_session_id:
        from app.memory.visual import wants_keep_visible

        if not wants_keep_visible(keep_request):
            jpeg = await _jpeg_from_attachment(session, attachment)
            if jpeg:
                dims = validate_jpeg(jpeg)
                width = dims[1] if dims else None
                height = dims[2] if dims else None
                _stash_frame(
                    call_id=call_id,
                    request_id=capture_id,
                    jpeg=jpeg,
                    width=width,
                    height=height,
                    camera_name=None,
                    detail=detail_value,
                    t0=t0,
                )
                return await _finish_vision_result(
                    session,
                    _live_image_result(
                        request_id=capture_id,
                        source=source,
                        width=width,
                        height=height,
                        encoded_bytes=len(jpeg),
                        camera_name=None,
                        focus=focus_value,
                        keep_request=keep_request,
                    ),
                    actor=actor,
                    device_id=device_id,
                    keep_request=keep_request,
                )

    from app.ev.vision import analyze_attachment
    from app.memory.visual import wants_keep_visible

    look_prompt = _look_vision_prompt(keep_request)
    analyze = analyze_attachment(
        session,
        attachment.id,
        actor=actor,
        permission=True,
        allow_raw=False,
        prompt=look_prompt,
    )
    try:
        if wants_keep_visible(keep_request):
            perception = await asyncio.wait_for(
                analyze, timeout=KEEP_ANALYZE_TIMEOUT_SECONDS
            )
        else:
            perception = await analyze
    except Exception:  # noqa: BLE001 - memorize still stores the capture
        logger.warning("look analysis skipped", exc_info=True)
        if not wants_keep_visible(keep_request):
            raise
        from app.memory.visual import keep_owner_spoken

        spoken = keep_owner_spoken(
            ocr=captured_ocr, labels=captured_labels, keep_request=keep_request, frame_ok=True
        )
        return await _finish_vision_result(
            session,
            {
                "ok": True,
                "spoken": spoken,
                "summary": spoken,
                "ocr_text": captured_ocr or None,
                "labels": captured_labels,
                "source": source,
                "attachment_id": str(attachment.id),
                "keep_request": keep_request or None,
                "media_kind": "frame",
                "hud": _card(spoken, {"ok": True, "source": source, "visor": True}),
            },
            actor=actor,
            device_id=device_id,
            keep_request=keep_request,
        )
    payload = dict(perception.payload or {})
    ocr_text = str(payload.get("ocr_text") or "") or captured_ocr
    labels = _label_names(list(payload.get("labels") or [])) or captured_labels
    things = await _enrolled_object_matches(session, labels=labels, ocr_text=ocr_text)
    confirmed = await _confirmed_recognition_matches(
        session, labels=labels, ocr_text=ocr_text
    )
    roster_text = await _roster_text_matches(session, ocr_text)
    people: list[dict[str, Any]] = []
    if focus_value in {"auto", "people"}:
        try:
            data = await get_object_store().get(attachment.storage_key)
            people = await _match_enrolled_faces(
                session, data, attachment_id=attachment.id
            )
        except Exception:  # noqa: BLE001 - people matching is optional
            logger.info("look face pass skipped", exc_info=True)
            people = []

    for item in things:
        try:
            from app.ev.world_memory import ObservationContract, record_owner_object_observation

            await record_owner_object_observation(
                session,
                item["id"],
                ObservationContract(
                    subject="owner",
                    subject_type="owner",
                    object_or_event=item["name"],
                    action="seen",
                    location="camera look",
                    source_device=str(device_id or "camera"),
                    evidence_ref=f"attachment:{attachment.id}",
                    confidence=0.7,
                    uncertainty="visual match from OCR or object labels, not certain identity",
                    consent_state="explicit",
                    fact_kind="observed",
                ),
                actor=actor,
            )
        except Exception:  # noqa: BLE001 - world-model write is optional
            logger.info("look object observation skipped", exc_info=True)

    keeping = wants_keep_visible(keep_request)
    draft = _compose_spoken(
        summary=str(payload.get("summary") or ""),
        ocr_text=ocr_text,
        labels=labels,
        things=things,
        people=people,
        roster_text=roster_text,
        confirmed=confirmed,
        focus=focus_value,
        keep=keeping,
    )
    spoken = await _polish_spoken(
        draft,
        {
            "ocr_text": ocr_text,
            "labels": labels,
            "things": things,
            "people": people,
            "keep": keeping,
        },
    )
    result = {
        "ok": True,
        "spoken": spoken,
        "summary": spoken,
        "ocr_text": ocr_text[:2000] or None,
        "ocr_provider": payload.get("ocr_provider"),
        "labels": labels,
        "things": things,
        "people": people,
        "focus": focus_value,
        "source": source,
        "attachment_id": str(attachment.id),
        "perception_event_id": str(perception.id),
        "raw_sent": False,
        "degraded": bool(payload.get("local_degraded")),
        "hud": _card(
            spoken,
            {
                "ok": True,
                "source": source,
                "attachment_id": str(attachment.id),
                "labels": labels,
                "ocr_provider": payload.get("ocr_provider"),
                "visor": True,
            },
        ),
        "media_kind": "frame",
        "person_count": len(people) or None,
        "keep_request": keep_request or None,
        "colors": [
            str(item).strip()
            for item in (payload.get("colors") or [])
            if str(item).strip()
        ],
    }
    return await _finish_vision_result(
        session, result, actor=actor, device_id=device_id, keep_request=keep_request
    )


async def look_with_timeout(
    session: AsyncSession,
    **kwargs: Any,
) -> dict[str, Any]:
    """Guard a look so a hung camera never blocks the live audio loop forever."""

    from app.memory.visual import attach_keep_to_latest_look, wants_keep_visible

    prompt = str(kwargs.get("prompt") or "")
    actor = str(kwargs.get("actor") or "owner")
    device_id = kwargs.get("device_id")
    try:
        result = await asyncio.wait_for(
            look_now(session, **kwargs),
            timeout=LOOK_TIMEOUT_SECONDS + KEEP_ANALYZE_TIMEOUT_SECONDS + 4.0,
        )
    except TimeoutError:
        spoken = TIMEOUT_SPOKEN
        result = {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }
    if not result.get("kept") and wants_keep_visible(prompt or str(result.get("keep_request") or "")):
        from app.memory.visual import (
            is_clarity_hedge,
            is_empty_visual_scene,
            keep_owner_spoken,
            persist_keep_intent,
        )

        asked = prompt or str(result.get("keep_request") or "")
        device = str(device_id) if device_id else None
        attached = await attach_keep_to_latest_look(
            session,
            asked,
            actor=actor,
            device_id=device,
        )
        if attached is None:
            attached = await persist_keep_intent(
                session,
                asked,
                actor=actor,
                device_id=device,
                scene=str(result.get("spoken") or "") if result.get("ok") else None,
                ocr=str(result.get("ocr_text") or "") if result.get("ok") else None,
                labels=list(result.get("labels") or []) if result.get("ok") else None,
            )
        really_kept = bool(
            result.get("kept")
            or (isinstance(attached, dict) and attached.get("kept"))
        )
        scene = " ".join(str(result.get("spoken") or "").split()).strip()
        frame_ok = _frame_was_delivered(result)
        hedge = is_empty_visual_scene(scene) or is_clarity_hedge(scene)
        if really_kept and (not hedge or frame_ok):
            result["kept"] = True
            result["remembered"] = True
            result["spoken"] = keep_owner_spoken(
                scene=scene,
                ocr=result.get("ocr_text") or result.get("local_ocr"),
                labels=list(result.get("labels") or []),
                colors=list(result.get("colors") or []),
                keep_request=asked,
                frame_ok=frame_ok,
            )
            result["summary"] = result["spoken"]
            result["follow_up"] = KEEP_STORED_HINT
            result["memory_note"] = KEEP_STORED_HINT
        else:
            result["kept"] = really_kept
            result["spoken"] = keep_owner_spoken(
                scene=scene,
                ocr=result.get("ocr_text") or result.get("local_ocr"),
                labels=list(result.get("labels") or []),
                colors=list(result.get("colors") or []),
                keep_request=asked,
                frame_ok=frame_ok,
            )
            result["summary"] = result["spoken"]
    return result


async def observe_camera_now(
    session: AsyncSession,
    *,
    actor: str = "owner",
    duration_seconds: float | None = None,
    objective: str | None = None,
    strategy: str = "interval",
    live_session_id: str | None = None,
    device_id: str | None = None,
    request_id: str | None = None,
    detail: str = "high",
) -> dict[str, Any]:
    """Capture a bounded sequence of frames and stash each for Realtime injection."""

    keep_request = resolve_keep_request(
        objective, live_session_id=live_session_id, device_id=device_id
    )
    t0 = now_mono()
    duration = clamp_observe_duration(duration_seconds)
    interval = OBSERVE_DEFAULT_INTERVAL if strategy != "change" else 0.9
    call_id = str(request_id or "").strip() or None
    capture_id = call_id or str(uuid4())
    detail_value = (detail or "high").strip().lower()
    if detail_value not in {"auto", "low", "high"}:
        detail_value = "high"
    log_camera(
        "camera.tool_called",
        request_id=capture_id,
        extra={"tool": "observe_camera", "duration_s": duration},
    )
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(live_session_id) or live_for_device(device_id)
    if live is None:
        spoken = UNAVAILABLE_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "not_connected",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "not_connected"}),
        }
    frames = await live.request_observe_frames(
        duration_s=duration,
        interval_s=interval,
        max_frames=OBSERVE_MAX_FRAMES,
        timeout=duration + 4.0,
        request_id=capture_id,
        detail=detail_value,
    )
    kept = 0
    last_error = None
    width = height = None
    camera_name = None
    encoded = 0
    last_frame = None
    labels: list[str] = []
    colors: list[str] = []
    ocr_bits: list[str] = []
    summaries: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if frame.error and not frame.jpeg:
            last_error = frame.error
            continue
        if not frame.jpeg:
            continue
        _stash_frame(
            call_id=call_id,
            request_id=frame.request_id or capture_id,
            jpeg=frame.jpeg,
            width=frame.width,
            height=frame.height,
            camera_name=frame.camera_name,
            detail=detail_value,
            t0=t0,
            sequence=index,
        )
        kept += 1
        last_frame = frame
        width = frame.width
        height = frame.height
        camera_name = frame.camera_name
        encoded += len(frame.jpeg)
        frame_labels = _frame_labels(frame)
        frame_colors = _frame_colors(frame)
        labels = _frame_labels(frame, labels)
        colors = _frame_colors(frame, colors)
        ocr = str(getattr(frame, "ocr_text", None) or "").strip() or None
        if ocr:
            ocr_bits.append(ocr)
        summaries.append(
            {
                "sequence": index,
                "labels": frame_labels,
                "colors": frame_colors,
                "ocr_text": ocr,
                "person_count": getattr(frame, "person_count", None),
                "face_count": getattr(frame, "face_count", None),
                "width": frame.width,
                "height": frame.height,
            }
        )
    if kept == 0:
        spoken = _spoken_for_capture_error(last_error or "timeout", purpose="look")
        return {
            "ok": False,
            "spoken": spoken,
            "error": last_error or "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": last_error or "timeout"}),
        }
    ocr = " ".join(ocr_bits).strip() or None
    lighting = getattr(last_frame, "lighting", None) if last_frame is not None else None
    spoken = _observe_spoken(summaries, duration_s=duration)
    return await _finish_vision_result(
        session,
        _live_image_result(
            request_id=capture_id,
            source="live_camera",
            width=width,
            height=height,
            encoded_bytes=encoded,
            camera_name=camera_name,
            focus="auto",
            frames=kept,
            ocr_text=ocr,
            labels=labels,
            observe=True,
            spoken=spoken,
            lighting=lighting,
            luminance=getattr(last_frame, "luminance", None) if last_frame is not None else None,
            media_kind="frame",
            colors=colors,
            person_count=getattr(last_frame, "person_count", None) if last_frame is not None else None,
            face_count=getattr(last_frame, "face_count", None) if last_frame is not None else None,
            frames_summary=summaries,
            keep_request=keep_request,
        ),
        actor=actor,
        device_id=device_id,
        keep_request=keep_request,
    )


async def observe_camera_with_timeout(
    session: AsyncSession,
    **kwargs: Any,
) -> dict[str, Any]:
    duration = clamp_observe_duration(kwargs.get("duration_seconds"))
    try:
        return await asyncio.wait_for(
            observe_camera_now(session, **kwargs),
            timeout=duration + 10.0,
        )
    except TimeoutError:
        spoken = TIMEOUT_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }


async def capture_photo_now(
    session: AsyncSession,
    *,
    actor: str = "owner",
    prompt: str | None = None,
    live_session_id: str | None = None,
    device_id: str | None = None,
    request_id: str | None = None,
    detail: str = "high",
) -> dict[str, Any]:
    """Take a still photo, save it, and describe what is in the frame."""

    keep_request = resolve_keep_request(
        prompt, live_session_id=live_session_id, device_id=device_id
    )
    t0 = now_mono()
    detail_value = (detail or "high").strip().lower()
    if detail_value not in {"auto", "low", "high"}:
        detail_value = "high"
    call_id = str(request_id or "").strip() or None
    capture_id = call_id or str(uuid4())
    log_camera("camera.tool_called", request_id=capture_id, extra={"tool": "capture_photo"})
    live, frame = await _wait_for_live_frame(
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=capture_id,
        detail=detail_value,
        action="capture_save",
    )
    if live is None:
        spoken = UNAVAILABLE_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "not_connected",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "not_connected"}),
        }
    if frame is None or (frame.error and not frame.jpeg):
        spoken = _spoken_for_capture_error(getattr(frame, "error", None) or "timeout")
        return {
            "ok": False,
            "spoken": spoken,
            "error": getattr(frame, "error", None) or "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": getattr(frame, "error", None)}),
        }
    attachment_id = frame.attachment_id
    if frame.jpeg and not attachment_id:
        attachment = await store_frame_attachment(
            session,
            frame.jpeg,
            actor=actor,
            device_id=device_id,
            filename="capture.jpg",
        )
        attachment_id = str(attachment.id)
    if frame.jpeg:
        _stash_frame(
            call_id=call_id,
            request_id=frame.request_id or capture_id,
            jpeg=frame.jpeg,
            width=frame.width,
            height=frame.height,
            camera_name=frame.camera_name,
            detail="high",
            t0=t0,
        )
    labels = _frame_labels(frame)
    colors = _frame_colors(frame)
    ocr = getattr(frame, "ocr_text", None)
    lighting = getattr(frame, "lighting", None) or lighting_from_luminance(
        getattr(frame, "luminance", None)
    )
    saved_path = getattr(frame, "saved_path", None)
    spoken = _spoken_from_frame(
        purpose="capture",
        frame=frame,
        labels=labels,
        colors=colors,
        ocr_text=ocr,
        lighting=lighting,
        saved_path=saved_path,
        width=frame.width,
        height=frame.height,
    )
    return await _finish_vision_result(
        session,
        _live_image_result(
            request_id=frame.request_id or capture_id,
            source="live_camera",
            width=frame.width,
            height=frame.height,
            encoded_bytes=len(frame.jpeg) if frame.jpeg else 0,
            camera_name=frame.camera_name,
            focus="auto",
            ocr_text=ocr,
            labels=labels,
            spoken=spoken,
            lighting=lighting,
            saved_path=saved_path,
            persist_raw=True,
            attachment_id=attachment_id,
            media_kind="photo",
            luminance=getattr(frame, "luminance", None),
            face_count=getattr(frame, "face_count", None),
            person_count=getattr(frame, "person_count", None),
            colors=colors,
            keep_request=keep_request,
        ),
        actor=actor,
        device_id=device_id,
        keep_request=keep_request,
    )


async def capture_photo_with_timeout(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            capture_photo_now(session, **kwargs),
            timeout=LOOK_TIMEOUT_SECONDS + 10.0,
        )
    except TimeoutError:
        spoken = TIMEOUT_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }


async def record_video_now(
    session: AsyncSession,
    *,
    actor: str = "owner",
    duration_seconds: float | None = None,
    prompt: str | None = None,
    live_session_id: str | None = None,
    device_id: str | None = None,
    request_id: str | None = None,
    detail: str = "high",
) -> dict[str, Any]:
    """Record a bounded video clip and save it."""

    keep_request = resolve_keep_request(
        prompt, live_session_id=live_session_id, device_id=device_id
    )
    t0 = now_mono()
    duration = clamp_record_duration(duration_seconds)
    call_id = str(request_id or "").strip() or None
    capture_id = call_id or str(uuid4())
    log_camera(
        "camera.tool_called",
        request_id=capture_id,
        extra={"tool": "record_video", "duration_s": duration},
    )
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(live_session_id) or live_for_device(device_id)
    if live is None:
        spoken = UNAVAILABLE_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "not_connected",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "not_connected"}),
        }
    frames = await live.request_record_clip(
        duration_s=duration,
        timeout=duration + 18.0,
        request_id=capture_id,
        detail=detail,
    )
    if not frames:
        spoken = RECORD_TIMEOUT_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }
    usable = [item for item in frames if item.saved_path or item.attachment_id or item.jpeg]
    frame = usable[-1] if usable else frames[-1]
    if frame.error and not frame.saved_path and not frame.attachment_id and not frame.jpeg:
        spoken = _spoken_for_capture_error(frame.error, purpose="record")
        return {
            "ok": False,
            "spoken": spoken,
            "error": frame.error,
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": frame.error}),
        }
    labels: list[str] = []
    colors: list[str] = []
    ocr_bits: list[str] = []
    people = 0
    faces = 0
    jpeg_count = 0
    for index, item in enumerate(frames):
        if item.jpeg:
            jpeg_count += 1
            _stash_frame(
                call_id=call_id,
                request_id=item.request_id or capture_id,
                jpeg=item.jpeg,
                width=item.width,
                height=item.height,
                camera_name=item.camera_name,
                detail="high",
                t0=t0,
                sequence=index,
            )
        labels = _frame_labels(item, extra=labels)
        colors = _frame_colors(item, extra=colors)
        people = max(people, int(getattr(item, "person_count", None) or 0))
        faces = max(faces, int(getattr(item, "face_count", None) or 0))
        ocr = str(getattr(item, "ocr_text", None) or "").strip()
        if ocr and ocr not in ocr_bits:
            ocr_bits.append(ocr)
    duration_s = None
    if frame.duration_ms:
        duration_s = max(frame.duration_ms / 1000.0, 0.0)
    spoken = _spoken_from_frame(
        purpose="record",
        frame=frame,
        labels=labels,
        colors=colors,
        ocr_text=" ".join(ocr_bits) or None,
        lighting=getattr(frame, "lighting", None),
        saved_path=frame.saved_path,
        duration_s=duration_s or duration,
        width=frame.width,
        height=frame.height,
        people=people or None,
        faces=faces or None,
    )
    return await _finish_vision_result(
        session,
        _live_image_result(
            request_id=frame.request_id or capture_id,
            source="live_camera",
            width=frame.width,
            height=frame.height,
            encoded_bytes=sum(len(item.jpeg) for item in frames if item.jpeg),
            camera_name=frame.camera_name,
            focus="auto",
            frames=jpeg_count or 1,
            ocr_text=" ".join(ocr_bits) or None,
            labels=labels,
            spoken=spoken,
            lighting=getattr(frame, "lighting", None),
            saved_path=frame.saved_path,
            persist_raw=True,
            attachment_id=frame.attachment_id,
            media_kind="video",
            duration_s=duration_s or duration,
            luminance=getattr(frame, "luminance", None),
            face_count=faces or getattr(frame, "face_count", None),
            person_count=people or getattr(frame, "person_count", None),
            colors=colors,
            keep_request=keep_request,
        ),
        actor=actor,
        device_id=device_id,
        keep_request=keep_request,
    )


async def record_video_with_timeout(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    duration = clamp_record_duration(kwargs.get("duration_seconds"))
    try:
        return await asyncio.wait_for(
            record_video_now(session, **kwargs),
            timeout=duration + 22.0,
        )
    except TimeoutError:
        spoken = RECORD_TIMEOUT_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "model_image_delivered": False,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }

