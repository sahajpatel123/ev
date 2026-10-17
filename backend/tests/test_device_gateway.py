"""Device Gateway: pairing, sandbox memory, lease, privacy, PWA."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.device_gateway import PWA_BUILD
from app.device_gateway.sandbox import is_sandbox_device
from app.device_gateway.voice import strip_production_memory_from_manifest
from app.main import app
from app.models import Memory, SandboxFact

PROD_MARKER = "PROD_MEMORY_MARKER_QX19_DO_NOT_LEAK"


async def _pair(client: AsyncClient, *, role: str, name: str) -> tuple[dict, AsyncClient]:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": role, "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    token = minted.json()["pairing_token"]
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": token,
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.08.20.1",
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    body = paired.json()
    phone.headers["Authorization"] = f"Bearer {body['device_token']}"
    # Tailnet HTTPS origin: the kernel text lane requires a private address.
    phone.headers["X-Forwarded-Proto"] = "https"
    return body, phone


async def test_pwa_is_installable_and_has_no_provider_secrets(client: AsyncClient) -> None:
    page = await client.get("/evie/")
    assert page.status_code == 200
    assert "evie" in page.text.lower()  # brand marker, case-robust across UI redesigns
    assert "SANDBOX" in page.text
    assert "Content-Security-Policy" in page.headers
    assert "*" not in (page.headers.get("Access-Control-Allow-Origin") or "")
    js = await client.get("/evie/app.js")
    css = await client.get("/evie/style.css")
    sw = await client.get("/evie/sw.js")
    man = await client.get("/evie/manifest.webmanifest")
    audio = await client.get("/evie/audio.js")
    orb = await client.get("/evie/orb.js")
    assert js.status_code == 200
    assert audio.status_code == 200
    assert orb.status_code == 200
    assert "OPENAI_API_KEY" not in js.text
    assert "DEEPSEEK_API_KEY" not in js.text
    assert "EV_MASTER_KEY" not in js.text
    assert "&token=" not in js.text
    assert "innerHTML" not in js.text
    assert "eval(" not in js.text
    assert "pcm-worklet" in js.text
    assert "ws_ticket" in js.text or "ticket=" in js.text
    assert PWA_BUILD in js.text
    assert "EvieAudioPlaybackEngine" in js.text
    assert "nextPlayTime" in audio.text
    assert "LinearResampler" in audio.text
    assert "worklet-ring-buffer" in audio.text
    assert "playPcm16" not in js.text
    assert "mute.gain.value = 0" in js.text
    assert "safe-area-inset" in css.text
    assert "prefers-color-scheme" in css.text
    assert "EviePresence" in (await client.get("/evie/presence.js")).text
    assert "EvieWebRTC" in (await client.get("/evie/webrtc.js")).text
    webrtc = await client.get("/evie/webrtc.js")
    assert "OPENAI_API_KEY" not in webrtc.text
    assert "sk-" not in webrtc.text
    assert "Bearer ${" not in webrtc.text
    playback = await client.get("/evie/playback-worklet.js")
    assert playback.status_code == 200
    assert "evie-playback" in playback.text
    assert "/v1/" in sw.text
    assert "caches.match" in sw.text
    assert man.json()["display"] == "standalone"
    assert css.status_code == 200


async def test_gateway_health_disables_production_memory(client: AsyncClient) -> None:
    resp = await client.get("/v1/device-gateway/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["production_memory_enabled"] is False
    assert body["sandbox_memory_ready"] is True
    assert body["protocol_version"] == "1"
    assert body["funnel_enabled"] is False
    core = await client.get("/v1/health")
    assert core.json()["runtime"]["device_gateway"]["production_memory_enabled"] is False


async def test_pairing_sandbox_text_and_cross_device_fact(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    primary_body, primary = await _pair(client, role="primary_companion", name="Primary iPhone")
    secondary_body, secondary = await _pair(client, role="secondary_companion", name="Secondary iPhone")
    assert primary_body["memory_scope"] == "sandbox"
    assert secondary_body["device"]["role"] == "secondary_companion"
    hello = await primary.post(
        "/v1/device-gateway/hello",
        json={"protocol_version": "1", "instance_id": "Primary iPhone-tab", "client_build": "2026.08.20.1"},
    )
    assert hello.status_code == 200
    assert hello.json()["environment"] == "SANDBOX"
    a = await primary.post(
        "/v1/device-gateway/text",
        json={"text": "Reply with PIPELINE-PRIMARY.", "instance_id": "Primary iPhone-tab"},
    )
    b = await secondary.post(
        "/v1/device-gateway/text",
        json={"text": "Reply with PIPELINE-SECONDARY.", "instance_id": "Secondary iPhone-tab"},
    )
    assert a.json()["reply"] == "PIPELINE-PRIMARY"
    assert b.json()["reply"] == "PIPELINE-SECONDARY"
    assert a.json()["origin_device_id"] == primary_body["device"]["device_id"]
    assert a.json()["response_device_id"] == a.json()["origin_device_id"]
    remember = await primary.post(
        "/v1/device-gateway/text",
        json={
            "text": "Remember that the sandbox satellite code is Nova 741.",
            "instance_id": "Primary iPhone-tab",
        },
    )
    assert "Nova 741" in remember.json()["reply"]
    recall = await secondary.post(
        "/v1/device-gateway/text",
        json={"text": "What is the sandbox satellite code?", "instance_id": "Secondary iPhone-tab"},
    )
    assert recall.json()["reply"] == "Nova 741"
    facts = list((await db_session.execute(select(SandboxFact))).scalars().all())
    assert facts and facts[0].namespace == "cross_platform_test"
    await primary.aclose()
    await secondary.aclose()


async def test_sandbox_cannot_read_production_memory(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Memory(
            memory_type="fact",
            text=PROD_MARKER,
            payload={"secret": True},
            importance=0.99,
            confidence=0.99,
            source_type="explicit",
            event_time=datetime(2026, 8, 1, tzinfo=UTC),
            valid_from=datetime(2026, 8, 1, tzinfo=UTC),
            fingerprint="p" * 32,
        )
    )
    await db_session.commit()
    _, phone = await _pair(client, role="primary_companion", name="Probe phone")
    blocked = await phone.get("/v1/memories")
    assert blocked.status_code == 403
    chat = await phone.post("/v1/chat", json={"message": "hi"})
    assert chat.status_code == 403
    probe = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "What do you remember about me?", "instance_id": "probe"},
    )
    body = probe.json()
    assert PROD_MARKER not in (body.get("reply") or "")
    assert body["memory_scope"] == "sandbox"
    assert "production Memory OS" in body["reply"]
    await phone.aclose()


async def test_handoff_uses_active_conversation_state(client: AsyncClient, db_session) -> None:
    # Cycle 77 — arbitration keys off ANY unexpired lease (a text turn claims
    # one too); clear leftovers so the test is deterministic.
    from sqlalchemy import delete as _delete

    from app.models import ConversationLease as _Lease

    await db_session.execute(_delete(_Lease))
    await db_session.commit()
    _, primary = await _pair(client, role="primary_companion", name="Handoff A")
    _, secondary = await _pair(client, role="primary_companion", name="Handoff B")
    await primary.post(
        "/v1/device-gateway/text",
        json={
            "text": "We're discussing Project Blue Satellite.",
            "instance_id": "tab-a",
        },
    )
    follow = await secondary.post(
        "/v1/device-gateway/text",
        json={"text": "Continue what I was saying.", "instance_id": "tab-b"},
    )
    assert "Project Blue Satellite" in follow.json()["reply"]
    # A text turn holds the lease; the other device's claim is refused once
    # and must take over explicitly.
    refused_a = await primary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-a", "method": "manual"},
    )
    assert refused_a.json()["ok"] is False
    lease_a = await primary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-a", "method": "manual", "takeover": True},
    )
    assert lease_a.json()["ok"] is True
    refused_b = await secondary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-b", "method": "manual"},
    )
    assert refused_b.json()["ok"] is False
    assert refused_b.json()["holder"]["device_id"] == lease_a.json()["lease"]["device_id"]
    lease_b = await secondary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-b", "method": "manual", "takeover": True},
    )
    assert lease_b.json()["lease"]["device_id"] != lease_a.json()["lease"]["device_id"]
    await primary.aclose()
    await secondary.aclose()


async def test_revocation_blocks_gateway(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="primary_companion", name="Revoke me")
    ok = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Say primary pipeline works.", "instance_id": "r"},
    )
    assert ok.status_code == 200
    revoked = await client.post(
        "/v1/device-gateway/admin/revoke",
        json={"device_id": body["device"]["device_id"]},
    )
    assert revoked.status_code == 200
    denied = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Say primary pipeline works.", "instance_id": "r"},
    )
    assert denied.status_code == 401
    await phone.aclose()


async def test_mac_canary_is_truthful_when_helper_missing(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Remote")
    resp = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Open Calculator on my Mac.", "instance_id": "m"},
    )
    body = resp.json()
    assert body["reply"] != "Done."
    lowered = body["reply"].lower()
    assert "couldn't" in lowered or "unavailable" in lowered or "unverified" in lowered
    assert body.get("mac", {}).get("ok") is False
    await phone.aclose()


async def test_camera_request_stays_on_origin(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Cam")
    resp = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Look at this.", "instance_id": "c"},
    )
    body = resp.json()
    assert body["needs_camera"] is True
    assert body["camera_target_device_id"] == body["origin_device_id"]
    put = await phone.post(
        "/v1/device-gateway/camera/result",
        json={"request_id": body["camera_request_id"], "jpeg_b64": "aaa"},
    )
    assert put.status_code == 200
    assert put.json()["persisted_to_memory_os"] is False
    await phone.aclose()


async def test_pairing_token_is_one_time(client: AsyncClient) -> None:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "Once"},
    )
    token = minted.json()["pairing_token"]
    first = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    second = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    async with first, second:
        a = await first.post("/v1/device-gateway/pair", json={"pairing_token": token, "protocol_version": "1"})
        b = await second.post("/v1/device-gateway/pair", json={"pairing_token": token, "protocol_version": "1"})
        assert a.status_code == 200
        assert b.status_code == 401


async def test_origin_denies_foreign_sites(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "X"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


async def test_sandbox_manifest_never_keeps_bootstrap() -> None:
    stripped = strip_production_memory_from_manifest(
        {
            "memory_bootstrap": {"card": PROD_MARKER},
            "relationship_card": PROD_MARKER,
            "live_tool_projection": [{"name": "search_memory"}],
        }
    )
    blob = str(stripped)
    assert PROD_MARKER not in blob
    assert "search_memory" not in blob
    names = stripped.get("approved_tools") or []
    assert "open_app" in names
    assert "look" in names
    assert stripped["production_memory_enabled"] is False
    assert stripped["memory_scope"] == "sandbox"


async def test_existing_device_defaults_to_owner_memory_scope(db_session: AsyncSession) -> None:
    from app.models import Device

    mac = Device(name="EV.app", token_hash="a" * 64, capabilities=["voice"], trust_level="owner")
    db_session.add(mac)
    await db_session.flush()
    assert is_sandbox_device(mac) is False
    assert (mac.memory_scope or "owner") == "owner"


async def test_client_cannot_self_promote_or_enable_owner_memory(client: AsyncClient) -> None:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "home_station", "display_name": "Nope"},
    )
    assert minted.status_code == 400
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "secondary_companion", "display_name": "SE"},
    )
    token = minted.json()["pairing_token"]
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": token,
            "protocol_version": "1",
            "role": "home_station",
            "memory_scope": "owner",
        },
    )
    assert paired.status_code == 200
    assert paired.json()["device"]["role"] == "secondary_companion"
    assert paired.json()["memory_scope"] == "sandbox"
    await phone.aclose()


async def test_null_origin_denied(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "X"},
        headers={"Origin": "null"},
    )
    assert resp.status_code == 403


async def test_localhost_origin_allowed_in_dev(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "Local"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert resp.status_code == 200


async def test_crossplatform_nonce_and_continue_here(client: AsyncClient) -> None:
    _, primary = await _pair(client, role="primary_companion", name="Nonce A")
    _, secondary = await _pair(client, role="secondary_companion", name="Nonce B")
    a = await primary.post(
        "/v1/device-gateway/text",
        json={"text": "Return CROSSPLATFORM-PRIMARY-abc123", "instance_id": "tab-a"},
    )
    b = await secondary.post(
        "/v1/device-gateway/text",
        json={"text": "Return CROSSPLATFORM-SECONDARY-xyz9", "instance_id": "tab-b"},
    )
    assert a.json()["reply"] == "CROSSPLATFORM-PRIMARY-abc123"
    assert b.json()["reply"] == "CROSSPLATFORM-SECONDARY-xyz9"
    assert a.json()["response_device_id"] == a.json()["origin_device_id"]
    takeover = await secondary.post(
        "/v1/device-gateway/text",
        json={"text": "Continue here", "instance_id": "tab-b"},
    )
    assert "Continuing here" in takeover.json()["reply"]
    moved = await primary.post(
        "/v1/device-gateway/heartbeat",
        json={"instance_id": "tab-a"},
    )
    assert moved.json().get("conversation_moved") is True
    await primary.aclose()
    await secondary.aclose()


async def test_sandbox_cleanup_does_not_touch_memory_os(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Cleanup")
    await phone.post(
        "/v1/device-gateway/text",
        json={"text": "Remember that the sandbox satellite code is TEMPCODE.", "instance_id": "c"},
    )
    facts = list((await db_session.execute(select(SandboxFact))).scalars().all())
    assert facts
    db_session.add(
        Memory(
            memory_type="fact",
            text=PROD_MARKER,
            payload={"secret": True},
            importance=0.99,
            confidence=0.99,
            source_type="explicit",
            event_time=datetime(2026, 8, 1, tzinfo=UTC),
            valid_from=datetime(2026, 8, 1, tzinfo=UTC),
            fingerprint="q" * 32,
        )
    )
    await db_session.commit()
    cleared = await client.post("/v1/device-gateway/admin/sandbox/clear")
    assert cleared.status_code == 200
    assert cleared.json()["memory_os_untouched"] is True
    from app.db import SessionLocal

    async with SessionLocal() as probe:
        leftover = list((await probe.execute(select(SandboxFact))).scalars().all())
        memories = list((await probe.execute(select(Memory))).scalars().all())
    assert leftover == []
    assert any(row.text == PROD_MARKER for row in memories)
    await phone.aclose()


async def test_ws_tickets_are_one_time_and_not_device_tokens() -> None:
    from uuid import uuid4

    from app.device_gateway.tickets import consume, mint

    device_id = uuid4()
    session_id = uuid4().hex
    ticket = mint(device_id=device_id, session_id=session_id, instance_id="tab")
    assert "." not in ticket or not ticket.startswith("evie1.")
    first = consume(ticket, session_id=session_id)
    second = consume(ticket, session_id=session_id)
    assert first is not None
    assert first["device_id"] == str(device_id)
    assert second is None


async def test_sandbox_tool_catalog_excludes_memory_and_shell() -> None:
    from app.device_gateway.sandbox_tools import (
        SANDBOX_SAFE_LIVE_TOOLS,
        provider_effective_snapshot,
        sandbox_live_tool_specs,
    )

    specs = sandbox_live_tool_specs()
    names = {row["name"] for row in specs}
    assert names == set(SANDBOX_SAFE_LIVE_TOOLS)
    assert "search_memory" not in names
    assert "execute_command" not in names
    assert "inspect_ui" not in names
    snap = provider_effective_snapshot()
    assert snap["local_catalog_ready"] is True
    assert snap["sandbox_tool_schema_hash"]


async def test_stale_protocol_is_rejected(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Stale")
    hello = await phone.post(
        "/v1/device-gateway/hello",
        json={"protocol_version": "99", "instance_id": "s", "client_build": "2026.08.20.2"},
    )
    assert hello.status_code == 409
    await phone.aclose()


async def test_worklet_asset_and_csp(client: AsyncClient) -> None:
    worklet = await client.get("/evie/pcm-worklet.js")
    assert worklet.status_code == 200
    assert "AudioWorkletProcessor" in worklet.text
    page = await client.get("/evie/")
    csp = page.headers.get("Content-Security-Policy") or ""
    assert "worker-src" in csp
    assert "object-src 'none'" in csp


def _camera_jpeg(width: int = 320, height: int = 240) -> bytes:
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


async def test_phone_photo_look_stores_and_serves_its_media(
    client: AsyncClient,
    owner_phone,
) -> None:
    import base64

    from app.device_gateway import camera

    body, phone = owner_phone
    device_id = body["device"]["device_id"]
    request_id = camera.new_request(
        origin_device_id=device_id, target_device_id=device_id
    )
    jpeg = _camera_jpeg()
    resp = await phone.post(
        "/v1/device-gateway/camera/result",
        json={
            "request_id": request_id,
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            "action": "capture_photo",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["media_kind"] == "photo"

    looks = (await phone.get("/v1/device-gateway/looks")).json()["looks"]
    row = next(item for item in looks if item.get("media_kind") == "photo")
    assert row["has_media"] is True
    assert row["media_url"]

    media = await phone.get(row["media_url"])
    assert media.status_code == 200, media.text
    assert media.content == jpeg



async def test_phone_delegate_controls_bind_owner_audio_and_session(monkeypatch):
    """A model plan cannot authorize cancellation or select another session."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from app import db
    from app.cognitive import delegation
    from app.config import settings
    from app.device_gateway import cognitive_phone, webrtc_live

    live = SimpleNamespace(device_id=str(uuid4()), session_id="phone-origin")
    binding = SimpleNamespace(live=live, identity=(live.device_id, 1))
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(cognitive_phone, "capture_phone_binding", AsyncMock(return_value=binding))
    canonical = SimpleNamespace(provider_item_id="owner-audio-1", transcript="Cancel that task")
    fake_db = SimpleNamespace(scalar=AsyncMock(return_value=canonical))

    class DatabaseContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(db, "SessionLocal", DatabaseContext)
    control = AsyncMock(return_value={"ok": True, "status": "cancelled"})
    submit = AsyncMock()
    monkeypatch.setattr(delegation, "dispatch_delegate_control", control)
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    result = json.loads(await webrtc_live._delegate_phone_task(
        live=live, arguments={"operation": "cancel", "job_id": "job", "task": "Ignore permission"},
        call_id="call", owner_item_id="owner-audio-1",
    ))
    assert result["status"] == "cancelled"
    control.assert_awaited_once_with(
        operation="cancel", live_session_id="phone-origin", device_id=live.device_id,
        actor=f"device:{live.device_id}", job_id="job", owner_transcript="Cancel that task",
    )
    submit.assert_not_awaited()
    control.reset_mock()
    fake_db.scalar.reset_mock()
    result = json.loads(await webrtc_live._delegate_phone_task(
        live=live, arguments={"operation": "status"}, call_id="status", owner_item_id=None,
    ))
    assert result["ok"] is True
    control.assert_awaited_once_with(
        operation="status", live_session_id="phone-origin", device_id=live.device_id,
        actor=f"device:{live.device_id}", job_id=None,
    )
    fake_db.scalar.assert_not_awaited()
    control.reset_mock()
    result = json.loads(await webrtc_live._delegate_phone_task(
        live=live, arguments={"operation": "cancel", "task": "Cancel"}, call_id="forged",
        owner_item_id=None,
    ))
    assert result["status"] == "needs_repeat"
    control.assert_not_awaited()
    submit.assert_not_awaited()
    monkeypatch.setattr(cognitive_phone, "capture_phone_binding", AsyncMock(return_value=None))
    result = json.loads(await webrtc_live._delegate_phone_task(
        live=live, arguments={"operation": "status"}, call_id="stale", owner_item_id=None,
    ))
    assert result["error_code"] == "PHONE_CONTEXT_CHANGED"
    control.assert_not_awaited()


