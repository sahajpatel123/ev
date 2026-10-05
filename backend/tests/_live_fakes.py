"""Shared Gemini Live bridge fakes for voice-session tests."""

from __future__ import annotations

import asyncio
import json


class _FakeRealtime:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

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
        await self.incoming.put(None)


async def _acknowledge_session(bridge, fake: _FakeRealtime) -> None:
    """Deliver the Live API setupComplete acknowledgement (no tool echo)."""

    assert "setup" in fake.sent[0]
    await bridge._handle_upstream({"setupComplete": {}})
