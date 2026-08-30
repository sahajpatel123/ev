"""Always-on /v1/ears/wake ingest."""

from __future__ import annotations

import array
import base64
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import VoiceSession
from app.voice.contracts import SpeakerDecision, WakeDetection
from app.voice.lifecycle import VoiceRuntime
from app.voice.speech import LISTEN_ACKS, choose_listen_ack
from app.voice.tts import MetaSynthesizer
from tests.test_voice_lifecycle import enroll_owner, grant_voice_consent


@pytest.mark.asyncio
async def test_ears_wake_refuses_without_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/ears/wake",
        json={"device_id": "mac-ears", "consent": False, "text_hint": "hey evie"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    assert body["listening"] is False
    assert body["message"] == "consent_not_granted"


@pytest.mark.asyncio
async def test_ears_wake_text_hint_opens_verifying_session(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "text_hint": "hey evie",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["session_id"]
    assert body["state"] == "verifying"
    assert body["listening"] is False


class _AlwaysOwner:
    name = "hash"

    async def enroll(self, samples, *, reason=None):
        return {"embedding": [0.0], "threshold": 0.5, "dim": 1}

    async def verify(self, sample, *, enrolled_payload, threshold=None):
        return SpeakerDecision(
            verified=True,
            confidence=0.99,
            threshold=threshold or 0.5,
            algorithm="hash",
        )


class _TranscriptWake:
    name = "phrase"
    power_state = "burst"

    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    async def detect(self, **kwargs) -> WakeDetection:
        return WakeDetection(
            triggered=True,
            wake_word="evie",
            confidence=0.95,
            details={"engine": "test", "transcript": self.transcript},
        )


@pytest.mark.asyncio
async def test_ears_same_turn_command_returns_spoken_reply(
    client: AsyncClient, monkeypatch
) -> None:
    """'hello EVIE are you here' should wake, chat, and return a reply in one shot."""

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hello evie are you here"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 1600).tobytes()
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["state"] in {"awake", "follow_up"}
    assert body["transcript"]
    assert "are you here" in body["transcript"].lower()
    assert body["reply"]
    ack = choose_listen_ack(body.get("transcript") or "hello evie are you here")
    assert body["reply"].startswith(ack)
    assert body["reply"] != ack
    assert body["tts"] is not None


@pytest.mark.asyncio
async def test_ears_wake_only_acks_yes(client: AsyncClient, monkeypatch) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["reply"] in LISTEN_ACKS
    assert body["reply"] == choose_listen_ack(body.get("transcript") or "evie")


@pytest.mark.asyncio
async def test_ears_can_defer_same_clip_command_after_fast_ack(
    client: AsyncClient, monkeypatch
) -> None:
    """The wake handshake must not wait for the full model turn."""

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie what is next"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-deferred",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "defer_command": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["command_deferred"] is True
    assert body["reply"] == choose_listen_ack(body["transcript"])
    assert body["listening"] is True


class _TriggeredNoTranscript:
    name = "openwakeword"
    power_state = "burst"

    async def detect(self, **kwargs) -> WakeDetection:
        return WakeDetection(
            triggered=True,
            wake_word="evie",
            confidence=0.91,
            details={"engine": "openwakeword"},
        )


class _NearMissOwner:
    name = "campp"

    async def enroll(self, samples, *, reason=None):
        return {"embedding": [0.0], "threshold": 0.72, "dim": 1}

    async def verify(self, sample, *, enrolled_payload, threshold=None):
        return SpeakerDecision(
            verified=False,
            confidence=0.45,
            threshold=threshold or 0.55,
            algorithm="campp",
            reason="voiceprint mismatch",
        )


@pytest.mark.asyncio
async def test_ears_pcm_only_wake_acks_yes_on_near_miss(
    client: AsyncClient, monkeypatch
) -> None:
    """Spoken path: PCM, no text_hint, spotter has no transcript, CAM++ 0.45."""

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_NearMissOwner(),
            wake_engine=_TriggeredNoTranscript(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 1600).tobytes()
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-pcm",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["reply"] in LISTEN_ACKS
    assert body["reply"] == choose_listen_ack(body.get("transcript") or "evie")
    assert body["session_id"]
    assert body["state"] in {"awake", "follow_up"}


@pytest.mark.asyncio
async def test_ears_pcm_wake_accepts_far_field_owner_score(
    client: AsyncClient, monkeypatch
) -> None:
    """Far-field CAM++ ~0.25 used to silent-reject after a real EVIE spot."""

    class _FarFieldOwner:
        name = "campp"

        async def enroll(self, samples, *, reason=None):
            return {"embedding": [0.0], "threshold": 0.72, "dim": 1}

        async def verify(self, sample, *, enrolled_payload, threshold=None):
            return SpeakerDecision(
                verified=False,
                confidence=0.25,
                threshold=threshold or 0.45,
                algorithm="campp",
                reason="voiceprint mismatch",
            )

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_FarFieldOwner(),
            wake_engine=_TriggeredNoTranscript(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 1600).tobytes()
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-far",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["reply"] in LISTEN_ACKS
    assert body["reply"] == choose_listen_ack(body.get("transcript") or "evie")


@pytest.mark.asyncio
async def test_ears_idle_without_evie_is_not_owner_reject(client: AsyncClient) -> None:
    """Ambient speech must not be rejected as 'not the owner' before EVIE is heard."""

    await grant_voice_consent(client)
    await enroll_owner(client)
    resp = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "text_hint": "what's the weather tomorrow",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    assert body["listening"] is False
    assert body["state"] == "idle"
    assert "not the owner" not in (body.get("message") or "").lower()
    assert "wake word" in (body.get("message") or "").lower()


@pytest.mark.asyncio
async def test_ears_one_wake_then_command_without_saying_evie(
    client: AsyncClient, monkeypatch
) -> None:
    """Say EVIE once, then keep talking — Siri-style session."""

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    first = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-siri",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert first.status_code == 200, first.text
    wake_body = first.json()
    assert wake_body["accepted"] is True
    assert wake_body["reply"] in LISTEN_ACKS
    assert wake_body["reply"] == choose_listen_ack(wake_body.get("transcript") or "evie")
    assert wake_body["listening"] is True
    session_id = wake_body["session_id"]

    second = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-siri",
            "consent": True,
            "text_hint": "remind me to call mom",
        },
    )
    assert second.status_code == 200, second.text
    follow = second.json()
    assert follow["accepted"] is True
    assert follow["listening"] is True
    assert follow["session_id"] == session_id
    assert follow["state"] in {"awake", "follow_up"}
    assert "remind me to call mom" in (follow.get("transcript") or "").lower()
    assert follow["reply"]
    assert follow["reply"] not in LISTEN_ACKS

    third = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-siri",
            "consent": True,
            "text_hint": "stop listening",
        },
    )
    assert third.status_code == 200, third.text
    ended = third.json()
    assert ended["accepted"] is True
    assert ended["listening"] is False
    assert ended["state"] == "ended"
    assert ended["reply"] == "Goodnight."


