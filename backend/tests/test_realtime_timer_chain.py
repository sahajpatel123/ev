"""Focused OpenAI Realtime timer function-call chain coverage."""

from __future__ import annotations

import asyncio
import base64
import json

from sqlalchemy import select

from app.models import AccessLog, OwnerTimer
from app.voice.live.events import HudEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.grok_voice import GrokVoiceBridge
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner
from tests.test_gateway_xai import _FakeRealtime


async def _wait_for(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


async def test_openai_realtime_one_minute_timer_completes_full_chain(db_session) -> None:
    reset_live_registry()
    fake = _FakeRealtime()
    session = LiveSession(session_id="timer-chain", device_id="mac", backchannel_enabled=False)
    runner = _grok_tool_runner(actor="voice", device_id=None, live=session)

    from app.ev.tools import get_spec

    bridge = GrokVoiceBridge(
        on_event=session.emit,
        on_tool=runner,
        connect=lambda url, additional_headers=None: _connect(fake, url, additional_headers),
        api_key="test",
        provider="openai",
        now_ms=session.now,
        approved_tool_specs=[get_spec("start_timer")],
    )
    session.grok_voice = bridge

    try:
        await bridge.start()
        injected = next(
            tool for tool in fake.sent[0]["session"]["tools"] if tool["name"] == "start_timer"
        )
        assert injected["type"] == "function"
        assert injected["parameters"]["additionalProperties"] is False
        assert injected["parameters"]["properties"]["minutes"] == {
            "type": "number",
            "minimum": 0,
            "default": None,
        }
        await bridge._handle_upstream(
            {
                "type": "session.updated",
                "session": {"tools": [injected]},
            }
        )
        assert bridge.upstream_session_ready is True
        assert bridge.upstream_tool_names == ("start_timer",)

        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "timer-call-1",
                    "arguments": json.dumps({"minutes": 1, "text": "one minute"}),
                }
            )
        )
        await _wait_for(
            lambda: any(
                item.get("type") == "conversation.item.create"
                and item.get("item", {}).get("type") == "function_call_output"
                for item in fake.sent
            )
        )

        output_item = next(
            item["item"]
            for item in fake.sent
            if item.get("type") == "conversation.item.create"
            and item.get("item", {}).get("type") == "function_call_output"
        )
        assert output_item["call_id"] == "timer-call-1"
        output = json.loads(output_item["output"])
        assert output["ok"] is True
        assert output["name"] == "start_timer"
        assert output["result"]["timer_id"] == output["result"]["id"]
        assert output["result"]["fire_at"]
        assert output["spoken"].startswith("Timer set for ")
        assert output["evidence"]["source"] == "owner_timer"
        assert output["evidence"]["accepted"] is True
        assert output["evidence"]["observed"] is True

        timer_spec = get_spec("start_timer")
        timer_output = timer_spec["output"]
        assert {"id", "timer_id", "fire_at", "spoken", "evidence"} <= set(
            timer_output["properties"]
        )

        output_index = fake.sent.index(next(
            item
            for item in fake.sent
            if item.get("type") == "conversation.item.create"
            and item.get("item", {}).get("type") == "function_call_output"
        ))
        assert fake.sent[output_index + 1]["type"] == "response.create"

        timers = (await db_session.execute(select(OwnerTimer))).scalars().all()
        assert len(timers) == 1
        assert timers[0].status == "pending"
        assert timers[0].payload["minutes"] == 1

        access_logs = (await db_session.execute(select(AccessLog))).scalars().all()
        timer_logs = [
            row
            for row in access_logs
            if row.resource_type == "tool" and "start_timer" in (row.resource_ids or [])
        ]
        assert timer_logs
        assert timer_logs[-1].details["status"] == "ok"
        assert timer_logs[-1].details["policy_effect"] == "allow"
        assert timer_logs[-1].details["channel"] == "voice"

        events = []
        while not session.outbound.empty():
            events.append(session.outbound.get_nowait())
        evidence = [event for event in events if isinstance(event, HudEvent) and event.kind == "evidence"]
        assert evidence
        assert evidence[-1].card["meta"]["source"] == "owner_timer"
        assert evidence[-1].card["meta"]["evidence"]["observed"] is True

        pcm_24k = b"\x00\x01" * 2400
        # The provider closes the function-call response before the
        # continuation response owns the spoken result.
        await fake.incoming.put(json.dumps({"type": "response.done"}))
        await _wait_for(lambda: not bridge._tool_boundary_pending)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio_transcript.delta",
                    "delta": "Timer set for one minute.",
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(pcm_24k).decode("ascii"),
                }
            )
        )
        await fake.incoming.put(json.dumps({"type": "response.done"}))

        def collect_continuation() -> bool:
            events.extend(_drain(session))
            return any(isinstance(event, TtsChunkEvent) for event in events)

        await _wait_for(
            collect_continuation
        )
        audio = next(event for event in events if isinstance(event, TtsChunkEvent))
        assert audio.provider == "openai-realtime"
        assert audio.sample_rate == 16000
        assert base64.b64decode(audio.audio_b64)
        assert any(
            isinstance(event, ReplyEvent) and event.text == "Timer set for one minute."
            for event in events
        )
    finally:
        bridge.close()
        session.close()


async def _connect(fake: _FakeRealtime, url: str, additional_headers=None):
    del url, additional_headers
    return fake


def _drain(session: LiveSession) -> list:
    events = []
    while not session.outbound.empty():
        events.append(session.outbound.get_nowait())
    return events
