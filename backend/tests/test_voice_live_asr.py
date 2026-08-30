"""Live ASR feed: raw PCM → incremental partials → accurate final transcript."""

from __future__ import annotations

import array
import asyncio
import base64
import wave

import pytest

from app.voice.asr import EchoTranscriber
from app.voice.contracts import Transcript, VoiceError
from app.voice.live.asr_feed import (
    LiveAsrFeed,
    LivePcmTranscriber,
    _wav_bytes,
    resolve_live_transcriber,
)

SPEECH = array.array("h", [1200, -900, 800, -700, 600, -500, 400] * 1000).tobytes()


class FakeTranscriber:
    """Deterministic transcriber double that accepts PCM and echoes lengths."""

    name = "fake_live"

    def __init__(self, *, degrade: bool = False, reject_audio: bool = False) -> None:
        self.degrade = degrade
        self.reject_audio = reject_audio
        self.calls: list[bytes] = []

    async def transcribe(self, **kwargs) -> Transcript:
        audio_b64 = kwargs.get("audio_b64")
        if self.reject_audio:
            raise VoiceError("no audio", status=422, code="asr_echo_no_audio")
        payload = base64.b64decode(audio_b64) if audio_b64 else b""
        self.calls.append(payload)
        if self.degrade:
            return Transcript(text="", confidence=0.0, language="en", degraded=True)
        with wave.open(__import__("io").BytesIO(payload), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
        return Transcript(
            text=f"words:{len(frames)//2}",
            confidence=0.9,
            language="en",
            provider=self.name,
        )


def pcm_bytes(n_seconds: float = 1.0) -> bytes:
    chunk = SPEECH
    repeats = max(1, int(n_seconds * 16000 / (len(chunk) // 2)))
    return chunk * repeats


async def _drain(feed: LiveAsrFeed, seconds: float = 2.0) -> None:
    """Let partial/final tasks complete on the test loop."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_feed_emits_partials_while_speaking() -> None:
    partials: list[str] = []
    feed = LiveAsrFeed(
        FakeTranscriber(),
        partial_interval_ms=50,
        on_partial=partials.append,
    )
    feed.begin()
    for _ in range(3):
        feed.feed(pcm_bytes(0.1))
        await asyncio.sleep(0.06)
    await _drain(feed)
    assert partials, "expected at least one partial while speech is active"
    feed.abort()


@pytest.mark.asyncio
async def test_final_text_returns_accurate_transcript() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), partial_interval_ms=50)
    feed.begin()
    feed.feed(pcm_bytes(0.5))
    await _drain(feed, 0.4)
    feed.end_speech()
    await _drain(feed, 0.4)
    text = await feed.final_text(timeout_ms=2000)
    assert text == "words:7000", text


@pytest.mark.asyncio
async def test_final_text_falls_back_to_partial_when_final_slow() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), partial_interval_ms=50)
    feed.begin()
    for _ in range(4):
        feed.feed(pcm_bytes(0.1))
        await asyncio.sleep(0.06)
    await _drain(feed, 0.4)
    # No end_speech() → final never started; final_text returns the partial.
    text = await feed.final_text(timeout_ms=200)
    assert text == "words:28000", text
    feed.abort()


@pytest.mark.asyncio
async def test_degraded_or_rejected_transcripts_are_ignored() -> None:
    feed = LiveAsrFeed(FakeTranscriber(degrade=True), partial_interval_ms=50)
    feed.begin()
    feed.feed(pcm_bytes(0.5))
    await _drain(feed, 0.4)
    feed.end_speech()
    await _drain(feed, 0.4)
    assert await feed.final_text(timeout_ms=500) is None

    rejected = LiveAsrFeed(FakeTranscriber(reject_audio=True), partial_interval_ms=50)
    rejected.begin()
    rejected.feed(pcm_bytes(0.5))
    await _drain(rejected, 0.4)
    rejected.end_speech()
    await _drain(rejected, 0.4)
    assert await rejected.final_text(timeout_ms=500) is None


@pytest.mark.asyncio
async def test_abort_drops_in_flight_work() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), partial_interval_ms=50)
    feed.begin()
    feed.feed(pcm_bytes(0.5))
    await _drain(feed, 0.3)
    feed.end_speech()
    feed.abort()
    assert await feed.final_text(timeout_ms=100) is None
    # A fresh utterance works after abort.
    feed.begin()
    feed.feed(pcm_bytes(0.2))
    await _drain(feed, 0.3)
    feed.end_speech()
    await _drain(feed, 0.3)
    assert await feed.final_text(timeout_ms=2000) == "words:7000"


@pytest.mark.asyncio
async def test_feed_preserves_only_the_most_recent_utterance() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), partial_interval_ms=50)
    feed.begin()
    feed.feed(pcm_bytes(0.3))
    await _drain(feed, 0.3)
    feed.end_speech()
    text_first = await feed.final_text(timeout_ms=2000)
    assert text_first == "words:7000"
    # New utterance: buffer and final are replaced, not appended.
    feed.begin()
    feed.feed(pcm_bytes(0.2))
    await _drain(feed, 0.3)
    feed.end_speech()
    await _drain(feed, 0.3)
    text_second = await feed.final_text(timeout_ms=2000)
    assert text_second == "words:7000", text_second


def test_prefix_padding_is_prepended_on_begin() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), prefix_padding_ms=300)
    feed.note_idle(b"\x01\x00" * 16_000)
    feed.begin()
    # 300 ms of 16 kHz PCM16 = 4800 samples = 9600 bytes.
    assert len(feed._buffer) == 9600
    feed.abort()


def test_feed_bounds_pcm_buffer_in_bytes() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), max_buffer_seconds=1.0)
    feed.begin()
    feed.feed(b"\x01\x00" * 16_000)
    feed.feed(b"\x01\x00" * 16_000)
    assert len(feed._buffer) == 32_000


@pytest.mark.asyncio
async def test_unusable_transcriber_notifies_instead_of_swallowing() -> None:
    notices: list[VoiceError] = []

    async def on_unusable(exc: VoiceError) -> None:
        notices.append(exc)

    feed = LiveAsrFeed(
        FakeTranscriber(reject_audio=True),
        partial_interval_ms=50,
        on_unusable=on_unusable,
    )
    feed.begin()
    feed.feed(pcm_bytes(0.3))
    await _drain(feed, 0.3)
    feed.end_speech()
    await _drain(feed, 0.3)
    assert notices, "echo-style refusal must notify the live session"
    assert notices[0].code == "asr_echo_no_audio"
    assert await feed.final_text(timeout_ms=100) is None


def test_feed_does_not_schedule_partial_on_80ms_of_pcm() -> None:
    feed = LiveAsrFeed(FakeTranscriber(), partial_interval_ms=50)
    feed.begin()
    feed.feed(b"\x01\x00" * 1280)  # 80 ms at 16 kHz
    assert feed._partial_task is None
    feed.feed(b"\x01\x00" * 4800)  # +300 ms
    assert feed._partial_task is not None
    feed.abort()


@pytest.mark.asyncio
async def test_empty_asr_result_is_not_unusable() -> None:
    notices: list[VoiceError] = []

    class _EmptyOnce:
        name = "empty-once"

        async def transcribe(self, **kwargs) -> Transcript:
            raise VoiceError("empty", status=502, code="asr_empty_result")

    async def on_unusable(exc: VoiceError) -> None:
        notices.append(exc)

    feed = LiveAsrFeed(_EmptyOnce(), partial_interval_ms=50, on_unusable=on_unusable)
    feed.begin()
    feed.feed(pcm_bytes(0.5))
    await _drain(feed, 0.3)
    feed.end_speech()
    await _drain(feed, 0.3)
    assert notices == []
    # A finished clip with no words completes as empty (typed no-speech),
    # not None — None made the live session stay mute.
    assert await feed.final_text(timeout_ms=100) == ""


class _EmptyThenPhrase:
    """Fallback that fails closed once, then returns a real phrase from PCM."""

    name = "empty-then-phrase"

    def __init__(self, text: str = "What's next on my calendar.") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, **kwargs) -> Transcript:
        assert kwargs.get("audio_b64"), "wrapper must pass PCM, not a text hint"
        self.calls += 1
        if self.calls == 1:
            raise VoiceError("empty short clip", status=502, code="asr_empty_result")
        return Transcript(text=self.text, confidence=0.91, language="en", provider=self.name)


@pytest.mark.asyncio
async def test_resolve_echo_wrapper_empty_then_phrase() -> None:
    """Stock echo wrap: first asr_empty_result must not kill later phrases."""

    engine = resolve_live_transcriber(EchoTranscriber())
    assert isinstance(engine, LivePcmTranscriber)
    fallback = _EmptyThenPhrase()
    engine._fallback_factory = None
    engine._fallback = fallback
    audio = _wav_bytes(pcm_bytes(0.4))

    first = await engine.transcribe(audio_b64=audio, language="en")
    assert not (first.text or "").strip()
    assert engine._fallback_unusable is False

    second = await engine.transcribe(audio_b64=audio, language="en")
    assert second.text == "What's next on my calendar."
    assert fallback.calls == 2
