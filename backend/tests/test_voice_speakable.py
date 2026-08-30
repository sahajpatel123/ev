"""Owner-facing speech, one playback owner, always-on liveness, one wake winner."""

from __future__ import annotations

import array
import base64
import inspect
import math
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import VoiceSession
from app.voice.anti_spoof import follow_up_liveness
from app.voice.contracts import SpeechStyle, SynthesisResult
from app.voice.lifecycle import VoiceRuntime
from app.voice.pipeline import synthesize_owner_facing
from app.voice.speech import (
    decide_playback,
    ears_device_matches_winner,
    ears_should_handle_follow_up,
    is_tool_chatter,
    owner_facing_speech,
    session_playback_owner,
    tts_is_playable,
)
from app.voice.tts import MetaSynthesizer
from tests.test_ears_api import _AlwaysOwner, _TranscriptWake
from tests.test_runtime import heartbeat, register_device
from tests.test_voice_lifecycle import (
    b64,
    enroll_owner,
    grant_voice_consent,
    verify,
    wake,
    wav_bytes,
)


def speech_like_wav(*, seconds: float = 0.6, seed: int = 3) -> bytes:
    rate = 16000
    n = int(seconds * rate)
    samples: list[int] = []
    for index in range(n):
        t = index / rate
        value = (
            0.25 * math.sin(2 * math.pi * 180 * t)
            + 0.20 * math.sin(2 * math.pi * 220 * t)
            + 0.15 * math.sin(2 * math.pi * 340 * t)
            + 0.10 * math.sin(2 * math.pi * 510 * t)
            + 0.08 * ((index * seed * 17) % 17 - 8) / 8.0
        )
        samples.append(int(max(-1.0, min(1.0, value)) * 8000))
    return wav_bytes(array.array("h", samples).tobytes())


class _SpySynthesizer:
    name = "spy"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        self.texts.append(text)
        return SynthesisResult(
            text=text,
            provider=self.name,
            style=style,
            audio=b"RIFF" + b"\x00" * 16,
            content_type="audio/wav",
        )


def test_owner_facing_speech_strips_tool_chatter() -> None:
    clean = "I'll remind you tomorrow morning."
    assert owner_facing_speech(clean) == clean
    assert not is_tool_chatter(clean)
    envelope = (
        '{"reply": "Rain later this afternoon.", '
        '"tool_calls": [{"name": "get_weather", "arguments": {"q": "Surat"}}]}'
    )
    assert owner_facing_speech(envelope) == "Rain later this afternoon."
    chatter = '{"tool_calls": [{"name": "search_web", "arguments": {"q": "x"}}]}'
    assert is_tool_chatter(chatter)
    assert owner_facing_speech(chatter) is None
    assert owner_facing_speech('Calling tool search_web with {"q": "x"}') is None
    assert owner_facing_speech("Traceback (most recent call last):\n  File") is None
    mixed = 'Calling tool get_weather\nIt is twenty-seven degrees.'
    assert owner_facing_speech(mixed) == "It is twenty-seven degrees."
    assert owner_facing_speech("DHM") is None
    assert owner_facing_speech("dhm") is None
    assert owner_facing_speech("I can hear you.") == "I can hear you."


async def test_synthesize_owner_facing_skips_chatter() -> None:
    spy = _SpySynthesizer()
    spoken = await synthesize_owner_facing(
        spy, "The next event is lunch with Maya.", style=SpeechStyle()
    )
    assert spoken.text == "The next event is lunch with Maya."
    assert spoken.audio
    assert spy.texts == ["The next event is lunch with Maya."]

    spy.texts.clear()
    silent = await synthesize_owner_facing(
        spy,
        '{"tool_calls": [{"name": "search_web", "arguments": {"q": "backend"}}]}',
    )
    assert silent.audio is None
    assert silent.text == ""
    assert silent.details.get("reason") == "not_speakable"
    assert spy.texts == []

    extracted = await synthesize_owner_facing(
        spy,
        '{"reply": "Done.", "tool_calls": [{"name": "set_reminder"}]}',
    )
    assert extracted.text == "Done."
    assert spy.texts == ["Done."]


