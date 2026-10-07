"""Anticipatory Today: the /today briefing block composes next event,
top reminder, digest preview, and honest counts — no model call.
Sandbox devices get counts and daypart only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

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


async def test_briefing_composes_next_event_and_counts(client: AsyncClient) -> None:
    body, phone = await _pair(client)
    try:
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": body["device"]["device_id"], "reason": "owner"},
        )
        start = (datetime.now(UTC) + timedelta(hours=2, minutes=15)).isoformat()
        snap = await phone.post(
            "/v1/device-gateway/calendar/snapshot",
            json={"events": [{"title": "Dentist", "start": start}], "captured_at": start},
        )
        assert snap.status_code == 200, snap.text
        queued = await phone.post(
            "/v1/device-gateway/queue",
            json={"idempotency_key": "brief-q-0001", "kind": "note", "payload": {"text": "buy milk"}},
        )
        assert queued.status_code == 201, queued.text
        res = await phone.get("/v1/device-gateway/today")
        assert res.status_code == 200, res.text
        briefing = res.json()["briefing"]
        assert briefing["daypart"] in {"morning", "afternoon", "evening", "night"}
        assert briefing["next_event"]["title"] == "Dentist"
        assert briefing["next_event"]["in_label"].startswith("in ")
        assert briefing["counts"]["queue_pending"] == 1
        assert isinstance(briefing["digest_preview"], list)
        assert "generated_at" in briefing
    finally:
        await phone.aclose()


async def test_briefing_without_events_is_honest(client: AsyncClient) -> None:
    body, phone = await _pair(client)
    try:
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": body["device"]["device_id"], "reason": "owner"},
        )
        res = await phone.get("/v1/device-gateway/today")
        briefing = res.json()["briefing"]
        assert briefing["next_event"] is None
        assert briefing["top_reminder"] is None
        assert briefing["counts"]["queue_pending"] == 0
    finally:
        await phone.aclose()


async def test_sandbox_briefing_has_counts_only(client: AsyncClient) -> None:
    _body, phone = await _pair(client)
    try:
        start = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        await phone.post(
            "/v1/device-gateway/calendar/snapshot",
            json={"events": [{"title": "Secret", "start": start}]},
        )
        await phone.post(
            "/v1/device-gateway/queue",
            json={"idempotency_key": "brief-q-0002", "kind": "note", "payload": {"text": "x"}},
        )
        res = await phone.get("/v1/device-gateway/today")
        assert res.json()["memory_enabled"] is False
        briefing = res.json()["briefing"]
        assert briefing["next_event"] is None
        assert briefing["top_reminder"] is None
        assert briefing["digest_preview"] == []
        assert briefing["counts"]["queue_pending"] == 1
        assert briefing["daypart"] in {"morning", "afternoon", "evening", "night"}
    finally:
        await phone.aclose()
