"""Hear → transcript → reply: speech is heard, silence is typed, never a mute drop."""

from __future__ import annotations

import base64
import io
import math
import wave
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.audio.capture import MicrophoneDeniedError, MicrophoneUnavailableError
from app.config import settings
from app.voice.asr import (
    _read_audio,
    _wav_pcm,
    classify_hear_failure,
    hear_failure_from_exception,
    hear_status_message,
    normalize_asr_audio,
    wav_is_silent,
)
from app.voice.contracts import Transcript, VoiceError
from app.voice.lifecycle import VoiceRuntime
from app.voice.pipeline import run_chat_tts_pipeline, transcribe_input
from app.voice.speaker import default_speaker_verifier
from app.voice.tts import MetaSynthesizer

REPO = Path(__file__).resolve().parents[2]
WAKE_FIXTURE = REPO / "backend" / "data" / "wake" / "clips" / "evie-001-close.wav"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def wav_at(payload: bytes, *, rate: int = 16000, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(payload)
    return buffer.getvalue()


def sine_pcm(*, seconds: float = 0.4, rate: int = 16000, hz: float = 220.0) -> bytes:
    n = int(seconds * rate)
    samples = []
    for index in range(n):
        value = int(8000 * math.sin(2 * math.pi * hz * index / rate))
        samples.append(max(-32767, min(32767, value)))
    import array

    buf = array.array("h", samples)
    return buf.tobytes()


def silence_wav(*, seconds: float = 0.4, rate: int = 16000) -> bytes:
    return wav_at(b"\x00\x00" * int(seconds * rate), rate=rate)


class EnergySpeechTranscriber:
    """Test engine that uses shipped decode: energy → words, silence → asr_no_speech."""

    name = "energy_speech"

    async def transcribe(self, **kwargs) -> Transcript:
        audio_b64 = kwargs.get("audio_b64")
        audio_ref = kwargs.get("audio_ref")
        raw, _name = await _read_audio(audio_b64, audio_ref)
        if wav_is_silent(raw):
            raise VoiceError(
                hear_status_message("asr_no_speech"),
                status=422,
                code="asr_no_speech",
            )
        return Transcript(
            text="what's the weather",
            confidence=0.91,
            language="en",
            provider=self.name,
        )


async def grant_voice_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/training/consent", json={"track": "voice_enrollment"})
    assert resp.status_code == 201, resp.text


async def ptt_session(client: AsyncClient, device_id: str = "mac-hear") -> str:
    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": device_id, "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    return wake.json()["session_id"]


def test_classify_hear_failure_is_typed_and_never_empty() -> None:
    code, message = classify_hear_failure(no_speech=True)
    assert code == "asr_no_speech"
    assert message
    code, message = classify_hear_failure(undecodable=True)
    assert code == "asr_undecodable_audio"
    assert "clip" in message.lower() or "read" in message.lower()
    code, message = classify_hear_failure(mic_denied=True)
    assert code == "mic_denied"
    assert "microphone" in message.lower()
    denied = hear_failure_from_exception(MicrophoneDeniedError("TCC denied"))
    assert denied[0] == "mic_denied"
    unusable = hear_failure_from_exception(MicrophoneUnavailableError("no input device"))
    assert unusable[0] == "asr_device_unusable"
    assert all(hear_status_message(item) for item in (
        "asr_no_speech",
        "asr_empty_result",
        "asr_degraded",
        "mic_denied",
        "asr_device_unusable",
    ))


def test_normalize_asr_audio_wraps_pcm_and_resamples() -> None:
    pcm = sine_pcm(seconds=0.3)
    wrapped = normalize_asr_audio(pcm)
    assert wrapped.startswith(b"RIFF")
    samples, rate = _wav_pcm(wrapped)
    assert rate == 16000
    assert len(samples) > 100

    high = wav_at(sine_pcm(seconds=0.3, rate=44100), rate=44100)
    normalized = normalize_asr_audio(high)
    samples, rate = _wav_pcm(normalized)
    assert rate == 16000
    assert len(samples) > 1000
    assert not wav_is_silent(normalized)

    quiet = normalize_asr_audio(silence_wav())
    assert wav_is_silent(quiet)

    with pytest.raises(VoiceError) as exc:
        normalize_asr_audio(b"")
    assert exc.value.code == "asr_empty_audio"

    with pytest.raises(VoiceError) as exc:
        normalize_asr_audio(b"not-audio-at-all")
    assert exc.value.code == "asr_undecodable_audio"


@pytest.mark.asyncio
async def test_read_audio_normalizes_44100_before_transcribe() -> None:
    high = wav_at(sine_pcm(seconds=0.25, rate=44100), rate=44100)
    raw, name = await _read_audio(b64(high), None)
    assert name == "voice.wav"
    samples, rate = _wav_pcm(raw)
    assert rate == 16000
    assert len(samples) > 100


@pytest.mark.asyncio
async def test_transcribe_input_text_and_empty_audio() -> None:
    from app.voice.asr import EchoTranscriber

    heard = await transcribe_input(EchoTranscriber(), text="hello evie")
    assert heard.text == "hello evie"
    with pytest.raises(VoiceError) as exc:
        await transcribe_input(EnergySpeechTranscriber(), audio_b64=b64(b""))
    assert exc.value.code == "asr_empty_audio"
    with pytest.raises(VoiceError) as exc:
        await transcribe_input(EnergySpeechTranscriber(), audio_b64=b64(silence_wav()))
    assert exc.value.code == "asr_no_speech"
    spoken = await transcribe_input(
        EnergySpeechTranscriber(),
        audio_b64=b64(wav_at(sine_pcm(seconds=0.4))),
    )
    assert spoken.text.strip()
    assert spoken.text == "what's the weather"


@pytest.mark.asyncio
async def test_asgi_utterance_speech_and_silence(client: AsyncClient, monkeypatch) -> None:
    session_id = await ptt_session(client)

    def make_runtime(session):
        return VoiceRuntime(
            session,
            master_key=settings.master_key,
            verifier=default_speaker_verifier(),
            transcriber=EnergySpeechTranscriber(),
            synthesizer=MetaSynthesizer(),
        )

    monkeypatch.setattr("app.api.voice._runtime", make_runtime)

    speech = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "audio_b64": b64(wav_at(sine_pcm(seconds=0.5))),
            "push_to_talk": True,
        },
    )
    assert speech.status_code == 200, speech.text
    body = speech.json()
    assert (body.get("transcript") or "").strip()
    assert (body.get("reply") or "").strip()

    # 44.1 kHz speech must still be heard after resample, not undecodable.
    resampled = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "audio_b64": b64(wav_at(sine_pcm(seconds=0.4, rate=44100), rate=44100)),
            "push_to_talk": True,
        },
    )
    assert resampled.status_code == 200, resampled.text
    assert (resampled.json().get("transcript") or "").strip()
    assert (resampled.json().get("reply") or "").strip()

    silent = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "audio_b64": b64(silence_wav()),
            "push_to_talk": True,
        },
    )
    assert silent.status_code == 200, silent.text
    silent_body = silent.json()
    assert not (silent_body.get("transcript") or "").strip()
    assert (silent_body.get("reply") or "").strip()
    assert silent_body.get("error")
    blob = (silent_body["reply"] + " " + silent_body["error"]).lower()
    assert "didn't" in blob or "hear" in blob or "catch" in blob

    empty = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session_id,
            "audio_b64": b64(b""),
            "push_to_talk": True,
        },
    )
    assert empty.status_code in {200, 422}, empty.text
    if empty.status_code == 200:
        assert (empty.json().get("reply") or "").strip()
        assert empty.json().get("error")
    else:
        assert empty.headers.get("x-error-code") in {
            "asr_empty_audio",
            "asr_bad_base64",
        }


