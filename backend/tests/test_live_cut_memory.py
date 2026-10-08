"""Cut memory — truncated pipeline replies, cut-in ledger, hold survival."""

from __future__ import annotations

import asyncio

from app.voice.live.engine import LiveEngine, ManualClock
from app.voice.live.eve_cutin import TRIGGER_TIMER_DUE
from app.voice.live.events import BargeInEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.session import LiveSession


async def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


def _evidence(**overrides) -> dict:
    frame = {
        "type": "owner_evidence",
        "speech_ms": 400,
        "confidence": 0.9,
        "client_confirmed": True,
        "aec_active": True,
        "playback_active": True,
        "audio_played_ms": 1500,
        "preroll_ms": 800,
        "response_id": "resp-cut",
    }
    frame.update(overrides)
    return frame


async def test_v2_cut_keeps_heard_prefix_only() -> None:
    started = asyncio.Event()

    async def respond(text: str, envelope):
        del text, envelope
        yield TtsChunkEvent(
            at_ms=0, index=0, text="The quick brown fox jumps.", duration_ms=2000
        )
        yield TtsChunkEvent(
            at_ms=0, index=1, text="Over the lazy dog tonight.", duration_ms=2000
        )
        started.set()
        await asyncio.sleep(30)
        yield ReplyEvent(at_ms=0, text="should not land")

    session = LiveSession(session_id="cut-1", respond=respond, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "tell me a story", "commit": True})
    await asyncio.wait_for(started.wait(), timeout=2)
    await session.handle_client(_evidence())
    await asyncio.sleep(0.2)
    events = await _drain(session)

    assert any(isinstance(event, BargeInEvent) for event in events)
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    cut = replies[0]
    assert cut.interrupted is True
    assert cut.interruption_reason == "user_barge_in"
    assert cut.audio_played_ms == 1500
    assert cut.generated_duration_ms == 4000
    assert cut.generated_text is not None and "lazy dog" in cut.generated_text
    # 1500/4000 of 10 words -> only the heard prefix is claimed.
    assert cut.text.startswith("The quick")
    assert "lazy dog" not in cut.text
    session.close()


async def test_cut_before_first_audio_claims_nothing() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str, envelope):
        del text, envelope
        started.set()
        try:
            await asyncio.sleep(30)
            yield ReplyEvent(at_ms=0, text="should not land")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session = LiveSession(session_id="cut-2", respond=slow, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "go on", "commit": True})
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.handle_client({"type": "control", "action": "cancel"})
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    events = await _drain(session)
    assert [event for event in events if isinstance(event, ReplyEvent)] == []
    session.close()


async def test_control_barge_in_also_truncates() -> None:
    started = asyncio.Event()

    async def respond(text: str, envelope):
        del text, envelope
        yield TtsChunkEvent(at_ms=0, index=0, text="Alpha beta gamma delta.", duration_ms=4000)
        started.set()
        await asyncio.sleep(30)

    session = LiveSession(session_id="cut-3", respond=respond, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "speak", "commit": True})
    await asyncio.wait_for(started.wait(), timeout=2)
    await session.handle_client(
        {"type": "control", "action": "barge_in", "audio_played_ms": 1000}
    )
    await asyncio.sleep(0.2)
    events = await _drain(session)
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].interrupted is True
    assert replies[0].text == "Alpha"
    session.close()


async def test_eve_cut_in_ledgers_what_owner_heard(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    calls: list[dict] = []

    def fake_remember(session, *, owner, assistant, kind=""):
        calls.append({"owner": owner, "assistant": assistant, "kind": kind})
        return session

    monkeypatch.setattr("app.cognitive.intent.remember_exchange", fake_remember)
    clock = ManualClock(1_000)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    engine.push_speech(True, now_ms=1_000)
    clock.advance(2_000)

    assert await session.speak_eve_cut_in("Timer done.", trigger=TRIGGER_TIMER_DUE) is True
    assert calls == [{"owner": "", "assistant": "Timer done.", "kind": "cut_in"}]
    session.close()


async def test_approval_hold_survives_interruptions(monkeypatch) -> None:
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda *a, **k: False)
    clock = ManualClock(1_000)
    engine = LiveEngine(clock_ms=clock)
    session = LiveSession(engine=engine, backchannel_enabled=False)
    await session.apply_approval_hold({"hold": "send-mail", "risk": "R3"}, speak=False)

    engine.push_assistant_speaking(True, now_ms=1_000)
    await session.handle_client(_evidence())
    engine.push_speech(True, now_ms=2_000)
    clock.advance(3_000)
    assert await session.speak_eve_cut_in("Timer done.", trigger=TRIGGER_TIMER_DUE) is True

    assert session._approval_hold == {"hold": "send-mail", "risk": "R3"}
    session.close()