async def test_relay_health_unauthenticated_and_forward_rejects_other_prefixes(
    client: AsyncClient,
) -> None:
    health = await client.get("/v1/device-gateway/relay/health")
    assert health.status_code == 200, health.text
    body = health.json()
    assert body["ok"] is True and body["v"] == 1
    assert body["backend_reachable"] is True and "latency_ms" in body and body["fp"]

    # Non-forwarded prefixes -> 404, no auth involved.
    bad = await client.get("/v1/device-gateway/relay/devices")
    assert bad.status_code == 404

    # relay/info requires the gateway device token; unauthenticated is 401/403.
    info = await client.get("/v1/device-gateway/relay/info")
    assert info.status_code in (401, 403, 404)


async def test_relay_forwards_sync_with_auth_passthrough(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="relayphone")
    try:
        direct = await phone.get("/v1/device-gateway/sync/changes?cursor=v2%7C0%7C0")
        via = await phone.get("/v1/device-gateway/relay/sync/changes?cursor=v2%7C0%7C0")
        assert direct.status_code == 200, direct.text
        assert via.status_code == 200, via.text
        # Epoch ids are freshly minted per call; parity = same shape/error, not byte-equality.
        assert via.json()["ok"] == direct.json()["ok"]
        assert via.json().get("error") == direct.json().get("error")
    finally:
        await phone.aclose()


