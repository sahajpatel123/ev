"""Agent 4's final live-session acceptance gate and Mac preflight contract."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.ev.capabilities import build_runtime_projection
from app.ev.protocols import capability_reply
from app.models import AccessLog, Integration, Memory
from app.voice.live.events import FinalTranscriptEvent, HudEvent, ReplyEvent
from app.voice.live.grok_voice import GrokVoiceBridge, approved_live_tool_specs
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner, serve_live_websocket
from tests.test_agent4_live_chain import LocalRealtimeProvider
from tests.test_voice_live_ws import FakeWebSocket


async def _connect(provider: LocalRealtimeProvider, url: str, additional_headers=None):
    del url, additional_headers
    return provider


async def _wait_for(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


def _events(session: LiveSession) -> list:
    events = []
    while not session.outbound.empty():
        events.append(session.outbound.get_nowait())
    return events


def _function_output(provider: LocalRealtimeProvider, call_id: str) -> dict:
    item = next(
        body["item"]
        for body in provider.sent
        if body.get("type") == "conversation.item.create"
        and body.get("item", {}).get("type") == "function_call_output"
        and body["item"].get("call_id") == call_id
    )
    return json.loads(item["output"])


async def _drive_provider_call(
    provider: LocalRealtimeProvider,
    bridge: GrokVoiceBridge,
    live: LiveSession,
    *,
    transcript: str,
    name: str,
    arguments: dict,
    call_id: str,
    spoken: str,
) -> dict:
    """Drive one owner-speech/provider-function/continuation turn."""

    await bridge.append_pcm(b"\x00\x01" * 800)
    await provider.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": transcript,
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
        lambda: any(
            body.get("type") == "conversation.item.create"
            and body.get("item", {}).get("call_id") == call_id
            for body in provider.sent
        )
    )
    output = _function_output(provider, call_id)
    await _wait_for(lambda: provider.sent[-1].get("type") == "response.create")

    # The first response.done closes the provider's function-call response.
    await provider.incoming.put(json.dumps({"type": "response.done"}))
    await _wait_for(lambda: not bridge._tool_boundary_pending)
    await provider.incoming.put(
        json.dumps({"type": "response.output_audio_transcript.delta", "delta": spoken})
    )
    await provider.incoming.put(json.dumps({"type": "response.done"}))
    await _wait_for(
        lambda: any(
            isinstance(event, ReplyEvent) and event.text == spoken
            for event in live.outbound._queue
        )
    )
    return output


async def test_agent4_live_chain_weather_timer_memory_present_in_order(
    db_session, monkeypatch
) -> None:
    """Acceptance gate: natural speech must not silently fall back to chat."""

    reset_live_registry()
    now = datetime.now(UTC)
    db_session.add(
        Memory(
            memory_type="preference",
            text="The owner prefers deterministic local voice tests.",
            payload={},
            importance=0.9,
            confidence=0.95,
            source_type="explicit",
            privacy_level="normal",
            event_time=now,
            valid_from=now,
            fingerprint="agent4-dogfood-memory".ljust(64, "0"),
        )
    )
    await db_session.commit()

    async def weather_results(query: str, *, limit: int = 3):
        assert query == "weather in Surat"
        assert limit == 3
        return [
            SimpleNamespace(
                title="Weather in Surat",
                url="https://local.test/weather/surat",
                snippet="Surat: clear and 31 degrees Celsius.",
            )
        ]

    async def local_present(**kwargs):
        return {
            "ok": True,
            "opened": True,
            "title": kwargs["title"],
            "body": kwargs["body"],
            "url": "ev://local/present/agent4",
            "via": "agent4_local_fixture",
        }

    monkeypatch.setattr("app.search.live.weather_results", weather_results)
    monkeypatch.setattr("app.notify.presence.open_presence", local_present)

    projection = await build_runtime_projection(
        db_session,
        actor="voice",
        realtime_provider="openai",
        channel="voice",
    )
    specs = approved_live_tool_specs(
        {"capabilities": projection["live_tool_projection"]}
    )
    required = {"get_weather", "start_timer", "search_memory", "present"}
    advertised = {str(spec["name"]) for spec in specs}
    assert required <= advertised, {
        "missing_advertised_tools": sorted(required - advertised),
        "advertised_tools": sorted(advertised),
    }
    # The live runner opens independent sessions. Release the projection
    # transaction before the provider starts making tool calls.
    await db_session.rollback()

    provider = LocalRealtimeProvider()
    live = LiveSession(
        session_id="agent4-dogfood-live",
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
    turns = [
        (
            "What's the weather in Surat?",
            "get_weather",
            {"place": "Surat"},
            "weather-acceptance",
            "Surat is clear and 31 degrees Celsius.",
        ),
        (
            "Set a timer for one minute to stretch.",
            "start_timer",
            {"minutes": 1, "text": "stretch"},
            "timer-acceptance",
            "The timer was created and its ID is timer-acceptance.",
        ),
        (
            "What do I prefer for voice tests?",
            "search_memory",
            {"query": "deterministic local voice tests", "k": 10},
            "memory-acceptance",
            "I found the saved voice-test preference.",
        ),
        (
            "Show me the local acceptance card.",
            "present",
            {"title": "Acceptance", "body": "The local voice chain is running."},
            "present-acceptance",
            "I opened the acceptance card on your Mac.",
        ),
    ]
    outputs: list[dict] = []
    memory_output: dict | None = None
    try:
        assert await bridge.start() is True
        await _wait_for(lambda: bridge.upstream_session_ready)
        ready = live.ready_event().as_dict()["config"]["realtime"]
        assert ready["tool_names"] == list(bridge.advertised_tool_names)
        assert ready["upstream_tool_names"] == list(bridge.upstream_tool_names)
        assert ready["tool_names"] == ready["upstream_tool_names"]
        assert ready["upstream_session_ready"] is True
        assert ready["capability_error"] is None

        for transcript, name, arguments, call_id, spoken in turns:
            outputs.append(
                await _drive_provider_call(
                    provider,
                    bridge,
                    live,
                    transcript=transcript,
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    spoken=spoken,
                )
            )

        assert [item["name"] for item in outputs] == [turn[1] for turn in turns]
        assert len(
            [
                body
                for body in provider.sent
                if body.get("type") == "input_audio_buffer.append"
            ]
        ) == len(turns)
        assert all(
            body.get("audio")
            for body in provider.sent
            if body.get("type") == "input_audio_buffer.append"
        )

        # Weather, timer, and present have the complete advertised -> policy
        # -> adapter -> evidence -> spoken-result chain. Memory is kept as a
        # hard acceptance assertion: a missing evidence object is a real gap,
        # never a reason to claim success or fall back to generic chat.
        for output, turn in zip(outputs, turns, strict=True):
            name = turn[1]
            assert output["ok"] is True, output
            assert output["name"] == name
            assert output["result"]
            assert output["spoken"] or turn[4]
            if name != "search_memory":
                assert output["evidence"], output
                assert output["result"].get("evidence"), output
            else:
                # Keep running the acceptance turn so present, continuation,
                # spoken output, and audit records are still verified before
                # the final hard failure below.
                memory_output = output

        events = _events(live)
        assert [
            event.text for event in events if isinstance(event, FinalTranscriptEvent)
        ] == [turn[0] for turn in turns]
        assert {event.text for event in events if isinstance(event, ReplyEvent)} >= {
            turn[4] for turn in turns
        }
        assert {
            event.card["meta"]["tool"]
            for event in events
            if isinstance(event, HudEvent) and event.kind == "evidence"
        } >= required
    finally:
        bridge.close()
        live.close()

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
    for name, row in by_name.items():
        assert row.details["policy_effect"] == "allow", (name, row.details)
        assert row.details["channel"] == "voice"
        assert row.details["live_session_id"] == live.session_id
    assert memory_output is not None
    assert memory_output["evidence"], (
        "search_memory completed without evidence; this must be fixed "
        "before EV can be called operational",
        memory_output,
    )


async def test_agent4_connected_fixture_exposes_calendar_and_messages_only_when_connected(
    db_session,
) -> None:
    """Calendar/messages are setup-dependent, not default local tools."""

    disconnected = await build_runtime_projection(
        db_session,
        actor="voice",
        realtime_provider="openai",
        channel="voice",
    )
    disconnected_names = {
        str(item["name"]) for item in disconnected["live_tool_projection"]
    }
    assert "calendar_read" not in disconnected_names
    assert "list_messages" not in disconnected_names

    db_session.add_all(
        [
            Integration(
                slug="agent4-calendar-connected",
                adapter="calendar",
                name="Agent 4 calendar fixture",
                scopes=["calendar:read"],
                status="active",
                config={"provider": "local"},
            ),
            Integration(
                slug="agent4-messaging-connected",
                adapter="messaging",
                name="Agent 4 messaging fixture",
                scopes=["message:read"],
                status="active",
                config={"provider": "local"},
            ),
        ]
    )
    await db_session.commit()
    connected = await build_runtime_projection(
        db_session,
        actor="voice",
        realtime_provider="openai",
        channel="voice",
    )
    connected_names = {
        str(item["name"]) for item in connected["live_tool_projection"]
    }
    assert {"calendar_read", "list_messages"} <= connected_names


async def test_agent4_mac_ready_event_is_safe_to_show_owner(db_session) -> None:
    """The actual Mac ready frame must include the completed tool handshake."""

    projection = await build_runtime_projection(
        db_session,
        actor="voice",
        realtime_provider="openai",
        channel="voice",
    )
    specs = approved_live_tool_specs(
        {"capabilities": projection["live_tool_projection"]}
    )
    provider = LocalRealtimeProvider()
    live = LiveSession(
        session_id="agent4-ready-gate",
        device_id="agent4-mac",
        backchannel_enabled=False,
    )
    bridge = GrokVoiceBridge(
        on_event=live.emit,
        connect=lambda url, additional_headers=None: _connect(
            provider, url, additional_headers
        ),
        api_key="local-test-key",
        provider="openai",
        now_ms=live.now,
        approved_tool_specs=specs,
    )
    live.grok_voice = bridge
    ws = FakeWebSocket()
    server = asyncio.create_task(serve_live_websocket(ws, live=live, tick_ms=20))
    try:
        ready = await ws.next_event()
        diagnostics = None
        for _ in range(10):
            event = await ws.next_event()
            if (
                event.get("type") == "realtime_diagnostics"
                and event.get("diagnostics", {}).get("phase")
                == "session.updated.received"
            ):
                diagnostics = event["diagnostics"]
                break
        assert diagnostics is not None, "upstream session acknowledgement was not emitted"

        ready_realtime = ready["config"]["realtime"]
        assert ready_realtime["tool_names"] == ready_realtime["upstream_tool_names"], ready
        assert ready_realtime["upstream_session_ready"] is True, ready
        assert ready_realtime["capability_error"] is None, ready

        # Keep the later diagnostic assertion in the same gate so a transport
        # fix can prove both the initial owner-facing frame and the provider
        # acknowledgement without changing this fixture.
        assert diagnostics["tool_names"] == diagnostics["upstream_tool_names"]
        assert diagnostics["upstream_session_ready"] is True
        assert diagnostics["capability_error"] is None
    finally:
        await ws.put_disconnect()
        await asyncio.wait_for(server, timeout=5.0)


async def test_agent4_capability_speech_has_no_internal_runtime_jargon(db_session) -> None:
    """Owner-facing capability speech must use plain language."""

    async def load_capabilities(**kwargs):
        return await capability_reply(
            db_session,
            actor="voice",
            realtime_provider="openai",
            channel="voice",
            **kwargs,
        )

    live = LiveSession(
        session_id="agent4-speech-language",
        capability_reply=load_capabilities,
        backchannel_enabled=False,
    )
    try:
        await live.handle_client(
            {"type": "text", "text": "what can you do", "commit": True}
        )
        replies = [event.text for event in _events(live) if isinstance(event, ReplyEvent)]
        assert replies
        spoken = " ".join(replies).lower()
        assert "manifest" not in spoken
        assert "runtime execution" not in spoken
    finally:
        live.close()
