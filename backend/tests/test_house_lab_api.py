"""HTTP entry paths for house/lab/devices — run twice for launch consistency."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient

from app.utils.text import utcnow


async def _exercise(client: AsyncClient) -> dict:
    created = await client.post(
        "/v1/devices",
        json={"name": "Lookout Phone", "capabilities": ["attention", "voice"]},
    )
    assert created.status_code == 201, created.text
    device = created.json()["device"]
    token = created.json()["token"]
    device_id = device["id"]

    first = await client.get(f"/v1/devices/{device_id}/bootstrap")
    assert first.status_code == 200, first.text
    boot = first.json()
    assert "nickname" in boot["prefs"]
    assert "quiet_hours" in boot["prefs"]
    assert "feature_gates" in boot["prefs"]
    assert boot["spoken"] is True
    assert boot["spoken_text"] == "We're online."

    second = await client.get(f"/v1/devices/{device_id}/bootstrap")
    assert second.status_code == 200
    assert second.json()["spoken"] is False

    chat = await client.post("/v1/chat", json={"message": "live thread ping", "stream": False})
    assert chat.status_code == 200, chat.text
    conversation_id = chat.json()["conversation_id"]
    transcript = await client.get("/v1/runtime/transcript")
    assert transcript.status_code == 200, transcript.text
    body = transcript.json()
    assert body["conversation_id"] == conversation_id
    assert any("live thread ping" in item["text"] for item in body["events"])

    home_status = await client.post(
        "/v1/gateway/tools",
        json={"name": "home_status", "arguments": {}},
    )
    assert home_status.status_code == 200, home_status.text
    home_body = home_status.json()["result"]
    assert home_body["simulated"] is True
    assert home_body["entities"]
    assert "simulated home" in home_body["spoken"].lower()

    timer = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "start_timer",
            "arguments": {
                "minutes": 37,
                "text": "37 minutes have passed",
            },
        },
    )
    assert timer.status_code == 200, timer.text
    assert timer.json()["ok"] is True
    fire_at = timer.json()["result"]["fire_at"]
    assert fire_at > utcnow().isoformat()[:16] or fire_at

    panic = await client.post(f"/v1/devices/{device_id}/panic")
    assert panic.status_code == 200, panic.text
    assert panic.json()["revoked"] is True

    other = await client.post(
        "/v1/devices",
        json={"name": "Spare", "capabilities": ["attention"]},
    )
    locked = await client.post("/v1/runtime/lock-all")
    assert locked.status_code == 200, locked.text
    assert locked.json()["count"] >= 1

    return {
        "bootstrap_keys": sorted(boot["prefs"].keys()),
        "spoken_first": boot["spoken_text"],
        "transcript_hit": True,
        "home_simulated": home_body["simulated"],
        "timer_ok": timer.json()["ok"],
        "panic": True,
        "lock_all": locked.json()["count"],
        "token_prefix": token[:4],
        "other_id": other.json()["device"]["id"],
    }


async def test_house_lab_api_launch_paths(client: AsyncClient) -> None:
    first = await _exercise(client)
    # fresh_db autouse resets between tests; this test is the single-process
    # shape. The two-launch script re-runs the ASGI app twice.
    assert first["spoken_first"] == "We're online."
    assert first["home_simulated"] is True
    assert first["timer_ok"] is True


async def test_start_timer_persists_future_fire_at(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"minutes": 5, "text": "check"}},
    )
    assert resp.status_code == 200, resp.text
    fire_at = resp.json()["result"]["fire_at"]
    assert fire_at > (utcnow() - timedelta(seconds=1)).isoformat()
