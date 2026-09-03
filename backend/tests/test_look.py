"""Look tool: one consented camera frame, OCR, enrolled identification."""

from __future__ import annotations

import asyncio
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.capabilities import build_runtime_projection
from app.ev.policy import ROUTED_CAPABILITIES, evaluate_policy
from app.ev.tool_select import LIVE_VOICE_TOOLS, resolve_live_action, select_tool
from app.ev.tools import dispatch, get_spec
from app.ev.world_memory import enroll_owner_object
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession


def _drain(session: LiveSession) -> list:
    items = []
    while True:
        try:
            items.append(session.outbound.get_nowait())
        except asyncio.QueueEmpty:
            return items


async def _upload(client: AsyncClient, content: bytes = b"INVOICE EV-42 laptop") -> str:
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("look.png", content, "image/png")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["attachment"]["id"]


def test_look_is_live_r1_vision_capability() -> None:
    spec = get_spec("look")
    assert spec is not None
    assert spec["permission"] == "vision:read"
    assert spec["provider"] == "vision"
    assert spec["risk_class"] == "R1"
    assert "look" in LIVE_VOICE_TOOLS
    assert "look" in ROUTED_CAPABILITIES
    decision = evaluate_policy(
        "look",
        actor="master",
        channel="voice",
        training_wheels_complete=True,
        provider_connected=True,
    )
    assert decision.allowed is True
    assert decision.risk_class == "R1"
    assert decision.confirmation_required is False


async def test_look_is_ready_on_live_capability_manifest(db_session: AsyncSession) -> None:
    from app.ev.protocols import spoken_ready_capability_line
    from app.voice.live.layer import build_live_capability_manifest, reset_live_registry
    from app.voice.live.session import LiveSession

    projection = await build_runtime_projection(
        db_session,
        actor="master",
        realtime_provider="openai",
    )
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}
    assert by_name["look"]["provider"] == "vision"
    assert by_name["look"]["availability"] == "not_connected"
    names = {tool["name"] for tool in projection["realtime"]["tools"]}
    assert "look" not in names

    reset_live_registry()
    session = LiveSession(session_id="look-ready", device_id="mac", backchannel_enabled=False)
    session._camera_state["permission_state"] = "authorized"
    projection = await build_runtime_projection(
        db_session,
        actor="master",
        realtime_provider="openai",
        session_id="look-ready",
    )
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}
    assert by_name["look"]["availability"] == "available"
    assert by_name["look"]["realtime_eligible"] is True
    assert by_name["look"]["capture_ready"] is True
    names = {tool["name"] for tool in projection["realtime"]["tools"]}
    assert "look" in names
    manifest = build_live_capability_manifest(projection, provider="openai")
    line = spoken_ready_capability_line(manifest).lower()
    assert "camera" in line
    session.close()
    reset_live_registry()


def test_look_intent_does_not_steal_search_or_health() -> None:
    assert select_tool("what am I holding").selected == "look"
    assert select_tool("read this").selected == "look"
    assert select_tool("what color is this").selected == "look"
    assert select_tool("watch this and tell me when it turns green").selected == "observe_camera"
    assert select_tool("take a photo of me").selected == "capture_photo"
    assert select_tool("record a video").selected == "record_video"
    assert select_tool("how do i look").selected == "health_how_do_i_look"
    assert select_tool("look this up on the web").selected != "look"
    action = resolve_live_action("what do you see")
    assert action is not None
    assert action[0] == "look"


async def test_look_describes_attachment_ocr_and_enrolled_object(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await enroll_owner_object(
        db_session,
        name="Invoice",
        object_type="document",
    )
    await db_session.commit()
    attachment_id = await _upload(client)
    result = await dispatch(
        db_session,
        "look",
        {"attachment_id": attachment_id, "focus": "auto"},
        actor="master",
        allow_sensitive=True,
        channel="action",
    )
    await db_session.commit()
    assert result.ok is True
    body = result.result or {}
    assert body["ok"] is True
    assert body["raw_sent"] is False
    spoken = (body["spoken"] or "").lower()
    assert "invoice" in spoken or "ev-42" in spoken
    assert "stranger" not in spoken
    names = {item["name"].lower() for item in body.get("things") or []}
    assert "invoice" in names
    assert UUID(body["attachment_id"])


async def test_look_without_frame_is_honest(db_session: AsyncSession) -> None:
    result = await dispatch(
        db_session,
        "look",
        {},
        actor="master",
        allow_sensitive=True,
        channel="action",
    )
    body = result.result or {}
    assert body.get("ok") is False
    spoken = (body.get("spoken") or "").lower()
    assert "camera" in spoken or "photo" in spoken
    assert "i see" not in spoken


async def test_look_never_names_unenrolled_people(db_session: AsyncSession, client: AsyncClient) -> None:
    attachment_id = await _upload(client, content=b"a person standing near a desk")
    result = await dispatch(
        db_session,
        "look",
        {"attachment_id": attachment_id, "focus": "people"},
        actor="master",
        allow_sensitive=True,
        channel="action",
    )
    body = result.result or {}
    spoken = body.get("spoken") or ""
    assert "alice" not in spoken.lower()
    assert "bob" not in spoken.lower()
    for item in body.get("people") or []:
        assert not item.get("label")


async def test_live_look_frame_handshake() -> None:
    reset_live_registry()
    session = LiveSession(session_id="look-1", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(session.request_look_frame(timeout=2))
    request = None
    for _ in range(30):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "camera_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    assert request.action == "capture"
    await session.handle_client({"type": "look_frame", "attachment_id": "att-look-1"})
    frame = await task
    assert frame is not None
    assert frame.attachment_id == "att-look-1"
    session.close()
    reset_live_registry()


async def test_camera_capture_action_is_valid_and_does_not_activate() -> None:
    reset_live_registry()
    session = LiveSession(session_id="look-2", device_id="mac", backchannel_enabled=False)
    await session.handle_client({"type": "camera", "action": "capture", "device_id": "mac"})
    request = next(event for event in _drain(session) if event.type == "camera_request")
    assert request.action == "capture"
    assert session.interaction_snapshot()["camera_state"]["state"] == "off"
    session.close()
    reset_live_registry()
