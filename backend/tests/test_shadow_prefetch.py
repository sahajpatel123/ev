"""Shadow recall prefetch (voice lag fix).

In shadow mode the provider stays silent (create_response=false) until the
bridge sends response.create, so an unbounded Postgres recall after the final
transcript is dead air + thin-start glitch. The bridge now recalls on partial
transcripts (concurrently, never awaited) and bounds the final wait, falling
back to a bare create (recall_history stays advertised). These tests pin that
contract without touching the frozen supervised path.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.voice.live.grok_voice import GrokVoiceBridge


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _shadow_bridge(monkeypatch) -> tuple[GrokVoiceBridge, _FakeWS]:
    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = _FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
    )
    bridge._ws = ws
    bridge._shadow_mode = True
    bridge._shadow_base_instructions = "You are Evie."
    return bridge, ws


def _creates(ws: _FakeWS) -> list[dict]:
    return [m for m in ws.sent if m.get("type") == "response.create"]


async def _wait_for_create(ws: _FakeWS) -> None:
    for _ in range(200):
        if _creates(ws):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("shadow response.create was not sent")


@pytest.mark.asyncio
async def test_prefetch_hit_reuses_partial_recall(monkeypatch) -> None:
    """Final reuses the partial-triggered recall instead of paying DB twice."""
    bridge, ws = _shadow_bridge(monkeypatch)
    calls: list[str] = []

    async def fake_block(text: str) -> str:
        calls.append(text)
        return "SHADOW MEMORY:\n- [0.91] I picked Postgres for the local store"

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    await bridge._emit_user_transcript("tell me about the local store workflow", final=False)
    await asyncio.sleep(0.05)
    await bridge._emit_user_transcript("tell me about the local store workflow setup", final=True)
    await _wait_for_create(ws)
    creates = _creates(ws)
    assert len(creates) == 1
    assert "Postgres" in creates[0]["response"]["instructions"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pending_prefetch_awaited_within_bound(monkeypatch) -> None:
    """A still-running prefetch is awaited (shielded) instead of re-recalled."""
    bridge, ws = _shadow_bridge(monkeypatch)
    monkeypatch.setattr(settings, "voice_shadow_wait_ms", 2000)
    calls: list[str] = []

    async def slow_block(text: str) -> str:
        calls.append(text)
        await asyncio.sleep(0.3)
        return "SHADOW MEMORY:\n- [0.9] slow note"

    monkeypatch.setattr(bridge, "_build_shadow_block", slow_block)
    await bridge._emit_user_transcript("tell me about the local store workflow", final=False)
    await bridge._emit_user_transcript("tell me about the local store workflow setup", final=True)
    await _wait_for_create(ws)
    creates = _creates(ws)
    assert len(creates) == 1
    assert "slow note" in creates[0]["response"]["instructions"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slow_recall_still_answers_bare_before_timeout(monkeypatch) -> None:
    """Recall slower than the bound degrades to a bare create, never dead air."""
    bridge, ws = _shadow_bridge(monkeypatch)
    monkeypatch.setattr(settings, "voice_shadow_wait_ms", 60)

    async def slow_block(_text: str) -> str:
        await asyncio.sleep(5)
        return "SHADOW MEMORY:\n- [0.9] too late"

    monkeypatch.setattr(bridge, "_build_shadow_block", slow_block)
    await asyncio.wait_for(
        bridge._emit_user_transcript("tell me about the local store setup", final=True),
        timeout=0.2,
    )
    await _wait_for_create(ws)
    creates = _creates(ws)
    assert len(creates) == 1
    assert "response" not in creates[0]


@pytest.mark.asyncio
async def test_diverged_prefetch_is_not_reused(monkeypatch) -> None:
    """A prefetch for another topic never leaks into this turn's instructions."""
    bridge, ws = _shadow_bridge(monkeypatch)
    calls: list[str] = []

    async def fake_block(text: str) -> str:
        calls.append(text)
        return f"SHADOW MEMORY:\n- [0.9] note for {text[:40]}"

    monkeypatch.setattr(bridge, "_build_shadow_block", fake_block)
    await bridge._emit_user_transcript("tell me about the local store workflow", final=False)
    await asyncio.sleep(0.05)
    await bridge._emit_user_transcript("what is the weather forecast for tomorrow", final=True)
    await _wait_for_create(ws)
    creates = _creates(ws)
    assert len(creates) == 1
    assert "weather forecast" in creates[0]["response"]["instructions"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_supervised_partials_spawn_no_prefetch(monkeypatch) -> None:
    """Frozen supervised path: partials never start shadow work."""
    monkeypatch.setattr(settings, "voice_live_mode", "supervised")
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    ws = _FakeWS()

    async def connect(*_a, **_k):
        return ws

    bridge = GrokVoiceBridge(
        on_event=on_event,
        api_key="k",
        provider="openai",
        connect=connect,
    )
    bridge._ws = ws
    bridge._shadow_mode = False
    await bridge._emit_user_transcript("tell me about the local store workflow", final=False)
    assert bridge._shadow_prefetch_task is None


@pytest.mark.asyncio
async def test_prefetch_cancelled_on_bridge_cancel(monkeypatch) -> None:
    """Barge-in teardown drops in-flight recall instead of leaking it."""
    bridge, ws = _shadow_bridge(monkeypatch)

    async def slow_block(_text: str) -> str:
        await asyncio.sleep(5)
        return "SHADOW MEMORY:\n- [0.9] stale"

    monkeypatch.setattr(bridge, "_build_shadow_block", slow_block)
    await bridge._emit_user_transcript("tell me about the local store workflow", final=False)
    assert bridge._shadow_prefetch_task is not None
    await bridge.cancel()
    assert bridge._shadow_prefetch_task is None


@pytest.mark.asyncio
async def test_final_transcript_does_not_await_slow_local_router(monkeypatch) -> None:
    """A 20s local tool route cannot park the sole provider event consumer."""

    route_release = asyncio.Event()

    async def on_event(_event):
        async def route() -> bool:
            await route_release.wait()
            return True

        return asyncio.create_task(route())

    bridge, ws = _shadow_bridge(monkeypatch)
    from app.voice.live.voice_memory import UserAudioTurn

    # Seed the bridge's private open-turn index with the corresponding turn;
    # production creates both together when VAD commits audio.  This keeps
    # the fixture representative instead of relying on an impossible orphan
    # ``_open_turn_id`` value.
    bridge._owner_turns["turn-slow-tool"] = UserAudioTurn(
        local_turn_id="turn-slow-tool",
        audio_committed=True,
        transcription_expected=True,
    )
    bridge._on_event = on_event
    bridge._open_turn_id = "turn-slow-tool"

    await asyncio.wait_for(
        bridge._emit_user_transcript("find the file I edited yesterday", final=True),
        timeout=0.1,
    )
    assert _creates(ws) == []

    route_release.set()
    await asyncio.sleep(0.05)
    # The local router handled the command, so Mini must not open a second
    # overlapping response after the tool completes.
    assert _creates(ws) == []
    assert bridge._shadow_response_for_turn == "turn-slow-tool"


@pytest.mark.asyncio
async def test_unhandled_local_route_releases_one_shadow_response(monkeypatch) -> None:
    """Ordinary speech answers once after the quick local routing decision."""

    async def on_event(_event):
        async def route() -> bool:
            await asyncio.sleep(0)
            return False

        return asyncio.create_task(route())

    bridge, ws = _shadow_bridge(monkeypatch)
    bridge._on_event = on_event
    bridge._open_turn_id = "turn-chat"

    await bridge._emit_user_transcript("how are you doing today", final=True)
    await _wait_for_create(ws)
    assert len(_creates(ws)) == 1
