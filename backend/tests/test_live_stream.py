"""Real-time live-event streaming: privacy slices, replay, SSE framing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.services.live_stream import stream_live_events


async def _ingest(
    client: AsyncClient,
    channel: str,
    kind: str,
    event_type: str,
    payload: dict,
    *,
    privacy_level: str = "normal",
) -> dict:
    body = {
        "channel": channel,
        "kind": kind,
        "events": [{"event_type": event_type, "payload": payload}],
    }
    if privacy_level != "normal":
        body["privacy_level"] = privacy_level
    resp = await client.post("/v1/live/events", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()[0]


async def test_live_stream_model_slice_emits_derived_context_only(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC)
    agen = stream_live_events(
        db_session, access="model", poll_interval=0.05, since=since
    )
    try:
        event = await _ingest(
            client,
            "screen-activity",
            "screen",
            "focus_change",
            {"app": "Xcode", "text": "secret raw screen text"},
        )
        item = await asyncio.wait_for(anext(agen), timeout=5)
        assert item["id"] == event["id"]
        assert item["channel_name"] == "screen-activity"
        assert item["kind"] == "screen"
        assert item["collector"] == "screen-activity"
        assert "payload" not in item
        assert "secret raw screen text" not in item["context"]
        assert "Xcode" in item["context"]
    finally:
        await agen.aclose()


async def test_live_stream_user_slice_includes_payload(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC)
    agen = stream_live_events(
        db_session, access="user", poll_interval=0.05, since=since
    )
    try:
        event = await _ingest(client, "health-belt", "health", "heart_rate", {"bpm": 132, "text": "steady"})
        item = await asyncio.wait_for(anext(agen), timeout=5)
        assert item["id"] == event["id"]
        assert item["payload"] == {"bpm": 132, "text": "steady"}
    finally:
        await agen.aclose()


async def test_live_stream_model_slice_skips_sensitive_channel(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC)
    agen = stream_live_events(
        db_session, access="model", poll_interval=0.05, since=since
    )
    try:
        resp = await client.post(
            "/v1/live/channels",
            json={
                "name": "private-audio",
                "kind": "audio",
                "privacy_level": "sensitive",
            },
        )
        assert resp.status_code == 201, resp.text
        channel_id = resp.json()["id"]
        resp = await client.post(
            f"/v1/live/channels/{channel_id}/events",
            json=[
                {
                    "event_type": "scene",
                    "payload": {"scene": "meeting", "in_call": True, "transcript": "private strategy"},
                }
            ],
        )
        assert resp.status_code == 201, resp.text
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(agen), timeout=0.4)
    finally:
        await agen.aclose()


async def test_live_stream_endpoint_sse_frames_events() -> None:
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-key"}
    stream_client = AsyncClient(transport=transport, base_url="http://test", headers=headers)
    ingest_client = AsyncClient(transport=transport, base_url="http://test", headers=headers)
    try:
        since = datetime.now(UTC)
        event = await _ingest(
            ingest_client,
            "screen-activity",
            "screen",
            "focus_change",
            {"app": "Xcode"},
        )
        async with stream_client.stream(
            "GET",
            (
                "/v1/live/stream?poll_interval=0.1&timeout_seconds=0.5"
                f"&since={quote(since.isoformat())}&access=user"
            ),
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = await asyncio.wait_for(resp.aread(), timeout=5)
        text = body.decode("utf-8")
        assert "event: live" in text
        assert "event: done" in text
        assert event["id"] in text
        assert '"event_type": "focus_change"' in text
    finally:
        await stream_client.aclose()
        await ingest_client.aclose()
