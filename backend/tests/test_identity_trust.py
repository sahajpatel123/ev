"""Identity & trust lifecycle: owner binding, trust escalation, recovery, session isolation."""

from __future__ import annotations

from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import VoiceSession


def _client(headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers or {},
    )


async def create_owner(client: httpx.AsyncClient) -> dict:
    resp = await client.post("/v1/identity/owner", json={"display_name": "Sahaj"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_device(client: httpx.AsyncClient, name: str, trust_level: str = "device") -> dict:
    resp = await client.post(
        "/v1/devices",
        json={"name": name, "capabilities": ["voice"], "trust_level": trust_level},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_owner_creation_binds_devices_and_status(client: httpx.AsyncClient) -> None:
    payload = await create_owner(client)
    assert payload["owner_id"]
    assert len(payload["recovery_codes"]) == 8
    assert all("-" in c["code"] for c in payload["recovery_codes"])

    device = await create_device(client, "phone", trust_level="owner")
    assert device["device"]["trust_level"] == "owner"
    assert device["device"]["owner_id"] == payload["owner_id"]

    status = (await client.get("/v1/identity/status")).json()
    assert status["owner_established"] is True
    assert status["trust_level"] == "master"
    assert status["devices_active"] == 1
    assert status["recovery_codes_remaining"] == 8


async def test_owner_creation_rejects_duplicate(client: httpx.AsyncClient) -> None:
    await create_owner(client)
    resp = await client.post("/v1/identity/owner", json={"display_name": "Again"})
    assert resp.status_code == 409
    assert resp.headers.get("X-Error-Code") == "owner_exists"


async def test_plain_device_cannot_manage_identity(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    device = await create_device(client, "watch")
    token = device["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await _client(headers).post("/v1/identity/recovery/codes")
    assert resp.status_code == 403

    resp = await _client(headers).post("/v1/identity/owner", json={"display_name": "Nope"})
    assert resp.status_code == 403


async def test_recovery_redeem_resets_fleet_and_is_single_use(
    client: httpx.AsyncClient,
) -> None:
    owner = await create_owner(client)
    old = await create_device(client, "lost-phone")
    old_token = old["token"]
    code = owner["recovery_codes"][0]["code"]

    anon = _client()
    resp = await anon.post(
        "/v1/identity/recovery/redeem",
        json={"code": code, "device_name": "new-phone", "capabilities": ["voice"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["owner_id"] == owner["owner_id"]
    assert body["device"]["owner_id"] == owner["owner_id"]
    assert body["device"]["trust_level"] == "owner"
    new_token = body["token"]

    # The old device is revoked by the recovery reset.
    old_resp = await _client({"Authorization": f"Bearer {old_token}"}).get("/v1/identity/status")
    assert old_resp.status_code == 401

    # The fresh owner device works.
    new_resp = await _client({"Authorization": f"Bearer {new_token}"}).get("/v1/identity/status")
    assert new_resp.status_code == 200
    assert new_resp.json()["trust_level"] == "owner"

    # The redeemed code is single-use.
    again = await anon.post(
        "/v1/identity/recovery/redeem",
        json={"code": code, "device_name": "third"},
    )
    assert again.status_code == 401


async def test_recovery_lockout_after_failed_attempts(client: httpx.AsyncClient) -> None:
    owner = await create_owner(client)
    anon = _client()
    for _ in range(5):
        resp = await anon.post(
            "/v1/identity/recovery/redeem",
            json={"code": "AAAA-BBBB-CCCC-DDDD-EEEE", "device_name": "bad"},
        )
        assert resp.status_code == 401
    locked = await anon.post(
        "/v1/identity/recovery/redeem",
        json={"code": owner["recovery_codes"][0]["code"], "device_name": "locked"},
    )
    assert locked.status_code == 429
    assert locked.headers.get("X-Error-Code") == "recovery_locked"


async def test_reverification_is_purpose_bound_and_single_use(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    device = await create_device(client, "owner-phone", trust_level="owner")
    token = device["token"]
    headers = {"Authorization": f"Bearer {token}"}
    dev_client = _client(headers)

    issued = await dev_client.post(
        "/v1/identity/reverification",
        json={"purpose": "memory.delete"},
    )
    assert issued.status_code == 200, issued.text
    proof = issued.json()["token"]

    ok = await dev_client.post(
        "/v1/identity/reverification/consume",
        json={"token": proof, "purpose": "memory.delete"},
    )
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    # Single-use: the same proof cannot be consumed twice.
    replay = await dev_client.post(
        "/v1/identity/reverification/consume",
        json={"token": proof, "purpose": "memory.delete"},
    )
    assert replay.status_code == 403

    # A proof minted for one purpose is rejected for another.
    other = await dev_client.post(
        "/v1/identity/reverification",
        json={"purpose": "voice.revoke"},
    )
    mismatch = await dev_client.post(
        "/v1/identity/reverification/consume",
        json={"token": other.json()["token"], "purpose": "memory.delete"},
    )
    assert mismatch.status_code == 403


async def test_reverification_rejects_different_device(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    owner_device = await create_device(client, "owner-phone", trust_level="owner")
    other_device = await create_device(client, "roommate-pad")
    dev_client = _client({"Authorization": f"Bearer {owner_device['token']}"})
    issued = await dev_client.post(
        "/v1/identity/reverification",
        json={"purpose": "memory.delete"},
    )
    proof = issued.json()["token"]

    other = await _client(
        {"Authorization": f"Bearer {other_device['token']}"}
    ).post(
        "/v1/identity/reverification/consume",
        json={"token": proof, "purpose": "memory.delete"},
    )
    assert other.status_code == 403


async def test_voice_enrollment_requires_owner_trust(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    plain = await create_device(client, "guest-pad")
    resp = await _client(
        {"Authorization": f"Bearer {plain['token']}"}
    ).post("/v1/voice/enroll", json={"samples": [], "reason": "x"})
    assert resp.status_code == 403
    assert resp.headers.get("X-Error-Code") == "owner_trust_required"


async def test_voice_session_cannot_be_inherited_by_another_device(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    await create_owner(client)
    device_a = await create_device(client, "owner-phone", trust_level="owner")
    device_b = await create_device(client, "other-pad")

    session = VoiceSession(
        device_id=str(device_a["device"]["id"]),
        state="awake",
        owner_verified=True,
    )
    db_session.add(session)
    await db_session.commit()

    b = _client({"Authorization": f"Bearer {device_b['token']}"})
    status = await b.get(f"/v1/voice/sessions/{session.id}")
    assert status.status_code == 403
    assert status.headers.get("X-Error-Code") == "session_device_mismatch"

    utterance = await b.post(
        "/v1/voice/utterance",
        json={"session_id": str(session.id), "text": "erase my memory"},
    )
    assert utterance.status_code == 403
    assert utterance.headers.get("X-Error-Code") == "session_device_mismatch"

    a = _client({"Authorization": f"Bearer {device_a['token']}"})
    ok = await a.get(f"/v1/voice/sessions/{session.id}")
    assert ok.status_code == 200


async def test_trust_matrix_available(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/identity/trust")
    assert resp.status_code == 200
    body = resp.json()
    assert "memory.delete" in body["owner_required_actions"]
    assert "memory.delete" in body["reverify_required_actions"]
    assert body["levels"]["master"] > body["levels"]["device"]


async def test_memory_delete_requires_reverification_for_device(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    device = await create_device(client, "owner-phone", trust_level="owner")
    headers = {"Authorization": f"Bearer {device['token']}"}
    dev_client = _client(headers)

    event = (
        await client.post(
            "/v1/events",
            json={"source": "test", "event_type": "note", "text": "erase me"},
        )
    ).json()["event"]

    # A device cannot delete memory without a fresh purpose-bound proof.
    denied = await dev_client.delete(f"/v1/events/{event['id']}")
    assert denied.status_code == 403
    assert denied.headers.get("X-Error-Code") == "reverification_required"

    proof = (
        await dev_client.post(
            "/v1/identity/reverification",
            json={"purpose": "memory.delete"},
        )
    ).json()["token"]
    ok = await dev_client.delete(
        f"/v1/events/{event['id']}",
        headers={"X-EV-Reverify": proof},
    )
    assert ok.status_code == 200, ok.text

    # The proof was single-use; replaying it for a second delete fails.
    event2 = (
        await client.post(
            "/v1/events",
            json={"source": "test", "event_type": "note", "text": "erase me too"},
        )
    ).json()["event"]
    replay = await dev_client.delete(
        f"/v1/events/{event2['id']}",
        headers={"X-EV-Reverify": proof},
    )
    assert replay.status_code == 403


async def test_voice_revoke_requires_reverification_for_device(
    client: httpx.AsyncClient,
) -> None:
    await create_owner(client)
    device = await create_device(client, "owner-phone", trust_level="owner")
    headers = {"Authorization": f"Bearer {device['token']}"}
    dev_client = _client(headers)

    denied = await dev_client.post(
        f"/v1/voice/enrollments/{uuid4()}/revoke",
        json={"reason": "voice changed"},
    )
    assert denied.status_code == 403
    assert denied.headers.get("X-Error-Code") == "reverification_required"

    proof = (
        await dev_client.post(
            "/v1/identity/reverification",
            json={"purpose": "voice.revoke"},
        )
    ).json()["token"]
    # Authentication passes now; the runtime reports the missing enrollment.
    passed = await dev_client.post(
        f"/v1/voice/enrollments/{uuid4()}/revoke",
        json={"reason": "voice changed"},
        headers={"X-EV-Reverify": proof},
    )
    assert passed.status_code != 403
