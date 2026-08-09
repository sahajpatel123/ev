"""EVIE voice lifecycle: wake → verify → listen → act → reply → follow-up → idle."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VoiceSession
from app.utils.text import utcnow
from app.voice.anti_spoof import ReplayError, ReplayGuard

SAMPLE_A = b"owner-voice-sample-" * 40
SAMPLE_B = b"other-speaker-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def owner_samples() -> list[str]:
    return [b64(SAMPLE_A) for _ in range(5)]


async def grant_voice_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text


async def enroll_owner(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/voice/enroll",
        json={
            "samples": [
                {"audio_b64": sample, "liveness_proof": "live"}
                for sample in owner_samples()
            ],
            "reason": "lifecycle test enrollment",
        },
    )
    assert resp.status_code == 201, resp.text


async def wake(client: AsyncClient, device_id: str) -> dict:
    resp = await client.post(
        "/v1/voice/wake",
        json={"device_id": device_id, "text_hint": "hey evie"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def verify(
    client: AsyncClient,
    *,
    session_id: str,
    nonce: str,
    phrase: str,
    samples: list[str],
    audio_sha256: str | None = None,
) -> dict:
    payload: dict = {
        "session_id": session_id,
        "nonce": nonce,
        "phrase": phrase,
        "samples": samples,
        "liveness_proof": "live",
    }
    if audio_sha256:
        payload["audio_sha256"] = audio_sha256
    resp = await client.post("/v1/voice/verify", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_full_voice_lifecycle(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)

    wake_out = await wake(client, "mac-1")
    assert wake_out["state"] == "verifying"
    assert wake_out["owner_enrolled"] is True
    assert wake_out["challenge_nonce"]
    assert wake_out["challenge_phrase"]
    session_id = wake_out["session_id"]

    verify_out = await verify(
        client,
        session_id=session_id,
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
        audio_sha256=hashlib.sha256(SAMPLE_A).hexdigest(),
    )
    assert verify_out["verified"] is True
    assert verify_out["state"] == "awake"
    assert verify_out["confidence"] >= 0.8

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "Remind me to call mom tomorrow morning",
        },
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["state"] == "follow_up"
    assert first["transcript"] == "Remind me to call mom tomorrow morning"
    assert first["reply"]
    assert first["tts"]["ssml"].startswith("<speak>")
    assert first["style"]["mode"] in {"casual", "command", "emergency"}
    assert first["conversation_id"]

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "Also remind me to buy milk",
            "follow_up": True,
        },
    )
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["state"] == "follow_up"
    assert second["reply"]

    resp = await client.get(f"/v1/voice/sessions/{session_id}")
    assert resp.status_code == 200
    status = resp.json()
    assert status["state"] == "follow_up"
    assert status["owner_verified"] is True
    assert 0 < status["follow_up_remaining_seconds"] <= 30

    resp = await client.post(f"/v1/voice/sessions/{session_id}/end")
    assert resp.status_code == 200
    assert resp.json()["state"] == "ended"


async def test_wake_without_enrollment_is_refused(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    wake_out = await wake(client, "mac-none")
    assert wake_out["state"] == "idle"
    assert wake_out["owner_enrolled"] is False
    assert "enroll" in (wake_out["message"] or "").lower()


async def test_unknown_voice_gets_polite_refusal(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-unknown")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_B)],
    )
    assert verify_out["verified"] is False
    assert verify_out["state"] == "ended"
    assert "owner" in verify_out["reason"].lower()


async def test_unverified_session_cannot_utter(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-eavesdrop")
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "text": "Delete everything",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Session not verified — owner verification required"


async def test_replay_fingerprint_is_rejected(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    fingerprint = hashlib.sha256(SAMPLE_A).hexdigest()

    first_wake = await wake(client, "mac-replay-1")
    first_verify = await verify(
        client,
        session_id=first_wake["session_id"],
        nonce=first_wake["challenge_nonce"],
        phrase=first_wake["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
        audio_sha256=fingerprint,
    )
    assert first_verify["verified"] is True
    resp = await client.post(f"/v1/voice/sessions/{first_wake['session_id']}/end")
    assert resp.status_code == 200

    second_wake = await wake(client, "mac-replay-2")
    resp = await client.post(
        "/v1/voice/verify",
        json={
            "session_id": second_wake["session_id"],
            "nonce": second_wake["challenge_nonce"],
            "phrase": second_wake["challenge_phrase"],
            "samples": [b64(SAMPLE_A)],
            "liveness_proof": "live",
            "audio_sha256": fingerprint,
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "replay_rejected"


async def test_nonce_replay_guard_rejects_reuse(db_session: AsyncSession) -> None:
    guard = ReplayGuard(db_session)
    challenge = await guard.issue(purpose="verify", session_id=None)
    consumed = await guard.consume(
        challenge.nonce, purpose="verify", session_id=None
    )
    assert consumed.nonce == challenge.nonce
    with pytest.raises(ReplayError, match="already used"):
        await guard.consume(challenge.nonce, purpose="verify", session_id=None)


async def test_follow_up_window_expires(client: AsyncClient, db_session: AsyncSession) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-followup")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True
    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "text": "Set a timer for ten minutes"},
    )
    assert resp.status_code == 200

    row = await db_session.get(VoiceSession, UUID(wake_out["session_id"]))
    assert row is not None
    row.follow_up_until = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "text": "Actually make it five",
            "follow_up": True,
        },
    )
    assert resp.status_code == 428
    assert resp.json()["detail"] == (
        "30-second follow-up window expired — say 'EVIE' to wake again"
    )


async def _establish_owner(client: AsyncClient) -> None:
    resp = await client.post("/v1/identity/owner", json={"display_name": "EVIE Owner"})
    assert resp.status_code == 201, resp.text


async def _verified_session(client: AsyncClient, device_id: str) -> dict:
    wake_out = await wake(client, device_id)
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True
    return wake_out


async def test_sensitive_voice_command_requires_reverification(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    await _establish_owner(client)
    session = await _verified_session(client, "mac-sensitive")

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Delete all my memories",
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "reverification_required"

    resp = await client.post(
        "/v1/identity/reverification",
        json={
            "purpose": "voice.sensitive_action",
            "voice_session_id": session["session_id"],
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Delete all my memories",
            "reverify_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"]


async def test_sensitive_voice_command_rejects_bad_proof(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    await _establish_owner(client)
    session = await _verified_session(client, "mac-badproof")

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Transfer money to an external account",
            "reverify_token": "bogus-token",
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "reverification_rejected"


async def test_quiet_hours_block_non_urgent_wake(
    client: AsyncClient, monkeypatch
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *_: True)

    resp = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-quiet", "text_hint": "hey evie"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "idle"
    assert "quiet hours" in (resp.json()["message"] or "").lower()

    resp = await client.post(
        "/v1/voice/wake",
        json={
            "device_id": "mac-quiet",
            "text_hint": "hey evie",
            "priority": 0.9,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "verifying"


async def test_silence_lock_ends_idle_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-silence")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True

    row = await db_session.get(VoiceSession, UUID(wake_out["session_id"]))
    assert row is not None
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "text": "hello evie"},
    )
    assert resp.status_code == 428

    resp = await client.get(f"/v1/voice/sessions/{wake_out['session_id']}")
    assert resp.status_code == 200
    status = resp.json()
    assert status["state"] == "ended"
    assert status["end_reason"] == "silence-lock"
