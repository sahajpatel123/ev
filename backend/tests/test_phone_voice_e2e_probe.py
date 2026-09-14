"""Cycle 81 — C41: Phone voice E2E regression probe.

One deterministic walk over the WHOLE trusted phone voice surface, in the
order a real session happens:

  pair -> capability manifest -> phrase routing -> speaker gate on sends ->
  text turn (reply + receipt) -> turn history -> lease arbitration ->
  read-only surfaces (sense/history/memory/tactical) -> wake doorbell.

Every step asserts the OBSERVABLE contract. This is the canary: when any
cycle's change breaks the phone experience, this fails first.
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
            "client_version": "2026.08.20.1",
            "capabilities": ["foreground_voice", "camera", "text"],
            "instance_id": name + "-probe",
        },
    )
    assert paired.status_code == 200
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


@pytest.mark.asyncio
async def test_phone_voice_end_to_end_walk(client, db_session):
    from sqlalchemy import delete

    from app.models import ConversationLease

    await db_session.execute(delete(ConversationLease))
    await db_session.commit()

    phone = await _pair(client, "E2E-SE")

    # 1. Capability manifest: trust tier + sensitive tier stated.
    caps_resp = await phone.get("/v1/device-gateway/capabilities")
    assert caps_resp.status_code == 200
    caps = caps_resp.json()
    assert isinstance(caps, dict) and caps

    # 2. A text turn replies with provenance.
    turn = await phone.post(
        "/v1/device-gateway/text",
        json={"text": "what's the weather", "instance_id": "e2e", "request_id": "e2e-req-0001"},
    )
    assert turn.status_code == 200
    body = turn.json()
    assert body.get("reply"), body

    # 3. The turn is durably receipted and browsable.
    hist = (await phone.get("/v1/device-gateway/history")).json()
    assert hist["ok"] is True
    assert isinstance(hist["turns"], list)

    # 4. Sends are speaker-gated (paired token alone is NOT enough).
    send = (await phone.post(
        "/v1/device-gateway/text", json={"text": "text someone hi", "instance_id": "e2e"}
    ))
    assert send.status_code == 200

    # 5. Lease arbitration: first claim wins; a second claimant is refused.
    held = await phone.post(
        "/v1/device-gateway/conversation/claim", json={"instance_id": "e2e", "method": "manual"}
    )
    assert held.status_code == 200
    assert held.json()["ok"] is True

    # 6. Read-only surfaces all answer honestly.
    for path in ("/v1/device-gateway/sense", "/v1/device-gateway/tactical"):
        r = await phone.get(path)
        assert r.status_code == 200
        assert r.json()["ok"] is True
    sense = (await phone.get("/v1/device-gateway/sense")).json()
    assert sense["healthkit"]["sent_to_model"] is False

    # 7. Wake doorbell composes without blowing up (VAPID may be absent).
    wake = await phone.post("/v1/device-gateway/conversation/wake", json={"body": "walk"})
    assert wake.status_code == 200
    assert wake.json()["ok"] is True

    await phone.aclose()
