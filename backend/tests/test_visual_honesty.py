"""Visual-memory honesty: grounded sightings only, truthful media kinds.

Locks the 2026-09-10 fixes:

- A look that produced no pixels, no on-device facts and no stored attachment
  must never be written as a scene. Measured production data had 42% of
  ``camera.observation`` rows carrying conversation or screen context instead
  of a sighting.
- ``record_video`` may only claim a recording when the client proved one; a
  browser that can send stills only is described as a still sequence.
- Phone camera requests map to truthful media kinds (no stills labelled video).
"""

from __future__ import annotations

import asyncio
import base64

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.device_gateway.phone_look import _phone_media_kind, ingest_phone_frame
from app.ev.camera_runtime import parse_look_frame_meta, reset_pending_observations
from app.ev.look import record_video_now
from app.memory.visual import (
    VISUAL_EVENT_TYPE,
    persist_visual_observation,
    search_visual_observations,
)
from app.models import Device, Event, Memory
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession


def _jpeg(width: int = 320, height: int = 240) -> bytes:
    sof = bytes(
        [
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            (height >> 8) & 0xFF,
            height & 0xFF,
            (width >> 8) & 0xFF,
            width & 0xFF,
            0x01,
            0x01,
            0x11,
            0x00,
        ]
    )
    return b"\xff\xd8" + sof + (b"\x00" * 80) + b"\xff\xd9"


def _drain(session: LiveSession) -> list:
    items = []
    while True:
        try:
            items.append(session.outbound.get_nowait())
        except asyncio.QueueEmpty:
            return items


async def _observations(db_session: AsyncSession) -> list[Event]:
    from sqlalchemy import select

    rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == VISUAL_EVENT_TYPE).order_by(Event.occurred_at)
        )
    ).scalars().all()
    return list(rows)


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------


async def test_ungrounded_look_never_writes_a_scene(db_session: AsyncSession) -> None:
    """The mind's own sentence is not evidence of a sighting."""

    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": [],
            "colors": [],
            "media_kind": "frame",
            # A conversational reply that used to become the stored "scene".
            "spoken": "Hey there! Yep, quiet hours are done. What's up?",
            "keep_request": "I am holding something in my hand. Can you see and memorize it?",
            "request_id": "ungrounded-1",
        },
        actor="owner",
        device_id="phone-1",
    )
    await db_session.commit()
    assert written is not None
    assert written.get("kept") is True
    rows = await _observations(db_session)
    assert len(rows) == 1
    content = dict(rows[0].content or {})
    body = content["text"].lower()
    assert "quiet hours" not in body
    assert "what's up" not in body
    assert content["kind"] == "keep_intent"
    assert content["grounded"] is False
    assert content["keep_request"].startswith("I am holding something")
    memories = (
        (await db_session.execute(Memory.__table__.select())).mappings().all()
    )
    intent = [row for row in memories if (row["payload"] or {}).get("kind") == "keep_intent"]
    assert intent and intent[0]["payload"]["grounded"] is False


async def test_ungrounded_look_without_keep_writes_nothing(db_session: AsyncSession) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "media_kind": "frame",
            "spoken": "Nice to meet you too.",
            "request_id": "ungrounded-2",
        },
        actor="owner",
        device_id="phone-1",
    )
    await db_session.commit()
    assert written is None
    assert await _observations(db_session) == []


async def test_grounded_look_keeps_the_scene_and_the_flag(db_session: AsyncSession) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "colors": ["white"],
            "media_kind": "frame",
            "spoken": "A white mug with a blue wave logo.",
            "keep_request": "memorize this",
            "encoded_bytes": 120_000,
            "image_ready": True,
            "request_id": "grounded-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written and written.get("kept")
    rows = await _observations(db_session)
    assert len(rows) == 1
    content = dict(rows[0].content or {})
    assert content["grounded"] is True
    assert content["grounding"] in {"frame_bytes", "derived_facts"}
    assert "white mug" in content["text"].lower()


async def test_keep_intent_is_not_returned_for_content_recall(db_session: AsyncSession) -> None:
    """The intent answers "what did I ask you to remember?", never "what did I hold?"."""

    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "spoken": "Sure, happy to help.",
            "keep_request": "memorize this",
            "media_kind": "frame",
        },
        actor="owner",
        device_id="phone-1",
    )
    await db_session.commit()
    assert written and written.get("kept") is True
    content_hits = await search_visual_observations(
        db_session, "which book was I holding", enrich=False
    )
    assert content_hits == []
    keep_hits = await search_visual_observations(
        db_session, "what did I ask you to remember?", enrich=False
    )
    assert keep_hits
    assert keep_hits[0]["reason"] == "keep_intent"
    assert "no camera frame was stored" in keep_hits[0]["text"].lower()


