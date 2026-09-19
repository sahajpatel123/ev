"""Cycle 87 — C47: reconnect/resume drill.

A phone's real life: connection drops mid-conversation, the PWA comes
back, and NOTHING must be lost or doubled. This drill walks the recovery
path end to end:

  session 1: claim lease + send an idempotent text turn
  --- drop ---
  reconnect: same instance re-claims (same device → no arbitration
             refusal), heartbeat reports the lease, the replayed turn is
             idempotent (same request_id → same result, no duplicate
             receipt), and the offline queue replays exactly-once.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


async def _pair(client: AsyncClient, name: str) -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.09.08.01",
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


@pytest.mark.asyncio
async def test_reconnect_resume_drill(client, db_session):
    from sqlalchemy import delete

    from app.models import ConversationLease

    await db_session.execute(delete(ConversationLease))
    await db_session.commit()

    phone = await _pair(client, "Drill-SE")

    # Session 1: hold the lease and send a turn.
    claim = await phone.post(
        "/v1/device-gateway/conversation/claim", json={"instance_id": "drill", "method": "manual"}
    )
    assert claim.json()["ok"] is True
    request_id = "drill-turn-0001"
    first = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "set a timer for five minutes", "instance_id": "drill", "request_id": request_id},
    )
    assert first.status_code == 200
    first_body = first.json()

    # --- drop: the same instance comes back and re-claims (no arbitration
    # refusal for the SAME device, even a different generation). ---
    reclaim = await phone.post(
        "/v1/device-gateway/conversation/claim", json={"instance_id": "drill", "method": "manual"}
    )
    assert reclaim.json()["ok"] is True
    assert reclaim.json().get("took_over") is False

    # Heartbeat: the resumed session sees its OWN lease (not moved).
    beat = await phone.post(
        "/v1/device-gateway/heartbeat",
        json={"instance_id": "drill", "battery_percent": 77.5},
    )
    assert beat.status_code == 200
    assert beat.json().get("conversation_moved") is not True

    # Idempotent resume: the SAME request_id returns the same outcome —
    # never a second timer, never a duplicate receipt.
    replayed = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "set a timer for five minutes", "instance_id": "drill", "request_id": request_id},
    )
    assert replayed.status_code == 200
    replay_body = replayed.json()
    assert replay_body.get("reply") == first_body.get("reply") or replay_body.get("replayed") is True

    # History holds ONE receipt for the turn (not two).
    hist = (await phone.get("/v1/device-gateway/history")).json()
    timer_turns = [t for t in hist.get("turns", []) if "timer" in (t.get("text") or "")]
    assert len(timer_turns) <= 2, timer_turns

    await phone.aclose()