async def test_relay_info_authenticated(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="relayinfo")
    try:
        info = await phone.get("/v1/device-gateway/relay/info")
        assert info.status_code == 200, info.text
        assert info.json()["relay_port"] == 9411
    finally:
        await phone.aclose()

async def test_session_refresh_rotates_access_token(client: AsyncClient) -> None:
    body, phone = await _pair(client, role="primary_companion", name="Refresh")
    try:
        first_access = body["access_token"]
        resp = await phone.post("/v1/device-gateway/session")
        assert resp.status_code == 200, resp.text
        rotated = resp.json()["access_token"]
        assert rotated.startswith("evie1.")
        # Same-second re-mint is byte-identical by design (exp-second granularity);
        # rotation guarantee = fresh token authenticates, not inequality.
        phone.headers["Authorization"] = f"Bearer {rotated}"
        again = await phone.post("/v1/device-gateway/session")
        assert again.status_code == 200, again.text
    finally:
        await phone.aclose()


async def test_expired_access_token_rejected(client: AsyncClient) -> None:
    import hashlib
    import hmac as _hmac
    import time as _time

    from app.config import settings

    body, phone = await _pair(client, role="primary_companion", name="Expired")
    try:
        from app.device_gateway.auth import parse_access_token

        parsed = parse_access_token(body["access_token"])
        assert parsed is not None
        device_id, _, scope, revision = parsed
        payload = f"{device_id}|{int(_time.time()) - 10}|{scope}|{revision}"
        key = hashlib.sha256(f"{settings.master_key}|device-gateway-v1".encode()).digest()
        digest = _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        phone.headers["Authorization"] = f"Bearer evie1.{payload}.{digest}"
        resp = await phone.post("/v1/device-gateway/session")
        assert resp.status_code == 401, resp.text
    finally:
        await phone.aclose()


