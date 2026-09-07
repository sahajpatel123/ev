"""The real Vosk engines, run against recorded speech.

Nothing here is faked: the point is to prove that the decoder the hands-free
loop depends on actually spots "EVIE" in speech and refuses to spot it in
ordinary conversation. The whole module is skipped when the Vosk runtime or the
small en-US model is not installed (CI), because a fake would prove nothing.
"""

from __future__ import annotations

import base64
import wave
from pathlib import Path

import pytest

from app.config import settings
from app.voice.asr import VoskTranscriber, get_transcriber
from app.voice.contracts import Transcript, TranscriptPartial
from app.voice.vosk_engine import (
    VoskStreamingRecognizer,
    VoskWakeEngine,
    VoskWakeSpotter,
    WakeSignal,
    vosk_available,
    vosk_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "voice"
BLOCK_BYTES = 3200  # 100 ms of 16 kHz mono PCM16

pytestmark = [
    pytest.mark.skipif(
        not vosk_available(),
        reason="the Vosk runtime and the en-US model are not installed",
    ),
    pytest.mark.skipif(
        not (Path(__file__).parent / "fixtures" / "voice" / "wake_hey.wav").is_file(),
        reason="voice fixtures are not present",
    ),
]


@pytest.fixture(autouse=True)
def fresh_db():
    """Speech engines only; skip the per-test database rebuild."""

    yield


def wav_file(name: str) -> bytes:
    return (FIXTURES / f"{name}.wav").read_bytes()


def pcm(name: str) -> bytes:
    with wave.open(str(FIXTURES / f"{name}.wav"), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (
            1,
            2,
            16000,
        )
        return handle.readframes(handle.getnframes())


def spot(name: str, **kwargs) -> list[WakeSignal]:
    """Stream a clip through the spotter and close the segment, as the loop does."""

    audio = pcm(name)
    spotter = VoskWakeSpotter(sample_rate=16000, **kwargs)
    signals: list[WakeSignal] = []
    for offset in range(0, len(audio), BLOCK_BYTES):
        signals.extend(spotter.feed(audio[offset : offset + BLOCK_BYTES]))
    signals.extend(spotter.flush())
    return signals


def transcribe_clip(name: str) -> tuple[list[str], object]:
    recognizer = VoskStreamingRecognizer(sample_rate=16000)
    audio = pcm(name)
    partials = []
    for offset in range(0, len(audio), BLOCK_BYTES):
        hypothesis = recognizer.feed(audio[offset : offset + BLOCK_BYTES])
        if hypothesis:
            partials.append(hypothesis)
    return partials, recognizer.final()


# --------------------------------------------------------------------------- #
# Wake spotting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("clip", "phrase"), [("wake_hey", "hey evie"), ("wake_plain", "evie")])
def test_wake_spotter_confirms_the_wake_phrase(clip: str, phrase: str) -> None:
    signals = spot(clip)
    confirmed = [signal for signal in signals if signal.kind == "confirmed"]

    assert len(confirmed) == 1
    assert confirmed[0].phrase == phrase
    assert confirmed[0].confidence >= settings.voice_wake_vosk_threshold
    assert confirmed[0].triggered is True
    # The phrase ends somewhere inside the clip, not at an invented offset.
    assert 0 < confirmed[0].end_offset < len(pcm(clip)) // 2
    assert [signal.kind for signal in signals][0] == "pending"


def test_okay_evie_confirms_the_wake_phrase() -> None:
    """The grammar lists 'okay evie' once so the posterior is not split."""

    verdict = spot("wake_ok")[-1]
    assert verdict.kind == "confirmed"
    assert verdict.phrase == "okay evie"
    assert verdict.confidence >= settings.voice_wake_vosk_threshold


@pytest.mark.parametrize("clip", ["no_wake", "follow_up"])
def test_wake_spotter_does_not_confirm_ordinary_speech(clip: str) -> None:
    """A partial may guess the name; only a closed segment may act on it."""

    signals = spot(clip)

    assert [signal for signal in signals if signal.kind == "confirmed"] == []
    assert signals[-1].kind == "rejected"


def test_wake_spotter_reports_where_the_phrase_ended() -> None:
    confirmed = next(signal for signal in spot("wake_hey") if signal.kind == "confirmed")

    # "Hey Evie, what did I decide..." — the phrase is over well inside the
    # first second, so the command that follows it is not swallowed.
    assert 0.4 < confirmed.end_offset / 16000 < 1.0


# --------------------------------------------------------------------------- #
# Batch wake engine
# --------------------------------------------------------------------------- #


async def test_wake_engine_triggers_only_on_a_wake_clip() -> None:
    engine = VoskWakeEngine()

    hit = await engine.detect(frames=pcm("wake_hey"), device_id="mac-1")
    assert hit.triggered is True
    assert hit.confidence >= settings.voice_wake_vosk_threshold
    assert hit.details["phrase"] == "hey evie"
    assert hit.details["engine"] == "vosk"
    assert hit.stage == "burst"
    assert 0 < hit.details["wake_end_sample"] < len(pcm("wake_hey")) // 2

    miss = await engine.detect(frames=pcm("no_wake"), device_id="mac-1")
    assert miss.triggered is False
    assert miss.confidence == 0.0


async def test_wake_engine_never_triggers_from_a_text_hint() -> None:
    engine = VoskWakeEngine()

    result = await engine.detect(frames=b"", text_hint="hey evie")
    assert result.triggered is False
    assert result.details["text_hint_present"] is True

    with pytest.raises(ValueError, match="requires 'frames'"):
        await engine.detect(text_hint="hey evie")


# --------------------------------------------------------------------------- #
# Command recognizer
# --------------------------------------------------------------------------- #


def test_streaming_recognizer_transcribes_with_intermediate_partials() -> None:
    partials, result = transcribe_clip("follow_up")

    assert "tomorrow" in result.text
    assert result.confidence > 0.0
    assert result.words
    assert partials, "the recognizer must emit hypotheses while decoding"
    assert partials[-1] != result.text or len(partials) > 1


def test_streaming_recognizer_hears_the_command_after_the_wake_phrase() -> None:
    _partials, result = transcribe_clip("wake_hey")

    assert "decide about the project" in result.text


# --------------------------------------------------------------------------- #
# Transcriber provider
# --------------------------------------------------------------------------- #


async def test_transcriber_returns_a_real_transcript() -> None:
    transcriber = VoskTranscriber()

    result = await transcriber.transcribe(
        audio_b64=base64.b64encode(wav_file("follow_up")).decode("ascii")
    )

    assert result.provider == "vosk"
    assert result.degraded is False
    assert "tomorrow" in result.text
    assert result.confidence > 0.0
    assert result.duration_ms == pytest.approx(1346, abs=50)


async def test_transcriber_streams_partials_before_the_final_transcript() -> None:
    transcriber = VoskTranscriber()

    items = [
        item
        async for item in transcriber.stream(
            audio_b64=base64.b64encode(wav_file("follow_up")).decode("ascii")
        )
    ]

    assert all(isinstance(item, TranscriptPartial) for item in items[:-1])
    assert [item.sequence for item in items[:-1]] == list(range(1, len(items)))
    final = items[-1]
    assert isinstance(final, Transcript)
    assert final.degraded is False
    assert "tomorrow" in final.text
    assert final.details["streaming"] is True


def test_auto_provider_resolves_to_vosk_when_the_model_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_asr_provider", "auto")
    assert isinstance(get_transcriber(), VoskTranscriber)


# --------------------------------------------------------------------------- #
# Readiness reporting
# --------------------------------------------------------------------------- #


def test_vosk_status_reports_a_ready_engine() -> None:
    status = vosk_status()

    assert status == {
        "engine": "vosk",
        "ready": True,
        "runtime_installed": True,
        "model_installed": True,
        "model_path": status["model_path"],
        "detail": "ready",
    }
    assert Path(status["model_path"]).is_dir()


async def test_missing_model_is_reported_and_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-model"
    monkeypatch.setattr(settings, "voice_vosk_model_path", str(missing))

    status = vosk_status()
    assert status["ready"] is False
    assert status["model_installed"] is False
    assert status["runtime_installed"] is True
    assert str(missing) in status["detail"]
    assert "models_setup" in status["detail"]

    result = await VoskTranscriber().transcribe(
        audio_b64=base64.b64encode(wav_file("follow_up")).decode("ascii")
    )
    assert result.degraded is True
    assert result.text == ""
    assert result.confidence == 0.0
    assert str(missing) in result.details["reason"]
