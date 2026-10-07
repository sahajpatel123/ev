"""Conversation teleport: the talk moves between phones with receipts.

Explicit takeover (claim with takeover, "continue here" text) moves the
lease and leaves a "continued on X" receipt on the previous holder.
"Continue on my other phone" sends a tap-to-accept offer; nothing moves
until the other phone accepts.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _pair(client: AsyncClient, *, role: str, name: str) -> tuple[dict, AsyncClient]:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": role, "display_name": name},
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


async def _promote(client: AsyncClient, body: dict) -> None:
    res = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        json={"device_id": body["device"]["device_id"], "reason": "owner"},
    )
    assert res.status_code == 200, res.text


async def _two_phones(client: AsyncClient) -> tuple[dict, AsyncClient, dict, AsyncClient]:
    pro_body, pro = await _pair(client, role="primary_companion", name="16 Pro")
    se_body, se = await _pair(client, role="secondary_companion", name="SE")
    await _promote(client, pro_body)
    await _promote(client, se_body)
    return pro_body, pro, se_body, se


async def test_takeover_claim_leaves_receipt(client: AsyncClient) -> None:
    pro_body, pro, _se_body, se = await _two_phones(client)
    try:
        first = await se.post(
            "/v1/device-gateway/conversation/claim",
            json={"instance_id": "SE-tab", "method": "manual"},
        )
        assert first.status_code == 200, first.text
        refused = await pro.post(
            "/v1/device-gateway/conversation/claim",
            json={"instance_id": "16 Pro-tab", "method": "manual"},
        )
        assert refused.json()["refused"] == "lease_active"
        took = await pro.post(
            "/v1/device-gateway/conversation/claim",
            json={"instance_id": "16 Pro-tab", "method": "manual", "takeover": True},
        )
        assert took.status_code == 200, took.text
        assert took.json()["took_over"] is True
        assert took.json()["previous_holder"]["device_id"] == _se_body["device"]["device_id"]
        inbox = (await se.get("/v1/device-gateway/inbox")).json()["items"]
        receipt = next(i for i in inbox if i.get("kind") == "conversation_continued")
        assert receipt["payload"]["to_device_id"] == pro_body["device"]["device_id"]
        assert "16 Pro" in receipt["body"]
    finally:
        await pro.aclose()
        await se.aclose()


async def test_continue_here_text_takes_over(client: AsyncClient) -> None:
    _pro_body, pro, se_body, se = await _two_phones(client)
    try:
        await se.post(
            "/v1/device-gateway/conversation/claim",
            json={"instance_id": "SE-tab", "method": "manual"},
        )
        res = await pro.post(
            "/v1/device-gateway/text",
            json={"text": "Continue here.", "instance_id": "16 Pro-tab"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["route"] == "TELEPORT"
        assert body["took_over"] is True
        assert body["previous_holder"]["device_id"] == se_body["device"]["device_id"]
        assert "SE" in body["reply"]
        inbox = (await se.get("/v1/device-gateway/inbox")).json()["items"]
        assert any(i.get("kind") == "conversation_continued" for i in inbox)
    finally:
        await pro.aclose()
        await se.aclose()


async def test_continue_on_other_phone_sends_offer(client: AsyncClient) -> None:
    pro_body, pro, _se_body, se = await _two_phones(client)
    try:
        res = await se.post(
            "/v1/device-gateway/text",
            json={"text": "Continue on my other phone.", "instance_id": "SE-tab"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["route"] == "TELEPORT"
        assert body["offer"] is True
        assert body["target_device_id"] == pro_body["device"]["device_id"]
        assert "16 Pro" in body["reply"]
        inbox = (await pro.get("/v1/device-gateway/inbox")).json()["items"]
        offer = next(i for i in inbox if i.get("kind") == "conversation_migrate_offer")
        assert offer["payload"]["from_display_name"] == "SE"
        # Nothing moved: no lease was claimed by the offer.
        assert body.get("lease") is None
    finally:
        await pro.aclose()
        await se.aclose()


async def test_teleport_without_other_phone_is_honest(client: AsyncClient) -> None:
    body, solo = await _pair(client, role="primary_companion", name="Solo")
    await _promote(client, body)
    try:
        res = await solo.post(
            "/v1/device-gateway/text",
            json={"text": "Continue on my other phone.", "instance_id": "Solo-tab"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["route"] == "TELEPORT"
        assert res.json()["offer"] is False
        assert "isn't paired" in res.json()["reply"]
    finally:
        await solo.aclose()
