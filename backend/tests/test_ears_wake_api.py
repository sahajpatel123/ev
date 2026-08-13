"""POST /v1/ears/wake — the always-on process's delivery contract."""

from __future__ import annotations

import base64

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models import VoiceSession
from app.voice.lifecycle import VoiceState


def _frames(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


async def test_ears_wake_requires_a_bearer_token() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.post(
            "/v1/ears/wake",
            json={"device_id": "mac-ears", "frames_b64": _frames(b"evie"), "consent": True},
        )
    assert resp.status_code == 401


async def test_ears_wake_refuses_without_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={"device_id": "mac-ears", "frames_b64": _frames(b"xxxx evie yyyy"), "consent": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": False, "message": "consent_not_granted"}


async def test_ears_wake_refuses_without_audio(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={"device_id": "mac-ears", "consent": True, "wake_confidence": 0.9},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": False, "message": "no audio"}


async def test_ears_wake_rejects_audio_the_offline_engine_cannot_hear(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "frames_b64": _frames(b"ordinary speech with no name in it"),
            "consent": True,
            "wake_confidence": 0.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] is False
    assert resp.json()["message"] == "wake not confirmed"


async def test_ears_wake_opens_a_session_when_the_phrase_is_in_the_frames(
    client: AsyncClient, db_session
) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "frames_b64": _frames(b"xxxx evie yyyy"),
            "consent": True,
            "wake_confidence": 0.91,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert "ASR cannot transcribe audio" in (body["message"] or "")

    rows = (await db_session.execute(select(VoiceSession))).scalars().all()
    assert len(rows) == 1
    assert rows[0].device_id == "mac-ears"
    assert rows[0].state == VoiceState.AWAKE
    assert rows[0].owner_verified is True


async def test_ears_wake_trusts_an_authenticated_client_confidence_for_offline_engines(
    client: AsyncClient, db_session
) -> None:
    """Phrase doubles never hear speech; the ears process already gated."""

    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "frames_b64": _frames(b"\x00\x01" * 64),
            "consent": True,
            "wake_confidence": 0.88,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    rows = (await db_session.execute(select(VoiceSession))).scalars().all()
    assert len(rows) == 1


async def test_ears_wake_runs_the_utterance_when_a_text_hint_is_supplied(
    client: AsyncClient, db_session
) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "frames_b64": _frames(b"xxxx evie yyyy"),
            "consent": True,
            "wake_confidence": 0.95,
            "text_hint": "what did I decide about the project",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    row = (await db_session.execute(select(VoiceSession))).scalar_one()
    assert row.state == VoiceState.FOLLOW_UP
    assert row.last_utterance_at is not None