async def test_v2_cancels_speech_not_durable_jobs() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str, envelope):
        del text, envelope
        started.set()
        try:
            await asyncio.sleep(30)
            yield ReplyEvent(at_ms=0, text="should not land")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session = LiveSession(session_id="cut-4", respond=slow, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "explain at length", "commit": True})
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.handle_client(_evidence())
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    events = await _drain(session)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert session._durable_jobs_cancelled is False
    assert not session._closed
    session.close()


async def _cut_with_remainder() -> LiveSession:
    """Cut a two-chunk reply early; returns the session holding a remainder."""

    started = asyncio.Event()

    async def respond(text: str, envelope):
        del text, envelope
        yield TtsChunkEvent(
            at_ms=0, index=0, text="The quick brown fox jumps.", duration_ms=2000
        )
        yield TtsChunkEvent(
            at_ms=0, index=1, text="Over the lazy dog tonight.", duration_ms=2000
        )
        started.set()
        await asyncio.sleep(30)
        yield ReplyEvent(at_ms=0, text="should not land")

    session = LiveSession(session_id="cut-resume", respond=respond, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": "tell me a story", "commit": True})
    await asyncio.wait_for(started.wait(), timeout=2)
    await session.handle_client(_evidence(audio_played_ms=500))
    await asyncio.sleep(0.2)
    return session


async def test_cut_stashes_remainder_and_continuation_resumes(monkeypatch) -> None:
    session = await _cut_with_remainder()
    stashed = session._resume_remainder
    assert stashed is not None
    assert "lazy dog" in stashed[0]
    assert "quick brown" not in stashed[0]

    spoken: list[str] = []

    async def spy(text: str, **kwargs) -> None:
        del kwargs
        spoken.append(text)

    monkeypatch.setattr(session, "speak_proactive", spy)
    await session.handle_client({"type": "text", "text": "go on", "commit": True})
    assert spoken == [stashed[0]]
    assert session._resume_remainder is None
    # Consumed as a resume: "go on" must not also start an owner turn.
    events = await _drain(session)
    assert not any(
        getattr(event, "text", "") == "go on" for event in events
    )
    session.close()


async def test_cut_without_remainder_clears_slot() -> None:
    session = await _cut_with_remainder()
    assert session._resume_remainder is not None
    session._resume_remainder = ("stale words that should vanish entirely", session.now())
    await session._emit_cut_reply(
        ["All heard."], 2000, reason="user_barge_in"
    )
    assert session._resume_remainder is None
    session.close()


async def test_other_text_clears_resume_slot() -> None:
    async def quiet(text: str, envelope):
        del text, envelope
        if False:
            yield ReplyEvent(at_ms=0, text="never")

    session = LiveSession(session_id="cut-clear", respond=quiet, backchannel_enabled=False)
    session._resume_remainder = ("unspoken tail that should vanish", session.now())
    await session.handle_client({"type": "text", "text": "tell me about otters", "commit": True})
    assert session._resume_remainder is None
    session.close()


async def test_stale_remainder_ignored(monkeypatch) -> None:
    session = LiveSession(session_id="cut-stale", respond=_quiet_respond, backchannel_enabled=False)
    session._resume_remainder = ("ancient unspoken tail", session.now() - 91_000)
    spoken: list[str] = []

    async def spy(text: str, **kwargs) -> None:
        del kwargs
        spoken.append(text)

    monkeypatch.setattr(session, "speak_proactive", spy)
    await session.handle_client({"type": "text", "text": "continue", "commit": True})
    assert spoken == []
    assert session._resume_remainder is None
    session.close()


async def test_continuation_while_speaking_falls_through() -> None:
    session = LiveSession(session_id="cut-busy", respond=_quiet_respond, backchannel_enabled=False)
    session._resume_remainder = ("kept tail", session.now())
    session.engine.state.assistant_is_speaking = True
    assert await session._maybe_resume_remainder("go on") is False
    assert session._resume_remainder == ("kept tail", session._resume_remainder[1])
    session.close()


async def _quiet_respond(text: str, envelope):
    del text, envelope
    return
    yield ReplyEvent(at_ms=0, text="never")  # pragma: no cover - keeps it a generator
