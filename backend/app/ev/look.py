"""One-shot camera look: capture, OCR, objects, enrolled identity only.

This is an observation tool, not a stream and not a stranger hunt.  A look
grabs at most one consented frame, runs on-device OCR and perception, then
uses the chat provider (DeepSeek) on *derived text and labels only* to
compose a spoken answer.  Official ``api.deepseek.com`` is text-only, so raw
pixels never go there.  People are named only when Agent 7 roster matching
resolves an enrolled, consented face or when OCR text matches that roster.
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
OCR_SNIPPET = 280
MAX_LABELS = 8

UNAVAILABLE_SPOKEN = (
    "I can't see a camera frame right now. Turn the camera on or share a photo, "
    "then ask me to look again."
)
DENIED_SPOKEN = (
    "Camera permission is denied. Grant camera access in Privacy settings, then ask again."
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
) -> str | None:
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(live_session_id) or live_for_device(device_id)
    if live is None:
        return None
    try:
        return await live.request_look_frame(timeout=timeout)
    except Exception:  # noqa: BLE001 - live capture is optional
        logger.info("live look frame request failed", exc_info=True)
        return None


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
) -> str:
    parts: list[str] = []
    named_people = [item["label"] for item in people if item.get("label")]
    unknown_people = sum(1 for item in people if item.get("unknown") and not item.get("label"))
    thing_names = [item["name"] for item in things if item.get("name")]

    if focus in {"auto", "objects"} and thing_names:
        parts.append("I can see " + ", ".join(thing_names) + ".")
    elif focus in {"auto", "objects"} and labels:
        parts.append("Visible: " + ", ".join(labels[:5]) + ".")

    if focus in {"auto", "text"} and ocr_text.strip():
        snippet = " ".join(ocr_text.split())[:OCR_SNIPPET]
        parts.append(f"Text reads: {snippet}.")

    if focus in {"auto", "people"}:
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
        cleaned = (summary or "").strip()
        if cleaned and "blocked" not in cleaned.lower() and "without a provider" not in cleaned.lower():
            parts.append(cleaned.split("\n")[0][:400])
        else:
            parts.append("I looked, but I could not read text or name anything with confidence.")
    return " ".join(parts)[:800]


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


async def look_now(
    session: AsyncSession,
    *,
    actor: str = "owner",
    prompt: str | None = None,
    attachment_id: str | None = None,
    focus: str = "auto",
    live_session_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Capture or accept one frame, perceive it, and speak an honest description."""

    focus_value = (focus or "auto").strip().lower()
    if focus_value not in {"auto", "text", "objects", "people"}:
        focus_value = "auto"
    source = "none"
    attachment: Attachment | None = None
    spoken_error: str | None = None

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
        live_id = await _wait_for_live_frame(
            live_session_id=live_session_id,
            device_id=device_id,
        )
        if live_id:
            try:
                attachment = await _resolve_attachment(session, UUID(str(live_id)))
                source = "live_camera"
            except (KeyError, ValueError):
                attachment = None

    if attachment is None:
        local, spoken_error = await _capture_local_frame(
            session, actor=actor, device_id=device_id
        )
        if local is not None:
            attachment = local
            source = "local_helper"

    if attachment is None:
        spoken = spoken_error or UNAVAILABLE_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "not_connected",
            "degraded": True,
            "source": source,
            "hud": _card(spoken, {"ok": False, "source": source}),
        }

    from app.ev.vision import analyze_attachment

    look_prompt = (
        prompt
        or "Describe visible objects and readable text. Do not name people unless enrolled."
    )
    perception = await analyze_attachment(
        session,
        attachment.id,
        actor=actor,
        permission=True,
        allow_raw=False,
        prompt=look_prompt,
    )
    payload = dict(perception.payload or {})
    ocr_text = str(payload.get("ocr_text") or "")
    labels = _label_names(list(payload.get("labels") or []))
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

    draft = _compose_spoken(
        summary=str(payload.get("summary") or ""),
        ocr_text=ocr_text,
        labels=labels,
        things=things,
        people=people,
        roster_text=roster_text,
        confirmed=confirmed,
        focus=focus_value,
    )
    spoken = await _polish_spoken(
        draft,
        {
            "ocr_text": ocr_text,
            "labels": labels,
            "things": things,
            "people": people,
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
            },
        ),
    }
    return result


async def look_with_timeout(
    session: AsyncSession,
    **kwargs: Any,
) -> dict[str, Any]:
    """Guard a look so a hung camera never blocks the live audio loop forever."""

    try:
        return await asyncio.wait_for(
            look_now(session, **kwargs),
            timeout=LOOK_TIMEOUT_SECONDS + 8.0,
        )
    except TimeoutError:
        spoken = UNAVAILABLE_SPOKEN
        return {
            "ok": False,
            "spoken": spoken,
            "error": "timeout",
            "degraded": True,
            "hud": _card(spoken, {"ok": False, "error": "timeout"}),
        }
