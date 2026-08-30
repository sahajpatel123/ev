"""Deterministic live-provider acceptance of the four safe required actions."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.ev.capabilities import build_runtime_projection
from app.models import AccessLog, Integration
from app.voice.live.events import FinalTranscriptEvent, HudEvent, ReplyEvent
from app.voice.live.grok_voice import GrokVoiceBridge, approved_live_tool_specs
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner


class LocalRealtimeProvider:
    """A local provider double that records safe wire metadata only."""

    mode = "local_deterministic"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, data: str) -> None:
        body = json.loads(data)
        self.sent.append(body)
        if body.get("type") == "session.update":
            session = body.get("session") or {}
            await self.incoming.put(
                json.dumps(
                    {
                        "type": "session.updated",
                        "session": {
                            "model": session.get("model"),
                            "tools": session.get("tools") or [],
                        },
                    }
                )
            )

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        await self.incoming.put(None)


async def _connect(provider: LocalRealtimeProvider, url: str, additional_headers=None):
    del url, additional_headers
    return provider


async def _wait_for(predicate) -> None:
    for _ in range(150):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


def _output(provider: LocalRealtimeProvider, call_id: str) -> dict:
    item = next(
        body["item"]
        for body in provider.sent
        if body.get("type") == "conversation.item.create"
        and body.get("item", {}).get("type") == "function_call_output"
        and body["item"].get("call_id") == call_id
    )
    return json.loads(item["output"])


def _queued(session: LiveSession) -> list:
    return list(session.outbound._queue)


async def test_live_voice_tool_evidence_chain_for_required_actions(
    db_session, monkeypatch
) -> None:
    """Drive PCM and realtime events through the production live runner."""

    reset_live_registry()
    db_session.add(
        Integration(
            slug="agent4-calendar",
            adapter="calendar",
            name="Agent 4 local calendar",
            scopes=["calendar:read"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()

    async def weather_results(query: str, *, limit: int = 3):
        assert query == "weather in Surat"
        assert limit == 3
        return [
            SimpleNamespace(
                title="Weather in Surat",
                url="https://local.test/weather",
                snippet="Surat: clear and 31 degrees Celsius.",
            )
        ]

    monkeypatch.setattr("app.search.live.weather_results", weather_results)
    projection = await build_runtime_projection(
        db_session,
        actor="voice",
        realtime_provider="openai",
        channel="voice",
    )
    specs = approved_live_tool_specs(
        {"capabilities": projection["live_tool_projection"]}
    )
    required = {"get_weather", "calibrate", "start_timer", "calendar_read"}
    assert required <= {str(spec["name"]) for spec in specs}
    # The live runner opens its own SessionLocal connection for each call.
    await db_session.rollback()

    provider = LocalRealtimeProvider()
    live = LiveSession(
        session_id="agent4-live-chain",
        device_id="agent4-mac",
        backchannel_enabled=False,
    )
    bridge = GrokVoiceBridge(
        on_event=live.emit,
        on_tool=_grok_tool_runner(actor="voice", device_id=None, live=live),
        connect=lambda url, additional_headers=None: _connect(
            provider, url, additional_headers
        ),
        api_key="local-test-key",
        provider="openai",
        now_ms=live.now,
        approved_tool_specs=specs,
    )
    live.grok_voice = bridge
    requests = [
        ("get_weather", "What's the weather in Surat?", {"place": "Surat"}, "weather-1", "Weather checked."),
        ("calibrate", "Please calibrate yourself.", {}, "calibrate-1", "Calibration complete."),
        ("start_timer", "Set a timer for one minute.", {"minutes": 1, "text": "stretch"}, "timer-1", "Timer set."),
        ("calendar_read", "What's on my calendar?", {"limit": 20}, "calendar-1", "No upcoming calendar events."),
    ]
    try:
        assert await bridge.start() is True
        await _wait_for(lambda: bridge.upstream_session_ready)
        update = provider.sent[0]
        assert update["type"] == "session.update"
        assert update["session"]["tool_choice"] == "auto"
        assert {tool["name"] for tool in update["session"]["tools"]} >= required
        assert set(bridge.upstream_tool_names) == {
            tool["name"] for tool in update["session"]["tools"]
        }
        diagnostics = bridge.diagnostics_snapshot()
        assert diagnostics["provider"] == "openai"
        assert diagnostics["upstream_session_ready"] is True
        assert set(diagnostics["advertised_tool_names"]) >= required
        assert set(diagnostics["acknowledged_tool_names"]) >= required
        assert diagnostics["provider_mismatch"] is False
        assert "local-test-key" not in json.dumps(diagnostics)

        for name, natural_request, arguments, call_id, spoken in requests:
            # Owner speaks: PCM reaches the provider before the provider
            # reports its transcript and function choice.
            await bridge.append_pcm(b"\x00\x01" * 800)
            await provider.incoming.put(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": natural_request,
                    }
                )
            )
            await provider.incoming.put(
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "name": name,
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    }
                )
            )
            await _wait_for(
                lambda call_id=call_id: any(
                    body.get("type") == "conversation.item.create"
                    and body.get("item", {}).get("call_id") == call_id
                    for body in provider.sent
                )
            )
            result = _output(provider, call_id)
            assert result["ok"] is True, result
            assert result["name"] == name
            assert result["result"]["evidence"]["source"]
            assert result["result"]["evidence"]["timestamp"]
            assert result["evidence"]
            assert result["spoken"]
            await _wait_for(
                lambda: provider.sent[-1].get("type") == "response.create"
            )
            # Realtime closes the function-call response first.  The
            # continuation response then owns the authoritative spoken text.
            await provider.incoming.put(json.dumps({"type": "response.done"}))
            await _wait_for(lambda: not bridge._tool_boundary_pending)
            await provider.incoming.put(
                json.dumps(
                    {
                        "type": "response.output_audio_transcript.delta",
                        "delta": spoken,
                    }
                )
            )
            await provider.incoming.put(json.dumps({"type": "response.done"}))
            await _wait_for(
                lambda spoken=spoken: any(
                    isinstance(event, ReplyEvent) and event.text == spoken
                    for event in _queued(live)
                )
            )

        sent_audio = [
            body for body in provider.sent if body.get("type") == "input_audio_buffer.append"
        ]
        assert len(sent_audio) == len(requests)
        assert all(body.get("audio") for body in sent_audio)
        events = list(live.outbound._queue)
        assert {event.text for event in events if isinstance(event, FinalTranscriptEvent)} >= {
            item[1] for item in requests
        }
        assert {event.card["meta"]["tool"] for event in events if isinstance(event, HudEvent) and event.kind == "evidence"} >= required
        assert {event.text for event in events if isinstance(event, ReplyEvent)} >= {
            item[4] for item in requests
        }

        await db_session.rollback()
        logs = (
            await db_session.execute(
                select(AccessLog).where(AccessLog.resource_type == "tool")
            )
        ).scalars().all()
        by_name = {
            row.resource_ids[0]: row
            for row in logs
            if row.resource_ids and row.resource_ids[0] in required
        }
        assert set(by_name) == required
        for row in by_name.values():
            assert row.details["status"] == "ok"
            assert row.details["policy_effect"] == "allow"
            assert row.details["channel"] == "voice"
            assert row.details["live_session_id"] == live.session_id
            assert row.details["result"]["evidence"]
    finally:
        bridge.close()
        live.close()
