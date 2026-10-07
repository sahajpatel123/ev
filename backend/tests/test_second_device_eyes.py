"""Second-device eyes: explicit cross-phone Look routing with dual provenance.

A trusted phone can say "look through my other phone" or name the target
("use the SE camera"). The target gets a named inbox camera_request, the
captured frame records BOTH parties, and sandbox phones are never targeted.
"""

from __future__ import annotations

import base64

from httpx import ASGITransport, AsyncClient

from app.main import app


def _jpeg(width: int = 320, height: int = 240) -> str:
    sof = bytes(
        [
            0xFF, 0xC0, 0x00, 0x0B, 0x08,
            (height >> 8) & 0xFF, height & 0xFF,
            (width >> 8) & 0xFF, width & 0xFF,
            0x01, 0x01, 0x11, 0x00,
        ]
    )
    raw = b"\xff\xd8" + sof + (b"\x00" * 80) + b"\xff\xd9"
    return base64.b64encode(raw).decode("ascii")


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


async def test_other_phone_routes_to_other_camera(client: AsyncClient) -> None:
    pro_body, pro, se_body, se = await _two_phones(client)
    try:
        res = await se.post(
            "/v1/device-gateway/text",
            json={"text": "Look at this through my other phone.", "instance_id": "SE-tab"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["route"] == "CAMERA"
        assert body["remote"] is True
        assert body["needs_camera"] is False
        assert body["camera_target_device_id"] == pro_body["device"]["device_id"]
        assert body["camera_reason"] == "explicit_named_device"
        assert body["target_display_name"] == "16 Pro"
    finally:
        await pro.aclose()
        await se.aclose()


async def test_named_camera_by_display_name(client: AsyncClient) -> None:
    pro_body, pro, se_body, se = await _two_phones(client)
    try:
        res = await pro.post(
            "/v1/device-gateway/text",
            json={"text": "Use the SE camera to look at this.", "instance_id": "16 Pro-tab"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["route"] == "CAMERA"
        assert body["remote"] is True
        assert body["camera_target_device_id"] == se_body["device"]["device_id"]
        assert body["origin_display_name"] == "16 Pro"
    finally:
        await pro.aclose()
        await se.aclose()


async def test_target_gets_named_camera_request(client: AsyncClient) -> None:
    _pro_body, pro, _se_body, se = await _two_phones(client)
    try:
        res = await se.post(
            "/v1/device-gateway/text",
            json={"text": "Look at this through my other phone.", "instance_id": "SE-tab"},
        )
        request_id = res.json()["camera_request_id"]
        inbox = (await pro.get("/v1/device-gateway/inbox")).json()["items"]
        req = next(i for i in inbox if i.get("kind") == "camera_request")
        assert req["payload"]["request_id"] == request_id
        assert req["payload"]["origin_display_name"] == "SE"
        assert req["payload"]["target_display_name"] == "16 Pro"
    finally:
        await pro.aclose()
        await se.aclose()


async def test_remote_frame_carries_dual_provenance(client: AsyncClient) -> None:
    _pro_body, pro, _se_body, se = await _two_phones(client)
    try:
        res = await se.post(
            "/v1/device-gateway/text",
            json={"text": "Look at this through my other phone.", "instance_id": "SE-tab"},
        )
        request_id = res.json()["camera_request_id"]
        posted = await pro.post(
            "/v1/device-gateway/camera/result",
            json={"request_id": request_id, "jpeg_b64": _jpeg(), "action": "look_once"},
        )
        assert posted.status_code == 200, posted.text
        vision = posted.json()["vision"]
        assert vision["capture_provenance"]["remote"] is True
        assert vision["capture_provenance"]["origin_display_name"] == "SE"
        assert vision["capture_provenance"]["capturing_display_name"] == "16 Pro"
        # The origin can poll the shared request for the frame.
        poll = await se.get(f"/v1/device-gateway/camera/{request_id}")
        assert poll.status_code == 200, poll.text
        assert poll.json()["has_frame"] is True
    finally:
        await pro.aclose()
        await se.aclose()


async def test_sandbox_phone_is_never_targeted(client: AsyncClient) -> None:
    _pro_body, pro, _se_body, se = await _two_phones(client)
    sandbox_body, sandbox = await _pair(client, role="companion", name="Sandbox Phone")
    try:
        sandbox_id = sandbox_body["device"]["device_id"]
        res = await pro.post(
            "/v1/device-gateway/text",
            json={"text": "Look at this.", "instance_id": "16 Pro-tab"},
        )
        assert res.json()["camera_target_device_id"] != sandbox_id
        # Even an explicit name cannot route a Look to a sandbox phone:
        # the named branch refuses and default routing runs instead.
        named = await se.post(
            "/v1/device-gateway/text",
            json={"text": "Use the sandbox phone camera to look at this.", "instance_id": "SE-tab"},
        )
        assert named.json()["camera_target_device_id"] != sandbox_id
        assert named.json()["camera_reason"] != "explicit_named_device"
    finally:
        await pro.aclose()
        await se.aclose()
        await sandbox.aclose()
