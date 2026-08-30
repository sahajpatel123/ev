"""TURN AUTHORITY V2 canary regressions.

LAW UNDER TEST: VAD is a sensor, not conversation authority.
- speech_stopped + bounded grace with NO continuation → exactly ONE explicit
  response.create per logical owner turn (idempotent).
- speech restarting inside the grace window → commit cancelled; floor stays
  OWNER; NO response.create.
- session.update carries create_response=False only when V2 is enabled.
"""
from __future__ import annotations

import asyncio
import json

from app.voice.live.grok_voice import GrokVoiceBridge, grok_session_update


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _v2_bridge(grace: float = 0.15) -> tuple[GrokVoiceBridge, FakeWS, list]:
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
        turn_authority_v2=True,
        turn_commit_grace_s=grace,
    )
    bridge._ws = ws
    return bridge, ws, events


def _speech(bridge, started: bool) -> None:
    kind = "input_audio_buffer.speech_started" if started else "input_audio_buffer.speech_stopped"
    asyncio.get_event_loop().run_until_complete if False else None
    asyncio.run(_fire(bridge, {"type": kind}))


async def _fire(bridge, event) -> None:
    await bridge._handle_upstream(event)


def test_v2_continuation_cancels_commit_and_single_create_on_final_yield() -> None:
    async def run() -> None:
        bridge, ws, _ = _v2_bridge(grace=0.12)
        # owner speaks…
        await _fire(bridge, {"type": "input_audio_buffer.speech_started"})
        # …brief pause → grace scheduled…
        await _fire(bridge, {"type": "input_audio_buffer.speech_stopped"})
        await asyncio.sleep(0.04)
        # …owner CONTINUES inside the grace window → commit must cancel.
        await _fire(bridge, {"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.2)
        assert not any(m.get("type") == "response.create" for m in ws.sent), "continuation must not answer"
        # …finally stops for real → exactly ONE response.create after grace.
        await _fire(bridge, {"type": "input_audio_buffer.speech_stopped"})
        await asyncio.sleep(0.3)
        creates = [m for m in ws.sent if m.get("type") == "response.create"]
        assert len(creates) == 1, f"expected one response.create, got {len(creates)}"
        assert bridge._v2_response_created_for_turn == bridge._open_turn_id

    asyncio.run(run())


def test_v2_idempotent_duplicate_speech_stopped() -> None:
    async def run() -> None:
        bridge, ws, _ = _v2_bridge(grace=0.1)
        await _fire(bridge, {"type": "input_audio_buffer.speech_started"})
        for _ in range(4):
            await _fire(bridge, {"type": "input_audio_buffer.speech_stopped"})
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.25)
        creates = [m for m in ws.sent if m.get("type") == "response.create"]
        assert len(creates) == 1, f"duplicate stops must not duplicate responses: {len(creates)}"

    asyncio.run(run())


def test_v2_off_never_schedules_commits() -> None:
    async def run() -> None:
        events: list = []

        async def on_event(event) -> None:
            events.append(event)

        ws = FakeWS()

        async def connect(*_a, **_k):
            return ws

        bridge = GrokVoiceBridge(
            on_event=on_event, api_key="k", provider="openai", connect=connect
        )
        bridge._ws = ws
        await _fire(bridge, {"type": "input_audio_buffer.speech_stopped"})
        await asyncio.sleep(0.05)
        assert bridge._v2_pending_commit is None
        assert not any(m.get("type") == "response.create" for m in ws.sent)

    asyncio.run(run())


def test_session_update_carries_v2_turn_detection() -> None:
    v2 = grok_session_update(provider="openai", turn_authority_v2=True)
    td = v2["session"]["audio"]["input"]["turn_detection"]
    assert td["create_response"] is False
    assert td["interrupt_response"] is False
    assert td["type"] == "server_vad"

    baseline = grok_session_update(provider="openai")
    td_base = baseline["session"]["audio"]["input"]["turn_detection"]
    assert td_base["create_response"] is True
