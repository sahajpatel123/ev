"""Live pipeline turn reproduction: text turn → TTS chunks out.

Drives the REAL LiveSession message path with the REAL make_pipeline_responder
(mocks only the chat pipeline + DB thread) and a real Edge-shaped synthesizer
(MP3 bytes). Asserts the session emits decodable live PCM and a reply.
"""

from __future__ import annotations

import asyncio
import base64
import io
import struct
import subprocess
import wave
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
                    model="muse-spark-1.3-contributor",
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
