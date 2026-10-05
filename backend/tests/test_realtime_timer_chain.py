"""Focused Gemini Live timer function-call chain coverage."""

from __future__ import annotations

import asyncio
import base64
import json

from sqlalchemy import select

from app.models import AccessLog, OwnerTimer
from app.voice.live.events import HudEvent, ReplyEvent, TtsChunkEvent
from app.voice.live.gemini_live import GeminiLiveBridge
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession
from app.voice.live.transport import _live_tool_runner
from tests._live_fakes import _FakeRealtime


async def _wait_for(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


async def test_gemini_live_one_minute_timer_completes_full_chain(db_session) -> None:
    reset_live_registry()
    fake = _FakeRealtime()
    session = LiveSession(session_id="timer-chain", device_id="mac", backchannel_enabled=False)
    runner = _live_tool_runner(actor="voice", device_id=None, live=session)

    from app.ev.tools import get_spec

    bridge = GeminiLiveBridge(
        on_event=session.emit,
        on_tool=runner,
        connect=lambda url, additional_headers=None: _connect(fake, url, additional_headers),
        api_key="test",
        provider="gemini",
        now_ms=session.now,
        approved_tool_specs=[get_spec("start_timer")],
    )
    session.gemini_live = bridge

    def function_responses():
        return [
            entry
            for item in fake.sent
            for entry in item.get("toolResponse", {}).get("functionResponses", [])
        ]

    try:
        await bridge.start()
        declarations = fake.sent[0]["setup"]["tools"][0]["functionDeclarations"]
        injected = next(
            tool for tool in declarations if tool["name"] == "start_timer"
        )
        assert injected["behavior"] == "NON_BLOCKING"
        assert injected["parameters"]["properties"]["minutes"] == {
            "type": "number",
            "minimum": 0,
            "default": None,
        }
        await bridge._handle_upstream({"setupComplete": {}})
        assert bridge.upstream_session_ready is True
        assert bridge.upstream_tool_names == ("start_timer",)

        await fake.incoming.put(
            json.dumps(
                {
                    "toolCall": {
                        "functionCalls": [
                            {
                                "id": "timer-call-1",
                                "name": "start_timer",
                                "args": {"minutes": 1, "text": "one minute"},
                            }
                        ]
                    }
                }
            )
        )
        await _wait_for(lambda: len(function_responses()) > 0)

        output_item = next(
            entry for entry in function_responses() if entry.get("id") == "timer-call-1"
        )
        assert output_item["name"] == "start_timer"
        output = output_item["response"]
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

        output_index = next(
            index for index, item in enumerate(fake.sent) if "toolResponse" in item
        )
        # The Live API continues implicitly after a toolResponse: no explicit
        # continuation turn may follow the FunctionResponse.
        assert not any("clientContent" in item for item in fake.sent[output_index + 1 :])

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
        # The provider closes the function-call turn before the implicit
        # continuation owns the spoken result.
        await fake.incoming.put(json.dumps({"serverContent": {"turnComplete": True}}))
        await _wait_for(lambda: not bridge._tool_boundary_pending)
        await fake.incoming.put(
            json.dumps(
                {
                    "serverContent": {
                        "outputTranscription": {"text": "Timer set for one minute."}
                    }
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "serverContent": {
                        "modelTurn": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/pcm;rate=24000",
                                        "data": base64.b64encode(pcm_24k).decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                }
            )
        )
        await fake.incoming.put(json.dumps({"serverContent": {"turnComplete": True}}))

        def collect_continuation() -> bool:
            events.extend(_drain(session))
            return any(isinstance(event, TtsChunkEvent) for event in events)

        await _wait_for(
            collect_continuation
        )
        audio = next(event for event in events if isinstance(event, TtsChunkEvent))
        assert audio.provider == "gemini-live"
        # Gemini Live emits native 24 kHz PCM; the Mac player performs
        # the single high-quality conversion to its hardware rate.
        assert audio.sample_rate == 24000
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
