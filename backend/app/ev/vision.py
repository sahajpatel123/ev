"""Permissioned vision & multimodal perception over user-owned attachments.

Perception is an observation layer, not surveillance: analysis only runs with
explicit user permission, respects the source event's privacy level, prefers
derived text (on-device OCR/extraction) over raw media, and records every
conclusion with provenance (attachment + source event + permission grant +
provider + ``raw_sent`` flag).  Model-suggested labels land in the recognition
log as pending (``source="model"``) until the user confirms them
(``source="user"``), so no identity claim is ever durable without confirmation.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import (
    ChatMessage,
    ChatProvider,
    EntityRef,
    MediaPart,
    MemoryCandidate,
    RequestEnvelope,
)
from app.ev.edith import record_command
from app.ev.live import get_or_create_channel, ingest_events
from app.gateway.providers import get_chat_provider
from app.gateway.service import ModelGateway
from app.memory.entities import get_or_create_entity
from app.memory.writer import MemoryWriter
from app.models import Attachment, Event, LiveChannel, LiveEvent, RecognitionLog
from app.schemas import EventCreate, LiveEventCreate
from app.services.access_log import log_access
from app.services.event_service import EventService
from app.storage.object_store import get_object_store

# Model processing is denied for these privacy levels (matches the live-data
# model slice: sensitive content stays out of provider context entirely).
MODEL_BLOCKED_LEVELS = {"sensitive", "never_send_to_model"}
# Raw media is only ever transmitted for normal-privacy sources, and only with
# explicit permission; everything else uses derived/minimal representations.
RAW_BLOCKED_LEVELS = {"sensitive", "private", "never_send_to_model"}

LABEL_CONFIDENCE_FLOOR = 0.6
PrivacyLevel = Literal["private", "normal", "sensitive", "never_send_to_model"]


def _parse_labels(text: str) -> list[dict]:
    """Parse ``LABEL: name 0.95`` lines or a JSON labels array from a response."""
    labels: list[dict] = []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("labels"), list):
        for item in data["labels"]:
            if isinstance(item, dict) and item.get("label"):
                try:
                    confidence = float(item.get("confidence", 1.0))
                except (TypeError, ValueError):
                    confidence = 1.0
                labels.append(
                    {
                        "label": str(item["label"])[:256],
                        "confidence": round(max(0.0, min(1.0, confidence)), 3),
                    }
                )
        return labels
    pattern = re.compile(
        r"(?:^|\n)\s*(?:label|LABEL)\s*:\s*(?P<label>[^0-9\n][^\n]*?)"
        r"(?:\s+(?P<conf>0?\.\d{1,3}|1(?:\.0+)?))?\s*$",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        raw_conf = match.group("conf")
        confidence = float(raw_conf) if raw_conf else 1.0
        labels.append(
            {
                "label": match.group("label").strip()[:256],
                "confidence": round(max(0.0, min(1.0, confidence)), 3),
            }
        )
    return labels


def _extract_summary(text: str) -> str:
    text = text.strip()
    if not text:
        return "No summary returned by the perception provider."
    summary = text.split("\n")[0]
    summary = re.sub(r"^SUMMARY\s*:\s*", "", summary, flags=re.IGNORECASE)
    return summary.strip()[:1000] or "Perception completed."


def _image_data_url(data: bytes, content_type: str | None) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type or 'application/octet-stream'};base64,{encoded}"


def _derived_text(attachment: Attachment, event: Event) -> str | None:
    """On-device derived text (OCR/extraction) is preferred over raw media."""
    metadata = event.metadata_ or {}
    content = event.content or {}
    for source in (metadata, content):
        value = source.get("derived_text") or source.get("ocr_text")
        if isinstance(value, str) and value.strip():
            return value.strip()[:4000]
    return None


def _perception_payload(
    *,
    summary: str,
    labels: list[dict],
    provider: str,
    raw_sent: bool,
    actor: str,
    attachment: Attachment,
    event: Event,
    derived_text_used: bool,
    request_id: str | None,
) -> dict:
    return {
        "summary": summary,
        "labels": labels,
        "confidence": round(
            max((lbl["confidence"] for lbl in labels), default=0.0), 3
        ),
        "provider": provider,
        "raw_sent": raw_sent,
        "permission": True,
        "permission_granted_by": actor,
        "attachment_id": str(attachment.id),
        "source_event_id": str(event.id),
        "sha256": attachment.sha256,
        "content_type": attachment.content_type,
        "derived_text_used": derived_text_used,
        "request_id": request_id,
    }


def recognition_memory_candidate(
    *,
    label: str,
    confidence: float,
    entity_type: str,
    recognition_id: str,
    attachment_id: str | None,
    perception_event_id: str | None,
    source_event_id: str | None,
    entity_id: str,
    privacy_level: str,
    event_time: datetime | None,
) -> MemoryCandidate:
    """Canonical observation memory for one user-confirmed recognition.

    Used by both the live confirmation path and the rebuild replay so the
    derived memory is deterministic and provenance-linked to the same raw
    ``recognition.confirm`` event.
    """
    return MemoryCandidate(
        memory_type="observation",
        text=f"Recognized {label} in a shared attachment (user-confirmed).",
        payload={
            "kind": "recognition",
            "recognition_id": recognition_id,
            "label": label,
            "confidence": confidence,
            "attachment_id": attachment_id,
            "perception_event_id": perception_event_id,
            "source_event_id": source_event_id,
            "entity_id": entity_id,
        },
        importance=0.55,
        confidence=confidence,
        source_type="explicit",
        privacy_level=privacy_level,
        event_time=event_time,
        entities=[EntityRef(name=label, entity_type=entity_type)],
    )


async def analyze_attachment(
    session: AsyncSession,
    attachment_id: UUID,
    *,
    actor: str,
    permission: bool = False,
    allow_raw: bool = False,
    prompt: str | None = None,
    provider: ChatProvider | None = None,
) -> LiveEvent:
    """Analyze one user-owned attachment under explicit permission."""
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None:
        raise KeyError(f"Attachment {attachment_id} not found")
    event = await session.get(Event, attachment.event_id)
    if event is None or event.tombstoned_at is not None:
        raise PermissionError("Attachment's source event is unavailable")

    provider = provider or get_chat_provider()
    privacy = event.privacy_level or "normal"
    if not permission:
        raise PermissionError(
            "Explicit permission is required before any perception analysis"
        )

    derived = _derived_text(attachment, event)
    supports_media = bool(getattr(provider, "supports_media", False))
    raw_allowed = bool(
        allow_raw
        and supports_media
        and privacy not in RAW_BLOCKED_LEVELS
        and (attachment.content_type or "").startswith("image/")
    )
    model_allowed = privacy not in MODEL_BLOCKED_LEVELS

    media: list[MediaPart] = []
    raw_sent = False
    derived_text_used = False
    if raw_allowed:
        data = await get_object_store().get(attachment.storage_key)
        media.append(
            MediaPart(
                kind="image",
                content_type=attachment.content_type or "image/*",
                data_url=_image_data_url(data, attachment.content_type),
                ref=str(attachment.id),
                sha256=attachment.sha256,
            )
        )
        raw_sent = True
    elif derived:
        media.append(
            MediaPart(
                kind="text",
                content_type="text/plain",
                text=derived,
                ref=str(attachment.id),
                sha256=attachment.sha256,
            )
        )
        derived_text_used = True

    if model_allowed and media:
        system = (
            "You are EV's perception layer. Describe only what the user has "
            "explicitly shared. Do not speculate about identity or location; "
            "report observable scene content. Return a short summary and a "
            "list of suggested labels as 'LABEL: name 0.95' lines."
        )
        user_content = (
            prompt
            or (
                "Describe this image: what is visible, what text (OCR) it "
                "contains, and suggested labels."
                if raw_sent
                else "Summarize this derived document text and suggest labels."
            )
        )
        request_id = str(uuid4())
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user_content, media=media),
        ]
        envelope = RequestEnvelope(
            request_id=request_id,
            strategy={"mode": "perception"},
            metadata={
                "privacy_level": privacy,
                "permission_granted_by": actor,
            },
            media_refs=[
                {
                    "kind": part.kind,
                    "content_type": part.content_type,
                    "ref": part.ref,
                    "sha256": part.sha256,
                    "raw": raw_sent,
                    "derived_text_used": derived_text_used,
                }
                for part in media
            ],
        )
        call = await ModelGateway(provider).chat(messages, envelope=envelope)
        summary = _extract_summary(call.result.text)
        labels = _parse_labels(call.result.text)
        request_id_value = request_id
    else:
        if not model_allowed:
            summary = (
                "Perception blocked: the source event's privacy level does not "
                "permit model processing."
            )
        elif not derived and not raw_allowed:
            summary = (
                "Attachment metadata only: no raw media or on-device derived "
                "text was provided, so no model interpretation was attempted."
            )
        else:
            summary = (
                "Perception recorded without a provider call (provider lacks "
                "media support or raw transmission was not permitted)."
            )
        labels = []
        request_id_value = None

    payload = _perception_payload(
        summary=summary,
        labels=labels,
        provider=provider.name,
        raw_sent=raw_sent,
        actor=actor,
        attachment=attachment,
        event=event,
        derived_text_used=derived_text_used,
        request_id=request_id_value,
    )
    channel = await get_or_create_channel(
        session,
        name="vision-perception",
        kind="vision",
        privacy_level="normal",
    )
    rows = await ingest_events(
        session,
        channel,
        [
            LiveEventCreate(
                event_type="perception.analyze",
                payload=payload,
                privacy_level=cast(PrivacyLevel, privacy),
            )
        ],
    )
    row = rows[0] if rows else None
    if row is None:
        raise ValueError("Perception event was deduplicated; no new record created")

    for label in labels:
        if label["confidence"] < LABEL_CONFIDENCE_FLOOR:
            continue
        session.add(
            RecognitionLog(
                event_id=event.id,
                live_event_id=row.id,
                attachment_id=attachment.id,
                entity_id=None,
                label=label["label"],
                confidence=label["confidence"],
                source="model",
            )
        )

    await record_command(
        session,
        command_type="perception.analyze",
        actor=actor,
        target_type="attachment",
        target_id=str(attachment.id),
        request={
            "allow_raw": allow_raw,
            "prompt": prompt,
            "privacy_level": privacy,
        },
        result={
            "perception_event_id": str(row.id),
            "raw_sent": raw_sent,
            "provider": provider.name,
            "label_count": len(labels),
        },
        status="completed",
    )
    await log_access(
        session,
        actor=actor,
        action="perception.analyze",
        endpoint="POST /v1/vision/analyze",
        resource_type="attachment",
        resource_ids=[attachment.id],
        details={
            "perception_event_id": str(row.id),
            "raw_sent": raw_sent,
            "provider": provider.name,
            "blocked": not model_allowed,
        },
    )
    return row


async def confirm_recognition(
    session: AsyncSession,
    recognition_id: UUID,
    *,
    actor: str,
    entity_type: str = "thing",
) -> RecognitionLog:
    """Promote a model-suggested label to a user-confirmed recognition."""
    row = await session.get(RecognitionLog, recognition_id)
    if row is None:
        raise KeyError(f"Recognition {recognition_id} not found")
    if row.source == "user":
        return row  # idempotent
    if row.source != "model":
        raise ValueError("Only model-suggested recognitions can be confirmed")
    entity = await get_or_create_entity(session, row.label, entity_type)
    row.entity_id = entity.id
    row.source = "user"

    source_event = None
    if row.event_id is not None:
        source_event = await session.get(Event, row.event_id)
    if source_event is None and row.attachment_id is not None:
        attachment = await session.get(Attachment, row.attachment_id)
        if attachment is not None:
            source_event = await session.get(Event, attachment.event_id)
    privacy = (source_event.privacy_level if source_event is not None else "normal") or "normal"

    confirm_event = await EventService(session, actor=actor).create(
        EventCreate(
            source="perception",
            event_type="recognition.confirm",
            content={
                "recognition_id": str(row.id),
                "label": row.label,
                "confidence": row.confidence,
                "entity_type": entity.entity_type,
                "attachment_id": str(row.attachment_id) if row.attachment_id else None,
                "source_event_id": str(row.event_id) if row.event_id else None,
                "perception_event_id": str(row.live_event_id) if row.live_event_id else None,
            },
            privacy_level=cast(PrivacyLevel, privacy),
        )
    )
    await MemoryWriter(session).write_all(
        confirm_event,
        [
            recognition_memory_candidate(
                label=row.label,
                confidence=row.confidence,
                entity_type=entity.entity_type,
                recognition_id=str(row.id),
                attachment_id=str(row.attachment_id) if row.attachment_id else None,
                perception_event_id=str(row.live_event_id) if row.live_event_id else None,
                source_event_id=str(row.event_id) if row.event_id else None,
                entity_id=str(entity.id),
                privacy_level=privacy,
                event_time=confirm_event.occurred_at,
            )
        ],
    )
    await session.flush()
    await record_command(
        session,
        command_type="recognition.confirm",
        actor=actor,
        target_type="entity",
        target_id=str(entity.id),
        request={
            "recognition_id": str(row.id),
            "label": row.label,
            "confirm_event_id": str(confirm_event.id),
        },
        result={
            "entity_id": str(entity.id),
            "source": "user",
            "memory": "observation",
        },
        status="completed",
    )
    await log_access(
        session,
        actor=actor,
        action="recognition.confirm",
        endpoint="POST /v1/vision/recognitions/{id}/confirm",
        resource_type="recognition",
        resource_ids=[row.id],
        details={
            "label": row.label,
            "entity_id": str(entity.id),
            "confirm_event_id": str(confirm_event.id),
        },
    )
    return row


async def list_perceptions(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[LiveEvent]:
    """Latest perception records across vision channels."""
    rows = (
        await session.execute(
            select(LiveEvent)
            .join(LiveChannel, LiveChannel.id == LiveEvent.channel_id)
            .where(
                LiveChannel.active.is_(True),
                LiveChannel.kind == "vision",
                LiveEvent.event_type == "perception.analyze",
            )
            .order_by(LiveEvent.occurred_at.desc())
            .limit(min(limit, 200))
        )
    ).scalars().all()
    return list(rows)
