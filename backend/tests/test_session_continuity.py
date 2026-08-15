"""Session continuity: follow-ups stay open without a re-wake.

Drives the shipped voice, runtime, ears, and push-to-talk handlers. The short
follow-up hint is not a door; only a sleep phrase, explicit end, or the long
idle lock close the session.
"""

from __future__ import annotations

import array
import base64
import inspect
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RuntimeSession, VoiceSession
from app.services import runtime as runtime_service
from app.utils.text import utcnow
from app.voice.lifecycle import (
    VoiceRuntime,
    follow_up_hint_expired,
    idle_lock_expired,
    is_sleep_phrase,
)
from app.voice.tts import MetaSynthesizer
from tests.test_ears_api import _AlwaysOwner, _TranscriptWake
from tests.test_runtime import _verified_runtime_session
from tests.test_runtime import enroll_owner as enroll_runtime_owner
from tests.test_runtime import grant_voice_consent as grant_runtime_consent
from tests.test_voice_lifecycle import (
    _verified_session,
    enroll_owner,
    grant_voice_consent,
    verify,
    wake,
    wav_bytes,
)
from tests.test_voice_lifecycle import b64 as voice_b64


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current = None
    for line in body.splitlines():
        if line.startswith("event: "):
            current = line[7:].strip()
        elif line.startswith("data: ") and current is not None:
            payload = json.loads(line[6:])
            events.append((current, payload))
            current = None
    return events


def _sse_reply(body: str) -> dict:
    replies = [payload for name, payload in _sse_events(body) if name == "reply"]
    assert replies, body
    return replies[-1]


async def _status(client: AsyncClient, session_id: str) -> dict:
    resp = await client.get(f"/v1/voice/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_timer_helpers_keep_hint_and_idle_lock_separate() -> None:
    now = utcnow()
    assert follow_up_hint_expired(None, now) is True
    assert follow_up_hint_expired(now - timedelta(seconds=1), now) is True
    assert follow_up_hint_expired(now + timedelta(seconds=30), now) is False
    assert idle_lock_expired(None, now) is False
    assert idle_lock_expired(now - timedelta(seconds=1), now) is True
    assert idle_lock_expired(now + timedelta(seconds=900), now) is False
    assert is_sleep_phrase("that's all")
    assert not is_sleep_phrase("set a timer")


def test_shipped_ptt_and_validation_reuse_open_session() -> None:
    begin = inspect.getsource(VoiceRuntime._begin_push_to_talk_session)
    validate = inspect.getsource(VoiceRuntime._validate_utterance_row)
    assert "_reuse_push_to_talk_session" in begin
    assert "_latest_session" in begin
    assert "superseded by push-to-talk" not in begin
    assert "follow_up_expired" not in validate
    assert "push_to_talk" in validate
    app_model = (
        Path(__file__).resolve().parents[2] / "macos" / "Sources" / "EV" / "AppModel.swift"
    ).read_text(encoding="utf-8")
    assert "if let existing = sessionId" in app_model
    assert "openTalkSession" in app_model
    assert "isDeadVoiceSession" in app_model


async def test_voice_followup_reuses_thread_without_rewake(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-cont-voice")

    first = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "What's next on my calendar?"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["state"] in {"awake", "follow_up"}
    assert first_body["reply"]
    assert first_body["conversation_id"]
    status = await _status(client, session["session_id"])
    assert status["state"] in {"awake", "follow_up"}
    assert status["ended_at"] is None

    second = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "Add milk to that",
        },
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["session_id"] == session["session_id"]
    assert second_body["conversation_id"] == first_body["conversation_id"]
    assert second_body["reply"]
    assert second_body["state"] in {"awake", "follow_up"}

    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    assert row.ended_at is None
    assert row.state in {"awake", "follow_up"}


async def test_voice_hint_expiry_still_accepts_owner_turn(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-cont-hint")
    first = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "Set a timer"},
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    row.follow_up_until = utcnow() - timedelta(seconds=5)
    assert row.expires_at is not None
    assert not idle_lock_expired(row.expires_at)
    await db_session.commit()

    third = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "Make it ten minutes"},
    )
    assert third.status_code == 200, third.text
    assert third.headers.get("x-error-code") not in {"follow_up_expired", "session_ended"}
    body = third.json()
    assert body["session_id"] == session["session_id"]
    assert body["conversation_id"] == conversation_id
    assert body["reply"]
    assert body["state"] in {"awake", "follow_up"}
    status = await _status(client, session["session_id"])
    assert status["ended_at"] is None
    assert status["state"] in {"awake", "follow_up"}


