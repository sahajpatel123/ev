"""Offline oracle drafts: replayed answers carry honest age labels.

The reply text is untouched; staleness rides as metadata (asked_at,
answered_late, age_label) so the phone can show WHEN the question was
asked. Exactly-once execution is unchanged.
"""

from __future__ import annotations

from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import OfflineQueueItem
from app.utils.text import utcnow


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


async def test_replay_labels_stale_answer(client: AsyncClient, db_session: AsyncSession) -> None:
    body, phone = await _pair(client)
    try:
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": body["device"]["device_id"], "reason": "owner"},
        )
        queued = await phone.post(
            "/v1/device-gateway/queue",
            json={
                "idempotency_key": "oracle-q-0001",
                "kind": "voice_intent",
                "payload": {"text": "what time is it"},
            },
        )
        assert queued.status_code == 201, queued.text
        row = (
            await db_session.execute(
                select(OfflineQueueItem).where(OfflineQueueItem.idempotency_key == "oracle-q-0001")
            )
        ).scalar_one()
        row.created_at = utcnow() - timedelta(hours=2, minutes=5)
        await db_session.commit()
        res = await phone.post(
            "/v1/device-gateway/queue/replay",
            json={"idempotency_key": "oracle-q-0001"},
        )
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["executed"] is True
        assert out["reply"]
        assert out["answered_late"] is True
        assert out["age_label"] == "asked 2h ago"
        assert out["asked_at"]
        # Exactly-once: a second replay reports the terminal state.
        again = await phone.post(
            "/v1/device-gateway/queue/replay",
            json={"idempotency_key": "oracle-q-0001"},
        )
        assert again.json()["executed"] is True
    finally:
        await phone.aclose()


async def test_replay_fresh_answer_is_not_late(client: AsyncClient) -> None:
    body, phone = await _pair(client)
    try:
        await client.post(
            "/v1/device-gateway/admin/promote-owner",
            json={"device_id": body["device"]["device_id"], "reason": "owner"},
        )
        await phone.post(
            "/v1/device-gateway/queue",
            json={
                "idempotency_key": "oracle-q-0002",
                "kind": "voice_intent",
                "payload": {"text": "what time is it"},
            },
        )
        res = await phone.post(
            "/v1/device-gateway/queue/replay",
            json={"idempotency_key": "oracle-q-0002"},
        )
        out = res.json()
        assert out["answered_late"] is False
        assert out["age_label"] == "just now"
    finally:
        await phone.aclose()
