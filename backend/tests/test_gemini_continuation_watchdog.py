"""Continuation watchdog: a tool turn awaiting its spoken reply must never
leave the phone on Thinking forever.

When the provider never answers a tool continuation, _on_turn_complete used
to reset and return in silence. The watchdog converts that silence into one
visible, non-fatal turn_incomplete error. Fully offline: fake socket.
"""

from __future__ import annotations

import asyncio
import json


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


def _bridge(**kwargs):
    from app.voice.live.gemini_live import GeminiLiveBridge

    events: list = []
    fake = _FakeWS()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_event(event):
        events.append(event)

    bridge = GeminiLiveBridge(
        on_event=on_event,
        connect=connect,
        api_key="test-key",
        provider="gemini",
        **kwargs,
    )
    bridge._continuation_watchdog_timeout_s = 0.05
    return bridge, fake, events


def _awaiting_state(bridge) -> None:
    """Reproduce the tool-boundary state: continuation sent, no content yet."""
    bridge._tool_boundary_pending = True
    bridge._continuation_sent = True
    bridge._reply_text = ""
    bridge._turn_audio_chunks = 0
    bridge._pending_tools = 0
    bridge._scheduled_tool_calls = set()


async def test_stalled_continuation_emits_visible_error() -> None:
    bridge, _fake, events = _bridge()
    try:
        _awaiting_state(bridge)
        await bridge._on_turn_complete({})
        await asyncio.sleep(0.3)
        errors = [e for e in events if e.type == "error"]
        assert errors, "stalled continuation stayed silent"
        assert errors[0].code == "turn_incomplete"
        assert not errors[0].fatal
        assert errors[0].message, "stall error must carry a human caption"
    finally:
        bridge.close()


async def test_healthy_continuation_stays_quiet() -> None:
    bridge, _fake, events = _bridge()
    try:
        _awaiting_state(bridge)
        await bridge._on_turn_complete({})
        # Content lands before the watchdog fires: audio chunk arrives.
        bridge._turn_audio_chunks = 4
        await asyncio.sleep(0.3)
        codes = [e.code for e in events if e.type == "error"]
        assert "turn_incomplete" not in codes, f"false stall alarm: {codes}"
    finally:
        bridge.close()


async def test_continuation_reply_still_emits_and_disarms() -> None:
    bridge, _fake, events = _bridge()
    try:
        _awaiting_state(bridge)
        bridge._reply_text = "hello there"
        await bridge._on_turn_complete({})
        replies = [e for e in events if e.type == "reply"]
        assert replies and replies[0].text == "hello there"
        await asyncio.sleep(0.3)
        codes = [e.code for e in events if e.type == "error"]
        assert "turn_incomplete" not in codes, f"false stall alarm: {codes}"
    finally:
        bridge.close()
