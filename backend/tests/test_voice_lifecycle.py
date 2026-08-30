"""EVIE voice lifecycle: wake → verify → listen → act → reply → follow-up → idle."""

from __future__ import annotations

import array
import asyncio
import base64
import hashlib
import io
import wave
from datetime import timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import VoiceSession
from app.utils.text import utcnow
from app.voice.anti_spoof import ReplayError, ReplayGuard
from app.voice.asr import clip_wav_to_max_seconds
from app.voice.contracts import SynthesisResult, Transcript, VoiceError
from app.voice.lifecycle import VoiceRuntime, VoiceState
from app.voice.speaker import default_speaker_verifier
from app.voice.tts import MetaSynthesizer

SAMPLE_A = b"owner-voice-sample-" * 40
SAMPLE_B = b"other-speaker-sample-" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def wav_bytes(payload: bytes) -> bytes:
    """Wrap raw bytes as 16 kHz mono 16-bit PCM WAV (for VAD/verifier tests)."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(payload)
    return buffer.getvalue()


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
    assert 0 < status["follow_up_remaining_seconds"] <= settings.voice_follow_up_seconds

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
    """Short follow-up hint expiry must not close the session or force a re-wake."""

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
    first = resp.json()
    assert first["state"] in {"awake", "follow_up"}
    assert first.get("ended_at") is None

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
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] in {"awake", "follow_up"}
    assert body["reply"]
    assert body["session_id"] == wake_out["session_id"]
    assert body["conversation_id"] == first["conversation_id"]
    assert resp.headers.get("x-error-code") not in {
        "follow_up_expired",
        "session_ended",
    }


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


class _DegradedTranscriber:
    name = "parakeet-eou-120m"

    async def transcribe(self, **kwargs) -> Transcript:
        return Transcript(
            text="",
            confidence=0.0,
            provider=self.name,
            degraded=True,
        )


async def test_degraded_asr_fails_closed_via_api(
    client: AsyncClient, monkeypatch
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-degraded")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=_DegradedTranscriber(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(b"real-speech.wav"),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 503
    assert resp.headers.get("x-error-code") == "asr_degraded"
    assert "degraded" in resp.json()["detail"].lower()


async def test_barge_in_stops_follow_up_and_reenters_listening(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-barge")
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
        json={"session_id": wake_out["session_id"], "text": "Play some music"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "follow_up"

    resp = await client.post(f"/v1/voice/sessions/{wake_out['session_id']}/barge_in")
    assert resp.status_code == 200, resp.text
    status = resp.json()
    assert status["state"] == "awake"
    assert status["follow_up_remaining_seconds"] == 0

    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "text": "Actually stop"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "follow_up"


async def test_streaming_utterance_emits_transcript_and_reply_sse(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-stream")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True

    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={"session_id": wake_out["session_id"], "text": "Remind me to stretch"},
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: final_transcript" in body
    assert "event: reply" in body
    assert "event: done" in body
    assert "Remind me to stretch" in body


async def test_streaming_utterance_speaks_filler_before_reply(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Talk stream must say Checking/Searching before the model finishes."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-filler", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    class _PlayableStreamSynth:
        name = "wav_like"
        streamable_output = True

        async def synthesize(self, text: str, *, style) -> SynthesisResult:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                audio=wav_bytes(b"\x00\x01" * 80),
                content_type="audio/wav",
            )

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_PlayableStreamSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": "search the web for the weather",
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: tts_chunk" in body
    assert "Searching." in body
    chunk_at = body.find("event: tts_chunk")
    reply_at = body.find("event: reply")
    assert chunk_at != -1 and reply_at != -1
    assert chunk_at < reply_at


def _sse_events(body: str) -> list[tuple[str, dict]]:
    import json

    events: list[tuple[str, dict]] = []
    name = ""
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "" and name:
            payload = "\n".join(data_lines)
            parsed: dict = {}
            if payload:
                try:
                    loaded = json.loads(payload)
                    parsed = loaded if isinstance(loaded, dict) else {}
                except json.JSONDecodeError:
                    parsed = {}
            events.append((name, parsed))
            name = ""
            data_lines = []
    return events


def _first_tts_text(body: str) -> str:
    for name, data in _sse_events(body):
        if name == "tts_chunk":
            return str(data.get("text") or "")
    return ""


async def test_streaming_evie_command_starts_with_listen_ack(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Evie + command: first chunk is Hmm/Yes, then the answer still arrives."""

    from app.voice.speech import LISTEN_ACKS, choose_listen_ack

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-listen-ack", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_NonStreamableSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie what's next on my calendar"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    ack = choose_listen_ack(spoken)
    first = next(data for name, data in _sse_events(body) if name == "tts_chunk")
    assert first.get("text") == ack
    assert first.get("audio_b64")
    assert first.get("content_type")
    assert ack in LISTEN_ACKS
    assert "event: reply" in body
    chunk_at = body.find("event: tts_chunk")
    reply_at = body.find("event: reply")
    assert chunk_at < reply_at
    assert "calendar" in body.lower() or "what's next" in body.lower()
    reply_event = next(data for name, data in _sse_events(body) if name == "reply")
    assert reply_event.get("reply")
    assert reply_event["reply"] != ack


