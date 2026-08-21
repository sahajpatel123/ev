"""Camera visual perception: schema, POL, handshake, Realtime image events."""

from __future__ import annotations

import asyncio
import base64
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.camera_runtime import (
    OBSERVE_MAX_SECONDS,
    VISION_TOOLS,
    CameraObservation,
    build_realtime_image_item,
    camera_operator_line,
    clamp_observe_duration,
    decode_frame_payload,
    jpeg_dimensions,
    overlay_vision_entry,
    pop_observations,
    readiness_from_camera_state,
    reset_pending_observations,
    stash_observation,
    validate_jpeg,
)
from app.ev.look import look_now
from app.ev.policy import OWNER_AUTO_PERCEPTION, evaluate_policy
from app.ev.tool_select import LIVE_VOICE_TOOLS, select_tool
from app.ev.tools import get_spec
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
    assert {"look", "observe_camera"} == VISION_TOOLS


def test_owner_auto_authorizes_visual_perception() -> None:
    assert {"look", "observe_camera"} <= OWNER_AUTO_PERCEPTION
    for name in ("look", "observe_camera"):
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