async def test_clip_timeline_and_transcript_are_recallable(db_session: AsyncSession) -> None:
    await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["person"],
            "colors": ["blue"],
            "media_kind": "clip",
            "spoken": "A person in a blue shirt waves at the camera.",
            "attachment_id": "clip-att-1",
            "encoded_bytes": 2_000_000,
            "image_ready": True,
            "duration_s": 8.0,
            "transcript": "Okay, this is the part where I turn it around.",
            "moments": [
                {"t_start": 0.0, "t_end": 2.0, "labels": ["person"], "colors": ["blue"]},
                {"t_start": 4.0, "t_end": 6.0, "labels": ["mug"], "ocr_text": "EV"},
            ],
            "request_id": "clip-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    content = dict((await _observations(db_session))[0].content or {})
    assert content["media_kind"] == "clip"
    assert content["moment_count"] == 2
    assert "turn it around" in content["transcript"]
    assert "recorded a video clip" in content["text"].lower()
    assert "8 seconds" in content["text"].lower()
    hits = await search_visual_observations(db_session, "what did I say in the clip", enrich=False)
    assert hits, "transcript words must be searchable"
    assert hits[0].get("transcript")
    assert hits[0].get("moments")


# --------------------------------------------------------------------------
# Media-kind truth
# --------------------------------------------------------------------------


def test_phone_media_kinds_are_truthful() -> None:
    assert _phone_media_kind("look_once") == "frame"
    assert _phone_media_kind("observe") == "frame"
    assert _phone_media_kind("capture_photo") == "photo"
    assert _phone_media_kind("record_clip") == "burst"
    assert _phone_media_kind("record") == "burst"
    assert _phone_media_kind("something-new") == "frame"


def test_look_frame_meta_reads_clip_evidence() -> None:
    meta = parse_look_frame_meta({"has_clip": "true", "clip_supported": "false"})
    assert meta["has_clip"] is True
    assert meta["clip_supported"] is False
    assert parse_look_frame_meta({})["has_clip"] is None
    assert parse_look_frame_meta({"clip_ready": 0})["has_clip"] is False


async def test_phone_record_request_is_a_burst_not_a_video(db_session: AsyncSession) -> None:
    device = Device(
        name="Primary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    result = await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-record-1",
        jpeg_b64=base64.b64encode(_jpeg()).decode("ascii"),
        action="record_clip",
    )
    await db_session.commit()
    assert result["ok"] and result["media_kind"] == "burst"
    assert result["persisted_to_memory_os"] is True
    rows = await _observations(db_session)
    assert len(rows) == 1
    content = dict(rows[0].content or {})
    assert content["media_kind"] == "burst"
    assert content["grounded"] is True
    body = content["text"].lower()
    assert "recorded a video" not in body
    assert "short sequence of frames" in body


def test_phone_kind_never_upgrades_stills_to_video() -> None:
    assert _phone_media_kind("record_clip", requested="video", has_clip=False) == "burst"
    assert _phone_media_kind("record_clip", requested="video", has_clip=True) == "video"
    assert _phone_media_kind("record_clip", requested="clip", has_clip=None) == "burst"
    assert _phone_media_kind("look_once", requested="frame") == "frame"


async def test_phone_burst_writes_a_timestamped_timeline(db_session: AsyncSession) -> None:
    device = Device(
        name="Primary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    jpeg = base64.b64encode(_jpeg()).decode("ascii")
    frames = [
        {"jpeg_b64": jpeg, "captured_at_ms": offset, "sequence": index}
        for index, offset in enumerate([0, 750, 1500, 2250])
    ]
    result = await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-burst-1",
        jpeg_b64=jpeg,
        action="record_clip",
        frames=frames,
        media_kind="burst",
        has_clip=False,
    )
    await db_session.commit()
    assert result["media_kind"] == "burst"
    assert result["frame_count"] == 4
    moments = result["moments"]
    assert len(moments) == 4
    assert [m["t_start"] for m in moments] == [0.0, 0.75, 1.5, 2.25]
    assert all(m["t_end"] >= m["t_start"] for m in moments)
    content = dict((await _observations(db_session))[-1].content or {})
    assert content["media_kind"] == "burst"
    assert content["moment_count"] == 4
    assert "recorded a video" not in content["text"].lower()
    assert "short sequence of frames" in content["text"].lower()


async def test_phone_frame_without_facts_is_still_grounded(db_session: AsyncSession) -> None:
    """Bytes arrived, so the sighting is real even when Vision found nothing."""

    device = Device(
        name="Secondary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-dark-1",
        jpeg_b64=base64.b64encode(_jpeg()).decode("ascii"),
        action="look_once",
    )
    await db_session.commit()
    rows = await _observations(db_session)
    assert len(rows) == 1
    content = dict(rows[0].content or {})
    assert content["grounded"] is True
    assert content["grounding"] == "frame_bytes"


# --------------------------------------------------------------------------
# record_video wording follows evidence
# --------------------------------------------------------------------------


async def _record_request(session: LiveSession, call_id: str) -> object:
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                return event
    raise AssertionError("no camera_request was emitted")