async def test_streaming_evie_wake_only_starts_with_listen_ack(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Wake-only Evie: first spoken chunk is the listen-ack, not Searching."""

    from app.voice.speech import LISTEN_ACKS, choose_listen_ack

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-listen-ack-wake", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_NonStreamableSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "hey evie"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    ack = choose_listen_ack(spoken)
    assert ack in LISTEN_ACKS
    assert _first_tts_text(body) == ack
    assert "Searching." not in _first_tts_text(body)
    assert "Checking." not in _first_tts_text(body)
    assert "On it." not in _first_tts_text(body)


class _NonStreamableSynth:
    """Edge-like engine: MP3, cannot concat sentence WAV chunks."""

    name = "edge_like"
    streamable_output = False

    async def synthesize(self, text: str, *, style) -> SynthesisResult:
        return SynthesisResult(
            text=text,
            provider=self.name,
            style=style,
            audio=b"ID3fake-mp3",
            content_type="audio/mpeg",
        )


async def test_streaming_evie_listen_ack_when_tts_not_streamable(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Live Talk uses Edge MP3 — Evie-start must still speak the listen-ack first."""

    from app.voice.speech import choose_listen_ack

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-listen-ack-mp3", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_NonStreamableSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie what's next on my calendar"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    ack = choose_listen_ack(spoken)
    chunks = [data for name, data in _sse_events(body) if name == "tts_chunk"]
    assert chunks
    assert chunks[0].get("text") == ack
    assert chunks[0].get("index") == 0
    assert chunks[0].get("content_type")
    audio_b64 = chunks[0].get("audio_b64") or ""
    assert audio_b64
    assert len(audio_b64) > 0
    reply_event = next(data for name, data in _sse_events(body) if name == "reply")
    assert reply_event.get("reply")
    assert reply_event["reply"] != ack
    assert "calendar" in body.lower() or "what's next" in body.lower()


class _SlowEdgeSynth:
    """Edge-like: slower than the old 2s listen-ack cut, but within the real budget."""

    name = "edge_like"
    streamable_output = False

    async def synthesize(self, text: str, *, style) -> SynthesisResult:
        await asyncio.sleep(2.3)
        return SynthesisResult(
            text=text,
            provider=self.name,
            style=style,
            audio=b"ID3slow-mp3-bytes",
            content_type="audio/mpeg",
        )


async def test_streaming_evie_listen_ack_waits_for_slow_edge(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """A 2s wait_for used to emit a silent first chunk before Edge finished."""

    from app.voice.speech import choose_listen_ack

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-listen-ack-slow", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_SlowEdgeSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie what's next on my calendar"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    chunks = [data for name, data in _sse_events(resp.text) if name == "tts_chunk"]
    assert chunks
    assert chunks[0].get("text") == choose_listen_ack(spoken)
    assert chunks[0].get("content_type") == "audio/mpeg"
    assert chunks[0].get("audio_b64")
    assert len(chunks[0]["audio_b64"]) > 0
    reply_event = next(data for name, data in _sse_events(resp.text) if name == "reply")
    assert reply_event.get("reply")
    assert reply_event["reply"] != choose_listen_ack(spoken)


async def test_api_first_asr_tts_round_trip_persists_playable_audio(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """API-first path: audio in -> transcript -> reply -> persisted playable audio."""

    import httpx

    from app.voice.asr import OpenAICompatTranscriber
    from app.voice.tts import OpenAICompatSynthesizer

    monkeypatch.setenv("EV_ALLOW_REMOTE_TTS", "true")
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-api-first")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True

    tts_bytes = b"RIFF" + b"\x00" * 128

    async def asr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"text": "Open the garage", "confidence": 0.97},
        )

    def tts_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=tts_bytes,
            headers={"content-type": "audio/mpeg"},
        )

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=OpenAICompatTranscriber(
                base_url="https://asr.test/v1",
                client=httpx.AsyncClient(transport=httpx.MockTransport(asr_handler)),
            ),
            synthesizer=OpenAICompatSynthesizer(
                base_url="https://tts.test/v1",
                fmt="mp3",
                client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
            ),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(wav_bytes(b"\x10\x00" * 3200)),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcript"] == "Open the garage"
    assert body["transcript_degraded"] is False
    assert body["transcript_provider"] == "openai_compat"
    assert body["reply"]
    tts = body["tts"]
    assert tts["provider"] == "openai_compat"
    assert tts["degraded"] is False
    assert tts["audio_ref"] and tts["audio_ref"].startswith("ev://voice/tts/")
    assert tts.get("audio_b64")
    assert base64.b64decode(tts["audio_b64"]) == tts_bytes

    key = tts["audio_ref"][len("ev://") :]
    audio_resp = await client.get(f"/v1/voice/audio/{key}")
    assert audio_resp.status_code == 200, audio_resp.text
    assert audio_resp.content == tts_bytes
    assert audio_resp.headers["content-type"] == "audio/mpeg"