async def test_voice_stream_two_turns_stay_open(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-cont-stream")

    first = await client.post(
        "/v1/voice/utterance/stream",
        json={"session_id": session["session_id"], "text": "Remind me to stretch"},
    )
    assert first.status_code == 200, first.text
    first_reply = _sse_reply(first.text)
    assert first_reply["state"] in {"awake", "follow_up"}
    assert first_reply["reply"]
    conversation_id = first_reply["conversation_id"]

    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    assert row.ended_at is None
    assert row.follow_up_until is not None
    assert not follow_up_hint_expired(row.follow_up_until)

    second = await client.post(
        "/v1/voice/utterance/stream",
        json={"session_id": session["session_id"], "text": "Also hydrate"},
    )
    assert second.status_code == 200, second.text
    second_reply = _sse_reply(second.text)
    assert second_reply["session_id"] == session["session_id"]
    assert second_reply["conversation_id"] == conversation_id
    assert second_reply["state"] in {"awake", "follow_up"}
    assert second_reply["reply"]


async def test_ptt_second_press_reuses_session_and_thread(client: AsyncClient) -> None:
    await grant_voice_consent(client)
    first_wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-cont-ptt", "push_to_talk": True},
    )
    assert first_wake.status_code == 201, first_wake.text
    session_id = first_wake.json()["session_id"]
    first = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "What's the weather",
            "push_to_talk": True,
        },
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    second_wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-cont-ptt", "push_to_talk": True},
    )
    assert second_wake.status_code == 201, second_wake.text
    assert second_wake.json()["session_id"] == session_id
    assert second_wake.json()["state"] in {"awake", "follow_up"}

    second = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "And tomorrow",
            "push_to_talk": True,
        },
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["session_id"] == session_id
    assert body["conversation_id"] == conversation_id
    assert body["reply"]
    assert body["state"] in {"awake", "follow_up"}


