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


async def test_handoff_uses_active_conversation_state(client: AsyncClient) -> None:
    _, primary = await _pair(client, role="primary_companion", name="Handoff A")
    _, secondary = await _pair(client, role="secondary_companion", name="Handoff B")
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
    lease_a = await primary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-a", "method": "manual"},
    )
    lease_b = await secondary.post(
        "/v1/device-gateway/conversation/claim",
        json={"instance_id": "tab-b", "method": "manual"},
    )
    assert lease_a.json()["lease"]["device_id"] != lease_b.json()["lease"]["device_id"]
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

