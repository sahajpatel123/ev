"""Spoken-unit splitting for first-word TTS."""

from __future__ import annotations

from app.ev.briefing import voice_needs_tools
from app.ev.personality import identity_block
from app.voice.speech import (
    LISTEN_ACKS,
    answer_speech_style,
    choose_listen_ack,
    choose_voice_filler,
    concat_wav_bytes,
    is_presence_check,
    is_unreadable_transcript,
    listen_ack_style,
    pop_speakable,
    starts_with_evie,
    strip_wake_prefix,
)


def test_choose_listen_ack_varies_with_evie_start() -> None:
    wake = choose_listen_ack("hey evie")
    question = choose_listen_ack("Evie what's next on my calendar")
    command = choose_listen_ack("EVIE send that email")
    soft = choose_listen_ack("evie can you check the time")
    assert wake in LISTEN_ACKS
    assert question in LISTEN_ACKS
    assert command in LISTEN_ACKS
    assert soft in LISTEN_ACKS
    assert len({wake, question, command, soft}) > 1
    assert starts_with_evie("Evie look that up")
    assert not starts_with_evie("what's the weather")
    assert strip_wake_prefix("Evie what's the weather") == "what's the weather"
    assert strip_wake_prefix("hey evie, text mom") == "text mom"
    assert strip_wake_prefix("EVIE") == ""
    assert strip_wake_prefix("what's the weather") == "what's the weather"
    assert strip_wake_prefix("That's interesting.") == "That's interesting."
    assert strip_wake_prefix("Evie what's the weather?") == "what's the weather?"
    listen = listen_ack_style()
    answer = answer_speech_style()
    assert listen.warmth > answer.warmth
    assert listen.urgency < answer.urgency


def test_choose_voice_filler_matches_intent() -> None:
    assert choose_voice_filler("search the web for Surat weather") == "Searching."
    assert choose_voice_filler("look up that paper") == "Searching."
    assert choose_voice_filler("what's next on my calendar") == "Checking."
    assert choose_voice_filler("how was my sleep") == "Checking."
    assert choose_voice_filler("text Mom I'm late") == "One second."
    assert choose_voice_filler("hey") == "On it."


def test_pop_speakable_first_audio_after_five_words() -> None:
    chunk, rest = pop_speakable("Right now it is mostly clear tonight leftover")
    assert chunk is not None
    assert len(chunk.split()) == 5
    assert rest.strip()
    sentence, rest = pop_speakable("It's 8:19 IST. Nothing else today.")
    assert sentence == "It's 8:19 IST."
    assert rest.startswith("Nothing")
    clause, rest2 = pop_speakable(
        "Right now in Surat it's mainly clear, 27 degrees with drizzle later"
    )
    assert clause is not None
    assert clause.endswith(",")
    assert "drizzle" in rest2


def test_pop_speakable_flush_short_reply() -> None:
    sentence, rest = pop_speakable("100", flush=True)
    assert sentence == "100"
    assert rest == ""
    none, buf = pop_speakable("almost")
    assert none is None
    assert buf == "almost"


def test_concat_wav_bytes_joins_two_files() -> None:
    import io
    import wave

    def one_wav(frames: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(24000)
            out.writeframes(frames)
        return buf.getvalue()

    a = one_wav(b"\x00\x00" * 10)
    b = one_wav(b"\x01\x00" * 5)
    joined = concat_wav_bytes([a, b])
    assert joined is not None
    with wave.open(io.BytesIO(joined), "rb") as src:
        assert src.getnframes() == 15


def test_voice_skips_tools_for_reads() -> None:
    assert voice_needs_tools("what's the weather") is False
    assert voice_needs_tools("what time is it") is False
    assert voice_needs_tools("text Mom I'm late") is True
    assert voice_needs_tools("show that on my screen") is True


def test_compact_identity_is_short() -> None:
    compact = identity_block("EVIE", "the owner's personal AI", compact=True)
    full = identity_block("EVIE", "the owner's personal AI", compact=False)
    assert "first clause" in compact
    assert len(compact) < len(full)
    assert "Personality profile" not in compact
    assert "hear them" in compact.lower()


def test_unreadable_transcript_rejects_dhm_class() -> None:
    assert is_unreadable_transcript("DHM")
    assert is_unreadable_transcript("dhm")
    assert is_unreadable_transcript("")
    assert is_unreadable_transcript("asr_timeout")
    assert is_unreadable_transcript("Speech recognition took too long")
    assert not is_unreadable_transcript("Evie can you hear me?")
    assert not is_unreadable_transcript("yes")
    assert not is_unreadable_transcript("100")
    assert not is_unreadable_transcript("what's next")


def test_presence_check_detects_hear_me() -> None:
    assert is_presence_check("Evie can you hear me?")
    assert is_presence_check("are you there")
    assert is_presence_check("hey evie are you listening")
    assert not is_presence_check("evie can you check the time")
    assert not is_presence_check("what's next on my calendar")