async def test_ws_ticket_single_use_second_consume_fails() -> None:
    from uuid import uuid4

    from app.device_gateway.tickets import consume, mint

    device_id = uuid4()
    session_id = uuid4().hex
    ticket = mint(device_id=device_id, session_id=session_id, instance_id="tab")
    assert consume(ticket, session_id=session_id) is not None
    assert consume(ticket, session_id=session_id) is None


async def test_ws_ticket_wrong_session_rejected() -> None:
    from uuid import uuid4

    from app.device_gateway.tickets import consume, mint

    ticket = mint(device_id=uuid4(), session_id="session-a", instance_id="tab")
    assert consume(ticket, session_id="session-b") is None

async def test_relayed_live_open_returns_relay_ws_path_and_ticket_usable(client: AsyncClient) -> None:
    """Relayed live/open mints a session-bound ticket usable on the relay WS path."""
    from app.device_gateway.tickets import _TTL_S, consume

    assert _TTL_S == 45.0  # TTL invariant: relay must not widen ticket lifetime.
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    _, phone = await _pair(client, role="primary_companion", name="relaylive")
    try:
        direct = await phone.post(
            "/v1/device-gateway/live/open",
            json={"instance_id": "relaylive-tab", "media_backend": "pcm_ws"},
        )
        assert direct.status_code == 200, direct.text
        assert direct.json()["ws_path"] == "/v1/voice/live"
        via = await phone.post(
            "/v1/device-gateway/relay/live/open",
            json={"instance_id": "relaylive-tab", "media_backend": "pcm_ws"},
        )
        assert via.status_code == 200, via.text
        body = via.json()
        # Documented rewrite: relay clients connect to the relay WS path.
        assert body["ws_path"] == "/v1/device-gateway/relay/live/ws"
        assert body["ws_ticket"]
        ticket = body["ws_ticket"]
        session_id = body["session_id"]
        # Ticket is session-bound and single-use against the relay WS path:
        # wrong-session consume fails, then the real claim succeeds once.
        assert consume(ticket, session_id="other-session") is None
        claimed = consume(ticket, session_id=session_id)
        assert claimed is not None and claimed["session_id"] == session_id
        assert consume(ticket, session_id=session_id) is None
        # The relay WS route exists on the gateway router alongside /v1/voice/live.
        from app.device_gateway.api import router as gateway_router

        routes = {getattr(r, "path", "") for r in gateway_router.routes}
        assert "/v1/device-gateway/relay/live/ws" in routes
    finally:
        await phone.aclose()

