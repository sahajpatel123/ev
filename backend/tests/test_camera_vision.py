"""Camera visual perception: schema, POL, handshake, Realtime image events."""

from __future__ import annotations

import asyncio
import base64
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.camera_runtime import (
    OBSERVE_MAX_SECONDS,
    RECORD_MAX_SECONDS,
    VISION_TOOLS,
    CameraObservation,
    build_realtime_image_item,
    camera_image_prompt,
    camera_model_instructions,
    camera_operator_line,
    clamp_observe_duration,
    clamp_record_duration,
    coerce_vision_arguments,
    decode_frame_payload,
    dominant_color_names,
    jpeg_dimensions,
    lighting_from_luminance,
    looks_like_dark_excuse,
    name_rgb_color,
    overlay_vision_entry,
    pop_observations,
    readiness_from_camera_state,
    reset_pending_observations,
    stash_observation,
    validate_jpeg,
)
from app.ev.look import capture_photo_now, look_now, observe_camera_now, record_video_now
from app.ev.policy import OWNER_AUTO_PERCEPTION, evaluate_policy
from app.ev.tool_select import LIVE_VOICE_TOOLS, select_tool
from app.ev.tools import dispatch, get_spec
from app.gateway.validation import validate_arguments
from app.voice.live.grok_voice import GrokVoiceBridge
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


def test_look_schema_has_no_permission_argument() -> None:
    spec = get_spec("look")
    assert spec is not None
    properties = spec["parameters"]["properties"]
    assert "permission" not in properties
    observe = get_spec("observe_camera")
    assert observe is not None
    assert "permission" not in observe["parameters"]["properties"]
    assert observe["confirmation"] == "none"
    assert observe["risk_class"] == "R1"
    assert "look" in LIVE_VOICE_TOOLS
    assert "observe_camera" in LIVE_VOICE_TOOLS
    assert "capture_photo" in LIVE_VOICE_TOOLS
    assert "record_video" in LIVE_VOICE_TOOLS
    assert {"look", "observe_camera", "capture_photo", "record_video"} == VISION_TOOLS
    record = get_spec("record_video")
    assert record is not None
    assert "detail" in record["parameters"]["properties"]
    effective, issues = validate_arguments(
        {"detail": "high", "duration_seconds": 8},
        record["parameters"],
    )
    assert issues == []
    assert effective["detail"] == "high"


def test_record_video_coerces_copied_look_arguments() -> None:
    cleaned = coerce_vision_arguments(
        "record_video",
        {"detail": "high", "focus": "people", "duration": 5, "prompt": "me"},
    )
    assert cleaned["detail"] == "high"
    assert cleaned["duration_seconds"] == 5.0
    assert cleaned["prompt"] == "me"
    assert "focus" not in cleaned
    spec = get_spec("record_video")
    _, issues = validate_arguments(cleaned, spec["parameters"])
    assert issues == []


def test_look_coerces_objective_to_prompt() -> None:
    cleaned = coerce_vision_arguments(
        "look",
        {"objective": "remember the item I am showing you", "detail": "high"},
    )
    assert cleaned["prompt"] == "remember the item I am showing you"
    assert "objective" not in cleaned
    spec = get_spec("look")
    _, issues = validate_arguments(cleaned, spec["parameters"])
    assert issues == []


def test_owner_auto_authorizes_visual_perception() -> None:
    assert {"look", "observe_camera", "capture_photo", "record_video"} <= OWNER_AUTO_PERCEPTION
    for name in ("look", "observe_camera", "capture_photo", "record_video"):
        decision = evaluate_policy(
            name,
            actor="master",
            channel="voice",
            training_wheels_complete=True,
            provider_connected=True,
        )
        assert decision.allowed is True
        assert decision.confirmation_required is False
        assert decision.risk_class == "R1"


def test_observe_duration_is_bounded() -> None:
    assert clamp_observe_duration(99) == OBSERVE_MAX_SECONDS
    assert clamp_observe_duration(0) == 1.0
    assert clamp_record_duration(99) == RECORD_MAX_SECONDS
    assert clamp_record_duration(0) == 2.0


