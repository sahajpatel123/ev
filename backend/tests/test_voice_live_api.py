"""Hands-free transport: readiness endpoints, the WebSocket, and the responder.

The readiness report and the socket are the only things a client can see before
audio flows, so both are pinned to the offline engines here — otherwise the
assertions would silently change meaning on a machine that has the speech
models installed.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.api import voice_live
from app.api.voice_live import LifecycleResponder, _authenticate, build_loop, hands_free_status
from app.auth import ActorContext
from app.config import settings
from app.main import app
from app.models import Device, VoiceSession
from app.utils.text import sha256_hex
from app.voice.contracts import Transcript, VoiceError
from app.voice.lifecycle import VoiceState
from app.voice.live import LiveConfig, LiveTurn
from app.voice.vosk_engine import (
    DEFAULT_WAKE_PHRASES,
    VoskStreamingRecognizer,
    VoskWakeSpotter,
    WakeSignal,
)

READY_REPORT = {
    "ready": True,
    "blockers": [],
    "wake": {"engine": "vosk", "phrases": ["evie"]},
    "asr": {"provider": "vosk"},
}


@pytest.fixture
def offline_engines(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Resolve every `auto` provider to its offline double, models or not."""

    monkeypatch.setattr(settings, "voice_wake_provider", "phrase")
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    monkeypatch.setattr(settings, "voice_tts_provider", "meta")
    monkeypatch.setattr(settings, "voice_vosk_model_path", str(tmp_path / "no-model"))


# --------------------------------------------------------------------------- #
# Readiness endpoints
# --------------------------------------------------------------------------- #


async def test_live_status_requires_a_bearer_token() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/voice/live/status")
    assert resp.status_code == 401


async def test_live_status_reports_every_engine_and_its_blockers(
    client: AsyncClient, offline_engines: None
) -> None:
    resp = await client.get("/v1/voice/live/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "ready",
        "blockers",
        "wake",
        "asr",
        "tts",
        "wake_and_asr",
        "audio",
        "turn_taking",
        "session",
    }
    assert body["ready"] is False
    assert len(body["blockers"]) == 2
    assert body["wake"] == {
        "engine": "multi-stage",
        "phrases": list(settings.voice_wake_phrases or DEFAULT_WAKE_PHRASES),
        "threshold": settings.voice_wake_vosk_threshold,
        "hears_real_audio": False,
    }
    assert body["asr"] == {
        "provider": "echo",
        "configured": "echo",
        "hears_real_audio": False,
    }
    assert body["tts"]["provider"] == "meta"
    assert body["tts"]["server_audio"] is False
    assert body["wake_and_asr"]["ready"] is False
    assert "no-model" in body["wake_and_asr"]["detail"]
    assert body["audio"] == {
        "sample_rate": 16000,
        "frame_ms": settings.live_frame_ms,
        "encoding": "pcm_s16le_mono",
    }
    assert body["turn_taking"]["follow_up_ms"] == settings.live_follow_up_ms
    assert body["session"] == {
        "verify_speaker": settings.live_verify_speaker,
        "allow_unenrolled": settings.live_allow_unenrolled,
    }


async def test_live_diagnostics_adds_the_owner_enrollment_block(
    client: AsyncClient, offline_engines: None
) -> None:
    resp = await client.get("/v1/voice/live/diagnostics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner"] == {
        "enrolled": False,
        "version": None,
        "algorithm": None,
        "sample_count": 0,
    }
    assert set(body) == set(hands_free_status()) | {"owner"}


async def test_live_diagnostics_requires_a_bearer_token() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/voice/live/diagnostics")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


class RecordingLoop:
    """Stands in for the audio loop so the socket's plumbing is what is tested."""

    def __init__(self) -> None:
        self.fed: list[bytes] = []
        self.playbacks = 0
        self.cancels: list[str] = []
        self.closed: list[str] = []

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def playback_finished(self) -> None:
        self.playbacks += 1

    async def cancel(self, *, reason: str) -> None:
        self.cancels.append(reason)

    async def close(self, *, reason: str) -> None:
        self.closed.append(reason)


