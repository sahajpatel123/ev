"""iPhone today dashboard — GET /v1/device-gateway/today contract."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _pair_phone(client: AsyncClient, *, name: str = "16 Pro") -> AsyncClient:
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
            "memory_scope": "owner",
            "role": "home_station",
        },
    )
    assert paired.status_code == 200, paired.text
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


async def test_today_shapes_trusted_phone(client: AsyncClient) -> None:
    phone = await _pair_phone(client)
    res = await phone.get("/v1/device-gateway/today")
    assert res.status_code == 200, res.text
    sandbox = res.json()
    assert sandbox["memory_enabled"] is False
    assert sandbox["memory_scope"] == "sandbox"

    # Promote on the Mac (master key); trust bump makes memory live.
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": sandbox["device"]["id"], "reason": "owner"},
    )
    assert promoted.status_code == 200, promoted.text
    body = (await phone.get("/v1/device-gateway/today")).json()
    assert body["ok"] is True
    assert body["memory_enabled"] is True
    assert body["memory_scope"] == "owner"
    assert body["device"]["role"] == "primary_companion"
    assert isinstance(body["hud"], dict)
    assert "title" in body["hud"] and "body" in body["hud"]
    assert body["health"] == {
        "available": False,
        "freshness": "unavailable",
        "captured_at": None,
        "metrics": {},
        "sent_to_model": False,
    }
    assert body["calendar"]["events"] == []
    assert body["reminders"] == []
    assert body["memories"] == []
    assert body["inbox_pending"] == 0
    assert "generated_at" in body


async def test_today_surfaces_health_and_calendar_snapshots(client: AsyncClient) -> None:
    phone = await _pair_phone(client)
    snap = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={
            "snapshot": {"steps": 8123, "sleep_hours": 7.2},
            "captured_at": "2026-09-08T07:00:00Z",
            "available": True,
        },
    )
    assert snap.status_code == 200, snap.text
    cal = await phone.post(
        "/v1/device-gateway/calendar/snapshot",
        json={
            "events": [{"title": "Dentist", "start": "2026-09-08T10:00:00Z"}],
            "captured_at": "2026-09-08T07:00:00Z",
        },
    )
    assert cal.status_code == 200, cal.text
    body = (await phone.get("/v1/device-gateway/today")).json()
    assert body["health"]["available"] is True
    assert body["health"]["freshness"] == "reported"
    assert body["health"]["metrics"] == {"steps": 8123, "sleep_hours": 7.2}
    assert body["health"]["sent_to_model"] is False
    assert body["calendar"]["events"][0]["title"] == "Dentist"
    assert body["calendar"]["sent_to_model"] is False


async def test_today_requires_gateway_credential(client: AsyncClient) -> None:
    res = await client.get("/v1/device-gateway/today")
    assert res.status_code == 401
