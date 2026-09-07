"""Phone capability manifest — the honest "what can this iPhone do" surface.

Owner problem (2026-09-08): a freshly paired iPhone is PAIRED_SANDBOX where
the spoken surface is deliberately chat-only, and nothing on the device
explains that tools/memory/camera are one promotion away. These tests pin the
server-computed manifest endpoint that makes the trust lifecycle visible.

Read-only endpoint; derives from the same policy code the turn paths enforce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Device

def _trusted_device() -> Device:
    return Device(
        name="iPhone 16 Pro",
        platform="ios",
        token_hash="hash",
        paired_at=datetime.now(UTC),
        memory_scope="owner",
    )

def _revoked_device() -> Device:
    return Device(
        name="iPhone SE",
        platform="ios",
        token_hash="hash",
        paired_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
        memory_scope="owner",
    )


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _pair_sandbox(client: AsyncClient, name: str) -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.09.08.01",
            "platform": "ios",
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


async def test_sandbox_manifest_hides_tools_and_explains_unlock(
    client: AsyncClient,
) -> None:
    phone = await _pair_sandbox(client, "SE-Sandbox")
    response = await phone.get("/v1/device-gateway/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["trust_state"] == "PAIRED_SANDBOX"
    assert body["environment"] == "SANDBOX"
    assert body["voice"]["tools"] == []
    assert all(enabled is False for enabled in body["tools"].values())
    assert all(enabled is False for enabled in body["reads"].values())
    assert body["memory"]["scope"] == "sandbox"
    assert body["camera_look"] is False
    assert body["limits"], "sandbox must state its limits"
    assert "promote" in (body["upgrade_hint"] or "").lower()


async def test_manifest_requires_device_auth() -> None:
    client = _client()
    response = await client.get("/v1/device-gateway/capabilities")
    assert response.status_code in (401, 403), response.text


def test_trusted_manifest_unlocks_full_surface() -> None:
    from app.device_gateway.capability_manifest import capability_manifest

    manifest = capability_manifest(_trusted_device())
    assert manifest["trust_state"] == "TRUSTED_OWNER_DEVICE"
    assert manifest["environment"] == "OWNER"
    assert manifest["voice"]["tools"] == (
        "evie_state_query",
        "phone_action",
        "evie_look",
        "evie_home_action",
    )
    assert manifest["tools"]["start_timer"] is True
    assert manifest["tools"]["computer_action"] is True
    assert manifest["reads"]["weather"] is True
    assert manifest["reads"]["memory_history"] is True
    assert manifest["memory"]["scope"] == "owner"
    assert manifest["memory"]["shadow_injection"] is True
    assert manifest["camera_look"] is True
    assert manifest["upgrade_hint"] is None
    assert manifest["limits"] == []


def test_revoked_manifest_states_repair_path() -> None:
    from app.device_gateway.capability_manifest import capability_manifest

    manifest = capability_manifest(_revoked_device())
    assert manifest["trust_state"] == "REVOKED"
    assert manifest["upgrade_hint"] is None
    assert all(enabled is False for enabled in manifest["tools"].values())
    assert any("re-pair" in item.lower() for item in manifest["limits"])
