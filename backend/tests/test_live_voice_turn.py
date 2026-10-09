"""Live pipeline turn reproduction: text turn → TTS chunks out.

Drives the REAL LiveSession message path with the REAL make_pipeline_responder
(mocks only the chat pipeline + DB thread) and a real Edge-shaped synthesizer
(MP3 bytes). Asserts the session emits a reply and a TTS chunk carrying audio.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _mp3_bytes(seconds: float = 0.5) -> bytes:
    """Real MP3 via ffmpeg (Edge TTS output shape), skipped if absent."""
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-ac", "1", "-ar", "24000", "-b:a", "48k", "-f", "mp3", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _fake_edge_synthesizer():
    audio = _mp3_bytes()

    class FakeEdge:
        name = "edge_tts"
        streamable_output = False
        voice = "en-GB-SoniaNeural"

        async def synthesize(self, text, *, style=None):
            from app.voice.contracts import SynthesisResult

            return SynthesisResult(
                text=text,
                provider=self.name,
                audio=audio,
                content_type="audio/mpeg",
                details={"engine": self.name},
            )

    return FakeEdge()


@pytest.mark.asyncio
async def test_pipeline_text_turn_speaks_decodable_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import make_pipeline_responder

    thread_id = uuid4()

    async def resolve_thread(*args, **kwargs):
        return SimpleNamespace(id=thread_id)

    async def chat(*args, **kwargs):
            return {
                "result": SimpleNamespace(
                    text="I am here and speaking.",
                    model="xiaomi/mimo-v2.6-flash",
                ),
            "conversation_id": str(thread_id),
            "context_tokens": 10,
            "memory_deltas": [],
        }

    from app.ev import assistant as assistant_mod
    from app.voice import pipeline as pipeline_mod

    monkeypatch.setattr(assistant_mod, "resolve_live_thread", resolve_thread)
    monkeypatch.setattr(pipeline_mod, "run_chat_pipeline", chat, raising=False)
    # make_pipeline_responder imports run_chat_pipeline from app.api.core.
    import app.api.core as core
    monkeypatch.setattr(core, "run_chat_pipeline", chat)

    respond = make_pipeline_responder(
        actor="master",
        device_id=None,
        conversation_id=thread_id,
        synthesizer=_fake_edge_synthesizer(),
    )
    live = LiveSession(session_id=str(uuid4()), respond=respond)

    await live.handle_client({"type": "text", "text": "Are you there?"})
    for _ in range(200):
        if live._respond_task is not None and live._respond_task.done():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)

    events = []
    while not live.outbound.empty():
        events.append(live.outbound.get_nowait())
    kinds = [e.type for e in events]
    tts_chunks = [e for e in events if e.type == "tts_chunk"]
    replies = [e for e in events if e.type == "reply"]

    for c in tts_chunks:
        print("CHUNK ct:", c.content_type, base64.b64decode(c.audio_b64 or b"")[:4], len(base64.b64decode(c.audio_b64 or b"")))
    assert replies, f"no reply event; kinds={kinds}"
    assert replies[-1].text.strip(), "reply text empty"
    assert tts_chunks, f"no tts_chunk; kinds={kinds}"
    spoken = base64.b64decode(tts_chunks[-1].audio_b64 or b"")
    assert spoken, "tts_chunk carried no audio"
    # The live emit lane delivers raw mono PCM16 with an explicit rate —
    # exactly what EV.app/iOS TTSPlayer accept (never audio/mpeg).
    assert tts_chunks[-1].content_type == "audio/pcm"
    assert tts_chunks[-1].sample_rate == 16_000
    assert not spoken.startswith(b"RIFF")
    assert len(spoken) % 2 == 0
    live.close()


@pytest.mark.asyncio
async def test_voice_yes_turn_resumes_parked_send_instead_of_chatting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WhatsApp voice reply: a 'yes' turn must resume the parked send through
    the approval gate (same helper the text pipeline uses) instead of going to
    the model as a fresh utterance. Regression: the voice responder never
    consulted the gate, so parked sends died silently on phone calls."""
    from unittest.mock import AsyncMock

    from app.ev.messaging import approval as approval_mod
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import make_pipeline_responder

    seen: dict = {}

    async def fake_gate(session, text, **kwargs):
        seen["text"] = text
        seen.update(kwargs)
        return {"ok": True, "sent": True, "spoken": "Sent WhatsApp to Ada."}

    monkeypatch.setattr(approval_mod, "handle_send_approval", fake_gate)
    chat = AsyncMock(side_effect=AssertionError("model must not run on approval turns"))
    import app.api.core as core

    monkeypatch.setattr(core, "run_chat_pipeline", chat)

    live_id = str(uuid4())
    respond = make_pipeline_responder(
        actor="master",
        device_id="dev-1",
        live_session_id=live_id,
        conversation_id=uuid4(),
        synthesizer=_fake_edge_synthesizer(),
    )
    live = LiveSession(session_id=live_id, respond=respond)
    await live.handle_client({"type": "text", "text": "yes, send it"})
    for _ in range(200):
        if live._respond_task is not None and live._respond_task.done():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)

    events = []
    while not live.outbound.empty():
        events.append(live.outbound.get_nowait())
    replies = [e for e in events if e.type == "reply"]
    assert seen.get("live_session_id") == live_id, seen
    assert replies, f"no reply event; kinds={[e.type for e in events]}"
    assert "Sent WhatsApp to Ada." in (replies[-1].text or "")
    chat.assert_not_awaited()
    live.close()


@pytest.mark.asyncio
async def test_voice_turn_without_parked_send_chats_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must be transparent when nothing is parked: gate returns None
    and the turn flows to the model exactly as before."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.ev.messaging import approval as approval_mod
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import make_pipeline_responder

    monkeypatch.setattr(approval_mod, "handle_send_approval", AsyncMock(return_value=None))

    async def chat(*args, **kwargs):
        return {
            "result": SimpleNamespace(text="Just chatting.", model="x"),
            "conversation_id": "c",
            "context_tokens": 1,
            "memory_deltas": [],
        }

    import app.api.core as core

    from app.ev import assistant as assistant_mod

    async def resolve_thread(*args, **kwargs):
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(assistant_mod, "resolve_live_thread", resolve_thread)
    monkeypatch.setattr(core, "run_chat_pipeline", chat)

    respond = make_pipeline_responder(
        actor="master",
        device_id=None,
        conversation_id=uuid4(),
        synthesizer=_fake_edge_synthesizer(),
    )
    live = LiveSession(session_id=str(uuid4()), respond=respond)
    await live.handle_client({"type": "text", "text": "tell me a joke"})
    for _ in range(200):
        if live._respond_task is not None and live._respond_task.done():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)

    events = []
    while not live.outbound.empty():
        events.append(live.outbound.get_nowait())
    replies = [e for e in events if e.type == "reply"]
    assert replies and "Just chatting." in (replies[-1].text or "")
    live.close()
