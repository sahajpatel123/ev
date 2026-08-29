"""Stay-alive + ChatGPT-style live loop: voice must not terminate EV.app.

Drives the shipped Swift sources and the live session/pipeline entry points.
A fatal live event, Talk, or a closed menu panel must not quit the process.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from app.voice.live.session import LiveSession
from app.voice.live.transport import make_pipeline_responder
from app.voice.pipeline import stream_chat_tts_pipeline

REPO = Path(__file__).resolve().parents[2]
MACOS_EV = REPO / "macos" / "Sources" / "EV"
CLIENT = REPO / "ios" / "EVClient" / "Sources" / "EVClient"


def _read(*parts: str) -> str:
    return Path(*parts).read_text(encoding="utf-8")


def test_last_window_closed_does_not_quit_the_app() -> None:
    evapp = _read(MACOS_EV / "EVApp.swift")
    lifecycle = _read(MACOS_EV / "AppLifecycle.swift")
    assert "applicationShouldTerminateAfterLastWindowClosed" in evapp
    snippet = evapp.split("applicationShouldTerminateAfterLastWindowClosed", 1)[1][:400]
    assert "TerminatePolicy.shouldTerminateAfterLastWindowClosed" in snippet
    assert "isQuitting" in lifecycle
    assert "TerminatePolicy.reply()" in evapp
    # Voice must not flip the quit latch — only AppLifecycle.quit() does.
    assert "TerminatePolicy.markExplicitQuit()" in lifecycle
    assert lifecycle.count("TerminatePolicy.markExplicitQuit()") == 1
    # ⌘Q / app menu must set the latch. NSApplication.terminate skips it
    # and applicationShouldTerminate then cancel-quits forever.
    menu = evapp.split("func installAppMenu", 1)[1].split("func ", 1)[0]
    assert "NSApplication.terminate" not in menu
    assert "quitFromMenu" in menu
    assert "AppLifecycle.quit()" in evapp


def test_voice_paths_never_call_app_quit() -> None:
    live = _read(MACOS_EV / "LiveConversation.swift")
    model = _read(MACOS_EV / "AppModel.swift")
    voice_client = _read(CLIENT / "LiveVoice.swift")
    for source, name in (
        (live, "LiveConversation.swift"),
        (model, "AppModel.swift"),
        (voice_client, "LiveVoice.swift"),
    ):
        assert "AppLifecycle.quit" not in source, name
        assert "NSApp.terminate" not in source, name
        assert "exit(" not in source, name
    assert "event.fatal" in live
    assert "never a process quit" in live or "Fatal is a channel close" in live
    # Talk must not start a second audio engine on top of live.
    assert "live.isRunning" in model
    assert "setVoiceProcessingEnabled" not in voice_client
    # OWNER LAW (no PTT): opening the app starts the audio — the safe startup
    # sequence itself must bring the live session up, and start() must invoke it.
    startup = model.split("private func runSafeStartup()", 1)[1].split("private ", 1)[0]
    assert "live.start()" in startup
    start_body = model.split("func start()", 1)[1].split("func ", 1)[0]
    assert "runSafeStartup()" in start_body
    assert "startRecording" not in model and "stopAndSend" not in model, (
        "push-to-talk clip capture is removed from the product"
    )
    run_loop = live.split("func runLoop()", 1)[1].split("func ", 1)[0]
    assert "defer" in run_loop
    defer_block = run_loop.split("defer", 1)[1]
    assert "loopTask = nil" in defer_block.split("}", 1)[0]


def test_live_open_exists_without_wake_word() -> None:
    from app.api.voice import router

    paths = [getattr(route, "path", "") for route in router.routes]
    assert "/v1/voice/live/open" in paths
    assert "/v1/voice/live" in paths
    transport = inspect.getsource(make_pipeline_responder)
    assert "skip_listen_ack=True" in transport
    assert "skip_status_filler=True" in transport


def test_pipeline_signature_streams_first_chunk_without_status_filler() -> None:
    source = inspect.getsource(stream_chat_tts_pipeline)
    assert "skip_status_filler" in source
    assert 'yield ("tts_chunk"' in source
    assert "wait for a complete answer" not in source.lower() or "tts_chunk" in source


async def test_live_session_does_not_wait_a_second_for_final_asr() -> None:
    """Turn authorization must not sit on a 1200 ms ASR final."""

    waits: list[int | None] = []

    class FastPartialFeed:
        async def final_text(self, *, timeout_ms: int | None = None) -> str | None:
            waits.append(timeout_ms)
            return None

        def abort(self) -> None:
            return None

    heard: list[str] = []

    async def respond(text: str, envelope):
        heard.append(text)
        from app.voice.live.events import ReplyEvent, TtsChunkEvent

        yield TtsChunkEvent(at_ms=1, index=0, text="On it.", audio_b64="QUE=")
        yield ReplyEvent(at_ms=2, text="On it.")

    session = LiveSession(respond=respond, backchannel_enabled=False)
    session.asr_feed = FastPartialFeed()
    await session.handle_client(
        {"type": "text", "text": "What's the weather?", "commit": True}
    )
    if session._respond_task is not None:
        await session._respond_task
    assert waits, "live session must consult the ASR feed"
    assert waits[0] is not None
    assert waits[0] <= 200, waits
    assert heard == ["What's the weather?"]
    kinds = []
    while not session.outbound.empty():
        kinds.append(session.outbound.get_nowait().type)
    assert "tts_chunk" in kinds
    session.close()


async def test_fatal_live_event_does_not_request_process_exit() -> None:
    """A fatal channel event must close the socket, not the host process."""

    session = LiveSession(backchannel_enabled=False)
    await session.handle_client({"type": "control", "action": "end"})
    events = []
    while not session.outbound.empty():
        events.append(session.outbound.get_nowait())
    assert any(getattr(event, "fatal", False) for event in events)
    assert session._closed is True
    source = inspect.getsource(LiveSession._end_sleep) + inspect.getsource(
        LiveSession.close
    )
    assert "os._exit" not in source
    assert "sys.exit" not in source


async def test_deep_work_filler_does_not_block_client_frames() -> None:
    """Background intelligence speaks a filler; user speech still barges in."""

    from app.voice.live.delegate import needs_deep_work, thinking_filler
    from app.voice.live.events import BargeInEvent

    text = "Why did the market crash today after the announcement"
    assert needs_deep_work(text)
    assert thinking_filler(text)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str, envelope):
        started.set()
        try:
            await asyncio.sleep(30)
            yield None
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session = LiveSession(respond=slow, backchannel_enabled=False)
    await session.handle_client({"type": "text", "text": text, "commit": True})
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.handle_client({"type": "speech", "active": True})
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    events = []
    while not session.outbound.empty():
        events.append(session.outbound.get_nowait())
    assert any(isinstance(event, BargeInEvent) for event in events)
    session.close()
