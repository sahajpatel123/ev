"""Stuck-Thinking recovery: a suppressed live turn must tell the phone.

When a response task is still in flight, the next turn used to be dropped
silently after its transcript was already on screen — the client sat on
"Thinking" forever. It must now emit a visible turn_suppressed error.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4


async def test_suppressed_turn_emits_visible_error() -> None:
    from app.voice.live.session import LiveSession

    live = LiveSession(session_id=str(uuid4()))
    pending = asyncio.create_task(asyncio.sleep(30), name="ev-test-hung-respond")
    live._respond_task = pending
    try:
        tick = SimpleNamespace(
            decision=SimpleNamespace(reason="test", last_partial="hello"),
            envelope=None,
        )
        await live._start_respond(tick)
        events = []
        while not live.outbound.empty():
            events.append(live.outbound.get_nowait())
        errors = [e for e in events if e.type == "error"]
        assert errors, f"suppressed turn stayed silent; events={[e.type for e in events]}"
        assert errors[0].code == "turn_suppressed"
        assert not errors[0].fatal
        assert errors[0].message, "suppression error must carry a human caption"
    finally:
        pending.cancel()
        live.close()


async def test_idle_session_still_starts_respond_task() -> None:
    """The suppression branch must not fire when nothing is in flight."""

    from app.voice.live.session import LiveSession

    async def quick_respond(text, envelope):
        return []

    live = LiveSession(session_id=str(uuid4()), respond=quick_respond)
    try:
        tick = SimpleNamespace(
            decision=SimpleNamespace(reason="test", last_partial="hello there"),
            envelope=None,
        )
        await live._start_respond(tick)
        assert live._respond_task is not None
        await asyncio.wait_for(asyncio.shield(live._respond_task), timeout=10)
        events = []
        while not live.outbound.empty():
            events.append(live.outbound.get_nowait())
        codes = [e.code for e in events if e.type == "error"]
        assert "turn_suppressed" not in codes, f"false suppression: {codes}"
    finally:
        live.close()