class _SpyTranscriber:
    """Records whether ASR was reached; returns a real-looking transcript."""

    name = "spy"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def transcribe(self, **kwargs) -> Transcript:
        self.calls.append(kwargs)
        return Transcript(text="hello world", confidence=0.9, provider=self.name)

    def stream(self, **kwargs):
        async def gen():
            yield await self.transcribe(**kwargs)

        return gen()


async def test_follow_up_timer_resets_on_owner_utterance(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-reset")

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Remind me to stretch",
        },
    )
    assert resp.status_code == 200, resp.text

    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    first_until = row.follow_up_until
    assert first_until is not None

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Also remind me to hydrate",
            "follow_up": True,
        },
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(row)
    assert row.follow_up_until is not None
    assert row.follow_up_until > first_until

    status = await client.get(f"/v1/voice/sessions/{session['session_id']}")
    assert status.status_code == 200
    remaining = status.json()["follow_up_remaining_seconds"]
    assert settings.voice_follow_up_seconds - 5 <= remaining <= settings.voice_follow_up_seconds


async def test_no_rewake_required_inside_active_and_follow_up(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-continue")

    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "hello evie"},
    )
    assert resp.status_code == 200, resp.text
    first_conversation_id = resp.json()["conversation_id"]
    assert first_conversation_id is not None

    wake_again = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-continue", "text_hint": "hey evie"},
    )
    assert wake_again.status_code == 201, wake_again.text
    assert wake_again.json()["session_id"] == session["session_id"]
    assert wake_again.json()["state"] == "follow_up"

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "keep going",
            "follow_up": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "follow_up"
    assert resp.json()["conversation_id"] == first_conversation_id


@pytest.mark.parametrize(
    "phrase",
    [
        "that's all",
        "go to sleep",
        "stop listening",
        "goodbye EVIE",
    ],
)
async def test_sleep_phrases_end_session(
    client: AsyncClient,
    db_session: AsyncSession,
    phrase: str,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    device_id = f"mac-sleep-{abs(hash(phrase)) % 100000}"
    session = await _verified_session(client, device_id)

    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": phrase},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "ended"
    assert body["reply"] == "Goodnight."

    status = await client.get(f"/v1/voice/sessions/{session['session_id']}")
    assert status.status_code == 200
    assert status.json()["state"] == "ended"
    assert status.json()["end_reason"] == "sleep phrase"

    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    assert row.state == "ended"
    assert row.end_reason == "sleep phrase"

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "one more thing",
            "follow_up": True,
        },
    )
    assert resp.status_code == 428
    assert resp.headers.get("x-error-code") == "session_ended"


async def test_addressivity_ignores_non_owner_and_silence_before_asr(
    client: AsyncClient,
    monkeypatch,
) -> None:
    owner_wav = wav_bytes(array.array("h", [3000, -3000] * 2000).tobytes())
    other_wav = wav_bytes(array.array("h", [5000, -5000] * 2000).tobytes())
    silence_wav = wav_bytes(b"\x00\x00" * 8000)

    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/enroll",
        json={
            "samples": [
                {"audio_b64": b64(owner_wav), "liveness_proof": "live"}
                for _ in range(5)
            ],
            "reason": "addressivity test",
        },
    )
    assert resp.status_code == 201, resp.text

    wake_out = await wake(client, "mac-addressivity")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(owner_wav)],
    )
    assert verify_out["verified"] is True

    spy = _SpyTranscriber()

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=spy,
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(other_wav),
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "voice_ignored"
    assert spy.calls == []

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(silence_wav),
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-error-code") == "voice_ignored"
    assert spy.calls == []

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(owner_wav),
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 1


