"""iPhone capability plan — automated acceptance gates."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.device_gateway.auth import parse_access_token
from app.device_gateway.live_fence import fence_phone_lives, fence_sandbox_lives
from app.everywhere.endpoint_profile import camera_quality_for_machine, merge_endpoint_profile
from app.main import app
from app.models import Device
from app.voice.live.events import ConversationMovedEvent
from app.voice.live.layer import register_live, reset_live_registry

ROOT = Path(__file__).resolve().parents[2]
PWA = ROOT / "backend" / "clients" / "pwa"
IOS = ROOT / "ios" / "EvieShell"


async def _pair(client: AsyncClient, *, role: str, name: str, platform: str = "ios") -> tuple[dict, AsyncClient]:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": role, "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.09.01.01",
            "platform": platform,
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": name + "-tab",
            "memory_scope": "owner",
            "role": "home_station",
        },
    )
    assert paired.status_code == 200, paired.text
    body = paired.json()
    phone.headers["Authorization"] = f"Bearer {body['device_token']}"
    return body, phone


class _FakeLive:
    def __init__(self, *, session_id: str, device_id: str, memory_scope: str, surface: str | None = None) -> None:
        self.session_id = session_id
        self.device_id = device_id
        self.memory_scope = memory_scope
        self.surface = surface
        self.closed = False
        self.events: list[object] = []

    def now(self) -> int:
        return 1

    async def emit(self, event: object) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pair_stays_sandbox_despite_client_owner_claim(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="primary_companion", name="Primary iPhone")
    assert body["memory_scope"] == "sandbox"
    assert body["device"]["trust_state"] == "PAIRED_SANDBOX"
    assert body["device"]["role"] == "primary_companion"
    assert body["device"]["platform"] == "ios"
    status = await phone.get("/v1/device-gateway/status")
    assert status.status_code == 200
    snap = status.json()
    assert snap["trust_state"] == "PAIRED_SANDBOX"
    assert snap["next_action"] == "promote_on_mac"
    assert snap["product"] == "Tailscale PWA"
    await phone.aclose()


@pytest.mark.asyncio
async def test_access_token_refresh_and_promotion_invalidation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    body, phone = await _pair(client, role="secondary_companion", name="SE Phone")
    session = await phone.post("/v1/device-gateway/session")
    assert session.status_code == 200
    token = session.json()["access_token"]
    parsed = parse_access_token(token)
    assert parsed is not None
    device_id, _, _, revision = parsed
    assert str(device_id) == body["device"]["device_id"]
    assert revision == 1

    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["device"]["trust_state"] == "TRUSTED_OWNER_DEVICE"

    stale = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    stale.headers["Authorization"] = f"Bearer {token}"
    denied = await stale.get("/v1/device-gateway/status")
    assert denied.status_code == 401
    await stale.aclose()

    fresh = await phone.post("/v1/device-gateway/session")
    assert fresh.status_code == 200
    hello = await phone.post(
        "/v1/device-gateway/hello",
        json={"protocol_version": "1", "instance_id": "SE Phone-tab", "platform": "ios"},
    )
    assert hello.status_code == 200
    assert hello.json()["status"]["trust_state"] == "TRUSTED_OWNER_DEVICE"
    assert hello.json()["status"]["next_action"] == "ready"
    await phone.aclose()


@pytest.mark.asyncio
async def test_trusted_text_uses_request_id_and_camera_preference(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    primary_body, primary = await _pair(client, role="primary_companion", name="Kitchen SE")
    pro_body, pro = await _pair(client, role="secondary_companion", name="Named Like Pro")
    for phone, device_id, machine in (
        (primary, primary_body["device"]["device_id"], "iPhone14,6"),
        (pro, pro_body["device"]["device_id"], "iPhone17,1"),
    ):
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": device_id, "reason": "owner"},
        )
        db_session.expire_all()
        row = await db_session.get(Device, UUID(device_id))
        assert row is not None
        merge_endpoint_profile(row, hardware={"model": machine}, permissions={"camera": "granted"})
        row.capabilities = ["foreground_voice", "camera", "text"]
        await db_session.commit()

    from app.device_gateway.presence import note as note_presence

    note_presence(__import__("uuid").UUID(primary_body["device"]["device_id"]), instance_id="a", state="ready")
    note_presence(__import__("uuid").UUID(pro_body["device"]["device_id"]), instance_id="b", state="ready")

    key = "req-" + uuid4().hex
    looked = await pro.post(
        "/v1/device-gateway/text",
        json={"text": "Look at this.", "instance_id": "Named Like Pro-tab", "request_id": key},
    )
    assert looked.status_code == 200, looked.text
    body = looked.json()
    assert body["camera_reason"] == "preferred_hardware"
    assert body["camera_target_device_id"] == pro_body["device"]["device_id"]
    assert body["executed"] is False
    await primary.aclose()
    await pro.aclose()


@pytest.mark.asyncio
async def test_offline_queue_201_409_422(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="companion", name="Queue Phone")
    key = "queuekey-" + uuid4().hex[:12]
    first = await phone.post(
        "/v1/device-gateway/queue",
        json={"idempotency_key": key, "kind": "capture", "payload": {"text": "note"}},
    )
    assert first.status_code == 201
    assert first.json()["executed"] is False
    dup = await phone.post(
        "/v1/device-gateway/queue",
        json={"idempotency_key": key, "kind": "capture", "payload": {"text": "note"}},
    )
    assert dup.status_code == 409
    assert dup.json()["executed"] is False
    bad = await phone.post(
        "/v1/device-gateway/queue",
        json={"idempotency_key": "short", "kind": "capture", "payload": {}},
    )
    assert bad.status_code == 422
    listed = await phone.get("/v1/device-gateway/queue")
    assert listed.status_code == 200
    assert listed.json()["items"]
    replayed = await phone.post(
        "/v1/device-gateway/queue/replay",
        json={"idempotency_key": key},
    )
    assert replayed.status_code == 200
    assert replayed.json()["executed"] is False
    assert replayed.json()["item"]["state"] == "accepted"
    await phone.aclose()


@pytest.mark.asyncio
async def test_turn_receipt_is_durable_and_not_self_authority(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.device_gateway.turn_receipts import record_turn_receipt

    body, phone = await _pair(client, role="primary_companion", name="Receipt Phone")
    await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    db_session.expire_all()
    device = await db_session.get(Device, UUID(body["device"]["device_id"]))
    assert device is not None
    key = "receipt-" + uuid4().hex[:12]
    first = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=key,
        transcript="Can you hear me?",
        session_id="sess-1",
    )
    await db_session.commit()
    assert first["durable"] is True
    assert first["authority"] is False
    assert first["life_mutation"] is False
    assert first.get("core_takeover") is not True
    clock_key = "receipt-clock-" + uuid4().hex[:12]
    clock = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=clock_key,
        transcript="What's the date today?",
        session_id="sess-1",
    )
    await db_session.commit()
    assert clock["core_takeover"] is True
    assert clock["core_route"] == "CLOCK"
    assert str(clock.get("core_reply") or "").startswith("It's")
    assert clock["authority"] is False
    name_key = "receipt-name-" + uuid4().hex[:12]
    unnamed = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=name_key,
        transcript="What's my name?",
        session_id="sess-1",
    )
    await db_session.commit()
    assert unnamed["core_takeover"] is True
    assert unnamed["core_route"] == "IDENTITY"
    from app.ev.assistant import set_owner_preferred_name

    await set_owner_preferred_name(db_session, "Sahaj")
    await db_session.commit()
    named_key = "receipt-named-" + uuid4().hex[:12]
    named = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=named_key,
        transcript="What's my name?",
        session_id="sess-1",
    )
    await db_session.commit()
    assert named["core_takeover"] is True
    assert "Sahaj" in str(named.get("core_reply") or "")
    weather_key = "receipt-wx-" + uuid4().hex[:12]
    weather = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=weather_key,
        transcript="What's the weather?",
        session_id="sess-1",
    )
    await db_session.commit()
    assert weather["core_takeover"] is True
    assert weather["core_route"] == "WEATHER"
    timer_key = "receipt-timer-" + uuid4().hex[:12]
    timer = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=timer_key,
        transcript="Set a timer for 5 minutes",
        session_id="sess-1",
    )
    await db_session.commit()
    assert timer["core_takeover"] is True
    assert timer["core_route"] == "HOME_STATION"
    assert timer["action_tool"] == "start_timer"
    assert timer["action_status"] == "COMPLETED"
    assert timer["action_executed"] is True
    assert timer["action_verified"] is True
    assert "timer" in str(timer.get("core_reply") or "").lower() or "minute" in str(
        timer.get("core_reply") or ""
    ).lower()
    replay = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key=key,
        transcript="Can you hear me?",
        session_id="sess-1",
    )
    assert replay["replayed"] is True
    await phone.aclose()


@pytest.mark.asyncio
async def test_inbox_records_conversation_move(client: AsyncClient) -> None:
    a_body, a = await _pair(client, role="primary_companion", name="Phone A")
    b_body, b = await _pair(client, role="secondary_companion", name="Phone B")
    claimed = await a.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "Phone A-tab", "method": "manual"},
    )
    assert claimed.status_code == 200
    moved = await b.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "Phone B-tab", "method": "manual"},
    )
    assert moved.status_code == 200
    beat = await a.post(
        "/v1/device-gateway/heartbeat",
        json={"instance_id": "Phone A-tab"},
    )
    assert beat.json().get("conversation_moved") is True
    inbox = await a.get("/v1/device-gateway/inbox")
    assert inbox.status_code == 200
    kinds = {item["kind"] for item in inbox.json()["items"]}
    assert "conversation_moved" in kinds
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio
async def test_stale_lease_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.device_gateway.live_authority import assert_live_authority
    from app.device_gateway.webrtc_live import attach_phone_control_live

    a_body, a = await _pair(client, role="primary_companion", name="Lease A")
    _b_body, b = await _pair(client, role="secondary_companion", name="Lease B")
    claimed = await a.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "Lease A-tab", "method": "manual"},
    )
    assert claimed.status_code == 200
    stolen = await b.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "Lease B-tab", "method": "manual"},
    )
    assert stolen.status_code == 200
    db_session.expire_all()
    device_a = await db_session.get(Device, UUID(a_body["device"]["device_id"]))
    assert device_a is not None
    attach_phone_control_live(
        device=device_a,
        session_id="lease-sess-a",
        actor="device:Lease A",
        instance_id="Lease A-tab",
    )
    with pytest.raises(HTTPException) as exc:
        await assert_live_authority(
            db_session,
            device=device_a,
            session_id="lease-sess-a",
            instance_id="Lease A-tab",
        )
    assert exc.value.status_code == 409
    live_tool = await a.post(
        "/v1/device-gateway/live/tool",
        json={
            "session_id": "lease-sess-a",
            "instance_id": "Lease A-tab",
            "name": "evie_state_query",
            "call_id": "stale-call-1",
            "arguments": {"query_text": "What's my name?"},
        },
    )
    assert live_tool.status_code == 409
    assert live_tool.headers.get("X-Error-Code") == "lease_not_held"
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_camera_target_uses_hardware_evidence_not_phone_name(
    db_session: AsyncSession,
) -> None:
    from app.everywhere.endpoint_profile import resolve_camera_target

    pro = Device(
        name="iPhone SE Backup",
        token_hash="camera-pro-evidence",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        endpoint_profile={
            "hardware": {
                "model": "iPhone17,1",
                "camera_quality": "pro",
                "camera_preference_rank": 0,
            },
            "permissions": {"camera": "granted"},
        },
    )
    se = Device(
        name="iPhone 16 Pro",
        token_hash="camera-se-evidence",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        endpoint_profile={
            "hardware": {
                "model": "iPhone14,6",
                "camera_quality": "standard",
                "camera_preference_rank": 10,
            },
            "permissions": {"camera": "granted"},
        },
    )
    db_session.add_all([pro, se])
    await db_session.commit()
    routed = await resolve_camera_target(
        db_session,
        origin=se,
        text="Look at this",
    )
    assert routed["device"].id == pro.id
    assert routed["reason"] == "preferred_hardware"
    assert routed["rank"] == 0
    explicit = await resolve_camera_target(
        db_session,
        origin=se,
        text="Look at this on this phone",
    )
    assert explicit["device"].id == se.id
    assert explicit["reason"] == "explicit_this_phone"


@pytest.mark.asyncio
async def test_offline_preferred_camera_never_claims_live_look(
    db_session: AsyncSession,
) -> None:
    from app.device_gateway.pipeline import run_trusted_device_turn

    device = Device(
        name="iPhone SE Fallback",
        token_hash="offline-camera-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        endpoint_profile={
            "hardware": {
                "model": "iPhone14,6",
                "camera_quality": "standard",
                "camera_preference_rank": 10,
            },
            "permissions": {"camera": "granted"},
        },
    )
    db_session.add(device)
    await db_session.commit()
    result = await run_trusted_device_turn(
        db_session,
        device=device,
        text="Look at this",
        idempotency_key="offline-look-001",
    )
    assert result["route"] == "CAMERA"
    assert result["needs_camera"] is False
    assert result["executed"] is False
    assert "offline" in result["reply"].lower()
    assert "inventing" in result["reply"].lower()


def test_camera_receipt_exposes_expiry_metadata_without_frame() -> None:
    from app.device_gateway.camera import get_frame, new_request

    request_id = new_request(
        origin_device_id="origin-phone",
        target_device_id="target-phone",
    )
    receipt = get_frame(request_id)
    assert receipt is not None
    assert receipt["request_id"] == request_id
    assert receipt["jpeg_b64"] is None
    assert receipt["expires_at"] > receipt["created_at"]


@pytest.mark.asyncio
async def test_visual_recall_without_durable_observation_stays_honest(
    db_session: AsyncSession,
) -> None:
    from app.device_gateway.pipeline import run_trusted_device_turn

    device = Device(
        name="Visual Recall Phone",
        token_hash="visual-recall-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await run_trusted_device_turn(
        db_session,
        device=device,
        text="What did you see?",
        idempotency_key="visual-recall-missing-001",
    )
    assert result["route"] == "VISUAL_RECALL"
    assert result["executed"] is False
    assert result["verified"] is False
    assert "saved camera observation" in result["reply"]


@pytest.mark.asyncio
async def test_healthkit_snapshot_never_claims_model_send(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="primary_companion", name="Health Phone")
    posted = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {"steps": 12}, "captured_at": "2026-09-01T00:00:00Z"},
    )
    assert posted.status_code == 200
    assert posted.json()["sent_to_model"] is False
    status = await phone.get("/v1/device-gateway/status")
    assert status.json()["healthkit"]["sent_to_model"] is False
    assert status.json()["healthkit"]["available"] is True
    unavailable = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {}, "available": False, "reason": "no_entitlement"},
    )
    assert unavailable.json()["freshness"] == "unavailable"
    assert unavailable.json()["sent_to_model"] is False
    await phone.aclose()


async def test_phone_fence_closes_trusted_phones_not_mac() -> None:
    reset_live_registry()
    mac = _FakeLive(session_id="mac", device_id="mac-1", memory_scope="owner")
    phone_a = _FakeLive(session_id="a", device_id="p1", memory_scope="owner", surface="phone")
    phone_b = _FakeLive(session_id="b", device_id="p2", memory_scope="sandbox", surface="phone")
    register_live(mac)
    register_live(phone_a)
    register_live(phone_b)
    closed = await fence_phone_lives(except_live=phone_a)
    assert closed == 1
    assert phone_b.closed is True
    assert phone_a.closed is False
    assert mac.closed is False
    sandbox_closed = await fence_sandbox_lives(except_live=phone_a)
    assert sandbox_closed == 1
    assert phone_a.closed is False
    assert mac.closed is False
    assert any(isinstance(ev, ConversationMovedEvent) for ev in phone_b.events)
    reset_live_registry()


def test_hardware_rank_ignores_display_names() -> None:
    assert camera_quality_for_machine("iPhone17,1") == ("pro", 0)
    assert camera_quality_for_machine("iPhone14,6") == ("standard", 10)
    d = Device(name="iPhone 16 Pro", token_hash="x", role="companion", device_type="phone")
    merge_endpoint_profile(d, hardware={"model": "iPhone14,6"}, permissions={})
    assert d.endpoint_profile["hardware"]["camera_quality"] == "standard"
    assert d.endpoint_profile["hardware"]["camera_preference_rank"] == 10


def test_trusted_webrtc_tools_are_server_validated() -> None:
    from app.device_gateway.webrtc_live import phone_webrtc_session

    d = Device(
        name="Owner Phone",
        token_hash="owner-phone-tools",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    cfg = phone_webrtc_session(device=d)
    names = [t.get("name") for t in cfg.get("tools", [])]
    assert names == ["evie_state_query", "phone_action", "evie_look", "evie_home_action"]
    blob = cfg["instructions"].lower()
    assert "evie_state_query" in blob
    assert "their name" in blob
    assert "home station" in blob
    home = next(t for t in cfg["tools"] if t.get("name") == "evie_home_action")
    caps = ((home.get("parameters") or {}).get("properties") or {}).get("capability") or {}
    assert "start_timer" in (caps.get("enum") or [])
    assert "list_reminders" in (caps.get("enum") or [])
    assert "home_act" in (caps.get("enum") or [])
    assert "resolve_contact" in (caps.get("enum") or [])
    assert "cancel_timer" in (caps.get("enum") or [])
    vad = cfg["audio"]["input"]["turn_detection"]
    assert vad["threshold"] == 0.68
    assert vad["silence_duration_ms"] == 700
    assert vad["create_response"] is True
    named = phone_webrtc_session(device=d, owner_name="Sahaj")
    assert "The person you are speaking with is Sahaj" in named["instructions"]
    assert "Sahaj" not in cfg["instructions"]


def test_phone_cognitive_public_reports_spark_or_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.device_gateway.webrtc_live import phone_cognitive_public
    from app.gateway.muse import MUSE_SPARK_MODEL

    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    legacy = phone_cognitive_public()
    assert legacy["muse_kernel"] is False
    assert legacy["realtime_thinks"] is True
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    kernel = phone_cognitive_public()
    assert kernel["muse_kernel"] is True
    assert kernel["brain"] == MUSE_SPARK_MODEL
    assert kernel["realtime_thinks"] is False
    assert "realtime" in str(kernel.get("speech") or "").lower() or "gpt-" in str(kernel.get("speech") or "")


def test_trusted_phone_webrtc_is_speech_coprocessor_when_muse_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.device_gateway.webrtc_live import phone_webrtc_session

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    d = Device(
        name="Owner Phone Spark",
        token_hash="owner-phone-spark",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    cfg = phone_webrtc_session(device=d, owner_name="Sahaj")
    assert cfg["tools"] == []
    assert cfg["tool_choice"] == "none"
    vad = cfg["audio"]["input"]["turn_detection"]
    assert vad["create_response"] is False
    assert vad["threshold"] == 0.68
    assert vad["silence_duration_ms"] == 700
    blob = cfg["instructions"]
    assert "not Evie's mind" in blob
    assert "voice coprocessor" in blob
    assert "evie_state_query" not in blob
    assert "Sahaj" not in blob


@pytest.mark.asyncio
async def test_muse_kernel_turn_receipt_lets_spark_decide(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.config import settings
    from app.device_gateway.turn_receipts import record_turn_receipt

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")

    async def fake_handle_turn(**kwargs: object) -> SimpleNamespace:
        assert "Can you hear me?" in str(kwargs.get("transcript") or "")
        return SimpleNamespace(spoken="Spark heard you.", kind="muse")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", fake_handle_turn)
    body, phone = await _pair(client, role="primary_companion", name="Spark Receipt Phone")
    await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    db_session.expire_all()
    device = await db_session.get(Device, UUID(body["device"]["device_id"]))
    assert device is not None
    heard = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="spark-heard-" + uuid4().hex[:12],
        transcript="Can you hear me?",
        session_id="sess-spark",
    )
    await db_session.commit()
    assert heard["core_takeover"] is True
    assert heard["core_reply"] == "Spark heard you."
    assert heard["core_route"] == "muse"

    kernel_calls = {"n": 0}

    async def boom(**kwargs: object) -> SimpleNamespace:
        kernel_calls["n"] += 1
        raise RuntimeError("spark down")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", boom)
    timer = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="spark-timer-" + uuid4().hex[:12],
        transcript="Set a timer for 5 minutes",
        session_id="sess-spark",
    )
    await db_session.commit()
    assert timer["core_takeover"] is True
    assert timer["core_route"] == "HOME_STATION"
    assert kernel_calls["n"] == 0
    dead = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="spark-dead-" + uuid4().hex[:12],
        transcript="Tell me a story about Saturn",
        session_id="sess-spark",
    )
    await db_session.commit()
    assert dead["core_takeover"] is True
    assert "can't think" in str(dead.get("core_reply") or "").lower()
    await phone.aclose()


def test_pwa_and_native_source_gates() -> None:
    app_js = (PWA / "app.js").read_text()
    webrtc = (PWA / "webrtc.js").read_text()
    html = (PWA / "index.html").read_text()
    native = (IOS / "App" / "NativeBridge.swift").read_text()
    broker = (IOS / "App" / "CapabilityBroker.swift").read_text()
    verify = (ROOT / "scripts" / "ios" / "verify-release.sh").read_text()
    product = (ROOT / "docs" / "IPHONE_PRODUCT.md").read_text()
    assert "_useDeviceToken" in app_js
    assert 'platform: detectPlatform()' in app_js
    assert "trust_state" in app_js
    assert "idempotency_key: requestId" in app_js
    assert "/v1/device-gateway/heartbeat" in webrtc
    assert "turn-receipt" in webrtc
    assert "_liveBody" in webrtc
    assert "leaseTimer" in webrtc
    assert "PAIRED_SANDBOX" in html
    assert "requestMediaCapturePermissionFor" in native
    assert "foreground_voice" in broker
    assert "look-frame" in app_js
    assert "pending_capture" in app_js
    assert 'delivery: "poll"' in app_js
    assert "web_notification" in app_js
    assert "EviePhoneAlerts" in app_js
    assert "enableLocalAlerts" in app_js
    assert 'id="enable-alerts-btn"' in html
    assert "local_timer" in (PWA / "mobile-actions.js").read_text()
    assert "/v1/device-gateway/sync/bootstrap" in app_js
    assert "isStandalonePwa" in app_js
    assert "cameraHardware" in app_js
    assert "owner_declared" in app_js
    assert "setCameraRole" in app_js
    assert "paintCameraRole" in app_js
    assert "Add to Home Screen" in html
    assert "Which iPhone is this?" in html
    assert 'data-surface="privacy"' in html
    assert 'data-surface="mission"' in html
    assert 'id="follow-chips"' in html
    assert "digitalFollowPrompts" in app_js
    assert 'id="more-sheet"' in html
    assert "folio-grid" in html
    assert "more-rail" not in html
    assert 'id="more-rail"' not in html
    assert 'data-quick="weather"' in html
    assert "paintLive" in app_js
    assert "choice-list" in html
    assert "camera-ask" in html
    assert "record_clip" in app_js
    assert "showCameraStatus" in app_js
    assert "waitForCameraReceipt" in app_js
    assert "_cameraReceiptGeneration" in app_js
    assert "receipt.expired" in app_js
    assert "/v1/device-gateway/camera/" in app_js
    assert "watchServiceWorkerUpdate" in app_js
    assert "Update ready" in app_js
    assert "Backend fingerprint" in app_js
    assert "asset_manifest_hash" in app_js
    assert "server_release" in app_js
    assert "copyPhoneDiagnostic" in app_js
    assert "evie.phone-diagnostic.v1" in app_js
    assert 'id="copy-phone-diag-btn"' in html
    assert 'hapticEvent("turnDone")' in app_js
    assert "confirmation required" in app_js
    assert "confirmation_required" in app_js
    assert 'aria-busy' in (PWA / "mobile-actions.js").read_text()
    assert 'data-action-state' in (PWA / "mobile-actions.js").read_text()
    css = (PWA / "style.css").read_text()
    assert "@media (max-width: 375px)" in css
    assert "@media (min-width: 391px) and (min-height: 800px)" in css
    assert "overflow-wrap: anywhere" in css
    assert "requestIdOverride" in app_js
    assert "await sendText(text, mark)" in app_js
    assert "preserveLease" in app_js
    assert "preserve_lease" in app_js
    assert "talkPhase" in app_js
    assert "Working on Home Station" in app_js
    assert "_captionStreaming" in app_js
    assert "previous.role === role" in app_js
    assert "home_station_capabilities" in app_js
    assert "Home Station executor" in app_js
    assert "never sent to a model" in app_js
    assert "/v1/device-gateway/phone-capabilities" in app_js
    assert "parsed.needs_camera" in webrtc
    assert "await this.onCamera({" in webrtc
    assert "opened.client_generation" in webrtc
    assert 'type === "response.output_audio.done"' in webrtc
    assert 'self.onState("failed")' in webrtc
    assert "_gateMicForPlayback" in webrtc
    assert "response.cancel" in webrtc
    assert "PLAYBACK_MIC_TAIL_MS = 800" in webrtc
    assert 'id="today-sheet"' in html
    assert "refreshToday" in app_js
    assert "scheduleHealthRender" in app_js
    assert "hardware: native.hardware" in app_js
    assert "healthkit_snapshot" in broker
    assert "calendar_snapshot" in broker
    assert "notification_status" in broker
    assert "siri_capture" in (IOS / "App" / "AppIntents.swift").read_text()
    assert "https://*.ts.net" in verify or "ts.net" in verify
    assert "EvieShell" in product
    assert "innerHTML" not in app_js
    assert "miniThinks" in webrtc
    assert "cognitive.muse_kernel" in app_js
    assert "cognitiveLine" in app_js
    assert '["Mind", cognitiveLine(hello)]' in app_js
    assert "phone_cognitive_public" in (
        ROOT / "backend" / "app" / "device_gateway" / "api.py"
    ).read_text()
    assert "PHONE_SPEECH_COPROCESSOR_CONTRACT" in (
        ROOT / "backend" / "app" / "device_gateway" / "mobile_voice.py"
    ).read_text()


def test_tailscale_pwa_is_the_release_path() -> None:
    product = (ROOT / "docs" / "IPHONE_PRODUCT.md").read_text()
    assert "/evie/" in product
    assert "Tailscale" in product
    assert "No Xcode" in product or "no Xcode" in product
    makefile = (ROOT / "Makefile").read_text()
    assert "iphone-parity-check" in makefile
    physical = (ROOT / "scripts" / "ios" / "physical-acceptance.sh").read_text()
    assert "iPhone 16 Pro" in physical
    assert "Add to Home Screen" in physical or "Safari" in physical


@pytest.mark.asyncio
async def test_push_poll_register_and_inbox_channel(client: AsyncClient) -> None:
    _body, phone = await _pair(client, role="primary_companion", name="Poll Phone")
    posted = await phone.post(
        "/v1/device-gateway/push/register",
        json={"token": "", "delivery": "poll", "bundle_id": "com.ev.evie.shell"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["delivery"] == "poll"
    assert posted.json()["registered"] is False
    inbox = await phone.get("/v1/device-gateway/inbox")
    assert inbox.status_code == 200
    assert inbox.json()["inbox_channel"] == "in_app_poll"
    assert inbox.json()["push_delivery"] == "poll"
    status = await phone.get("/v1/device-gateway/status")
    assert status.json()["notifications"]["inbox_channel"] == "in_app_poll"
    await phone.aclose()


@pytest.mark.asyncio
async def test_web_notification_register_stays_honest(client: AsyncClient) -> None:
    _body, phone = await _pair(client, role="primary_companion", name="Alert Phone")
    posted = await phone.post(
        "/v1/device-gateway/push/register",
        json={"token": "", "delivery": "web_notification", "bundle_id": "com.ev.evie.shell"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["delivery"] == "web_notification"
    status = await phone.get("/v1/device-gateway/status")
    assert status.json()["notifications"]["push_delivery"] == "web_notification"
    assert status.json()["notifications"]["inbox_channel"] == "in_app_poll"
    await phone.aclose()


@pytest.mark.asyncio
async def test_phone_core_reads_are_server_validated(client: AsyncClient, db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr("app.device_gateway.phone_core.home_coords", lambda: None)
    monkeypatch.setattr("app.device_gateway.phone_core.default_place", lambda: None)
    body, phone = await _pair(client, role="primary_companion", name="Core Phone")
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200
    weather = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's the weather?", "instance_id": "Core Phone-tab", "request_id": "wx-" + uuid4().hex[:12]},
    )
    assert weather.status_code == 200, weather.text
    assert weather.json()["route"] == "WEATHER"
    assert weather.json()["executed"] is False
    assert "place" in (weather.json().get("reply") or "").lower()

    named = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's my name?", "instance_id": "Core Phone-tab", "request_id": "nm0-" + uuid4().hex[:12]},
    )
    assert named.status_code == 200, named.text
    assert named.json()["route"] == "IDENTITY"
    from app.ev.assistant import set_owner_preferred_name

    await set_owner_preferred_name(db_session, "Sahaj")
    await db_session.commit()
    named2 = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's my name?", "instance_id": "Core Phone-tab", "request_id": "nm1-" + uuid4().hex[:12]},
    )
    assert named2.status_code == 200, named2.text
    assert named2.json()["route"] == "IDENTITY"
    assert "Sahaj" in (named2.json().get("reply") or "")

    clock = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's the date today?", "instance_id": "Core Phone-tab", "request_id": "dt-" + uuid4().hex[:12]},
    )
    assert clock.status_code == 200, clock.text
    assert clock.json()["route"] == "CLOCK"
    assert (clock.json().get("reply") or "").startswith("It's")

    can = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What can you do?", "instance_id": "Core Phone-tab", "request_id": "cap-" + uuid4().hex[:12]},
    )
    assert can.status_code == 200, can.text
    assert can.json()["route"] == "CAPABILITIES"
    assert "weather" in (can.json().get("reply") or "").lower()
    assert "home station" in (can.json().get("reply") or "").lower()

    empty_cal = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's on my calendar?", "instance_id": "Core Phone-tab", "request_id": "cal0-" + uuid4().hex[:12]},
    )
    assert empty_cal.status_code == 200, empty_cal.text
    empty_cal_body = empty_cal.json()
    assert empty_cal_body["route"] in {"CALENDAR", "HOME_STATION"}
    assert "Dentist" not in (empty_cal_body.get("reply") or "")
    assert empty_cal_body.get("conversational") is not True

    cal = await phone.post(
        "/v1/device-gateway/calendar/snapshot",
        json={"events": [{"title": "Dentist", "start": "2026-09-02T09:00:00Z"}]},
    )
    assert cal.status_code == 200
    assert cal.json()["sent_to_model"] is False
    asked = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What's on my calendar?", "instance_id": "Core Phone-tab", "request_id": "cal-" + uuid4().hex[:12]},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["route"] == "CALENDAR"
    assert "Dentist" in (asked.json().get("reply") or "")

    health = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "How many steps did I take?", "instance_id": "Core Phone-tab", "request_id": "hk-" + uuid4().hex[:12]},
    )
    assert health.status_code == 200
    assert health.json()["route"] == "HEALTHKIT"
    assert health.json().get("sent_to_model") is False
    book = await phone.post(
        "/v1/device-gateway/contacts/snapshot",
        json={"contacts": [{"name": "Maya"}]},
    )
    assert book.json()["sent_to_model"] is False
    listed = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Who's in my contacts?", "instance_id": "Core Phone-tab", "request_id": "co-" + uuid4().hex[:12]},
    )
    assert listed.status_code == 200
    assert listed.json()["route"] == "CONTACTS"
    assert "Maya" in (listed.json().get("reply") or "")

    await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {"steps": 99999}, "available": True},
    )
    steps = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "How many steps did I take?", "instance_id": "Core Phone-tab", "request_id": "hk2-" + uuid4().hex[:12]},
    )
    assert "99999" not in (steps.json().get("reply") or "")
    assert steps.json().get("sent_to_model") is False

    memory = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What did we talk about yesterday?", "instance_id": "Core Phone-tab", "request_id": "mem-" + uuid4().hex[:12]},
    )
    assert memory.status_code == 200
    assert memory.json()["route"] == "MEMORY"
    await phone.aclose()


@pytest.mark.asyncio
async def test_phone_core_weather_uses_home_location(db_session: AsyncSession, monkeypatch) -> None:
    from app.device_gateway.phone_core import maybe_phone_core_read

    monkeypatch.setattr("app.device_gateway.phone_core.home_coords", lambda: (37.77, -122.42))
    monkeypatch.setattr("app.device_gateway.phone_core.default_place", lambda: "San Francisco")

    class _Hit:
        snippet = "San Francisco: partly cloudy 18°C"

    async def _fake_weather(_query: str, limit: int = 2):
        return [_Hit()]

    monkeypatch.setattr("app.device_gateway.phone_core.weather_results", _fake_weather)
    d = Device(
        name="Weather Phone",
        token_hash="weather-phone-home",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    core = await maybe_phone_core_read(db_session, device=d, text="What's the weather?")
    assert core is not None
    assert core["route"] == "WEATHER"
    assert core["executed"] is True
    assert "partly cloudy" in (core.get("reply") or "")
    assert core["location"] == "San Francisco"
    assert core["location_source"] == "home_station_coordinates"


@pytest.mark.asyncio
async def test_phone_core_weather_timeout_is_explicit(
    db_session: AsyncSession, monkeypatch
) -> None:

    from app.device_gateway.phone_core import maybe_phone_core_read

    monkeypatch.setattr("app.device_gateway.phone_core.home_coords", lambda: (37.77, -122.42))
    monkeypatch.setattr("app.device_gateway.phone_core.default_place", lambda: "San Francisco")

    async def _timeout(_query: str, limit: int = 2):
        raise TimeoutError

    monkeypatch.setattr("app.device_gateway.phone_core.weather_results", _timeout)
    device = Device(
        name="Weather Timeout Phone",
        token_hash="weather-timeout-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await maybe_phone_core_read(
        db_session,
        device=device,
        text="What's the weather?",
    )
    assert result is not None
    assert result["executed"] is False
    assert result["error_code"] == "WEATHER_TIMEOUT"
    assert "won't guess" in result["reply"]


def test_healthkit_never_enters_webrtc_session() -> None:
    from app.device_gateway.webrtc_live import phone_webrtc_session

    d = Device(
        name="Private Phone",
        token_hash="private-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        endpoint_profile={"healthkit": {"snapshot": {"steps": 99999}, "sent_to_model": False}},
    )
    cfg = phone_webrtc_session(device=d)
    blob = str(cfg)
    assert "99999" not in blob
    assert "never sent to a model" in cfg["instructions"].lower() or "never sent to a model" in blob.lower()


@pytest.mark.asyncio
async def test_healthkit_value_never_enters_phone_receipt(
    db_session: AsyncSession,
) -> None:
    from app.device_gateway.turn_receipts import record_turn_receipt

    device = Device(
        name="Private Receipt Phone",
        token_hash="private-receipt-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        endpoint_profile={
            "healthkit": {
                "available": True,
                "snapshot": {"steps": 99999, "heart_rate": 211},
                "sent_to_model": False,
            }
        },
    )
    db_session.add(device)
    await db_session.commit()
    receipt = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="healthkit-receipt-private-001",
        transcript="How many steps did I take?",
        session_id="health-private",
    )
    core_reply = str(receipt.get("core_reply") or "")
    assert "99999" not in str(receipt)
    assert "99999" not in core_reply
    assert "211" not in core_reply
    assert "heart_rate" not in str(receipt)
    assert receipt.get("core_takeover") is True
    assert receipt.get("core_route") == "HEALTHKIT"


@pytest.mark.asyncio
async def test_sync_bootstrap_isolates_sandbox(client: AsyncClient) -> None:
    _body, phone = await _pair(client, role="companion", name="Sandbox Sync")
    boot = await phone.get("/v1/device-gateway/sync/bootstrap")
    assert boot.status_code == 200, boot.text
    payload = boot.json()
    assert payload["ok"] is True
    trust = (payload.get("device_trust") or {}).get("state")
    assert trust == "PAIRED_SANDBOX"
    await phone.aclose()


@pytest.mark.asyncio
async def test_pair_stores_hardware_not_display_name(client: AsyncClient) -> None:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "secondary_companion", "display_name": "iPhone 16 Pro"},
    )
    assert minted.status_code == 200
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": "iPhone 16 Pro",
            "protocol_version": "1",
            "client_version": "2026.09.02.02",
            "platform": "ios",
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": "se-tab",
            "hardware": {"model": "iPhone14,6"},
            "permissions": {"camera": "granted"},
        },
    )
    assert paired.status_code == 200, paired.text
    assert paired.json()["memory_scope"] == "sandbox"
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    status = await phone.get("/v1/device-gateway/status")
    profile = status.json().get("endpoint_profile") or {}
    hardware = profile.get("hardware") or {}
    assert hardware.get("camera_quality") == "standard"
    assert hardware.get("camera_preference_rank") == 10
    await phone.aclose()


@pytest.mark.asyncio
async def test_hello_owner_declared_camera_rank(client: AsyncClient) -> None:
    _body, phone = await _pair(client, role="primary_companion", name="Named Like Pro")
    hello = await phone.post(
        "/v1/device-gateway/hello",
        json={
            "protocol_version": "1",
            "instance_id": "Named Like Pro-tab",
            "platform": "ios",
            "hardware": {"camera_quality": "pro", "camera_preference_rank": 0},
        },
    )
    assert hello.status_code == 200, hello.text
    status = await phone.get("/v1/device-gateway/status")
    hardware = (status.json().get("endpoint_profile") or {}).get("hardware") or {}
    assert hardware.get("camera_quality") == "pro"
    assert hardware.get("camera_preference_rank") == 0
    await phone.aclose()


def test_spark_phone_skips_hearing_chat() -> None:
    from app.ev.spark_phone import looks_like_phone_chat, should_ask_spark

    assert looks_like_phone_chat("Can you hear me?")
    assert looks_like_phone_chat("hello")
    assert not looks_like_phone_chat("Set a timer for 5 minutes")
    assert not should_ask_spark("yes")
    assert not should_ask_spark("How many steps did I take?")
    assert should_ask_spark("Open Calculator on my Mac")


def test_spark_phone_structured_parser_normalizes_safe_aliases() -> None:
    from app.ev.spark_phone import _parse_tool

    assert _parse_tool('{"tool":"weather"}') == ("get_weather", {})
    assert _parse_tool(
        '```json\n{"tool":"message","to":"Maya","text":"On my way"}\n```'
    ) == ("send_message", {"to": "Maya", "text": "On my way"})
    assert _parse_tool('{"tool":"execute_command","goal":"rm -rf /"}') is None
    assert _parse_tool("not json") is None


def test_phone_action_surface_excludes_unsafe_computer_control() -> None:
    from app.device_gateway.phone_mac import _BLOCKED
    from app.ev.spark_phone import PHONE_MAC_TOOLS

    forbidden = {
        "execute_command",
        "drone",
        "computer",
        "code",
        "open_url",
        "ui_action",
        "inspect_ui",
        "screen_look",
        "app_action",
    }
    assert forbidden.isdisjoint(PHONE_MAC_TOOLS)
    assert forbidden <= _BLOCKED | {"execute_command", "drone"}


def test_phone_inputs_cannot_reach_urls_credentials_payments_or_unsafe_ui() -> None:
    from app.device_gateway.phone_mac import _BLOCKED, PHONE_HOME_CAPABILITIES
    from app.ev.spark_phone import _parse_tool

    forbidden = {
        "open_url",
        "execute_command",
        "computer",
        "code",
        "ui_action",
        "inspect_ui",
        "screen_look",
        "payments",
        "credentials",
    }
    assert forbidden.isdisjoint(PHONE_HOME_CAPABILITIES)
    assert {"open_url", "execute_command", "computer", "code"} <= _BLOCKED
    assert _parse_tool('{"tool":"execute_command","goal":"cat ~/.ssh/id_rsa"}') is None
    assert _parse_tool('{"tool":"open_url","url":"https://evil.example"}') is None
    assert _parse_tool('{"tool":"ui_action","action":"click"}') is None


def test_phone_scope_guard_fences_frozen_mac_surfaces() -> None:
    from app.device_gateway.phone_scope import (
        FROZEN_MAC_LIVE_SURFACES,
        fingerprint_mismatches,
        phone_scope_violations,
    )

    assert phone_scope_violations(
        ["backend/app/device_gateway/phone_mac.py", "backend/clients/pwa/app.js"]
    ) == ()
    violations = phone_scope_violations(
        ["backend/app/device_gateway/phone_mac.py", "macos/Sources/EV/LiveConversation.swift"]
    )
    assert violations == ("macos/Sources/EV/LiveConversation.swift",)
    assert "backend/app/voice/live/session.py" in FROZEN_MAC_LIVE_SURFACES
    assert fingerprint_mismatches({"phone.py": "a"}, {"phone.py": "b"}) == {
        "phone.py": ("b", "a")
    }


def test_phone_capability_catalog_matches_safe_dispatch_surface() -> None:
    from app.device_gateway.phone_mac import (
        PHONE_HOME_CAPABILITIES,
        PHONE_HOME_CAPABILITY_MANIFEST,
    )

    assert set(PHONE_HOME_CAPABILITY_MANIFEST["safe_actions"]) == set(
        PHONE_HOME_CAPABILITIES
    )
    assert PHONE_HOME_CAPABILITY_MANIFEST["executor"] == "home_station"
    assert "shell" in PHONE_HOME_CAPABILITY_MANIFEST["blocked"]


@pytest.mark.asyncio
async def test_spark_phone_structured_contributor_decides_action_only(
    monkeypatch,
) -> None:
    from app.contracts import ChatResult
    from app.ev.spark_phone import spark_phone_tool

    calls = {"count": 0}

    class _Provider:
        async def chat_structured(self, messages, *, schema, schema_name, model):
            calls["count"] += 1
            assert schema_name == "phone_mac_tool"
            assert "start_timer" in schema["properties"]["tool"]["enum"]
            return ChatResult(text='{"tool":"start_timer","minutes":7}')

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-1.3")
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _Provider(),
    )
    decided = await spark_phone_tool("Please start a seven minute timer.")
    assert decided == ("start_timer", {"minutes": 7})
    assert calls["count"] == 1

    hearing = await spark_phone_tool("Can you hear me?")
    assert hearing is None
    assert calls["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "provider", "malformed"])
async def test_spark_phone_failures_return_no_fake_action(
    monkeypatch, failure: str
) -> None:

    from app.contracts import ChatResult
    from app.ev.spark_phone import spark_phone_tool
    from app.gateway.muse import MuseProviderUnavailable

    class _Provider:
        async def chat_structured(self, messages, *, schema, schema_name, model):
            if failure == "timeout":
                raise TimeoutError
            if failure == "provider":
                raise MuseProviderUnavailable("unavailable")
            return ChatResult(text='{"tool": "start_timer"')

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-1.3")
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _Provider(),
    )
    result = await spark_phone_tool("Please handle this unusual owner request.")
    assert result is None


def test_phone_timer_list_and_cancel_phrases_resolve_to_home_tools() -> None:
    from app.ev.tool_select import resolve_live_action

    assert resolve_live_action("Show my pending timers") == ("list_timers", {})
    assert resolve_live_action("How many timers are running?") == ("list_timers", {})
    assert resolve_live_action("Cancel my timer") == ("cancel_timer", {})
    assert resolve_live_action("Cancel the timer for pasta") == (
        "cancel_timer",
        {"text": "pasta"},
    )
    assert resolve_live_action("Show my reminders") == ("list_reminders", {})
    assert resolve_live_action("Cancel reminder to stretch") == (
        "cancel_reminder",
        {"text": "stretch"},
    )
    assert resolve_live_action("Start a 10 minute timer") == ("start_timer", {"minutes": 10})
    assert resolve_live_action("Set a timer for 5 minutes") == ("start_timer", {"minutes": 5})


@pytest.mark.asyncio
async def test_phone_home_station_opens_calculator(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _open_app(session, name, arguments, **kwargs):
        assert name == "open_app"
        assert arguments["name"] == "Calculator"
        assert kwargs.get("device_id") is None
        return ToolCallResponse(
            name="open_app",
            ok=True,
            result={"ok": True, "spoken": "Opened Calculator.", "opened": True},
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _open_app)
    body, phone = await _pair(client, role="primary_companion", name="Act Phone")
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200
    opened = await phone.post(
        "/v1/device-gateway/text",
        json={
            "text": "Open calculator",
            "instance_id": "Act Phone-tab",
            "request_id": "calc-" + uuid4().hex[:12],
        },
    )
    assert opened.status_code == 200, opened.text
    payload = opened.json()
    assert payload["route"] == "HOME_STATION"
    assert payload["accepted"] is True
    assert payload["status"] == "COMPLETED"
    assert payload["executed"] is True
    assert payload["verified"] is True
    assert "Calculator" in (payload.get("reply") or "")

    db_session.expire_all()
    device = await db_session.get(Device, UUID(body["device"]["device_id"]))
    assert device is not None
    skipped = await maybe_phone_mac_act(db_session, device=device, text="Don't open Calculator.")
    assert skipped is None
    hearing = await maybe_phone_mac_act(
        db_session, device=device, text="Turn off the Wi-Fi after I finish this sentence."
    )
    assert hearing is None
    await phone.aclose()


@pytest.mark.asyncio
async def test_phone_transcript_helper_receipt_spoken_vertical_slice(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.turn_receipts import record_turn_receipt
    from app.schemas import ToolCallResponse

    calls: list[tuple[str, dict, object]] = []

    async def _fake_home_helper(session, name, arguments, **kwargs):
        calls.append((name, dict(arguments), kwargs.get("device_id")))
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "opened": True,
                "spoken": "Opened Calculator.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _fake_home_helper)
    device = Device(
        name="Vertical Slice Phone",
        token_hash="vertical-slice-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    receipt = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="vertical-slice-receipt-001",
        transcript="Open Calculator",
        session_id="vertical-slice-session",
    )
    await db_session.commit()
    assert calls == [("open_app", {"name": "Calculator"}, None)]
    assert receipt["core_takeover"] is True
    assert receipt["core_reply"] == "Opened Calculator."
    assert receipt["action_tool"] == "open_app"
    assert receipt["action_status"] == "COMPLETED"
    assert receipt["action_executed"] is True
    assert receipt["action_verified"] is True


def test_phone_calculator_phrases_are_deterministic() -> None:
    from app.device_gateway.phone_mac import _phrase_action
    from app.ev.tool_select import resolve_live_action

    assert _phrase_action("Launch the calculator") == (
        "open_app",
        {"name": "Calculator"},
    )
    assert _phrase_action("Quit Calculator") == (
        "close_app",
        {"name": "Calculator"},
    )
    assert resolve_live_action("Open browser") == (
        "open_app",
        {"name": "browser"},
    )
    assert resolve_live_action("Close VS Code") == (
        "close_app",
        {"name": "VS Code"},
    )


@pytest.mark.asyncio
async def test_phone_calculator_helper_failure_is_truthful(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _missing_helper(session, name, arguments, **kwargs):
        return ToolCallResponse(
            name=name,
            ok=False,
            result={
                "ok": False,
                "spoken": "I need the EV app live to operate the Mac.",
            },
            latency_ms=1,
            error="MAC_HELPER_UNAVAILABLE",
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _missing_helper)
    device = Device(
        name="Calculator Failure Phone",
        token_hash="calculator-failure-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Open Calculator",
        idempotency_key="calculator-failure-001",
    )
    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "FAILED"
    assert result["executed"] is False
    assert "EV app live" in result["reply"]


@pytest.mark.asyncio
async def test_phone_mail_read_uses_home_station_truthfully(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _mail_success(session, name, arguments, **kwargs):
        assert name == "list_mail"
        assert kwargs.get("device_id") is not None
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "spoken": "Latest mail: Maya — the meeting moved to 3 PM.",
                "messages": [{"subject": "Meeting", "body": "private body omitted"}],
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _mail_success)
    device = Device(
        name="Mail Phone",
        token_hash="mail-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    success = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Check my mail",
    )
    assert success is not None
    assert success["status"] == "COMPLETED"
    assert success["executed"] is True
    assert "meeting moved" in success["reply"]
    assert "private body omitted" not in success["reply"]

    async def _mail_missing(session, name, arguments, **kwargs):
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": False,
                "degraded": True,
                "error": "not_connected",
                "spoken": "Home Station Mail isn't connected yet.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _mail_missing)
    missing = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Read my email",
    )
    assert missing is not None
    assert missing["status"] == "FAILED"
    assert missing["executed"] is False
    assert "isn't connected" in missing["reply"]


@pytest.mark.asyncio
async def test_phone_calendar_read_uses_home_station_truthfully(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _calendar_success(session, name, arguments, **kwargs):
        assert name == "calendar_read"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "spoken": "Upcoming: Dentist at 9 AM.",
                "events": [{"title": "Dentist", "start": "9 AM"}],
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _calendar_success)
    device = Device(
        name="Calendar Phone",
        token_hash="calendar-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    success = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="What's on my calendar?",
    )
    assert success is not None
    assert success["status"] == "COMPLETED"
    assert success["executed"] is True
    assert "Dentist" in success["reply"]

    async def _calendar_missing(session, name, arguments, **kwargs):
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": False,
                "degraded": True,
                "error": "not_connected",
                "spoken": "Home Station Calendar isn't connected yet.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _calendar_missing)
    missing = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="What meetings are on my calendar?",
    )
    assert missing is not None
    assert missing["status"] == "FAILED"
    assert missing["executed"] is False
    assert "isn't connected" in missing["reply"]


@pytest.mark.asyncio
async def test_phone_messages_read_is_read_only_and_truthful(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _messages_success(session, name, arguments, **kwargs):
        assert name == "list_messages"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "spoken": "Latest message from Maya: Running 10 minutes late.",
                "messages": [{"sender": "Maya", "body": "private body omitted"}],
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _messages_success)
    device = Device(
        name="Messages Phone",
        token_hash="messages-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    success = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Check my messages",
    )
    assert success is not None
    assert success["status"] == "COMPLETED"
    assert success["executed"] is True
    assert "10 minutes late" in success["reply"]
    assert "private body omitted" not in success["reply"]

    async def _messages_missing(session, name, arguments, **kwargs):
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": False,
                "degraded": True,
                "error": "not_connected",
                "spoken": "Home Station Messages isn't connected yet.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _messages_missing)
    missing = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Read my messages",
    )
    assert missing is not None
    assert missing["status"] == "FAILED"
    assert missing["executed"] is False
    assert "isn't connected" in missing["reply"]


@pytest.mark.asyncio
async def test_phone_message_send_separates_fields_and_replays_once(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    calls = {"count": 0}

    async def _send_success(session, name, arguments, **kwargs):
        calls["count"] += 1
        assert name == "send_message"
        assert arguments["to"] == "Maya"
        assert arguments["text"] == "I am running late"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "sent": True,
                "to": "Maya",
                "channel": "messages",
                "spoken": "Sent a message to Maya.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _send_success)
    device = Device(
        name="Send Phone",
        token_hash="send-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    first = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Text Maya I am running late",
        idempotency_key="phone-message-retry-001",
    )
    second = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Text Maya I am running late",
        idempotency_key="phone-message-retry-001",
    )
    assert first and first["status"] == "COMPLETED"
    assert first["sent"] is True
    assert second and second.get("idempotent_replay") is True
    assert calls["count"] == 1

    async def _composer_only(session, name, arguments, **kwargs):
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "sent": False,
                "opened": True,
                "channel": "messages",
                "spoken": "I opened Messages, but the message is not sent.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _composer_only)
    prepared = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Text Maya I am running late",
        idempotency_key="phone-message-composer-001",
    )
    assert prepared is not None
    assert prepared["status"] == "FAILED"
    assert prepared["executed"] is False
    assert prepared["sent"] is False
    assert "not sent" in prepared["reply"]


@pytest.mark.asyncio
async def test_phone_call_reports_initiation_without_claiming_connection(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _call(session, name, arguments, **kwargs):
        assert name == "place_call"
        assert arguments["name"] == "Maya"
        assert arguments["kind"] == "facetime"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "opened": True,
                "connected": False,
                "spoken": "Opening FaceTime to Maya.",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _call)
    device = Device(
        name="Call Phone",
        token_hash="call-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="FaceTime Maya",
    )
    assert result is not None
    assert result["status"] == "COMPLETED"
    assert result["executed"] is True
    assert result["verified"] is False
    assert result["connected"] is False
    assert "Opening FaceTime" in result["reply"]
    assert "connected" not in result["reply"].lower()


@pytest.mark.asyncio
async def test_phone_contact_resolution_does_not_return_raw_address_book_data(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _contacts(session, name, arguments, **kwargs):
        assert name == "resolve_contact"
        assert arguments["name"] == "Maya"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "matches": [
                    {
                        "full_name": "Maya Patel",
                        "phone": "+15551234567",
                        "email": "maya@example.com",
                    }
                ],
                "spoken": "Maya Patel +15551234567 maya@example.com",
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _contacts)
    device = Device(
        name="Contacts Phone",
        token_hash="contacts-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="What is Maya's phone number?",
    )
    assert result is not None
    assert result["status"] == "COMPLETED"
    assert "Maya Patel" in result["reply"]
    assert "+15551234567" not in result["reply"]
    assert "maya@example.com" not in result["reply"]
    assert "matches" not in result


@pytest.mark.asyncio
async def test_phone_home_action_only_allows_verified_light_on_off(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _home(session, name, arguments, **kwargs):
        assert name == "home_act"
        assert arguments["entity"] == "living room lights"
        assert arguments["action"] == "on"
        return ToolCallResponse(
            name=name,
            ok=True,
            result={
                "ok": True,
                "spoken": "Living room lights are now on.",
                "simulated": False,
                "evidence": {"accepted": True, "observed": True},
            },
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _home)
    device = Device(
        name="Home Action Phone",
        token_hash="home-action-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()
    result = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Turn on the living room lights",
    )
    assert result is not None
    assert result["status"] == "COMPLETED"
    assert result["executed"] is True
    assert result["verified"] is True

    blocked = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Unlock the front door",
    )
    assert blocked is not None
    assert blocked["status"] == "FAILED"
    assert blocked["error_code"] == "PHONE_HOME_ACTION_LIMIT"
    assert blocked["executed"] is False


@pytest.mark.asyncio
async def test_phone_home_station_sets_timer(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="primary_companion", name="Timer Phone")
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200
    timed = await phone.post(
        "/v1/device-gateway/text",
        json={
            "text": "Set a timer for 5 minutes",
            "instance_id": "Timer Phone-tab",
            "request_id": "tmr-" + uuid4().hex[:12],
        },
    )
    assert timed.status_code == 200, timed.text
    timer_body = timed.json()
    assert timer_body["route"] == "HOME_STATION"
    assert timer_body["accepted"] is True
    assert timer_body["status"] == "COMPLETED"
    assert timer_body["executed"] is True
    assert timer_body["queued"] is False
    assert timer_body.get("tool") == "start_timer" or timer_body.get("operation") == "start_timer"
    local = timer_body.get("phone_action") or {}
    card = local.get("card") or local
    assert card.get("pwa_kind") == "local_timer"
    assert "clock" in str(timer_body.get("reply") or "").lower()
    from app.device_gateway.mobile_actions.tool import dispatch_phone_action

    native_miss = await dispatch_phone_action(
        device_id=body["device"]["device_id"],
        role="primary_companion",
        instance_id="Timer Phone-tab",
        session_id="sess-timer",
        origin="https://home.example.ts.net",
        arguments={"operation": "create_timer", "duration_minutes": 3},
        transcript="",
        device_label="Timer Phone",
    )
    assert native_miss.get("home_station") is True
    assert native_miss.get("ok") is True
    assert "timer" in str(native_miss.get("spoken") or "").lower() or "minute" in str(
        native_miss.get("spoken") or ""
    ).lower()
    await phone.aclose()


@pytest.mark.asyncio
async def test_sandbox_phone_cannot_dispatch_home_station(db_session: AsyncSession) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act

    d = Device(
        name="Sandbox Act",
        token_hash="sandbox-act",
        trust_level="paired",
        memory_scope="sandbox",
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    acted = await maybe_phone_mac_act(db_session, device=d, text="Open calculator")
    assert acted is None


def test_sandbox_phone_does_not_advertise_owner_state_or_home_brokers() -> None:
    from app.device_gateway.webrtc_live import phone_webrtc_session

    device = Device(
        name="Sandbox Live Phone",
        token_hash="sandbox-live-phone",
        trust_level="paired",
        memory_scope="sandbox",
        device_type="phone",
    )
    cfg = phone_webrtc_session(device=device)
    names = {tool.get("name") for tool in cfg.get("tools", [])}
    assert "evie_state_query" not in names
    assert "evie_home_action" not in names
    assert "home_station_capabilities" not in cfg["instructions"]


@pytest.mark.asyncio
async def test_revoked_phone_fails_closed_for_action_receipt_and_live_surface(
    db_session: AsyncSession,
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.device_gateway.turn_receipts import record_turn_receipt
    from app.device_gateway.webrtc_live import phone_webrtc_session
    from app.utils.text import utcnow

    device = Device(
        name="Revoked Phone",
        token_hash="revoked-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
        revoked_at=utcnow(),
        revoked_reason="cycle-22",
    )
    db_session.add(device)
    await db_session.commit()
    assert (
        await maybe_phone_mac_act(
            db_session,
            device=device,
            text="Open Calculator",
        )
        is None
    )
    receipt = await record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="revoked-phone-receipt-001",
        transcript="Open Calculator",
        session_id="revoked-session",
    )
    assert receipt["trusted_owner"] is False
    assert receipt.get("core_takeover") is not True
    cfg = phone_webrtc_session(device=device)
    names = {tool.get("name") for tool in cfg.get("tools", [])}
    assert "evie_state_query" not in names
    assert "evie_home_action" not in names


def test_phone_home_station_status_contract() -> None:
    from app.device_gateway.phone_mac import _ok

    completed = _ok("Opened Calculator.", route="HOME_STATION", tool="open_app", executed=True)
    assert completed["accepted"] is True
    assert completed["status"] == "COMPLETED"
    assert completed["executed"] is True
    assert completed["verified"] is True
    assert completed["queued"] is False
    assert completed["error_code"] is None
    queued = _ok(
        "Queued for Home Station.",
        route="HOME_STATION",
        tool="start_timer",
        executed=False,
        queued=True,
    )
    assert queued["status"] == "QUEUED"
    assert queued["accepted"] is True
    assert queued["ok"] is True
    failed = _ok(
        "I couldn't complete that.",
        route="HOME_STATION",
        tool="calendar_read",
        executed=False,
        ok=False,
        error_code="not_connected",
    )
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "not_connected"
    assert failed["verified"] is False


@pytest.mark.asyncio
async def test_phone_timer_and_reminder_retries_are_exactly_once(
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import select

    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.models import Alert, OwnerTimer

    device = Device(
        name="Idempotent Phone",
        token_hash="idempotent-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()

    timer_first = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Set a timer for 3 minutes",
        idempotency_key="phone-timer-retry-001",
    )
    timer_second = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Set a timer for 3 minutes",
        idempotency_key="phone-timer-retry-001",
    )
    timers = list((await db_session.execute(select(OwnerTimer))).scalars().all())
    assert timer_first and timer_first["executed"] is True
    assert timer_second and timer_second.get("idempotent_replay") is True
    assert len(timers) == 1

    reminder_first = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Remind me to stretch",
        idempotency_key="phone-reminder-retry-001",
    )
    reminder_second = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Remind me to stretch",
        idempotency_key="phone-reminder-retry-001",
    )
    alerts = list(
        (
            await db_session.execute(
                select(Alert).where(Alert.source == "set_reminder")
            )
        ).scalars().all()
    )
    assert reminder_first and reminder_first["executed"] is True
    assert reminder_second and reminder_second.get("idempotent_replay") is True
    assert len(alerts) == 1

    listed = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Show my reminders",
    )
    assert listed and listed["executed"] is True
    assert listed["count"] == 1
    cancelled = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Cancel reminder to stretch",
    )
    assert cancelled and cancelled["executed"] is True
    assert "cancelled" in str(cancelled["reply"]).lower()
    listed_again = await maybe_phone_mac_act(
        db_session,
        device=device,
        text="Show my reminders",
    )
    assert listed_again and listed_again["count"] == 0


def test_people_display_name_never_uses_a_number() -> None:
    from app.device_gateway.phone_people import display_name

    assert display_name("Patel, Rahul") == "Rahul Patel"
    assert display_name("Ada Lovelace <ada@example.com>") == "Ada Lovelace"
    assert display_name("+14155552671") is None
    assert display_name("14155552671") is None


@pytest.mark.asyncio
async def test_people_harvest_and_call_without_number(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.models import Event, HealthSnapshot
    from app.utils.text import utcnow

    body, phone = await _pair(client, role="primary_companion", name="People Phone")
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert promoted.status_code == 200
    db_session.add(
        Event(
            event_type="message.whatsapp.received",
            source="whatsapp",
            content={"handle": "Mansi", "text": "hello"},
            occurred_at=utcnow(),
            sha256="a" * 64,
        )
    )
    db_session.add(
        Event(
            event_type="contact.discovered",
            source="contacts",
            content={"name": "Rahul", "phone": "+14155552671"},
            occurred_at=utcnow(),
            sha256="b" * 64,
        )
    )
    db_session.add(
        HealthSnapshot(
            source="healthkit",
            metrics={"steps": 8123, "sleep_hours": 7.2},
            readiness=72.0,
            band="steady",
            occurred_at=utcnow(),
        )
    )
    await db_session.commit()

    listed = await phone.get("/v1/device-gateway/contacts")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    names = {row["name"] for row in (payload.get("home") or [])}
    assert "Mansi" in names
    assert "Rahul" in names
    rahul = next(row for row in payload["home"] if row["name"] == "Rahul")
    assert rahul["callable"] is True
    assert "+14155552671" not in str(payload)

    missed = await phone.post(
        "/v1/device-gateway/text",
        json={
            "text": "Call Mansi",
            "instance_id": "People Phone-tab",
            "request_id": "call-" + uuid4().hex[:12],
        },
    )
    assert missed.status_code == 200, missed.text
    reply = str(missed.json().get("reply") or "").lower()
    assert "number" in reply
    assert "address book" in reply
    assert missed.json().get("route") == "PHONE_CALL"

    dial = await phone.post(
        "/v1/device-gateway/text",
        json={
            "text": "Call Rahul",
            "instance_id": "People Phone-tab",
            "request_id": "callr-" + uuid4().hex[:12],
        },
    )
    assert dial.status_code == 200, dial.text
    action = dial.json().get("phone_action") or {}
    open_url = str(action.get("open_url") or (action.get("card") or {}).get("open_url") or "")
    assert open_url.startswith("tel:")
    assert "14155552671" in open_url.replace(" ", "")
    assert "14155552671" not in str(dial.json().get("reply") or "")

    vitals = await phone.get("/v1/device-gateway/vitals")
    assert vitals.status_code == 200, vitals.text
    series = vitals.json().get("series") or []
    assert series
    assert series[0].get("metrics", {}).get("steps") == 8123

    steps = await phone.post(
        "/v1/device-gateway/text",
        json={
            "text": "How many steps did I take?",
            "instance_id": "People Phone-tab",
            "request_id": "st-" + uuid4().hex[:12],
        },
    )
    assert steps.status_code == 200, steps.text
    assert steps.json().get("sent_to_model") is False
    assert "8123" not in str(steps.json().get("reply") or "")
    await phone.aclose()