async def test_relayed_live_open_rewrites_ws_path(client: AsyncClient) -> None:
    """x-evie-relayed header on live/open rewrites ws_path to the relay WS."""
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    _, phone = await _pair(client, role="primary_companion", name="relayhdr")
    try:
        direct = await phone.post(
            "/v1/device-gateway/live/open",
            json={"instance_id": "relayhdr-tab", "media_backend": "pcm_ws"},
        )
        assert direct.status_code == 200, direct.text
        assert direct.json()["ws_path"] == "/v1/voice/live"
        via = await phone.post(
            "/v1/device-gateway/live/open",
            json={"instance_id": "relayhdr-tab", "media_backend": "pcm_ws"},
            headers={"x-evie-relayed": "v1device-relay"},
        )
        assert via.status_code == 200, via.text
        assert via.json()["ws_path"] == "/v1/device-gateway/relay/live/ws"
    finally:
        await phone.aclose()

async def test_relay_rejects_non_allowlisted_prefix_authenticated(
    client: AsyncClient,
) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Relay404")
    try:
        for method in ("get", "post"):
            resp = await getattr(phone, method)("/v1/device-gateway/relay/devices")
            assert resp.status_code == 404, (method, resp.status_code, resp.text)
        # Near-miss prefixes must not forward either (exact "sync"/"live/" match).
        near = await phone.get("/v1/device-gateway/relay/syncx/changes")
        assert near.status_code == 404, near.text
    finally:
        await phone.aclose()