async def test_talk_button_raw_pcm_opens_session(client: AsyncClient) -> None:
    """macOS Talk sends raw 16 kHz PCM, not a WAV. That must not 500."""

    await grant_voice_consent(client)
    await enroll_owner(client)
    pcm = (b"\x00\x10" * 2000)
    resp = await client.post(
        "/v1/voice/wake",
        json={
            "device_id": "mac-talk",
            "audio_b64": b64(pcm),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awake"
    assert body["session_id"]
    assert body.get("message", "").lower().find("listening") >= 0

    spoken = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": body["session_id"],
            "text": "what is on my calendar",
            "push_to_talk": True,
        },
    )
    assert spoken.status_code == 200, spoken.text
    assert spoken.json()["reply"]


async def test_push_to_talk_bypasses_addressivity(
    client: AsyncClient,
    monkeypatch,
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-ptt")

    spy = _SpyTranscriber()

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=spy,
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "audio_b64": b64(b"not-a-wav"),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_push_to_talk_wake_skips_addressivity_on_following_utterance(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Menu-bar Talk must not 403 'not the owner' on the spoken turn."""

    await grant_voice_consent(client)
    await enroll_owner(client)
    spy = _SpyTranscriber()

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=spy,
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    wake = await client.post(
        "/v1/voice/wake",
        json={
            "device_id": "mac-ptt-talk",
            "audio_b64": b64(b"ptt-clip"),
            "push_to_talk": True,
        },
    )
    assert wake.status_code == 201, wake.text
    body = wake.json()
    assert body["state"] == "awake"
    assert body["session_id"]

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": body["session_id"],
            "audio_b64": b64(b"not-a-wav"),
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 1


async def test_configurable_follow_up_timeout(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "voice_follow_up_seconds", 180)
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-config-fu")

    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Set a reminder",
        },
    )
    assert resp.status_code == 200, resp.text

    status = await client.get(f"/v1/voice/sessions/{session['session_id']}")
    assert status.status_code == 200
    remaining = status.json()["follow_up_remaining_seconds"]
    assert 0 < remaining <= 180
    assert remaining > 180 - 5


def test_clip_wav_keeps_last_seconds() -> None:
    pcm = b"\x00\x01" * (16000 * 20)
    raw = wav_bytes(pcm)
    clipped = clip_wav_to_max_seconds(raw, max_seconds=5)
    with wave.open(io.BytesIO(clipped), "rb") as handle:
        assert handle.getnframes() == 16000 * 5
        assert handle.getframerate() == 16000
    short = wav_bytes(b"\x00\x01" * 16000)
    assert clip_wav_to_max_seconds(short, max_seconds=5) == short


@pytest.mark.asyncio
async def test_push_to_talk_wake_without_audio_or_enrollment(
    client: AsyncClient,
) -> None:
    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-no-audio", "push_to_talk": True},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awake"
    assert body["session_id"]


@pytest.mark.asyncio
async def test_push_to_talk_wake_supersedes_processing_session(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await grant_voice_consent(client)
    first = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-busy", "push_to_talk": True},
    )
    assert first.status_code == 201, first.text
    sid = first.json()["session_id"]
    row = await db_session.get(VoiceSession, UUID(sid))
    assert row is not None
    row.state = VoiceState.PROCESSING
    await db_session.commit()

    second = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-busy", "push_to_talk": True},
    )
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["state"] == "awake"
    assert body["session_id"] == sid


class _EmptyTranscriber:
    name = "empty"

    async def transcribe(self, **kwargs) -> Transcript:
        raise VoiceError(
            "ASR returned an empty transcript",
            status=502,
            code="asr_empty_result",
        )

    def stream(self, **kwargs):
        async def gen():
            yield await self.transcribe(**kwargs)

        return gen()


@pytest.mark.asyncio
async def test_empty_asr_speaks_retry_instead_of_502(
    client: AsyncClient,
    monkeypatch,
) -> None:
    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-empty", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=_EmptyTranscriber(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake.json()["session_id"],
            "audio_b64": b64(wav_bytes(b"\x00\x01" * 1600)),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "didn't catch that" in resp.json()["reply"].lower()
    assert resp.json().get("error")
    assert "didn't catch that" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_streaming_missing_session_emits_error_not_500(
    client: AsyncClient,
) -> None:
    """Talk stream must stay 200 with an SSE error, not abort the socket."""

    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": "00000000-0000-0000-0000-000000000001",
            "text": "hello",
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: error" in body
    assert "session not found" in body.lower()
    assert "event: done" in body


@pytest.mark.asyncio
async def test_streaming_empty_asr_speaks_retry(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Talk uses /utterance/stream — empty Whisper must speak a retry, not 5xx."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-empty-stream", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=_EmptyTranscriber(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "audio_b64": b64(wav_bytes(b"\x00\x01" * 1600)),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: reply" in body
    assert "didn't catch that" in body.lower()
    assert "event: done" in body


@pytest.mark.asyncio
async def test_streaming_text_only_does_not_require_audio(
    client: AsyncClient,
) -> None:
    """PTT text (tests / typed Talk) must not force Whisper onto missing audio."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-text-only", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": "Evie what is two plus two",
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: final_transcript" in body
    assert "two plus two" in body
    assert "couldn't read that clip" not in body.lower()
    assert "event: reply" in body
    assert "event: done" in body


class _SlowTranscriber:
    name = "slow"

    async def transcribe(self, **kwargs) -> Transcript:
        await asyncio.sleep(1.0)
        return Transcript(text="should not be used", confidence=1.0, provider="slow")


@pytest.mark.asyncio
async def test_asr_timeout_speaks_and_returns_visible_error(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Talk path: ASR timeout is spoken AND returned as error, not a silent 200."""

    monkeypatch.setattr(settings, "voice_asr_timeout_seconds", 0.05)
    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-timeout", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=_SlowTranscriber(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake.json()["session_id"],
            "audio_b64": b64(wav_bytes(b"\x00\x01" * 1600)),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    spoken = body["reply"].lower()
    assert "too long to hear" in spoken
    assert "shorter" in spoken
    visible = (body.get("error") or "").lower()
    assert visible
    assert "too long" in visible or "timeout" in visible


@pytest.mark.asyncio
async def test_ordinary_text_turns_are_not_timeout_fallback(
    client: AsyncClient,
) -> None:
    """Two valid Talk-after-wake turns must not collapse to the timeout speech."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-ok", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    sid = wake.json()["session_id"]
    for text in ("What's next on my calendar?", "Remind me to drink water"):
        resp = await client.post(
            "/v1/voice/utterance",
            json={"session_id": sid, "text": text, "push_to_talk": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["transcript"] == text
        assert body["reply"]
        assert "too long to hear" not in body["reply"].lower()
        assert "shorter question" not in body["reply"].lower()
        assert not body.get("error")


class _GarbageTranscriber:
    name = "garbage"

    async def transcribe(self, **kwargs) -> Transcript:
        return Transcript(text="DHM", confidence=0.1, provider=self.name)

    def stream(self, **kwargs):
        async def gen():
            yield await self.transcribe(**kwargs)

        return gen()


class _PlayableAckSynth:
    name = "wav_like"
    streamable_output = True

    async def synthesize(self, text: str, *, style) -> SynthesisResult:
        return SynthesisResult(
            text=text,
            provider=self.name,
            style=style,
            audio=wav_bytes(b"\x00\x01" * 80),
            content_type="audio/wav",
        )


@pytest.mark.asyncio
async def test_streaming_evie_hear_check_ack_is_first_and_fast(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Evie check-in: first spoken event is a listen-ack, well under 45s."""

    import time

    from app.voice.speech import LISTEN_ACKS, choose_listen_ack

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-hear-fast", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_PlayableAckSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie can you hear me?"
    started = time.monotonic()
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert resp.status_code == 200, resp.text
    first = _first_tts_text(resp.text)
    ack = choose_listen_ack(spoken)
    assert first == ack
    assert first in LISTEN_ACKS
    assert elapsed_ms < 15_000
    reply_event = next(data for name, data in _sse_events(resp.text) if name == "reply")
    assert reply_event.get("reply")
    assert reply_event["reply"] != ack
    assert reply_event["reply"].strip().upper() != "DHM"


@pytest.mark.asyncio
async def test_streaming_evie_hear_check_reply_is_english(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Later reply for a hear-check is readable English, not a leftover token."""

    from app.voice.speech import choose_listen_ack, is_unreadable_transcript

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-hear-reply", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_PlayableAckSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie can you hear me?"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    chunks = [data for name, data in events if name == "tts_chunk"]
    reply_event = next(data for name, data in events if name == "reply")
    reply = (reply_event.get("reply") or "").strip()
    assert reply
    ack = choose_listen_ack(spoken)
    assert reply != ack
    assert not is_unreadable_transcript(reply)
    lowered = reply.lower()
    assert any(
        token in lowered
        for token in ("hear", "heard", "listening", "here", "yes", "present")
    )
    # Talk only plays tts_chunk audio. After the listen-ack, the answer
    # itself must arrive as a later playable chunk or the user hears only Mhm.
    assert len(chunks) >= 2
    assert chunks[0].get("text") == ack
    assert chunks[0].get("audio_b64")
    answer_chunks = [c for c in chunks[1:] if (c.get("text") or "").strip() != ack]
    assert answer_chunks
    spoken_answer = " ".join(c.get("text") or "" for c in answer_chunks)
    assert not is_unreadable_transcript(spoken_answer)
    assert spoken_answer.strip().upper() != "DHM"
    assert any(c.get("audio_b64") for c in answer_chunks)


@pytest.mark.asyncio
async def test_streaming_hear_check_answer_chunk_when_tts_not_streamable(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Live Talk uses Edge MP3: hear-check must play the answer after Mhm."""

    from app.voice.speech import choose_listen_ack, is_unreadable_transcript

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-hear-mp3", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_NonStreamableSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    spoken = "Evie can you hear me?"
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": spoken,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    chunks = [data for name, data in _sse_events(resp.text) if name == "tts_chunk"]
    ack = choose_listen_ack(spoken)
    assert chunks
    assert chunks[0].get("text") == ack
    assert chunks[0].get("audio_b64")
    answers = [c for c in chunks[1:] if (c.get("text") or "").strip() != ack]
    assert answers
    spoken_answer = " ".join(c.get("text") or "" for c in answers)
    assert not is_unreadable_transcript(spoken_answer)
    assert any(c.get("audio_b64") for c in answers)


@pytest.mark.asyncio
async def test_streaming_garbage_transcript_is_not_spoken(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """ASR leftover 'DHM' must not be spoken as the answer."""

    from app.voice.speech import is_unreadable_transcript

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-dhm", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=_GarbageTranscriber(),
            synthesizer=_PlayableAckSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "audio_b64": b64(wav_bytes(b"\x00\x01" * 1600)),
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    reply_event = next(data for name, data in _sse_events(body) if name == "reply")
    reply = (reply_event.get("reply") or "").strip()
    assert reply
    assert reply.upper() != "DHM"
    assert "DHM" not in reply.upper().split()
    assert not is_unreadable_transcript(reply)
    spoken_chunks = [
        str(data.get("text") or "")
        for name, data in _sse_events(body)
        if name == "tts_chunk"
    ]
    assert spoken_chunks
    assert all(chunk.strip().upper() != "DHM" for chunk in spoken_chunks)


@pytest.mark.asyncio
async def test_wake_evie_accepted_non_evie_rejected(
    client: AsyncClient,
) -> None:
    """Shipped wake: Evie-bearing input opens a session; non-Evie does not."""

    await grant_voice_consent(client)
    await enroll_owner(client)
    evie = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-wake-evie", "text_hint": "hey evie"},
    )
    assert evie.status_code == 201, evie.text
    evie_body = evie.json()
    assert evie_body["session_id"]
    assert evie_body["state"] in {"verifying", "awake", "follow_up"}

    other = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-wake-other", "text_hint": "what's the weather tomorrow"},
    )
    assert other.status_code in {200, 201}, other.text
    other_body = other.json()
    assert other_body.get("session_id") in {None, ""}
    assert other_body.get("state") in {"idle", None, ""}
    assert "wake word" in (other_body.get("message") or "").lower()


@pytest.mark.asyncio
async def test_ptt_turn_still_completes_after_hear_path(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Push-to-talk still finishes a turn (no hard Talk/API error)."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-complete", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            synthesizer=_PlayableAckSynth(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": "what's next on my calendar",
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "event: error" not in resp.text or "event: reply" in resp.text
    reply_event = next(data for name, data in _sse_events(resp.text) if name == "reply")
    assert reply_event.get("reply")
    assert reply_event.get("state") in {"follow_up", "awake"}

