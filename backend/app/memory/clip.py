"""Clip → memory: keyframe moments, speech, and one durable observation.

A recorded clip is processed in three honest steps:

1. ``app.vision.clips`` extracts keyframes on a deterministic time grid and a
   16 kHz mono WAV, or reports ``degraded=True`` with nothing (no ffmpeg, or an
   undecodable file).
2. Each frame goes through the **local** OCR provider (Apple Vision on this
   Mac), and the clip audio goes through the configured ASR engine. Both are
   optional: a failure never fails the ingest.
3. The clip pixels land in the object store as one attachment, and
   ``persist_visual_observation`` writes one ``camera.observation`` with
   ``media_kind="clip"``, a ``moments[]`` timeline, and the transcript.

Nothing here talks to a hosted model. The mind may be shown stills later through
the normal recall/keep paths, never the raw clip.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment
from app.schemas import EventCreate
from app.storage.object_store import get_object_store, sha256_bytes
from app.utils.text import utcnow
from app.vision.clips import (
    MAX_FRAMES,
    ClipFrames,
    extract_audio_wav,
    extract_frames,
    probe_clip,
)

logger = logging.getLogger("ev.memory.clip")

CLIP_EVENT_TYPE = "camera.clip"
MAX_MOMENT_LABELS = 8
MAX_TRANSCRIPT_CHARS = 2000


@dataclass
class ClipMoment:
    """One sampled moment of a clip, with everything the device could read."""

    t_start: float
    t_end: float
    labels: list[str]
    ocr_text: str | None = None
    engine: str | None = None
    degraded: bool = False
    person_count: int | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "t_start": round(self.t_start, 2),
            "t_end": round(self.t_end, 2),
            "labels": self.labels[:MAX_MOMENT_LABELS],
        }
        if self.ocr_text:
            payload["ocr_text"] = self.ocr_text[:400]
        if self.engine:
            payload["engine"] = self.engine
        if self.degraded:
            payload["degraded"] = True
        if self.person_count is not None:
            payload["person_count"] = self.person_count
        return payload


@dataclass
class ClipIngest:
    ok: bool
    attachment_id: str | None = None
    event_id: str | None = None
    memory_id: str | None = None
    duration_s: float | None = None
    frames: int = 0
    moments: list[dict[str, Any]] | None = None
    transcript: str | None = None
    engine: str = "none"
    extraction_degraded: bool = True
    transcript_degraded: bool = False
    error: str | None = None
    spoken: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "attachment_id": self.attachment_id,
            "event_id": self.event_id,
            "memory_id": self.memory_id,
            "duration_s": self.duration_s,
            "frames": self.frames,
            "moments": list(self.moments or []),
            "transcript": self.transcript,
            "engine": self.engine,
            "extraction_degraded": self.extraction_degraded,
            "transcript_degraded": self.transcript_degraded,
            "error": self.error,
            "spoken": self.spoken,
        }


async def store_clip_attachment(
    session: AsyncSession,
    data: bytes,
    *,
    actor: str,
    device_id: str | None = None,
    filename: str = "clip.mov",
    content_type: str = "video/quicktime",
    request_id: str | None = None,
) -> Attachment:
    """Persist the clip itself as a normal attachment (object store + row)."""

    store = get_object_store()
    storage_key = f"attachments/{uuid4()}.bin"
    await store.put(storage_key, data, content_type)
    from app.services.event_service import EventService

    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="camera",
            event_type=CLIP_EVENT_TYPE,
            content={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
                "storage_key": storage_key,
                "request_id": request_id,
            },
            metadata={"clip": True, "persist_raw": True},
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


async def _frame_labels(jpeg: bytes) -> tuple[list[str], str | None, str, bool]:
    """Local OCR (+ optional on-device detectors) for one keyframe."""

    try:
        from app.vision.providers import get_vision_provider

        provider = get_vision_provider()
        result = await provider.analyze(data=jpeg, content_type="image/jpeg", filename="frame.jpg")
        labels = [
            str(entry.get("label") or entry)
            for entry in (getattr(result, "labels", None) or [])
            if str(entry.get("label") or entry).strip()
        ][:MAX_MOMENT_LABELS]
        ocr = (getattr(result, "ocr_text", None) or "").strip() or None
        return labels, ocr, str(getattr(result, "provider", "") or "none"), bool(
            getattr(result, "degraded", False)
        )
    except Exception:  # noqa: BLE001 - a frame we cannot read is still a frame
        logger.info("clip frame perception skipped", exc_info=True)
        return [], None, "error", True


async def _perceive_frames(frames: ClipFrames, duration_s: float | None) -> list[ClipMoment]:
    moments: list[ClipMoment] = []
    ordered = sorted(frames.frames, key=lambda item: item.t_ms)
    for index, frame in enumerate(ordered):
        start = frame.t_ms / 1000.0
        if index + 1 < len(ordered):
            end = ordered[index + 1].t_ms / 1000.0
        else:
            end = duration_s if duration_s and duration_s > start else start + 1.0
        labels, ocr, engine, degraded = await _frame_labels(frame.jpeg)
        moments.append(
            ClipMoment(
                t_start=start,
                t_end=max(end, start),
                labels=labels,
                ocr_text=ocr,
                engine=engine or None,
                degraded=degraded,
            )
        )
    return moments


async def _transcribe_clip(data: bytes, *, filename: str | None, content_type: str | None) -> tuple[str | None, bool]:
    """Local ASR over the clip audio. Never remote, never fatal."""

    try:
        wav = await extract_audio_wav(data, filename=filename, content_type=content_type)
    except Exception:  # noqa: BLE001
        wav = None
    if not wav:
        return None, False
    try:
        from app.voice.asr import get_transcriber

        transcriber = get_transcriber()
        transcript = await transcriber.transcribe(audio_b64=base64.b64encode(wav).decode("ascii"))
    except Exception:  # noqa: BLE001 - dev providers refuse audio by design
        logger.info("clip transcription skipped", exc_info=True)
        return None, True
    text = " ".join(str(getattr(transcript, "text", "") or "").split()).strip()
    degraded = bool(getattr(transcript, "degraded", False))
    return (text[:MAX_TRANSCRIPT_CHARS] or None), degraded


def _clip_facts(
    *,
    moments: list[dict[str, Any]],
    frames: int,
) -> str:
    """Facts for the memory line; duration and speech are added by the writer."""

    parts = [f"Sampled {frames} moments across the clip."]
    labels: list[str] = []
    for moment in moments:
        for name in moment.get("labels") or []:
            name = str(name)
            if name and name.lower() not in {item.lower() for item in labels}:
                labels.append(name)
    if labels:
        parts.append("Visible: " + ", ".join(labels[:8]) + ".")
    printed = [str(m.get("ocr_text")) for m in moments if m.get("ocr_text")]
    if printed:
        parts.append("Text: " + printed[0][:160] + ".")
    return " ".join(parts)[:1000]


def _clip_summary_line(
    *,
    facts: str,
    transcript: str | None,
    duration_s: float | None,
) -> str:
    """Self-contained line for an API caller (never the memory lead)."""

    seconds = f"{duration_s:.0f}" if duration_s else "unknown"
    line = f"A recorded clip is stored ({seconds} seconds). {facts}"
    if transcript:
        line += " Speech in the clip: " + transcript[:300] + "."
    return " ".join(line.split())[:1000]


async def ingest_clip(
    session: AsyncSession,
    data: bytes,
    *,
    actor: str,
    device_id: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    duration_ms: int | None = None,
    request_id: str | None = None,
    keep_request: str | None = None,
    frame_count: int = MAX_FRAMES,
    analyze: bool = True,
) -> ClipIngest:
    """Store a clip, sample it, and write one durable observation."""

    if not data:
        return ClipIngest(ok=False, error="empty_clip", spoken="I did not receive a clip.")
    probe = await probe_clip(data, filename=filename, content_type=content_type)
    duration_s = probe.duration_s
    if duration_s is None and duration_ms:
        duration_s = max(0.0, float(duration_ms) / 1000.0)
    clip_name = filename or "clip.mov"
    clip_type = content_type or "video/quicktime"
    attachment = await store_clip_attachment(
        session,
        data,
        actor=actor,
        device_id=device_id,
        filename=clip_name,
        content_type=clip_type,
        request_id=request_id,
    )
    frames = await extract_frames(
        data, count=frame_count, filename=clip_name, content_type=clip_type
    )
    moments: list[dict[str, Any]] = []
    transcript: str | None = None
    transcript_degraded = False
    if analyze:
        if frames.frames:
            moments = [moment.as_payload() for moment in await _perceive_frames(frames, duration_s)]
        transcript, transcript_degraded = await _transcribe_clip(
            data, filename=clip_name, content_type=clip_type
        )
    spoken = _clip_facts(moments=moments, frames=len(frames.frames))
    summary = _clip_summary_line(
        facts=spoken, transcript=transcript, duration_s=duration_s
    )
    result: dict[str, Any] = {
        "ok": True,
        "spoken": spoken,
        "summary": summary,
        "labels": [],
        "colors": [],
        "media_kind": "clip",
        "attachment_id": str(attachment.id),
        "duration_s": duration_s,
        "encoded_bytes": len(data),
        "image_ready": True,
        "request_id": request_id,
        "moments": moments,
        "transcript": transcript,
        "clip_engine": frames.engine,
        "clip_degraded": bool(frames.degraded),
    }
    if keep_request:
        result["keep_request"] = keep_request[:400]
    written = await _persist(session, result, actor=actor, device_id=device_id)
    return ClipIngest(
        ok=True,
        attachment_id=str(attachment.id),
        event_id=(written or {}).get("event_id"),
        memory_id=(written or {}).get("memory_id"),
        duration_s=duration_s,
        frames=len(frames.frames),
        moments=moments,
        transcript=transcript,
        engine=frames.engine,
        extraction_degraded=bool(frames.degraded),
        transcript_degraded=transcript_degraded,
        error=frames.error,
        spoken=summary,
    )


async def _persist(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    actor: str,
    device_id: str | None,
) -> dict[str, Any] | None:
    from app.memory.visual import persist_visual_observation

    try:
        return await persist_visual_observation(
            session, result, actor=actor, device_id=device_id
        )
    except Exception:  # noqa: BLE001 - the clip is stored even if memory write fails
        logger.warning("clip observation persist skipped", exc_info=True)
        return None


async def clip_bytes_for_attachment(session: AsyncSession, attachment_id: UUID | str) -> bytes | None:
    """Read a stored clip back for playback or deterministic re-sampling."""

    try:
        key = UUID(str(attachment_id))
    except (TypeError, ValueError):
        return None
    attachment = await session.get(Attachment, key)
    if attachment is None:
        return None
    try:
        return await get_object_store().get(attachment.storage_key)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "CLIP_EVENT_TYPE",
    "ClipIngest",
    "ClipMoment",
    "clip_bytes_for_attachment",
    "ingest_clip",
    "store_clip_attachment",
    "utcnow",
]