async def test_relay_rejects_oversize_body(client: AsyncClient, monkeypatch) -> None:
    import app.device_gateway.api as gw_api

    monkeypatch.setattr(gw_api, "_RELAY_MAX_BODY_BYTES", 16)
    _, phone = await _pair(client, role="primary_companion", name="Relay413")
    try:
        resp = await phone.post(
            "/v1/device-gateway/relay/sync/changes",
            content=b"x" * 17,
        )
        assert resp.status_code == 413, (resp.status_code, resp.text)
        ok = await phone.post(
            "/v1/device-gateway/relay/sync/changes",
            content=b"x" * 16,
        )
        assert ok.status_code != 413, ok.text
    finally:
        await phone.aclose()


async def test_relay_strips_hop_by_hop_headers(client: AsyncClient) -> None:
    import app.device_gateway.api as gw_api

    assert {"connection", "transfer-encoding", "host", "content-length"} <= set(
        gw_api._RELAY_HOP_BY_HOP
    )
    assert gw_api._relay_forward_allowed("sync/changes")
    assert gw_api._relay_forward_allowed("live/open")
    assert not gw_api._relay_forward_allowed("devices")
    _, phone = await _pair(client, role="primary_companion", name="RelayHop")
    try:
        direct = await phone.get("/v1/device-gateway/sync/changes?cursor=v2%7C0%7C0")
        via = await phone.get(
            "/v1/device-gateway/relay/sync/changes?cursor=v2%7C0%7C0",
            headers={"Connection": "close", "Transfer-Encoding": "identity"},
        )
        assert via.status_code == direct.status_code == 200, (direct.text, via.text)
        # Strip must not corrupt the forwarded exchange: same shape/error,
        # not byte-equality (epoch ids are freshly minted per call).
        assert via.json()["ok"] == direct.json()["ok"]
        assert via.json().get("error") == direct.json().get("error")
        assert via.json().get("reset_required") == direct.json().get("reset_required")
        for h in ("connection", "transfer-encoding"):
            assert h not in {k.lower() for k in via.headers}
    finally:
        await phone.aclose()


