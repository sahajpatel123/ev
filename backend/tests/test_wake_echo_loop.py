"""Eve/EVIE stays always-on; our own TTS must not become the next user turn."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.config import settings
from app.utils.text import utcnow
from app.voice.contracts import SpeechStyle, SynthesisResult, Transcript
from app.voice.lifecycle import VoiceRuntime, VoiceState
from app.voice.pipeline import PipelineOutcome
from app.voice.speech import (
    LISTEN_ACKS,
    SPOKEN_SECONDS_PER_WORD,
    audio_duration_s,
    clear_spoken,
    echo_hold_seconds,
    estimate_spoken_duration_s,
    is_echo_of_last_reply,
    is_wake_only_name,
    last_spoken,
    looks_like_new_owner_turn,
    remember_spoken,
    should_drop_as_echo,
)

# Live failure: ~20-word neural mp3 played ~12.7s; residual arrived at 10.7s.
STUCK_MIC_REPLY = (
    "That sounded like a stuck mic on your end none of that "
    "made it into memory as a real request."
)
from app.models import VoiceSession
from app.voice.tts import MetaSynthesizer
from app.voice.wake import PhraseWakeEngine


def test_eve_and_evie_are_wake_only_names() -> None:
    assert is_wake_only_name("Eve")
    assert is_wake_only_name("EVIE")
    assert is_wake_only_name("hey eve")
    assert is_wake_only_name("hey evie")
    assert not is_wake_only_name("eve what's the weather")
    assert not is_wake_only_name("what's next")


def test_phrase_engine_remembers_eve() -> None:
    engine = PhraseWakeEngine()
    assert "eve" in engine.WAKE_PHRASES


def test_ears_echo_hold_drops_pending_and_waits_out_the_tail() -> None:
    import inspect

    from clients.ears import main as ears_main
    from clients.ears.main import EarConfig

    assert EarConfig().echo_tail_s >= 0.5
    hold = inspect.getsource(ears_main.run_ears)
    assert "pending_segment = None" in hold
    assert "echo_tail_s" in inspect.getsource(ears_main.deliver_wake_utterance)


@pytest.mark.asyncio
async def test_phrase_engine_detects_eve_text() -> None:
    engine = PhraseWakeEngine()
    hit = await engine.detect(text_hint="Eve")
    assert hit.triggered
    miss = await engine.detect(text_hint="what's next")
    assert not miss.triggered


def test_last_reply_and_listen_ack_are_echo() -> None:
    assert is_echo_of_last_reply("Yes?", "Yes?")
    assert is_echo_of_last_reply("yes", "Yes?")
    assert is_echo_of_last_reply(
        "Your next thing is lunch.",
        "Your next thing is lunch.",
    )
    assert is_echo_of_last_reply("next thing is lunch", "Your next thing is lunch.")
    assert not is_echo_of_last_reply("what's next", "Your next thing is lunch.")


def test_residual_after_speech_is_echo_unless_new_turn() -> None:
    now = utcnow()
    spoken_at = now - timedelta(seconds=0.4)
    assert should_drop_as_echo(
        "idiot",
        last_reply="Yes?",
        spoken_at=spoken_at,
        now=now,
    )
    assert should_drop_as_echo(
        "Your next thing is lunch.",
        last_reply="Your next thing is lunch.",
        spoken_at=spoken_at,
        now=now,
    )
    assert not should_drop_as_echo(
        "Eve",
        last_reply="Yes?",
        spoken_at=spoken_at,
        now=now,
    )
    assert not should_drop_as_echo(
        "what's next",
        last_reply="Yes?",
        spoken_at=spoken_at,
        now=now,
    )
    later = now + timedelta(seconds=10)
    assert not should_drop_as_echo(
        "idiot",
        last_reply="Yes?",
        spoken_at=spoken_at,
        now=later,
    )


def test_playback_window_drops_residual_but_allows_new_turn() -> None:
    assert should_drop_as_echo(
        "hmm leftover",
        last_reply="Yes?",
        playing=True,
    )
    assert not should_drop_as_echo("Eve", last_reply="Yes?", playing=True)
    assert not should_drop_as_echo("what's next", last_reply="Yes?", playing=True)


def test_whisper_junk_is_not_a_new_owner_turn() -> None:
    assert not looks_like_new_owner_turn("Thanks for watching")
    assert not looks_like_new_owner_turn("idiot")
    assert not looks_like_new_owner_turn("hmm leftover")
    assert looks_like_new_owner_turn("what's next")
    assert looks_like_new_owner_turn("text mom I'm late")
    assert looks_like_new_owner_turn("can you check the weather")
    assert not looks_like_new_owner_turn("Eve")

    now = utcnow()
    long_reply = (
        "Your next thing is lunch with Maya at noon then a call with Jordan."
    )
    assert should_drop_as_echo(
        "Thanks for watching",
        last_reply=long_reply,
        spoken_at=now - timedelta(seconds=1),
        now=now,
        duration_s=10.0,
    )


def test_echo_window_is_playback_duration_plus_tail() -> None:
    """Residual hold lasts until playback_end + tail, not 3.5s from generation."""

    assert len(STUCK_MIC_REPLY.split()) == 20
    duration_s = estimate_spoken_duration_s(STUCK_MIC_REPLY)
    # 0.35s/word would be 7.4s and miss a 10.7s residual.
    assert duration_s >= 20 * SPOKEN_SECONDS_PER_WORD
    window = echo_hold_seconds(duration_s)
    now = utcnow()
    spoken_at = now - timedelta(seconds=10.7)
    assert window >= 10.7
    assert should_drop_as_echo(
        "idiot",
        last_reply=STUCK_MIC_REPLY,
        spoken_at=spoken_at,
        now=now,
        duration_s=duration_s,
    )
    later = now + timedelta(seconds=window + 2)
    assert not should_drop_as_echo(
        "idiot",
        last_reply=STUCK_MIC_REPLY,
        spoken_at=spoken_at,
        now=later,
        duration_s=duration_s,
    )


def test_estimate_prefers_parsed_wav_duration() -> None:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 16000 * 2)
    wav = buf.getvalue()
    assert 1.9 <= (audio_duration_s(wav, content_type="audio/wav") or 0) <= 2.1
    estimated = estimate_spoken_duration_s("hi", audio=wav, content_type="audio/wav")
    assert 1.9 <= estimated <= 2.1


async def _session(db_session, device_id: str) -> VoiceSession:
    row = VoiceSession(
        device_id=device_id,
        wake_word="evie",
        state=VoiceState.AWAKE,
        owner_verified=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _runtime(db_session) -> VoiceRuntime:
    return VoiceRuntime(db_session, master_key=settings.master_key, synthesizer=MetaSynthesizer())


@pytest.mark.asyncio
async def test_second_and_third_eve_still_listen_without_chat(
    db_session, monkeypatch
) -> None:
    """After a completed turn, Eve again is a listen door, not a chat turn."""

    heard: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        heard.append(transcript.text)
        return PipelineOutcome(
            transcript=transcript,
            reply="Your next thing is lunch.",
            conversation_id=str(uuid4()),
            tts=SynthesisResult(text="Your next thing is lunch.", provider="meta"),
            style=SpeechStyle(),
            model="mock",
            context_tokens=1,
            memory_deltas=[],
        )

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    row = await _session(db_session, "mac-eve-repeat")
    runtime = _runtime(db_session)
    first = await runtime.handle_utterance(session_id=row.id, text="what's next")
    assert first.reply
    assert heard == ["what's next"]

    second = await runtime.handle_ears_ingest(
        device_id="mac-eve-repeat",
        frames_b64=None,
        consent=True,
        text_hint="Eve",
    )
    assert second.accepted is True
    assert second.listening is True
    assert second.reply in LISTEN_ACKS
    assert heard == ["what's next"]

    third = await runtime.handle_ears_ingest(
        device_id="mac-eve-repeat",
        frames_b64=None,
        consent=True,
        text_hint="EVIE",
    )
    assert third.accepted is True
    assert third.listening is True
    assert third.reply in LISTEN_ACKS
    assert heard == ["what's next"]
    clear_spoken("mac-eve-repeat")


@pytest.mark.asyncio
async def test_last_reply_echo_does_not_run_chat(db_session, monkeypatch) -> None:
    called: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        called.append(transcript.text)
        raise AssertionError("chat must not run on our own playback")

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    row = await _session(db_session, "mac-eve-echo")
    runtime = _runtime(db_session)
    remember_spoken(
        "mac-eve-echo",
        "Your next thing is lunch.",
        duration_s=2.0,
        now=utcnow(),
    )
    outcome = await runtime.handle_ears_ingest(
        device_id="mac-eve-echo",
        frames_b64=None,
        consent=True,
        text_hint="Your next thing is lunch.",
    )
    assert outcome.accepted is True
    assert outcome.listening is True
    assert called == []
    assert outcome.reply in {None, ""} or outcome.message == "still listening"
    clear_spoken("mac-eve-echo")
    _ = row


@pytest.mark.asyncio
async def test_wake_only_then_new_question_is_that_question(
    db_session, monkeypatch
) -> None:
    heard: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        heard.append(transcript.text)
        return PipelineOutcome(
            transcript=transcript,
            reply=f"answer:{transcript.text}",
            conversation_id=str(uuid4()),
            tts=SynthesisResult(text=transcript.text, provider="meta"),
            style=SpeechStyle(),
            model="mock",
            context_tokens=1,
            memory_deltas=[],
        )

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    await _session(db_session, "mac-eve-next")
    runtime = _runtime(db_session)
    wake = await runtime.handle_ears_ingest(
        device_id="mac-eve-next",
        frames_b64=None,
        consent=True,
        text_hint="Eve",
    )
    assert wake.listening is True
    assert wake.reply in LISTEN_ACKS
    assert heard == []

    follow = await runtime.handle_ears_ingest(
        device_id="mac-eve-next",
        frames_b64=None,
        consent=True,
        text_hint="what's next",
    )
    assert follow.accepted is True
    assert follow.listening is True
    assert heard == ["what's next"]
    assert "what's next" in (follow.transcript or "").lower()
    assert follow.reply != wake.reply
    clear_spoken("mac-eve-next")


@pytest.mark.asyncio
async def test_idiot_at_playback_end_plus_tail_does_not_run_chat(
    db_session, monkeypatch
) -> None:
    """Ingest after _remember_spoken_reply (mp3, no duration_ms) at 10.7s."""

    called: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        called.append(transcript.text)
        return PipelineOutcome(
            transcript=transcript,
            reply=STUCK_MIC_REPLY,
            conversation_id=str(uuid4()),
            tts=SynthesisResult(
                text=STUCK_MIC_REPLY,
                provider="edge",
                duration_ms=None,
                content_type="audio/mpeg",
            ),
            style=SpeechStyle(),
            model="mock",
            context_tokens=1,
            memory_deltas=[],
        )

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    await _session(db_session, "mac-eve-tail")
    runtime = _runtime(db_session)
    first = await runtime.handle_ears_ingest(
        device_id="mac-eve-tail",
        frames_b64=None,
        consent=True,
        text_hint="what's next",
    )
    assert first.reply == STUCK_MIC_REPLY
    assert called == ["what's next"]
    spoken = last_spoken("mac-eve-tail")
    assert spoken is not None
    assert spoken["duration_s"] >= 20 * SPOKEN_SECONDS_PER_WORD
    # Observed residual age on the live mp3 path (playback_end + ~0.6s).
    spoken["at"] = utcnow() - timedelta(seconds=10.7)
    called.clear()

    outcome = await runtime.handle_ears_ingest(
        device_id="mac-eve-tail",
        frames_b64=None,
        consent=True,
        text_hint="idiot",
    )
    assert called == []
    assert outcome.accepted is True
    assert outcome.listening is True
    assert outcome.reply in {None, ""} or outcome.message == "still listening"
    clear_spoken("mac-eve-tail")


@pytest.mark.asyncio
async def test_thanks_for_watching_during_long_reply_does_not_run_chat(
    db_session, monkeypatch
) -> None:
    called: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        called.append(transcript.text)
        return PipelineOutcome(
            transcript=transcript,
            reply=STUCK_MIC_REPLY,
            conversation_id=str(uuid4()),
            tts=SynthesisResult(
                text=STUCK_MIC_REPLY,
                provider="edge",
                duration_ms=None,
                content_type="audio/mpeg",
            ),
            style=SpeechStyle(),
            model="mock",
            context_tokens=1,
            memory_deltas=[],
        )

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    await _session(db_session, "mac-eve-junk")
    runtime = _runtime(db_session)
    first = await runtime.handle_ears_ingest(
        device_id="mac-eve-junk",
        frames_b64=None,
        consent=True,
        text_hint="what's next",
    )
    assert first.reply == STUCK_MIC_REPLY
    assert called == ["what's next"]
    spoken = last_spoken("mac-eve-junk")
    assert spoken is not None
    spoken["at"] = utcnow() - timedelta(seconds=1)
    called.clear()

    outcome = await runtime.handle_ears_ingest(
        device_id="mac-eve-junk",
        frames_b64=None,
        consent=True,
        text_hint="Thanks for watching",
    )
    assert called == []
    assert outcome.accepted is True
    assert outcome.listening is True
    clear_spoken("mac-eve-junk")


@pytest.mark.asyncio
async def test_fallback_tts_is_remembered_and_echo_rejected(
    db_session, monkeypatch
) -> None:
    called: list[str] = []

    async def pipeline(session, *, transcript, **kwargs):
        called.append(transcript.text)
        raise AssertionError("chat must not run on fallback echo")

    monkeypatch.setattr("app.voice.lifecycle.run_chat_tts_pipeline", pipeline)
    row = await _session(db_session, "mac-eve-fallback")
    runtime = _runtime(db_session)
    recovery = (
        "I heard you, but the answer is taking too long. "
        "Try asking again in a moment."
    )
    fallback = await runtime._fallback_utterance(
        row=row,
        transcript=Transcript(text="what's next", confidence=1.0, provider="mock"),
        reply=recovery,
        error="timeout",
    )
    spoken = last_spoken("mac-eve-fallback")
    assert spoken is not None
    assert "taking too long" in spoken["text"]
    assert spoken["duration_s"] >= estimate_spoken_duration_s(recovery) - 0.05
    assert fallback.reply == recovery

    outcome = await runtime.handle_ears_ingest(
        device_id="mac-eve-fallback",
        frames_b64=None,
        consent=True,
        text_hint=recovery,
    )
    assert called == []
    assert outcome.accepted is True
    assert outcome.listening is True
    clear_spoken("mac-eve-fallback")