@pytest.mark.asyncio
async def test_ears_ambient_during_session_stays_listening(
    client: AsyncClient, monkeypatch
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("evie"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    first = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-ambient",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert first.json()["listening"] is True

    silence = array.array("h", [0] * 1600).tobytes()
    ambient = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-ambient",
            "consent": True,
            "frames_b64": base64.b64encode(silence).decode("ascii"),
        },
    )
    assert ambient.status_code == 200, ambient.text
    body = ambient.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["reply"] in {None, ""}


@pytest.mark.asyncio
async def test_ears_processing_overlap_stays_listening_without_409(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """A live chat/TTS turn must not 409 the always-on mic."""

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    first = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-busy",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert first.status_code == 200, first.text
    session_id = first.json()["session_id"]
    db_session.expire_all()
    row = await db_session.get(VoiceSession, UUID(session_id))
    assert row is not None
    row.state = "processing"
    await db_session.commit()

    overlap = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-busy",
            "consent": True,
            "text_hint": "what's next",
        },
    )
    assert overlap.status_code == 200, overlap.text
    body = overlap.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["session_id"] == session_id
    # A processing row with no in-flight pipeline is treated as recoverable:
    # the follow-up is heard instead of being accepted-and-dropped.
    assert body["queued"] is False
    assert "what's next" in (body.get("transcript") or "").lower()
    assert body["reply"]
    assert body["reply"] not in LISTEN_ACKS


@pytest.mark.asyncio
async def test_ears_stale_processing_recovers_and_answers(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """A crashed PROCESSING session must not block follow-ups forever."""

    from datetime import timedelta

    from sqlalchemy import update

    from app.utils.text import utcnow

    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 800).tobytes()
    first = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-stale",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert first.status_code == 200, first.text
    session_id = first.json()["session_id"]
    await db_session.execute(
        update(VoiceSession)
        .where(VoiceSession.id == UUID(session_id))
        .values(state="processing", updated_at=utcnow() - timedelta(seconds=90))
    )
    await db_session.commit()

    recovered = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-stale",
            "consent": True,
            "text_hint": "remind me to call mom",
        },
    )
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["session_id"] == session_id
    assert body["state"] in {"awake", "follow_up"}
    assert "remind me to call mom" in (body.get("transcript") or "").lower()
    assert body["reply"]
    assert body["reply"] not in LISTEN_ACKS


def test_command_after_wake_strips_name() -> None:
    runtime = VoiceRuntime.__new__(VoiceRuntime)
    assert runtime._command_after_wake("hello EVIE are you here") == "are you here"
    assert runtime._command_after_wake("hey evie") == ""
    assert runtime._is_wake_only("hello evie")
    assert not runtime._is_wake_only("hello evie are you here")
    # faster-whisper base hears the owner's spoken "EVIE" as "Eve"/"evil";
    # those wake forms must strip like the canonical spellings.
    assert runtime._command_after_wake("Eve what's the weather") == "what's the weather"
    assert runtime._command_after_wake("hey Eve") == ""
    assert runtime._command_after_wake("evil") == ""
    assert runtime._is_wake_only("eve")
    assert runtime._is_wake_only("hey evil")
    assert not runtime._is_wake_only("eve what's the weather")
    assert runtime._is_wake_only("evie here")
    assert runtime._command_after_wake("evie here") == ""
    assert runtime._command_after_wake("EVIE here what's next") == "what's next"
    assert runtime._command_after_wake("hello EVIE are you here") == "are you here"
