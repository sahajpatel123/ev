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
    await a.aclose()
    await b.aclose()


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
    vad = cfg["audio"]["input"]["turn_detection"]
    assert vad["threshold"] == 0.68
    assert vad["silence_duration_ms"] == 700
    assert vad["create_response"] is True
    named = phone_webrtc_session(device=d, owner_name="Sahaj")
    assert "The person you are speaking with is Sahaj" in named["instructions"]
    assert "Sahaj" not in cfg["instructions"]


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
    assert "/v1/device-gateway/sync/bootstrap" in app_js
    assert "isStandalonePwa" in app_js
    assert "cameraHardware" in app_js
    assert "owner_declared" in app_js
    assert "setCameraRole" in app_js
    assert "paintCameraRole" in app_js
    assert "Add to Home Screen" in html
    assert "Which iPhone is this?" in html
    assert 'data-surface="privacy"' in html
    assert 'id="more-sheet"' in html
    assert "folio-grid" in html
    assert "more-rail" not in html
    assert 'id="more-rail"' not in html
    assert 'data-quick="weather"' in html
    assert "paintLive" in app_js
    assert "choice-list" in html
    assert "camera-ask" in html
    assert "record_clip" in app_js
    assert "parsed.needs_camera" in webrtc
    assert "await this.onCamera({" in webrtc
    assert "opened.client_generation" in webrtc
    assert 'type === "response.output_audio.done"' in webrtc
    assert 'self.onState("failed")' in webrtc
    assert "_gateMicForPlayback" in webrtc
    assert "response.cancel" in webrtc
    assert "PLAYBACK_MIC_TAIL_MS = 800" in webrtc
    assert "what's today's date" in app_js
    assert "scheduleHealthRender" in app_js
    assert "hardware: native.hardware" in app_js
    assert "healthkit_snapshot" in broker
    assert "calendar_snapshot" in broker
    assert "notification_status" in broker
    assert "siri_capture" in (IOS / "App" / "AppIntents.swift").read_text()
    assert "https://*.ts.net" in verify or "ts.net" in verify
    assert "EvieShell" in product
    assert "innerHTML" not in app_js


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
    assert should_ask_spark("Open Calculator on my Mac")


@pytest.mark.asyncio
async def test_phone_home_station_opens_calculator(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.schemas import ToolCallResponse

    async def _open_app(session, name, arguments, **kwargs):
        assert name == "open_app"
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
    assert payload["executed"] is True
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
    assert timer_body["executed"] is True
    assert timer_body.get("tool") == "start_timer" or timer_body.get("operation") == "start_timer"
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