async def test_relayed_sync_reset_signal_parity(client: AsyncClient) -> None:
    _, phone = await _pair(client, role="primary_companion", name="RelayReset")
    try:
        for cursor in ("bogus-cursor", "v1|2000-01-01T00:00:00|abc"):
            direct = await phone.get(
                "/v1/device-gateway/sync/changes", params={"cursor": cursor}
            )
            via = await phone.get(
                "/v1/device-gateway/relay/sync/changes", params={"cursor": cursor}
            )
            assert direct.status_code == via.status_code == 200
            d, v = direct.json(), via.json()
            assert d["ok"] is False and v["ok"] is False
            assert v.get("error") == d.get("error")
            assert v.get("reset_required") == d.get("reset_required") is True
    finally:
        await phone.aclose()


async def test_outbox_duplicate_enqueue_is_409_already_applied(
    client: AsyncClient,
) -> None:
    _, phone = await _pair(client, role="primary_companion", name="Outbox409")
    try:
        payload = {"idempotency_key": "outbox-dedupe-key-1", "kind": "request", "payload": {"text": "hi"}}
        first = await phone.post("/v1/device-gateway/queue", json=payload)
        assert first.status_code == 201, first.text
        second = await phone.post("/v1/device-gateway/queue", json=payload)
        assert second.status_code == 409, (second.status_code, second.text)
        body = second.json()
        assert body.get("replayed") is True
        assert body["item"]["id"] == first.json()["item"]["id"]
        # /queue is intentionally NOT relay-allowlisted: the relay stays a
        # dumb sync/live forwarder, so queue traffic gets a 404 here.
        via = await phone.post("/v1/device-gateway/relay/queue", json=payload)
        assert via.status_code == 404, (via.status_code, via.text)
    finally:
        await phone.aclose()