def test_decide_playback_one_speaker() -> None:
    tts = decide_playback(has_tts_audio=True, owner="ears", surface="ears")
    assert tts.play_tts is True
    assert tts.invoke_say is False
    skip = decide_playback(has_tts_audio=True, already_played=True)
    assert skip.play_tts is False
    assert skip.invoke_say is False
    assert skip.reason == "already_played"
    other = decide_playback(
        has_tts_audio=True, audio_ref="ev://voice/tts/x.wav", owner="client", surface="ears"
    )
    assert other.play_tts is False
    assert other.invoke_say is False
    assert other.reason == "other_surface_owns"
    silent = decide_playback(has_tts_audio=False, audio_ref=None)
    assert silent.play_tts is False
    assert silent.invoke_say is False
    assert session_playback_owner("push_to_talk") == "client"
    assert session_playback_owner("campp") == "ears"
    assert ears_device_matches_winner(
        ears_device_id="mac-ears", winner_name="Mac", winner_type="mac"
    )
    assert not ears_device_matches_winner(
        ears_device_id="mac-ears", winner_name="Phone A", winner_type="phone"
    )
    assert not tts_is_playable(None)
    assert not tts_is_playable({"provider": "meta"})
    assert tts_is_playable({"audio_ref": "ev://voice/tts/x.wav"})
    assert (
        ears_should_handle_follow_up(
            verifier_name="push_to_talk",
            last_utterance_at=__import__("datetime").datetime.now(
                tz=__import__("datetime").UTC
            ),
            now=__import__("datetime").datetime.now(tz=__import__("datetime").UTC),
            busy=False,
        )
        is False
    )


def test_playback_paths_are_single_speaker() -> None:
    app_model = (
        Path(__file__).resolve().parents[2] / "macos" / "Sources" / "EV" / "AppModel.swift"
    ).read_text(encoding="utf-8")
    ears = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "clients"
        / "ears"
        / "main.py"
    ).read_text(encoding="utf-8")
    assert "speakFallback(chunk.text)" not in app_model
    assert "speakFallback(response.reply)" not in app_model
    assert "Never /usr/bin/say" in app_model or "never /usr/bin/say" in app_model.lower()
    assert "decide_playback" in ears
    assert "surface=\"ears\"" in ears or "surface='ears'" in ears
    assert "/usr/bin/say" not in ears
    begin = inspect.getsource(VoiceRuntime.handle_ears_ingest)
    assert "push_to_talk=False" in begin
    assert "ears_should_handle_follow_up" in begin


async def test_always_on_owner_audio_passes_liveness_without_ptt(
    client: AsyncClient, monkeypatch
) -> None:
    owner = speech_like_wav(seed=3)
    other = wav_bytes(array.array("h", [5000, -5000] * 4000).tobytes())
    silence = wav_bytes(b"\x00\x00" * 8000)
    live_ok, score, reason = follow_up_liveness(owner, owner_verified=True, speaker_matched=True)
    assert live_ok is True, reason
    assert score >= 0.5 or reason == "owner-verified follow-up"

    await grant_voice_consent(client)
    resp = await client.post(
        "/v1/voice/enroll",
        json={
            "samples": [
                {"audio_b64": b64(owner), "liveness_proof": "live"} for _ in range(5)
            ],
            "reason": "always-on liveness",
        },
    )
    assert resp.status_code == 201, resp.text
    wake_out = await wake(client, "mac-live-owner")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[b64(owner)],
    )
    assert verify_out["verified"] is True

    ok = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": wake_out["session_id"],
            "audio_b64": b64(owner),
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] in {"awake", "follow_up"}
    assert body["reply"]
    assert "push_to_talk" not in (ok.request.content.decode("utf-8"))

    ignored = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "audio_b64": b64(other)},
    )
    assert ignored.status_code == 403
    assert ignored.headers.get("x-error-code") == "voice_ignored"
    assert not (ignored.json().get("reply") or "")

    quiet = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "audio_b64": b64(silence)},
    )
    assert quiet.status_code == 403
    assert quiet.headers.get("x-error-code") == "voice_ignored"


