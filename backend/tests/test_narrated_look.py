"""Narrated Look: "narrate" routes to a burst capture the phone can speak."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.everywhere.endpoint_profile import perception_action
from app.main import app


async def _pair(client: AsyncClient, *, name: str = "16 Pro") -> tuple[dict, AsyncClient]:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "primary_companion", "display_name": name},
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
            "platform": "ios",
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    body = paired.json()
    phone.headers["Authorization"] = f"Bearer {body['device_token']}"
    phone.headers["X-Forwarded-Proto"] = "https"
    return body, phone


def test_perception_action_maps_narrate() -> None:
    assert perception_action("narrate what you see") == "narrate"
    assert perception_action("Narrate this shelf for me") == "narrate"
    assert perception_action("look at this") == "look_once"


async def test_narrate_text_routes_camera_burst(client: AsyncClient) -> None:
    body, phone = await _pair(client)
    try:
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": body["device"]["device_id"], "reason": "owner"},
        )
        res = await phone.post(
            "/v1/device-gateway/text",
            json={"text": "Narrate this shelf for me.", "instance_id": "16 Pro-tab"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["route"] == "CAMERA"
        assert res.json()["camera_action"] == "narrate"
        assert res.json()["needs_camera"] is True
    finally:
        await phone.aclose()
