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


def test_keyframe_ring_buffer_bounded_capacity() -> None:
    session_id = "test-ring-buf-sess"
    clear_live_visual_state(session_id)
    try:
        for seq in range(1, 11):
            frame = LookFrame(
                request_id=f"frame-{seq}",
                sequence=seq,
                streaming=True,
                device_id="iphone-15",
                labels=[f"item-{seq}"],
                ocr_text=f"Text {seq}",
                luminance=0.5,
                jpeg=b"\xff\xd8" + b"\x00" * 100,
            )
            state, is_novel = update_live_visual_state(session_id, frame, force_novel=True)
            assert is_novel is True

        assert state.total_frames_received == 10
        # Ring buffer must be capped at max 5 keyframes
        assert len(state.keyframes) == 5
        # Oldest keyframe should be sequence 6, newest sequence 10
        assert state.keyframes[0].sequence == 6
        assert state.keyframes[-1].sequence == 10
        assert state.last_delivered_sequence == 10
    finally:
        clear_live_visual_state(session_id)


def test_multi_device_streaming_discrimination() -> None:
    session_id = "test-multi-dev-sess"
    clear_live_visual_state(session_id)
    try:
        # First frame from iPhone 1 (back camera)
        f_phone1 = LookFrame(
            request_id="p1-1",
            sequence=1,
            streaming=True,
            device_id="iphone-1",
            camera_name="iPhone 1 Back Camera",
            place_hint="Office",
            labels=["laptop", "keyboard"],
        )
        state1, _ = update_live_visual_state(session_id, f_phone1)
        assert state1.device_id == "iphone-1"
        assert state1.camera_name == "iPhone 1 Back Camera"
        prompt1 = build_visual_context_prompt(state1)
        assert "iPhone 1 Back Camera" in prompt1
        assert "Office" in prompt1

        # Second frame switches to iPhone 2 (front camera)
        f_phone2 = LookFrame(
            request_id="p2-1",
            sequence=2,
            streaming=True,
            device_id="iphone-2",
            camera_name="iPhone 2 Front Camera",
            place_hint="Living Room",
            labels=["couch"],
        )
        state2, _ = update_live_visual_state(session_id, f_phone2)
        assert state2.device_id == "iphone-2"
        assert state2.camera_name == "iPhone 2 Front Camera"
        assert state2.current_place == "Living Room"
        prompt2 = build_visual_context_prompt(state2)
        assert "iPhone 2 Front Camera" in prompt2
        assert "Living Room" in prompt2
    finally:
        clear_live_visual_state(session_id)


@pytest.mark.asyncio
async def test_streaming_frame_with_error_and_malformed_payload() -> None:
    session = LiveSession(session_id="err-stream-sess")
    try:
        # Frame with error reported
        err_msg = {
            "type": "look_frame",
            "request_id": "err-1",
            "streaming": True,
            "error": "permission_denied",
            "permission": "denied",
        }
        await session._handle_look_frame(err_msg)
        assert session._last_capture_status == "permission_denied"
        assert session._camera_state.get("permission_state") == "denied"

        # Frame with invalid image bytes
        bad_msg = {
            "type": "look_frame",
            "request_id": "bad-1",
            "streaming": True,
            "jpeg_b64": "not-valid-base64-!!!",
        }
        await session._handle_look_frame(bad_msg)
        # Should handle gracefully without unhandled exception
    finally:
        session.close()
        clear_live_visual_state("err-stream-sess")


@pytest.mark.asyncio
async def test_stream_observation_recall_across_surfaces(db_session: AsyncSession) -> None:
    from app.memory.recall import build_explicit_recall_payload

    # Persist sighting on workbench in garage
    await persist_continuous_stream_observation(
        db_session,
        jpeg=b"\xff\xd8" + b"\x00" * 150,
        labels=["multimeter", "soldering iron"],
        device_id="phone-1",
        place_hint="garage",
        spoken_context="Put the multimeter on the workbench.",
    )
    # Persist sighting on nightstand in bedroom
    await persist_continuous_stream_observation(
        db_session,
        jpeg=b"\xff\xd8" + b"\x00" * 150,
        labels=["glasses", "watch"],
        device_id="phone-2",
        place_hint="bedroom",
        spoken_context="Left my glasses on the nightstand.",
    )
    await db_session.commit()

    pack1 = await build_explicit_recall_payload(db_session, "where did I leave my multimeter", k=6)
    spoken1 = str(pack1.get("spoken") or "").lower()
    assert "multimeter" in spoken1
    assert any(term in spoken1 for term in ("workbench", "garage", "table"))

    pack2 = await build_explicit_recall_payload(db_session, "where are my glasses", k=6)
    spoken2 = str(pack2.get("spoken") or "").lower()
    assert "glasses" in spoken2
    assert any(term in spoken2 for term in ("nightstand", "bedroom", "table"))

