"""Reconciliation regressions — P0 2026-08-22 follow-up.

A. UPSTREAM SOCKET LIFECYCLE: every abandoned upstream session owns a
   deterministic close; stale-socket cleanup never touches the live socket.
B. ZERO-AUDIO TRUNCATION: no assistant audio ever generated+delivered
   → NO conversation.item.truncate is sent (provider rejects that shape as
   missing_required_parameter — proven in the 2026-08-22 incident traces).
   Generated-but-partially-delivered audio still truncates at the delivered
   boundary.
C. QUOTA NOTIFICATION LATCH: one truthful realtime_quota per quota episode;
   bounded 60 s retry floor.
"""
from __future__ import annotations

import asyncio

from app.voice.live.grok_voice import GrokVoiceBridge


class FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []
        self.close_calls = 0

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _bridge() -> GrokVoiceBridge:
    events: list = []

    async def on_event(event) -> None:
        events.append(event)

    return GrokVoiceBridge(on_event=on_event, api_key="k", provider="openai")


def test_stale_socket_closed_live_socket_untouched() -> None:
    async def run() -> None:
        b = _bridge()
        live = FakeWS()
        stale = FakeWS()
        b._ws = live
        exc = ConnectionError("boom")
        await b._note_disconnect(exc, ws=stale)  # type: ignore[arg-type]
        assert stale.close_calls == 1, "stale socket must be deterministically closed"
        assert b._ws is live, "live socket ownership must be untouched by stale cleanup"
        assert not live.closed
        if b._reconnect_task is not None:
            # Stale notifications must not have scheduled a recovery cycle.
            assert b._reconnect_task.done() or b._reconnect_task.cancelled()
            b._reconnect_task.cancel()

    asyncio.run(run())


def test_abandoned_current_socket_is_closed_and_forgotten() -> None:
    async def run() -> None:
        b = _bridge()
        current = FakeWS()
        b._ws = current
        await b._abandon_upstream_ws()
        assert current.close_calls == 1
        assert b._ws is None

    asyncio.run(run())


def test_note_disconnect_closes_current_without_leak() -> None:
    async def run() -> None:
        b = _bridge()
        ws = FakeWS()
        b._ws = ws
        task = asyncio.create_task(
            b._note_disconnect(ConnectionError("stream closed"))  # type: ignore[arg-type]
        )
        await asyncio.wait_for(task, 5)
        assert ws.close_calls == 1, "every abandoned upstream session owns a close"
        assert b._ws is None
        rt = b._reconnect_task
        assert rt is not None and not rt.done()
        rt.cancel()

    asyncio.run(run())


def test_zero_audio_truncate_suppressed() -> None:
    async def run() -> None:
        b = _bridge()
        ws = FakeWS()
        b._ws = ws
        b._assistant_item_id = "item-1"
        b._turn_audio_bytes = 0
        await b._truncate_assistant_item(0)
        assert ws.sent == [], "no delivered assistant audio → no truncation event"

    asyncio.run(run())


def test_generated_audio_truncates_at_delivered_boundary() -> None:
    async def run() -> None:
        import json

        b = _bridge()
        ws = FakeWS()
        b._ws = ws
        b._assistant_item_id = "item-2"
        b._turn_audio_bytes = 48_000
        await b._truncate_assistant_item(250)
        assert len(ws.sent) == 1
        payload = json.loads(ws.sent[0])
        assert payload["type"] == "conversation.item.truncate"
        assert payload["item_id"] == "item-2"
        assert payload["audio_end_ms"] == 250

    asyncio.run(run())


def test_truncate_clamps_stale_client_playback_duration() -> None:
    async def run() -> None:
        import json

        b = _bridge()
        ws = FakeWS()
        b._ws = ws
        b._assistant_item_id = "item-stale-clock"
        # 512,000 bytes = 16,000 ms of 16 kHz mono PCM.
        b._turn_audio_bytes = 512_000
        await b._truncate_assistant_item(97_079)
        payload = json.loads(ws.sent[0])
        assert payload["audio_end_ms"] == 16_000

    asyncio.run(run())


def test_quota_notifies_once_and_floors_backoff() -> None:
    async def run() -> None:
        events: list = []

        async def on_event(event) -> None:
            events.append(event)

        b = GrokVoiceBridge(on_event=on_event, api_key="k", provider="openai")
        ws = FakeWS()
        b._ws = ws
        for _ in range(3):
            await b._note_quota_block("insufficient_quota.organization_spend_limit_exceeded")
        quota_events = [e for e in events if getattr(e, "code", "") == "realtime_quota"]
        assert len(quota_events) == 1, "one truthful notification per quota episode"
        assert b._reconnect_delay >= 60.0, "bounded retry while blocked"
        assert ws.close_calls == 1 and b._ws is None, "upstream resource released once"
        rt = b._reconnect_task
        assert rt is not None and not rt.done()
        rt.cancel()

    asyncio.run(run())
