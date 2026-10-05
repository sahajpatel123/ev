"""TURN AUTHORITY V2 canary regressions.

LAW UNDER TEST: VAD is a sensor, not conversation authority.
- owner turn finalizes + bounded grace with NO continuation → exactly ONE
  turn commit recorded for the logical owner turn (idempotent).
- fresh input text inside the grace window → commit cancelled; floor stays
  OWNER; nothing recorded.
- The commit is bookkeeping only: Gemini answers audio turns via server
  VAD on its own, so V2 never sends an explicit turn.
"""
from __future__ import annotations

import asyncio
import json

from app.voice.live.gemini_live import GeminiLiveBridge, gemini_live_setup


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _v2_bridge(grace: float = 0.15) -> tuple[GeminiLiveBridge, FakeWS, list]:
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GeminiLiveBridge(
        on_event=on_event,
        api_key="k",
        provider="gemini",
        connect=connect,
        turn_authority_v2=True,
        turn_commit_grace_s=grace,
    )
    bridge._ws = ws
    return bridge, ws, events


async def _fire(bridge, event) -> None:
    await bridge._handle_upstream(event)


def _input_text(text: str) -> dict:
    return {"serverContent": {"inputTranscription": {"text": text}}}


_TURN_COMPLETE = {"serverContent": {"turnComplete": True}}


def test_v2_continuation_cancels_commit_and_single_commit_on_final_yield() -> None:
    async def run() -> None:
        bridge, ws, _ = _v2_bridge(grace=0.12)
        # owner speaks, then the turn finalizes → grace scheduled…
        await _fire(bridge, _input_text("set a timer"))
        await _fire(bridge, dict(_TURN_COMPLETE))
        await asyncio.sleep(0.04)
        # …owner CONTINUES inside the grace window → commit must cancel.
        await _fire(bridge, _input_text("set a timer for tea"))
        await asyncio.sleep(0.2)
        assert bridge._v2_response_created_for_turn is None, "continuation must not commit"
        assert ws.sent == [], "the commit path sends nothing upstream"
        # …finally stops for real → exactly ONE commit after grace.
        await _fire(bridge, dict(_TURN_COMPLETE))
        await asyncio.sleep(0.3)
        assert bridge._v2_response_created_for_turn == bridge._open_turn_id
        assert ws.sent == []

    asyncio.run(run())


def test_v2_idempotent_duplicate_turn_complete() -> None:
    async def run() -> None:
        bridge, ws, _ = _v2_bridge(grace=0.1)
        await _fire(bridge, _input_text("hello"))
        for _ in range(4):
            await _fire(bridge, dict(_TURN_COMPLETE))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.25)
        assert bridge._v2_response_created_for_turn == bridge._open_turn_id
        assert bridge._open_turn_id is not None
        assert ws.sent == []

    asyncio.run(run())


def test_v2_off_never_schedules_commits() -> None:
    async def run() -> None:
        events: list = []

        async def on_event(event) -> None:
            events.append(event)

        ws = FakeWS()

        async def connect(*_a, **_k):
            return ws

        bridge = GeminiLiveBridge(
            on_event=on_event, api_key="k", provider="gemini", connect=connect
        )
        bridge._ws = ws
        await _fire(bridge, _input_text("hello"))
        await _fire(bridge, dict(_TURN_COMPLETE))
        await asyncio.sleep(0.05)
        assert bridge._v2_pending_commit is None
        assert bridge._v2_response_created_for_turn is None
        assert ws.sent == []

    asyncio.run(run())


def test_v2_flag_does_not_change_setup_vad() -> None:
    # V2 is bridge-side turn bookkeeping now: server VAD always answers, so
    # the flag must not add any VAD override to the setup message.
    v2 = gemini_live_setup(provider="gemini", turn_authority_v2=True)["setup"]
    assert "realtimeInputConfig" not in v2
    baseline = gemini_live_setup(provider="gemini")["setup"]
    assert "realtimeInputConfig" not in baseline
