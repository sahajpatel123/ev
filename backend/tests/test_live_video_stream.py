"""Tests for continuous video streaming, rolling visual buffer, and place/object memory."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.camera_runtime import (
    LookFrame,
    build_visual_context_prompt,
    clear_live_visual_state,
    get_live_visual_state,
    update_live_visual_state,
)
from app.memory.room import infer_place_and_surface
from app.memory.visual import persist_continuous_stream_observation
from app.voice.live.session import LiveSession


def test_live_visual_state_lifecycle() -> None:
    session_id = "test-live-vid-1"
    clear_live_visual_state(session_id)

    # Initial frame
    f1 = LookFrame(
        request_id="stream-1",
        sequence=1,
        streaming=True,
        device_id="phone-alpha",
        camera_name="Back Camera",
        labels=["laptop", "mug"],
        ocr_text="Designing Data-Intensive Applications",
        luminance=0.55,
        place_hint="office",
        jpeg=b"\xff\xd8" + b"\x00" * 100,
    )
    state, is_novel = update_live_visual_state(session_id, f1)
    assert is_novel is True
    assert state.session_id == session_id
    assert state.total_frames_received == 1
    assert state.device_id == "phone-alpha"
    assert state.current_place == "office"
    assert len(state.keyframes) == 1
    assert "laptop" in state.all_observed_labels
    assert "Designing Data-Intensive Applications" in state.all_observed_ocr

    # Identical frame shortly after should NOT be novel (deduplication)
    f2 = LookFrame(
        request_id="stream-2",
        sequence=2,
        streaming=True,
        device_id="phone-alpha",
        camera_name="Back Camera",
        labels=["laptop", "mug"],
        ocr_text="Designing Data-Intensive Applications",
        luminance=0.55,
        jpeg=b"\xff\xd8" + b"\x00" * 100,
    )
    state, is_novel = update_live_visual_state(session_id, f2)
    assert is_novel is False
    assert state.total_frames_received == 2
    assert len(state.keyframes) == 1

    # Frame with new label or motion spike SHOULD be novel
    f3 = LookFrame(
        request_id="stream-3",
        sequence=3,
        streaming=True,
        device_id="phone-alpha",
        camera_name="Back Camera",
        labels=["laptop", "mug", "keys"],
        ocr_text="Designing Data-Intensive Applications",
        luminance=0.55,
        motion_score=0.35,
        jpeg=b"\xff\xd8" + b"\x00" * 100,
    )
    state, is_novel = update_live_visual_state(session_id, f3)
    assert is_novel is True
    assert state.total_frames_received == 3
    assert len(state.keyframes) == 2
    assert "keys" in state.all_observed_labels

    prompt = build_visual_context_prompt(state)
    assert "Live camera active from Back Camera" in prompt
    assert "Location/Place: office" in prompt
    assert "laptop" in prompt

    clear_live_visual_state(session_id)
    assert get_live_visual_state(session_id).current_frame is None


def test_infer_place_and_surface() -> None:
    # Kitchen inference
    place, surface = infer_place_and_surface(
        scene="Keys placed on the kitchen counter next to the microwave.",
        labels=["refrigerator", "keys"],
    )
    assert place == "kitchen"
    assert surface == "kitchen counter"

    # Office workbench inference
    place, surface = infer_place_and_surface(
        scene="Soldering iron and multimeter on the workbench in the office.",
        labels=["monitor", "keyboard"],
    )
    assert place == "office"
    assert surface == "workbench"

    # Direct place hint precedence
    place, surface = infer_place_and_surface(
        place_hint="Balcony",
        scene="A cup of coffee on the small table.",
    )
    assert place == "Balcony"
    assert surface in {"table", "small table", None}


@pytest.mark.asyncio
async def test_persist_continuous_stream_observation(db_session: AsyncSession) -> None:
    result = await persist_continuous_stream_observation(
        db_session,
        jpeg=b"\xff\xd8" + b"\x00" * 200,
        labels=["keys", "wallet"],
        ocr_text="Owner Pass",
        colors=["black", "silver"],
        device_id="phone-1",
        camera_name="iPhone 15 Pro",
        place_hint="living room",
        spoken_context="Left my keys on the coffee table.",
    )
    await db_session.commit()
    assert result is not None
    assert result.get("event_id") is not None

    # Memory recall check: locates keys left on coffee table
    from app.memory.recall import build_explicit_recall_payload

    pack = await build_explicit_recall_payload(
        db_session, "where did I leave my keys", k=6
    )
    spoken = str(pack.get("spoken") or "").lower()
    assert "keys" in spoken
    assert any(term in spoken for term in ("table", "coffee table", "living room", "last seen"))


@pytest.mark.asyncio
async def test_live_session_streaming_look_frame_ingestion() -> None:
    session = LiveSession(session_id="stream-sess-42")
    try:
        # Client sends a streaming frame without an existing synchronous request queue
        message = {
            "type": "look_frame",
            "request_id": "stream-sess-42-1",
            "streaming": True,
            "sequence": 1,
            "device_id": "iphone-12",
            "camera_name": "Back Camera",
            "labels": ["screwdriver", "circuit board"],
            "ocr_text": "Revision B",
            "luminance": 0.6,
            "place_hint": "lab",
        }
        await session._handle_look_frame(message)

        vstate = get_live_visual_state("stream-sess-42")
        assert vstate is not None
        assert vstate.total_frames_received == 1
        assert vstate.device_id == "iphone-12"
        assert vstate.current_place == "lab"
        assert "circuit board" in vstate.all_observed_labels
    finally:
        session.close()
        clear_live_visual_state("stream-sess-42")