async def test_ptt_revives_ended_session_instead_of_wake_again(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Talk must not get stuck on a stale ENDED session id.

    The Mac client reuses ``sessionId`` across presses. Sleep / idle-lock
    used to 428 every later Talk press with "wake EVIE again".
    """

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-revive", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    session_id = wake.json()["session_id"]
    first = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "stop listening",
            "push_to_talk": True,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "ended"

    revived = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "what's next on my calendar",
            "push_to_talk": True,
        },
    )
    assert revived.status_code == 200, revived.text
    body = revived.json()
    assert body["session_id"] == session_id
    assert body["state"] in {"awake", "follow_up"}
    assert body["reply"]
    status = await _status(client, session_id)
    assert status["state"] in {"awake", "follow_up"}

    stream = await client.post(
        "/v1/voice/sessions/" + session_id + "/end",
    )
    assert stream.status_code == 200
    streamed = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": session_id,
            "text": "what time is it",
            "push_to_talk": True,
        },
    )
    assert streamed.status_code == 200, streamed.text
    events = _sse_events(streamed.text)
    kinds = [name for name, _ in events]
    assert "error" not in kinds or all(
        payload.get("code") != "session_ended"
        for name, payload in events
        if name == "error"
    )
    assert "reply" in kinds


async def test_stream_revives_ended_ptt_session_even_without_flag(
    client: AsyncClient,
) -> None:
    """EV.app may reuse a silence-locked Talk session id.

    The stream must revive it even if push_to_talk is omitted — the session
    was opened by Talk (verifier_name=push_to_talk).
    """

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-ended-stream", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    session_id = wake.json()["session_id"]
    ended = await client.post(f"/v1/voice/sessions/{session_id}/end")
    assert ended.status_code == 200, ended.text
    assert ended.json()["state"] == "ended"

    streamed = await client.post(
        "/v1/voice/utterance/stream",
        json={"session_id": session_id, "text": "Evie can you hear me?"},
    )
    assert streamed.status_code == 200, streamed.text
    events = _sse_events(streamed.text)
    assert all(
        payload.get("code") != "session_ended"
        for name, payload in events
        if name == "error"
    )
    assert any(name == "reply" for name, _ in events)
    reply = next(data for name, data in events if name == "reply")
    assert reply.get("state") in {"awake", "follow_up"}
    assert reply.get("reply")


async def test_ptt_revives_idle_locked_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Idle lock used to 428 Talk forever while the Mac still held the id."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-idle-revive", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    session_id = wake.json()["session_id"]
    first = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "what's next",
            "push_to_talk": True,
        },
    )
    assert first.status_code == 200, first.text

    row = await db_session.get(VoiceSession, UUID(session_id))
    assert row is not None
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    revived = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "text": "and after that",
            "push_to_talk": True,
        },
    )
    assert revived.status_code == 200, revived.text
    assert revived.headers.get("x-error-code") != "session_ended"
    body = revived.json()
    assert body["session_id"] == session_id
    assert body["state"] in {"awake", "follow_up"}
    assert body["reply"]

    ended = await client.post(f"/v1/voice/sessions/{session_id}/end")
    assert ended.status_code == 200
    wake_again = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-ptt-idle-revive", "push_to_talk": True},
    )
    assert wake_again.status_code == 201, wake_again.text
    assert wake_again.json()["session_id"] == session_id
    assert wake_again.json()["state"] == "awake"


async def test_ears_followup_and_hint_expiry_reuse_session(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
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
            "device_id": "mac-cont-ears",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
        },
    )
    assert first.status_code == 200, first.text
    wake_body = first.json()
    session_id = wake_body["session_id"]
    assert wake_body["listening"] is True

    second = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-cont-ears",
            "consent": True,
            "text_hint": "remind me to call mom",
        },
    )
    assert second.status_code == 200, second.text
    follow = second.json()
    assert follow["session_id"] == session_id
    assert follow["listening"] is True
    assert follow["state"] in {"awake", "follow_up"}
    assert follow["reply"]
    assert follow["reply"] != "Yes?"

    row = await db_session.get(VoiceSession, UUID(session_id))
    assert row is not None
    row.follow_up_until = utcnow() - timedelta(seconds=5)
    await db_session.commit()

    third = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-cont-ears",
            "consent": True,
            "text_hint": "make it tomorrow morning",
        },
    )
    assert third.status_code == 200, third.text
    later = third.json()
    assert later["accepted"] is True
    assert later["listening"] is True
    assert later["session_id"] == session_id
    assert later["state"] in {"awake", "follow_up"}
    assert later["reply"]
    assert later["reply"] != "Yes?"


async def test_runtime_unflagged_followup_and_hint_expiry(client: AsyncClient, db_session) -> None:
    await grant_runtime_consent(client)
    await enroll_runtime_owner(client)
    outcome = await _verified_runtime_session(client, "mac-cont-runtime")

    first = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "Remind me to call mom"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["state"] in {"awake", "follow_up"}
    assert first_body["reply"]
    conversation_id = first_body["conversation_id"]

    second = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "Also buy milk"},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["state"] in {"awake", "follow_up"}
    assert second_body["conversation_id"] == conversation_id
    assert second_body["reply"]

    row = (
        await db_session.execute(
            select(RuntimeSession).where(RuntimeSession.id == UUID(outcome["session_id"]))
        )
    ).scalar_one()
    row.updated_at = utcnow() - timedelta(seconds=settings.runtime_followup_timeout_seconds + 5)
    await db_session.commit()

    third = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "Change that to Tuesday"},
    )
    assert third.status_code == 200, third.text
    assert third.headers.get("x-error-code") not in {"follow_up_expired", "session_ended"}
    later = third.json()
    assert later["state"] in {"awake", "follow_up"}
    assert later["conversation_id"] == conversation_id
    assert later["reply"]


async def test_sleep_explicit_end_and_idle_lock_still_close(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-cont-sleep")
    resp = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "stop listening"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "ended"
    status = await _status(client, session["session_id"])
    assert status["state"] == "ended"
    assert status["end_reason"] == "sleep phrase"
    denied = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "one more thing"},
    )
    assert denied.status_code == 428
    assert denied.headers.get("x-error-code") == "session_ended"

    other = await _verified_session(client, "mac-cont-end")
    ended = await client.post(f"/v1/voice/sessions/{other['session_id']}/end")
    assert ended.status_code == 200
    assert ended.json()["state"] == "ended"
    denied = await client.post(
        "/v1/voice/utterance",
        json={"session_id": other["session_id"], "text": "hello"},
    )
    assert denied.status_code == 428

    idle = await _verified_session(client, "mac-cont-idle")
    await client.post(
        "/v1/voice/utterance",
        json={"session_id": idle["session_id"], "text": "Set a reminder"},
    )
    row = await db_session.get(VoiceSession, UUID(idle["session_id"]))
    assert row is not None
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    locked = await client.post(
        "/v1/voice/utterance",
        json={"session_id": idle["session_id"], "text": "still there?"},
    )
    assert locked.status_code == 428
    status = await _status(client, idle["session_id"])
    assert status["state"] == "ended"
    assert status["end_reason"] == "silence-lock"


async def test_ambient_and_non_owner_do_not_steal_session(
    client: AsyncClient, monkeypatch
) -> None:
    from app.voice.contracts import Transcript
    from app.voice.lifecycle import VoiceRuntime
    from app.voice.speaker import default_speaker_verifier
    from app.voice.tts import MetaSynthesizer

    owner_wav = wav_bytes(array.array("h", [3000, -3000] * 2000).tobytes())
    other_wav = wav_bytes(array.array("h", [5000, -5000] * 2000).tobytes())
    silence_wav = wav_bytes(b"\x00\x00" * 8000)

    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/enroll",
        json={
            "samples": [
                {"audio_b64": voice_b64(owner_wav), "liveness_proof": "live"}
                for _ in range(5)
            ],
            "reason": "continuity addressivity",
        },
    )
    assert resp.status_code == 201, resp.text
    wake_out = await wake(client, "mac-cont-ambient")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[voice_b64(owner_wav)],
    )
    assert verify_out["verified"] is True
    first = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "text": "hello evie"},
    )
    assert first.status_code == 200, first.text

    class _Spy:
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

    spy = _Spy()

    def make_runtime(db):
        return VoiceRuntime(
            db,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=spy,
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)

    ignored = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "audio_b64": voice_b64(other_wav)},
    )
    assert ignored.status_code == 403
    assert ignored.headers.get("x-error-code") == "voice_ignored"
    silent = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "audio_b64": voice_b64(silence_wav)},
    )
    assert silent.status_code == 403
    assert silent.headers.get("x-error-code") == "voice_ignored"
    assert spy.calls == []
    status = await _status(client, wake_out["session_id"])
    assert status["state"] in {"awake", "follow_up"}
    assert status["ended_at"] is None


async def test_runtime_sleep_and_long_idle_still_close(client: AsyncClient, db_session) -> None:
    await grant_runtime_consent(client)
    await enroll_runtime_owner(client)
    outcome = await _verified_runtime_session(client, "mac-cont-runtime-sleep")
    await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "hello there"},
    )
    slept = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "go to sleep"},
    )
    assert slept.status_code == 200, slept.text
    assert slept.json()["state"] == "idle"
    assert slept.json()["reply"] == "Goodnight."
    denied = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "one more thing"},
    )
    assert denied.status_code in {409, 428}

    other = await _verified_runtime_session(client, "mac-cont-runtime-end")
    await client.post(
        "/v1/runtime/utterance",
        json={"session_id": other["session_id"], "text": "set a timer"},
    )
    ended = await client.post("/v1/runtime/transition", json={"to_state": "idle"})
    assert ended.status_code == 200
    assert ended.json()["state"] == "idle"
    denied = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": other["session_id"], "text": "still there"},
    )
    assert denied.status_code in {409, 428}

    idle = await _verified_runtime_session(client, "mac-cont-runtime-idle")
    await client.post(
        "/v1/runtime/utterance",
        json={"session_id": idle["session_id"], "text": "what's next"},
    )
    row = (
        await db_session.execute(
            select(RuntimeSession).where(RuntimeSession.id == UUID(idle["session_id"]))
        )
    ).scalar_one()
    row.updated_at = utcnow() - timedelta(seconds=settings.runtime_session_timeout_seconds + 5)
    await db_session.commit()
    locked = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": idle["session_id"], "text": "you there"},
    )
    assert locked.status_code == 428
    assert locked.headers.get("x-error-code") == "session_ended"
    assert runtime_service.listening_idle_expired(row.updated_at, utcnow())


async def test_open_live_creates_awake_session_without_wake_word(
    client: AsyncClient,
) -> None:
    """Opening EV.app is the door — no Evie gate."""

    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/live/open",
        json={"device_id": "mac-live-open"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awake"
    assert body["live"] is True
    assert body["session_id"]
    assert body["conversation_id"]
    again = await client.post(
        "/v1/voice/live/open",
        json={"device_id": "mac-live-open"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["session_id"] == body["session_id"]


async def test_bind_live_revives_ended_app_open_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A dropped live socket must reopen the same owner-authenticated session."""

    from app.auth import ActorContext
    from app.voice.live.transport import bind_live_session

    await grant_voice_consent(client)
    opened = await client.post(
        "/v1/voice/live/open",
        json={"device_id": "mac-live-bind"},
    )
    assert opened.status_code == 201, opened.text
    session_id = UUID(opened.json()["session_id"])
    row = await db_session.get(VoiceSession, session_id)
    assert row is not None
    row.state = "ended"
    row.ended_at = utcnow()
    row.end_reason = "sleep-phrase"
    await db_session.commit()

    live = await bind_live_session(
        session_id=session_id,
        ctx=ActorContext(actor="master", is_master=True),
    )
    assert live.session_id == str(session_id)
    revived = await db_session.get(VoiceSession, session_id)
    await db_session.refresh(revived)
    assert revived is not None
    assert revived.ended_at is None
    assert revived.state == "awake"