def test_lighting_and_dark_excuse_helpers() -> None:
    assert lighting_from_luminance(0.4) == "normally lit"
    assert lighting_from_luminance(0.05) == "dim"
    assert looks_like_dark_excuse("the photo is a bit darker so I could not see")
    assert not looks_like_dark_excuse("I can see a person holding a mug")
    assert name_rgb_color(250, 250, 248) == "white"
    assert name_rgb_color(12, 10, 9) == "black"
    assert "white" in dominant_color_names([(250, 250, 248)] * 20 + [(12, 10, 9)] * 2)
    instructions = camera_model_instructions(
        readiness_from_camera_state(
            {"permission_state": "authorized"},
            client_connected=True,
            realtime_provider="openai",
        )
    ).lower()
    assert "missing text is not a failure" in instructions
    assert "listed colors are scene hints" in instructions
    assert "attached images are already in the conversation" in instructions
    assert "natural sentences" in instructions
    assert "do not read the function json aloud" in instructions
    assert "readable text" not in instructions or "only when" in instructions
    assert "which app is open" in instructions
    assert "that is computer" in instructions


def test_jpeg_validation_and_realtime_item() -> None:
    raw = _jpeg(640, 480)
    validated = validate_jpeg(raw)
    assert validated is not None
    data, width, height = validated
    assert data.startswith(b"\xff\xd8")
    assert (width, height) == (640, 480)
    assert jpeg_dimensions(raw) == (640, 480)
    assert validate_jpeg(b"not-an-image") is None
    assert validate_jpeg(b"\xff\xd8" + b"x") is None
    item = build_realtime_image_item(raw, event_id="cam-1", detail="high")
    assert item["type"] == "conversation.item.create"
    assert item["event_id"] == "cam-1"
    content = item["item"]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(content[1]["image_url"].split(",", 1)[1])
    assert decoded == raw


def test_capability_overlay_requires_connected_client() -> None:
    entry = {
        "name": "look",
        "availability": "available",
        "model_exposed": True,
        "realtime_eligible": True,
        "executable": True,
    }
    disconnected = overlay_vision_entry(
        entry,
        readiness_from_camera_state(
            {},
            client_connected=False,
            realtime_provider="openai",
        ),
    )
    assert disconnected["availability"] == "not_connected"
    assert disconnected["realtime_eligible"] is False
    ready = overlay_vision_entry(
        entry,
        readiness_from_camera_state(
            {"permission_state": "authorized"},
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert ready["availability"] == "available"
    assert ready["capture_ready"] is True
    line = camera_operator_line(ready["camera"]).upper()
    assert "AVAILABLE" in line
    denied = overlay_vision_entry(
        entry,
        readiness_from_camera_state(
            {"permission_state": "denied", "state": "denied"},
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert denied["capture_ready"] is False
    assert "macOS" in camera_operator_line(denied["camera"]) or "macos" in camera_operator_line(
        denied["camera"]
    ).lower()


async def test_look_frame_request_id_and_jpeg_bytes() -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-1", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(session.request_look_frame(timeout=2, request_id="req-1"))
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    assert request.action == "capture"
    assert request.request_id == "req-1"
    jpeg = _jpeg()
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "req-1",
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            "permission": "authorized",
            "camera_name": "FaceTime HD Camera",
            "last": True,
        }
    )
    frame = await task
    assert frame is not None
    assert frame.request_id == "req-1"
    assert frame.jpeg == jpeg
    assert frame.width == 320
    assert frame.permission == "authorized"
    session.close()
    reset_live_registry()


async def test_look_frame_timeout_and_disconnect() -> None:
    reset_live_registry()
    session = LiveSession(session_id="cam-timeout", device_id="mac", backchannel_enabled=False)
    frame = await session.request_look_frame(timeout=0.05, request_id="req-timeout")
    assert frame is not None
    assert frame.error == "timeout"

    task = asyncio.create_task(session.request_look_frame(timeout=2, request_id="req-disc"))
    await asyncio.sleep(0)
    session.close()
    disconnected = await task
    assert disconnected is not None
    assert disconnected.error == "client_disconnected"
    reset_live_registry()


async def test_look_frame_malformed_and_permission_denied() -> None:
    reset_live_registry()
    session = LiveSession(session_id="cam-bad", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(session.request_look_frame(timeout=2, request_id="req-bad"))
    await asyncio.sleep(0)
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "req-bad",
            "jpeg_b64": base64.b64encode(b"not-a-jpeg-but-long-enough-to-pass-length").decode("ascii"),
        }
    )
    frame = await task
    assert frame.error == "malformed_image"
    assert frame.jpeg is None

    task = asyncio.create_task(session.request_look_frame(timeout=2, request_id="req-denied"))
    await asyncio.sleep(0)
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "req-denied",
            "error": "denied",
            "permission": "denied",
            "last": True,
        }
    )
    denied = await task
    assert denied.error == "denied"
    assert denied.permission == "denied"
    session.close()
    reset_live_registry()


