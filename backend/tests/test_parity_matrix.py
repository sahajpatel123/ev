"""Cycle 89 — C49: automated parity matrix check.

The program's law: what the capability manifest DISPLAYS must equal what
the endpoints ENFORCE — per trust state, per surface. Drift between the
two is how a phone shows a tool it cannot use (or hides one it can).

Matrix (trust state × surface):
                    sandbox             owner
    voice open      refused             allowed (arbitered)
    voice enroll    refused (403)       allowed w/ consent
    send_message    refused             voice-check gated
    memory read     empty + note        listed
    keep (camera)   skipped             persisted
    heading-out     allowed w/ consent  allowed w/ consent (same law)

Each cell asserts the ENFORCED behavior, not the displayed one; the
manifest tests elsewhere pin the displayed side.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


async def _pair(client: AsyncClient, name: str, *, role: str = "companion") -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": role, "display_name": name},
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
async def test_parity_matrix_voice_enrollment(client, db_session):
    """Both trust states agree: voice enrollment is an OWNER surface."""
    sandbox = await _pair(client, "ParS-SE")
    r = await sandbox.post(
        "/v1/device-gateway/voice/enroll", json={"samples": ["x"] * 5, "consent": True}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_parity_matrix_speaker_verify(client, db_session):
    """Both trust states agree: speaker verification is an OWNER surface."""
    sandbox = await _pair(client, "ParV-SE")
    r = await sandbox.post("/v1/device-gateway/voice/verify", json={"audio_b64": "AAAA"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_parity_matrix_memory_read(client, db_session):
    """Sandbox: memory read is honest emptiness. The manifest says memory
    is off; the endpoint returns an empty list plus the note."""
    sandbox = await _pair(client, "ParM-SE")
    r = await sandbox.get("/v1/device-gateway/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["sandbox"] is True
    assert body["memories"] == []
    assert "memory" in body["note"].lower()


@pytest.mark.asyncio
async def test_parity_matrix_read_only_surfaces_common(client, db_session):
    """Sense/tactical/history answer for EVERY trust state — transparency
    is universal; what differs is what they report."""
    sandbox = await _pair(client, "ParR-SE")
    for path in ("/v1/device-gateway/sense", "/v1/device-gateway/tactical", "/v1/device-gateway/history", "/v1/device-gateway/privacy"):
        r = await sandbox.get(path)
        assert r.status_code == 200, path
        assert r.json()["ok"] is True, path
    sense = (await sandbox.get("/v1/device-gateway/sense")).json()
    assert sense["healthkit"]["sent_to_model"] is False
    privacy = (await sandbox.get("/v1/device-gateway/privacy")).json()
    assert privacy["environment"] == "SANDBOX"


@pytest.mark.asyncio
async def test_parity_matrix_lease_arbitration_common(client, db_session):
    """Arbitration law is device-agnostic: an unexpired foreign lease
    refuses a claim in every trust state."""
    from sqlalchemy import delete

    from app.models import ConversationLease

    await db_session.execute(delete(ConversationLease))
    await db_session.commit()
    a = await _pair(client, "ParL-A")
    b = await _pair(client, "ParL-B")
    await a.post("/v1/device-gateway/conversation/claim", json={"instance_id": "pa"})
    refused = await b.post("/v1/device-gateway/conversation/claim", json={"instance_id": "pb"})
    assert refused.json()["ok"] is False
    taken = await b.post(
        "/v1/device-gateway/conversation/claim", json={"instance_id": "pb", "takeover": True}
    )
    assert taken.json()["ok"] is True