async def test_burst_only_client_never_hears_a_recording_claim(
    db_session: AsyncSession,
) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-burst", device_id="phone-1", backchannel_enabled=False)
    task = asyncio.create_task(
        record_video_now(
            db_session,
            actor="owner",
            live_session_id="cam-burst",
            request_id="call-burst",
            duration_seconds=4,
        )
    )
    await _record_request(session, "call-burst")
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "call-burst",
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "media_kind": "burst",
            "has_clip": False,
            "clip_supported": False,
            "duration_ms": 4000,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert result["clip_evidence"] is False
    assert result["media_kind"] == "burst"
    assert "still frames, not a video" in spoken
    assert "never say a video or clip was recorded" in spoken
    assert "recorded a video" not in spoken
    content = dict((await _observations(db_session))[-1].content or {})
    assert content["media_kind"] == "burst"
    assert "recorded a video" not in content["text"].lower()
    assert "saved to" not in content["text"].lower()
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_real_clip_keeps_the_recording_wording(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-clip", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(
        record_video_now(
            db_session,
            actor="owner",
            live_session_id="cam-clip",
            request_id="call-clip",
            duration_seconds=3,
        )
    )
    await _record_request(session, "call-clip")
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "call-clip",
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "has_clip": True,
            "media_kind": "video",
            "saved_path": "/Users/owner/Movies/EV/EV-clip.mov",
            "duration_ms": 3000,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["clip_evidence"] is True
    assert result["media_kind"] == "video"
    assert "recorded" in spoken
    assert "ev-clip.mov" in spoken
    content = dict((await _observations(db_session))[-1].content or {})
    assert content["media_kind"] == "video"
    assert "recorded a video clip" in content["text"].lower()
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_phone_frame_runs_perception_and_memory_carries_names(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Server-side perception (objects + roster faces) reaches phone memory."""

    from app.ev import look as look_module

    async def fake_perception(session, data, *, attachment_id=None, max_labels=8):
        return {
            "labels": ["laptop", "cup"],
            "objects": [
                {
                    "label": "laptop",
                    "confidence": 0.9,
                    "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.3},
                    "class_id": 63,
                }
            ],
            "people_matches": [
                {"label": "Ada", "unknown": False, "confidence": 0.91, "entity_id": None}
            ],
            "engines": {"detect": "onnx", "face": "onnx"},
            "degraded": False,
        }

    monkeypatch.setattr(look_module, "local_perception", fake_perception)
    device = Device(
        name="Primary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    result = await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-perception-1",
        jpeg_b64=base64.b64encode(_jpeg()).decode("ascii"),
        action="look_once",
    )
    await db_session.commit()
    assert result["labels"][:2] == ["laptop", "cup"]
    assert "Ada" in result["spoken"]
    content = dict((await _observations(db_session))[0].content or {})
    assert content["people_names"] == ["Ada"]
    assert "possible person match: ada" in content["text"].lower()

    memories = (
        (await db_session.execute(select(Memory))).scalars().all()
    )
    visual = [row for row in memories if (row.payload or {}).get("kind") == "visual"]
    assert visual
    assert visual[-1].payload["people_names"] == ["Ada"]


async def test_phone_photo_stores_pixels_as_an_attachment(
    db_session: AsyncSession,
) -> None:
    """capture_photo keeps its pixels, and the look row references them."""

    from app.models import Attachment

    device = Device(
        name="Secondary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    jpeg = _jpeg()
    await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-photo-1",
        jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
        action="capture_photo",
    )
    await db_session.commit()
    attachments = (await db_session.execute(select(Attachment))).scalars().all()
    assert len(attachments) == 1
    assert attachments[0].content_type == "image/jpeg"
    assert attachments[0].size_bytes == len(jpeg)
    looks = (
        (
            await db_session.execute(
                select(Event).where(Event.event_type == "camera.look")
            )
        )
        .scalars()
        .all()
    )
    assert looks
    assert dict(looks[-1].content or {})["attachment_id"] == str(attachments[0].id)
    observations = await _observations(db_session)
    assert dict(observations[-1].content or {})["attachment_id"] == str(
        attachments[0].id
    )


async def test_phone_look_without_optin_keeps_pixels_ephemeral(
    db_session: AsyncSession,
) -> None:
    """Ordinary looks do not store pixels unless the owner opts in."""

    from app.models import Attachment

    device = Device(
        name="Secondary iPhone",
        trust_level="device",
        capabilities=["camera"],
        device_type="phone",
    )
    db_session.add(device)
    await db_session.flush()
    await ingest_phone_frame(
        db_session,
        device=device,
        request_id="phone-look-ephemeral",
        jpeg_b64=base64.b64encode(_jpeg()).decode("ascii"),
        action="look_once",
    )
    await db_session.commit()
    attachments = (await db_session.execute(select(Attachment))).scalars().all()
    assert attachments == []