async def test_sequential_looks_use_distinct_request_ids() -> None:
    reset_live_registry()
    session = LiveSession(session_id="cam-seq", device_id="mac", backchannel_enabled=False)
    first = asyncio.create_task(session.request_look_frame(timeout=2, request_id="a"))
    second = asyncio.create_task(session.request_look_frame(timeout=2, request_id="b"))
    await asyncio.sleep(0)
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "b",
            "jpeg_b64": base64.b64encode(_jpeg(100, 80)).decode("ascii"),
            "last": True,
        }
    )
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": "a",
            "jpeg_b64": base64.b64encode(_jpeg(200, 100)).decode("ascii"),
            "last": True,
        }
    )
    frame_a = await first
    frame_b = await second
    assert frame_a.width == 200
    assert frame_b.width == 100
    session.close()
    reset_live_registry()


async def test_observe_collects_multiple_frames() -> None:
    reset_live_registry()
    session = LiveSession(session_id="cam-obs", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(
        session.request_observe_frames(
            duration_s=3,
            interval_s=0.2,
            max_frames=3,
            timeout=2,
            request_id="obs-1",
        )
    )
    await asyncio.sleep(0)
    for index in range(3):
        await session.handle_client(
            {
                "type": "look_frame",
                "request_id": "obs-1",
                "jpeg_b64": base64.b64encode(_jpeg(120 + index, 80)).decode("ascii"),
                "sequence": index,
                "last": index == 2,
            }
        )
    frames = await task
    assert len(frames) == 3
    assert frames[-1].last is True
    session.close()
    reset_live_registry()


async def test_look_now_stashes_live_jpeg_without_persisting(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-stash", device_id="mac", backchannel_enabled=False)
    call_id = "call-live-1"
    task = asyncio.create_task(
        look_now(
            db_session,
            actor="master",
            live_session_id="cam-stash",
            request_id=call_id,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "last": True,
        }
    )
    result = await task
    assert result["ok"] is True
    assert result["image_ready"] is True
    assert result["persist_raw"] is False
    assert "attachment_id" not in result or result.get("attachment_id") is None
    spoken = (result.get("spoken") or "").lower()
    assert "too dark" not in spoken
    assert "could not see" not in spoken
    stashed = pop_observations(call_id)
    assert len(stashed) == 1
    assert stashed[0].jpeg.startswith(b"\xff\xd8")
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_realtime_bridge_injects_input_image(monkeypatch) -> None:
    reset_pending_observations()
    sent: list[dict] = []

    async def fake_send(self, payload, *, timeout_s: float = 2.0) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(GrokVoiceBridge, "_send", fake_send)
    bridge = GrokVoiceBridge(on_event=lambda event: asyncio.sleep(0), provider="openai")
    call_id = "call-inject"
    stash_observation(
        CameraObservation(
            request_id="req-inject",
            call_id=call_id,
            jpeg=_jpeg(),
            width=320,
            height=240,
            detail="high",
        )
    )
    output = await bridge._deliver_camera_images(
        "look",
        call_id,
        json.dumps({"ok": True, "spoken": "frame captured", "width": 320, "height": 240}),
    )
    payload = json.loads(output)
    assert payload["image_delivered"] is True
    assert payload["model_image_delivered"] is True
    assert payload["frames"] == 1
    assert sent[0]["type"] == "conversation.item.create"
    assert sent[0]["item"]["content"][1]["type"] == "input_image"
    assert "image_url" not in json.dumps(payload)
    reset_pending_observations()


def test_decode_frame_payload_accepts_data_url() -> None:
    raw = _jpeg()
    encoded = base64.b64encode(raw).decode("ascii")
    assert decode_frame_payload(f"data:image/jpeg;base64,{encoded}") == raw
    assert decode_frame_payload(encoded) == raw
    assert decode_frame_payload("") is None


def test_visual_intents_do_not_require_the_word_camera() -> None:
    assert select_tool("What am I holding?").selected == "look"
    assert select_tool("Read this.").selected == "look"
    assert select_tool("What's the weather?").selected != "look"
    assert select_tool("Take a photo of me.").selected == "capture_photo"
    assert select_tool("Take a picture.").selected == "capture_photo"
    assert select_tool("Record a video of this.").selected == "record_video"
    assert select_tool("Take a look at this.").selected == "look"


async def test_look_uses_client_vision_facts_instead_of_dark_excuse(
    db_session: AsyncSession,
) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-facts", device_id="mac", backchannel_enabled=False)
    call_id = "call-facts"
    task = asyncio.create_task(
        look_now(
            db_session,
            actor="master",
            live_session_id="cam-facts",
            request_id=call_id,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "labels": ["person", "mug"],
            "ocr_text": "HELLO",
            "luminance": 0.42,
            "lighting": "normally lit",
            "colors": ["white", "gray"],
            "face_count": 1,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert "mug" in spoken or "person" in spoken
    assert "hello" in spoken
    assert "white" in spoken
    assert "describe" in spoken
    assert "clothing" in spoken
    assert "too dark" not in spoken
    assert "could not see" not in spoken
    assert "readable text" not in spoken
    assert result.get("follow_up")
    assert "missing printed text is not a defect" in (result.get("follow_up") or "").lower()
    assert result.get("colors") == ["white", "gray"]
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_capture_photo_persists_and_is_not_look(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-save", device_id="mac", backchannel_enabled=False)
    call_id = "call-save"
    task = asyncio.create_task(
        capture_photo_now(
            db_session,
            actor="master",
            live_session_id="cam-save",
            request_id=call_id,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    assert request.action == "capture_save"
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "saved_path": "/Users/owner/Pictures/EV/EV-20260830.jpg",
            "labels": ["person"],
            "luminance": 0.5,
            "media_kind": "photo",
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert result["persist_raw"] is True
    assert result["media_kind"] == "photo"
    assert "saved" in spoken
    assert "person" in spoken
    assert "describe" in spoken
    assert "clothing" in spoken
    assert "do not only list labels" in spoken or "not only" in spoken
    stashed = pop_observations(call_id)
    assert len(stashed) == 1
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_record_video_is_distinct_from_observe(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-rec", device_id="mac", backchannel_enabled=False)
    call_id = "call-rec"
    task = asyncio.create_task(
        record_video_now(
            db_session,
            actor="master",
            live_session_id="cam-rec",
            request_id=call_id,
            duration_seconds=3,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    assert request.action == "record"
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "saved_path": "/Users/owner/Movies/EV/EV-clip.mov",
            "media_kind": "video",
            "duration_ms": 3000,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert result["media_kind"] == "video"
    assert result["persist_raw"] is True
    assert "recorded" in spoken
    assert "ev-clip.mov" in spoken
    assert "describe" in spoken
    assert "clothing" in spoken
    assert "what they are doing" in spoken
    assert "do not only say that you saved" in spoken
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_record_video_succeeds_without_poster_jpeg(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-rec-path", device_id="mac", backchannel_enabled=False)
    call_id = "call-rec-path"
    task = asyncio.create_task(
        record_video_now(
            db_session,
            actor="master",
            live_session_id="cam-rec-path",
            request_id=call_id,
            duration_seconds=3,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "permission": "authorized",
            "saved_path": "/Users/owner/Movies/EV/EV-me.mov",
            "media_kind": "video",
            "duration_ms": 8000,
            "labels": ["person"],
            "colors": ["white"],
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert result["saved_path"].endswith("EV-me.mov")
    assert "recorded" in spoken
    assert "white" in spoken
    assert "usable data" not in spoken
    assert pop_observations(call_id) == []
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_observe_reports_color_change_across_frames(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-obs", device_id="mac", backchannel_enabled=False)
    call_id = "call-obs"
    task = asyncio.create_task(
        observe_camera_now(
            db_session,
            actor="master",
            live_session_id="cam-obs",
            request_id=call_id,
            duration_seconds=3,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    jpeg = base64.b64encode(_jpeg()).decode("ascii")
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": jpeg,
            "permission": "authorized",
            "labels": ["remote"],
            "colors": ["black"],
            "luminance": 0.08,
            "lighting": "dim",
            "sequence": 0,
            "last": False,
        }
    )
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": jpeg,
            "permission": "authorized",
            "labels": ["remote"],
            "colors": ["white"],
            "luminance": 0.08,
            "lighting": "dim",
            "sequence": 1,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert "black" in spoken
    assert "white" in spoken
    assert "changed" in spoken
    assert "the frame is dim" not in spoken
    assert "readable text" not in spoken
    summaries = result.get("frames_summary") or []
    assert len(summaries) == 2
    assert summaries[0]["colors"] == ["black"]
    assert summaries[1]["colors"] == ["white"]
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_look_without_ocr_does_not_seek_text(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-scene", device_id="mac", backchannel_enabled=False)
    call_id = "call-scene"
    task = asyncio.create_task(
        look_now(
            db_session,
            actor="master",
            live_session_id="cam-scene",
            request_id=call_id,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg()).decode("ascii"),
            "permission": "authorized",
            "labels": ["person", "sofa"],
            "colors": ["white", "gray"],
            "luminance": 0.11,
            "lighting": "moderately lit",
            "face_count": 1,
            "person_count": 1,
            "last": True,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert "person" in spoken or "sofa" in spoken
    assert "white" in spoken
    assert "text reads" not in spoken
    assert "readable text" not in spoken
    assert "dark" not in spoken
    assert "local_ocr" not in result
    assert "missing printed text" in (result.get("follow_up") or "").lower()
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_record_delivery_stays_ok_without_image(monkeypatch) -> None:
    reset_pending_observations()
    sent: list[dict] = []

    async def fake_send(self, payload, *, timeout_s: float = 2.0) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(GrokVoiceBridge, "_send", fake_send)
    bridge = GrokVoiceBridge(on_event=lambda event: asyncio.sleep(0), provider="openai")
    output = await bridge._deliver_camera_images(
        "record_video",
        "call-rec-deliver",
        json.dumps(
            {
                "ok": True,
                "spoken": "I recorded 8 seconds and saved the clip to /Movies/EV/me.mov.",
                "saved_path": "/Movies/EV/me.mov",
                "media_kind": "video",
                "colors": ["white"],
            }
        ),
    )
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["saved_path"].endswith("me.mov")
    assert payload["image_delivered"] is False
    assert "local_ocr" not in payload
    assert "white" in payload.get("colors") or payload.get("colors") == ["white"]
    reset_pending_observations()


async def test_look_delivery_omits_empty_ocr_and_keeps_follow_up(monkeypatch) -> None:
    reset_pending_observations()
    sent: list[dict] = []

    async def fake_send(self, payload, *, timeout_s: float = 2.0) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(GrokVoiceBridge, "_send", fake_send)
    bridge = GrokVoiceBridge(on_event=lambda event: asyncio.sleep(0), provider="openai")
    call_id = "call-follow"
    stash_observation(
        CameraObservation(
            request_id="req-follow",
            call_id=call_id,
            jpeg=_jpeg(),
            width=320,
            height=240,
            detail="high",
        )
    )
    output = await bridge._deliver_camera_images(
        "look",
        call_id,
        json.dumps(
            {
                "ok": True,
                "spoken": "I can see a person. Colors look white.",
                "labels": ["person"],
                "colors": ["white"],
                "visual_facts": "a person; colors: white",
                "follow_up": "Later questions about this image should describe people.",
            }
        ),
    )
    payload = json.loads(output)
    prompt = sent[0]["item"]["content"][0]["text"].lower()
    assert payload["image_delivered"] is True
    assert "local_ocr" not in payload
    assert payload["colors"] == ["white"]
    assert "follow_up" in payload
    assert "missing text is not a failure" in prompt
    assert "too dark" in prompt
    assert "clothing" in prompt
    reset_pending_observations()


async def test_record_video_detail_does_not_reject_the_call(
    db_session: AsyncSession,
) -> None:
    result = await dispatch(
        db_session,
        "record_video",
        {"detail": "high", "focus": "people", "duration": 4},
        actor="master",
        allow_sensitive=True,
        channel="voice",
    )
    error = (result.error or "").lower()
    body = result.result or {}
    spoken = str(body.get("spoken") or "").lower()
    assert "unknown argument" not in error
    assert "invalid arguments" not in error
    assert "unknown argument" not in spoken
    assert "unknown_argument" not in spoken


def test_live_record_video_accepts_detail_argument() -> None:
    spec = get_spec("record_video")
    assert spec is not None
    bridge = GrokVoiceBridge(
        on_event=lambda event: asyncio.sleep(0),
        provider="openai",
        tool_specs=[spec],
    )
    bridge._upstream_session_ready = True
    bridge._upstream_tool_names = ("record_video",)
    effective, error = bridge._validate_function_call(
        "record_video",
        {"detail": "high", "focus": "people", "duration_seconds": 8},
    )
    assert error is None
    assert effective["detail"] == "high"
    assert effective["duration_seconds"] == 8
    assert "focus" not in effective


def test_camera_image_prompt_asks_for_a_real_description() -> None:
    look = camera_image_prompt("look").lower()
    capture = camera_image_prompt("capture_photo").lower()
    record = camera_image_prompt("record_video", index=1, total=3).lower()
    assert "clothing" in look
    assert "missing text is not a failure" in look
    assert "photo you just took" in capture
    assert "frame 2 of 3" in record
    assert "what they are doing" in record
    assert "do not only say the clip was saved" in record


async def test_record_video_stashes_multiple_poster_frames(db_session: AsyncSession) -> None:
    reset_live_registry()
    reset_pending_observations()
    session = LiveSession(session_id="cam-rec-multi", device_id="mac", backchannel_enabled=False)
    call_id = "call-rec-multi"
    task = asyncio.create_task(
        record_video_now(
            db_session,
            actor="master",
            live_session_id="cam-rec-multi",
            request_id=call_id,
            duration_seconds=4,
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg(320, 240)).decode("ascii"),
            "permission": "authorized",
            "labels": ["person"],
            "colors": ["white"],
            "sequence": 0,
            "last": False,
            "saved_path": "/Users/owner/Movies/EV/EV-wave.mov",
            "media_kind": "video",
            "duration_ms": 4000,
        }
    )
    await session.handle_client(
        {
            "type": "look_frame",
            "request_id": call_id,
            "jpeg_b64": base64.b64encode(_jpeg(640, 480)).decode("ascii"),
            "permission": "authorized",
            "labels": ["person", "mug"],
            "colors": ["white", "blue"],
            "sequence": 1,
            "last": True,
            "saved_path": "/Users/owner/Movies/EV/EV-wave.mov",
            "media_kind": "video",
            "duration_ms": 4000,
        }
    )
    result = await task
    spoken = (result.get("spoken") or "").lower()
    stashed = pop_observations(call_id)
    assert result["ok"] is True
    assert result["frames"] == 2
    assert len(stashed) == 2
    assert "mug" in spoken
    assert "blue" in spoken
    assert "describe" in spoken
    assert "what they are doing" in spoken
    session.close()
    reset_live_registry()
    reset_pending_observations()


async def test_record_delivery_asks_the_model_to_describe_the_clip(monkeypatch) -> None:
    reset_pending_observations()
    sent: list[dict] = []

    async def fake_send(self, payload, *, timeout_s: float = 2.0) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(GrokVoiceBridge, "_send", fake_send)
    bridge = GrokVoiceBridge(on_event=lambda event: asyncio.sleep(0), provider="openai")
    call_id = "call-rec-describe"
    for index in range(2):
        stash_observation(
            CameraObservation(
                request_id=f"req-rec-{index}",
                call_id=call_id,
                jpeg=_jpeg(),
                width=320,
                height=240,
                detail="high",
                sequence=index,
            )
        )
    output = await bridge._deliver_camera_images(
        "record_video",
        call_id,
        json.dumps(
            {
                "ok": True,
                "spoken": "Describe the clip.",
                "saved_path": "/Movies/EV/wave.mov",
                "media_kind": "video",
                "visual_facts": "a person; colors: white",
            }
        ),
    )
    payload = json.loads(output)
    prompts = [item["item"]["content"][0]["text"].lower() for item in sent]
    assert payload["image_delivered"] is True
    assert payload["frames"] == 2
    assert payload.get("describe_attached") is True
    assert any("what they are doing" in text for text in prompts)
    assert any("frame 1 of 2" in text for text in prompts)
    assert all("already saved" not in text for text in prompts)
    from app.voice.live.grok_voice import openai_realtime_instructions

    live = openai_realtime_instructions().lower()
    assert "describe the attached images in natural speech" in live
    assert "read the function json aloud" in live
    reset_pending_observations()