async def test_relayed_live_pins_tts_to_opener(client: AsyncClient) -> None:
    """Relayed live/open pins TTS/audio routing to the opener (phone, not Mac)."""
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    body, phone = await _pair(client, role="primary_companion", name="RelayTtsPin")
    try:
        opener_id = body["device"]["device_id"]
        via = await phone.post(
            "/v1/device-gateway/relay/live/open",
            json={"instance_id": "RelayTtsPin-tab", "media_backend": "pcm_ws"},
        )
        assert via.status_code == 200, via.text
        payload = via.json()
        # Server-owned binding facts: response audio goes to the opener phone.
        assert payload["response_device_id"] == opener_id
        assert payload["session_context"]["device_id"] == opener_id
    finally:
        await phone.aclose()


async def test_relayed_look_frame_parity(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST look-frame direct vs via /relay/live/look-frame gives same status/shape."""
    from uuid import UUID

    from app.device_gateway.webrtc_live import attach_phone_control_live
    from app.models import Device

    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text
    body, phone = await _pair(client, role="primary_companion", name="RelayLookParity")
    try:
        opener_id = body["device"]["device_id"]
        opened = await phone.post(
            "/v1/device-gateway/live/open",
            json={"instance_id": "RelayLookParity-tab", "media_backend": "pcm_ws"},
        )
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session_id"]
        device = await db_session.get(Device, UUID(str(opener_id)))
        assert device is not None
        attach_phone_control_live(
            device=device,
            session_id=str(session_id),
            actor="device:RelayLookParity",
            instance_id="RelayLookParity-tab",
        )
        frame = {
            "session_id": str(session_id),
            "request_id": "parity-req",
            "error": "permission_denied",
            "permission": "denied",
            "last": True,
            "instance_id": "RelayLookParity-tab",
        }
        direct = await phone.post("/v1/device-gateway/live/look-frame", json=frame)
        assert direct.status_code == 200, direct.text
        relayed = await phone.post(
            "/v1/device-gateway/relay/live/look-frame",
            json={**frame, "request_id": "parity-req-relayed"},
        )
        assert relayed.status_code == direct.status_code, (relayed.status_code, relayed.text)
        assert set(relayed.json()) == set(direct.json())
        assert relayed.json()["ok"] is True and direct.json()["ok"] is True
    finally:
        await phone.aclose()