async def test_ears_always_on_owner_followup_without_ptt(
    client: AsyncClient, monkeypatch
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=_AlwaysOwner(),
            wake_engine=_TranscriptWake("hey evie remind me to call mom"),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.ears._runtime", make_runtime)
    pcm = array.array("h", [3000, -3000] * 4000).tobytes()
    first = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-live",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body["state"] in {"awake", "follow_up"}
    assert body["reply"]
    assert body.get("playback_owner") in {None, "ears"}

    second = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears-live",
            "consent": True,
            "frames_b64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        },
    )
    assert second.status_code == 200, second.text
    follow = second.json()
    assert follow["accepted"] is True
    assert follow["listening"] is True
    assert follow["session_id"] == body["session_id"]
    assert follow["state"] in {"awake", "follow_up"}


async def test_ptt_session_does_not_let_ears_replay(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-talk", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    sid = wake.json()["session_id"]
    uttered = await client.post(
        "/v1/voice/utterance",
        json={"session_id": sid, "text": "what's next", "push_to_talk": True},
    )
    assert uttered.status_code == 200, uttered.text
    row = await db_session.get(VoiceSession, UUID(sid))
    assert row is not None
    assert session_playback_owner(row.verifier_name) == "client"
    assert (
        ears_should_handle_follow_up(
            verifier_name=row.verifier_name,
            last_utterance_at=row.last_utterance_at,
            now=row.last_utterance_at,
            busy=False,
        )
        is False
    )

    ears = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "text_hint": "hey evie what's next",
        },
    )
    assert ears.status_code == 200, ears.text
    body = ears.json()
    assert body["accepted"] is True
    assert body["listening"] is True
    assert body.get("playback_owner") == "client"
    assert not tts_is_playable(body.get("tts"))


async def test_phone_wake_winner_keeps_mac_ears_silent(client: AsyncClient) -> None:
    """After the phone wins fleet wake, /v1/ears/wake must not return playable TTS."""

    await grant_voice_consent(client)
    mac = await client.post(
        "/v1/devices",
        json={"name": "Mac", "capabilities": ["voice", "wake"], "device_type": "mac"},
    )
    phone = await client.post(
        "/v1/devices",
        json={
            "name": "Phone A",
            "capabilities": ["voice", "wake"],
            "device_type": "phone",
        },
    )
    assert mac.status_code == 201, mac.text
    assert phone.status_code == 201, phone.text
    mac_id = str(mac.json()["device"]["id"])
    phone_id = str(phone.json()["device"]["id"])
    await heartbeat(client, mac_id)
    await heartbeat(client, phone_id)
    wake = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": mac_id,
                "signal_score": 0.4,
                "battery_percent": 40.0,
                "text_hint": "evie",
            },
            {
                "device_id": phone_id,
                "signal_score": 0.95,
                "battery_percent": 90.0,
                "text_hint": "evie",
            },
        ],
    )
    assert wake.status_code == 200, wake.text
    outcome = wake.json()
    assert outcome["blocked"] is False
    assert outcome["winner"]["device_id"] == phone_id

    ears = await client.post(
        "/v1/ears/wake",
        json={
            "device_id": "mac-ears",
            "consent": True,
            "text_hint": "hey evie",
        },
    )
    assert ears.status_code == 200, ears.text
    body = ears.json()
    assert body["accepted"] is True
    assert body.get("playback_owner") == "none"
    assert not tts_is_playable(body.get("tts"))


async def test_two_devices_same_wake_one_winner(client: AsyncClient) -> None:
    mac = await register_device(client, "Mac")
    phone = await register_device(client, "Phone A")
    await heartbeat(client, str(mac["id"]))
    await heartbeat(client, str(phone["id"]))
    resp = await client.post(
        "/v1/runtime/wake",
        json=[
            {
                "device_id": str(mac["id"]),
                "signal_score": 0.8,
                "battery_percent": 70.0,
                "text_hint": "evie",
            },
            {
                "device_id": str(phone["id"]),
                "signal_score": 0.8,
                "battery_percent": 70.0,
                "text_hint": "evie",
            },
        ],
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["blocked"] is False
    assert outcome["winner"] is not None
    assert outcome["session_id"]
    selected = [c for c in outcome["candidates"] if c.get("selected")]
    assert len(selected) == 1
    assert selected[0]["device_id"] == outcome["winner"]["device_id"]
    losers = [c for c in outcome["candidates"] if not c.get("selected")]
    assert losers
    assert all(c["device_id"] != outcome["winner"]["device_id"] or not c.get("selected") for c in losers)