def test_websocket_rejects_an_unknown_token() -> None:
    with TestClient(app).websocket_connect("/v1/voice/live") as socket:
        socket.send_json({"token": "not-the-master-key"})
        message = socket.receive_json()
        assert message == {
            "type": "error",
            "data": {"code": "unauthorized", "message": "Invalid bearer token"},
        }
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_websocket_refuses_to_listen_when_the_engines_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never pretend to listen: say what is missing and hang up."""

    monkeypatch.setattr(
        voice_live,
        "hands_free_status",
        lambda: {"ready": False, "blockers": ["vosk model missing"]},
    )

    with TestClient(app).websocket_connect("/v1/voice/live") as socket:
        socket.send_json({"token": settings.master_key})
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_json() == {
            "type": "error",
            "data": {"code": "engines_unavailable", "message": "vosk model missing"},
        }
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_websocket_accepts_the_master_key_and_streams_audio_to_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loops: list[RecordingLoop] = []

    def fake_build_loop(**kwargs) -> RecordingLoop:
        loops.append(RecordingLoop())
        return loops[-1]

    monkeypatch.setattr(voice_live, "hands_free_status", lambda: dict(READY_REPORT))
    monkeypatch.setattr(voice_live, "build_loop", fake_build_loop)

    with TestClient(app).websocket_connect("/v1/voice/live") as socket:
        socket.send_json({"token": settings.master_key, "device_id": "web-1"})
        assert socket.receive_json() == {"type": "ready", "data": READY_REPORT}
        socket.send_bytes(b"\x00\x01" * 320)
        socket.send_json({"type": "playback_finished"})
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong", "data": {}}

    assert loops[0].fed == [b"\x00\x01" * 320]
    assert loops[0].playbacks == 1
    assert loops[0].closed == ["disconnect"]


async def test_authenticate_resolves_tokens_without_fastapi(
    db_session: AsyncSession,
) -> None:
    assert await _authenticate("") is None
    assert await _authenticate("wrong") is None

    master = await _authenticate(settings.master_key)
    assert master is not None and master.is_master is True

    device = Device(id=uuid4(), name="kitchen", token_hash=sha256_hex("device-token"))
    db_session.add(device)
    await db_session.commit()

    resolved = await _authenticate("device-token")
    assert resolved is not None
    assert resolved.actor == "device:kitchen"
    assert resolved.device_id == device.id
    assert resolved.is_master is False


def test_build_loop_wires_the_real_engines() -> None:
    config = LiveConfig(sample_rate=16000, frame_ms=20)
    loop = build_loop(responder=object(), emit=None, device_id="mac-1", config=config)

    assert isinstance(loop.spotter, VoskWakeSpotter)
    assert isinstance(loop.recognizer_factory(), VoskStreamingRecognizer)
    assert loop.device_id == "mac-1"
    assert loop.config is config


# --------------------------------------------------------------------------- #
# LifecycleResponder
# --------------------------------------------------------------------------- #


def live_turn(text: str, *, follow_up: bool = False) -> LiveTurn:
    return LiveTurn(
        transcript=Transcript(text=text, confidence=0.9, provider="vosk"),
        wav=b"RIFF-test-audio",
        follow_up=follow_up,
        wake=WakeSignal(kind="confirmed", phrase="hey evie", confidence=0.93),
    )


def responder(device_id: str = "mac-1") -> LifecycleResponder:
    return LifecycleResponder(
        device_id=device_id,
        ctx=ActorContext(actor="master", is_master=True),
        actor="voice",
    )


async def test_responder_opens_an_unverified_session_when_nobody_is_enrolled(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "live_allow_unenrolled", True)

    info = await responder().open_session(wake=live_turn("hi").wake, wav=b"RIFF-wake")

    assert info["state"] == VoiceState.AWAKE
    assert info["owner_enrolled"] is False
    row = await db_session.get(VoiceSession, UUID(info["session_id"]))
    assert row is not None
    assert row.owner_verified is True
    assert row.state == VoiceState.AWAKE
    assert row.device_id == "mac-1"


async def test_responder_refuses_an_unenrolled_session_when_policy_forbids_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_allow_unenrolled", False)

    with pytest.raises(VoiceError) as excinfo:
        await responder().open_session(wake=live_turn("hi").wake, wav=b"RIFF-wake")

    assert excinfo.value.code == "hands_free_refused"
    assert "voiceprint" in excinfo.value.message


async def test_responder_answers_a_turn_and_opens_the_follow_up_window(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "live_allow_unenrolled", True)
    monkeypatch.setattr(settings, "voice_tts_provider", "meta")
    live = responder()

    await live.open_session(wake=live_turn("hi").wake, wav=b"RIFF-wake")
    reply = await live.respond(live_turn("what did i decide about the project"))

    assert reply.text
    assert reply.session_id == str(live.session_id)
    assert reply.provider == "meta"
    row = await db_session.get(VoiceSession, live.session_id)
    assert row is not None
    assert row.state == VoiceState.FOLLOW_UP
    assert row.follow_up_until is not None

    await live.close(reason="follow_up_timeout")
    await db_session.refresh(row)
    assert row.state == VoiceState.ENDED
    assert row.end_reason == "follow_up_timeout"
    assert live.session_id is None


async def test_responder_without_a_session_refuses_to_answer() -> None:
    with pytest.raises(VoiceError) as excinfo:
        await responder().respond(live_turn("what time is it"))

    assert excinfo.value.code == "no_session"