@pytest.mark.asyncio
async def test_live_feed_speech_and_silence_are_not_mute() -> None:
    from app.voice.live.asr_feed import LiveAsrFeed

    feed = LiveAsrFeed(EnergySpeechTranscriber(), partial_interval_ms=40)
    feed.begin()
    speech = sine_pcm(seconds=0.4)
    for offset in range(0, len(speech), 640):
        feed.feed(speech[offset : offset + 640])
    feed.end_speech()
    import asyncio

    for _ in range(40):
        await asyncio.sleep(0.02)
        text = await feed.final_text(timeout_ms=10)
        if text:
            break
    heard = await feed.final_text(timeout_ms=500)
    assert heard
    assert "weather" in heard

    notices: list[VoiceError] = []

    async def on_unusable(exc: VoiceError) -> None:
        notices.append(exc)

    quiet = LiveAsrFeed(
        EnergySpeechTranscriber(),
        partial_interval_ms=40,
        on_unusable=on_unusable,
    )
    quiet.begin()
    zeros = b"\x00\x00" * 8000
    quiet.feed(zeros)
    quiet.end_speech()
    for _ in range(40):
        await asyncio.sleep(0.02)
    final = await quiet.final_text(timeout_ms=200)
    assert final == ""
    # Transient no-speech is not a permanent unusable mute.
    assert notices == []


def test_client_capture_is_16khz_mono_pcm() -> None:
    mic = (REPO / "macos" / "Sources" / "EV" / "MicCapture.swift").read_text(encoding="utf-8")
    live = (REPO / "ios" / "EVClient" / "Sources" / "EVClient" / "LiveVoice.swift").read_text(
        encoding="utf-8"
    )
    assert "16_000" in mic
    assert "pcm16Wave" in mic
    assert "16_000" in live
    perms = (REPO / "macos" / "Sources" / "EV" / "AppModel.swift").read_text(encoding="utf-8")
    assert "Microphone permission denied" in perms
    assert "Microphone capture failed" in perms
    assert WAKE_FIXTURE.is_file()


@pytest.mark.asyncio
async def test_empty_pipeline_reply_is_recovered(db_session, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.ev.assistant import get_profile

    class _EmptyChat:
        async def __call__(self, *args, **kwargs):
            return {
                "result": SimpleNamespace(text="", model="mock"),
                "context_tokens": 0,
                "memory_deltas": [],
            }

    async def fake_pipeline(*args, **kwargs):
        return {
            "result": SimpleNamespace(text="   ", model="mock"),
            "context_tokens": 0,
            "memory_deltas": [],
        }

    monkeypatch.setattr("app.api.core.run_chat_pipeline", fake_pipeline)
    await get_profile(db_session)
    outcome = await run_chat_tts_pipeline(
        db_session,
        actor="master",
        device_id="mac-hear",
        transcript=Transcript(text="what's the weather", confidence=0.9, provider="test"),
        synthesizer=MetaSynthesizer(),
    )
    assert (outcome.reply or "").strip()
    assert "heard you" in outcome.reply.lower() or outcome.reply.strip()
