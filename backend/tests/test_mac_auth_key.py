"""Mac menu-bar auth: master key works; short leftovers 401 as device tokens."""

from __future__ import annotations

from httpx import AsyncClient


async def test_master_key_is_accepted_on_actor_surface(client: AsyncClient) -> None:
    resp = await client.get("/v1/runtime/status")
    assert resp.status_code == 200, resp.text


async def test_short_leftover_key_is_rejected_as_device_token(client: AsyncClient) -> None:
    for leftover in ("changeme", "earskey", "dev"):
        resp = await client.get(
            "/v1/runtime/status",
            headers={"Authorization": f"Bearer {leftover}"},
        )
        assert resp.status_code == 401, leftover
        assert "device token" in resp.json()["detail"].lower()
