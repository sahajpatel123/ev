"""Gear telemetry → ranked, deduped, quiet-hours-aware alerts."""

from __future__ import annotations

from httpx import AsyncClient


async def snapshot(
    client: AsyncClient,
    *,
    device_id: str = "iphone-16-pro",
    battery: float | None = 80.0,
    storage_free_bytes: int | None = 100 * 1024**3,
    cpu: float | None = 5.0,
    memory: float | None = 30.0,
) -> dict:
    resp = await client.post(
        "/v1/gear/snapshot",
        json={
            "device_id": device_id,
            "battery_percent": battery,
            "storage_free_bytes": storage_free_bytes,
            "cpu_percent": cpu,
            "memory_used_percent": memory,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_low_battery_creates_ranked_alert_and_dedupes(client: AsyncClient) -> None:
    await snapshot(client, battery=8.0)

    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["scanned_devices"] == 1
    assert payload["duplicates_skipped"] == 0
    assert len(payload["alerts_created"]) == 1
    alert = payload["alerts_created"][0]
    assert alert["kind"] == "gear"
    assert alert["tier"] == "urgent"
    assert alert["priority"] == 0.8
    assert "Battery at 8%" in alert["body"]

    # Second scan: same fingerprint, no new alerts.
    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200
    second = resp.json()
    assert second["alerts_created"] == []
    assert second["duplicates_skipped"] == 1


async def test_healthy_device_produces_no_alerts(client: AsyncClient) -> None:
    await snapshot(client, battery=80.0, storage_free_bytes=100 * 1024**3)

    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alerts_created"] == []


async def test_quiet_hours_suppress_non_urgent_alerts(
    client: AsyncClient, monkeypatch
) -> None:
    from app.config import settings

    await snapshot(client, battery=15.0)  # useful tier, not urgent

    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alerts_created"] == []
    monkeypatch.setattr(settings, "quiet_hours_start", "23:59")
    monkeypatch.setattr(settings, "quiet_hours_end", "00:00")

    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["alerts_created"]) == 1
    assert payload["alerts_created"][0]["tier"] == "useful"


async def test_multiple_metrics_on_one_device(client: AsyncClient) -> None:
    await snapshot(client, battery=5.0, storage_free_bytes=1 * 1024**3, cpu=95.0, memory=95.0)

    resp = await client.post("/v1/gear/scan")
    assert resp.status_code == 200, resp.text
    alerts = resp.json()["alerts_created"]
    metrics = {a["title"].split("— ")[-1] for a in alerts}
    assert metrics == {"battery", "storage", "cpu", "memory"}
    assert len(alerts) == 4
