"""Official xAI provider: Grok 4.6 for chat, Grok Voice for live."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import httpx
from fastapi import FastAPI, Request

from app.config import Settings, settings
from app.contracts import ChatMessage
from app.gateway.providers import XAIProvider, get_chat_provider
from app.voice.live.events import (
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    PartialTranscriptEvent,
    ReplyEvent,
    TtsChunkEvent,
)
from app.voice.live.grok_voice import (
    GrokVoiceBridge,
    approved_live_tool_specs,
    grok_session_update,
    grok_voice_enabled,
    grok_voice_tools,
    grok_voice_url,
    live_realtime_provider,
    openai_realtime_url,
    resample_pcm16,
)
from app.voice.live.session import LiveSession


def test_xai_settings_pin_grok_46_and_voice_think_fast() -> None:
    assert Settings.model_fields["xai_model"].default == "grok-4.6"
    assert Settings.model_fields["xai_voice_model"].default == "grok-voice-think-fast-2.0"
    assert Settings.model_fields["xai_voice_voice"].default == "eve"
    assert Settings.model_fields["voice_live_brain"].default == "auto"
    assert Settings.model_fields["openai_realtime_model"].default == "gpt-realtime-2.1-mini"
    assert Settings.model_fields["openai_realtime_voice"].default == "marin"
    assert Settings.model_fields["xai_voice_vad_threshold"].default == 0.72
    assert Settings.model_fields["xai_voice_silence_ms"].default == 550


def test_xai_provider_is_registered_and_has_tools() -> None:
    provider = XAIProvider(
        base_url="https://api.x.ai/v1",
        api_key="test",
        default_model="grok-4.6",
    )
    assert provider.name == "xai"
    assert provider.supports_tools is True
    assert provider._thinking_payload() is None


def test_xai_factory_uses_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "xai_api_key", "test-key")
    monkeypatch.setattr(settings, "xai_model", "grok-4.6")
    provider = get_chat_provider()
    assert provider.name == "xai"
    assert provider.default_model == "grok-4.6"


def test_grok_voice_enabled_follows_xai_key_not_typed_provider(monkeypatch) -> None:
    """Live Grok Voice is independent of EV_CHAT_PROVIDER (DeepSeek typed chat)."""

    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "xai_api_key", "k")
    assert grok_voice_enabled() is True
    monkeypatch.setattr(settings, "chat_provider", "echo")
    assert grok_voice_enabled() is True
    monkeypatch.setattr(settings, "xai_api_key", "")
    assert grok_voice_enabled() is False
    monkeypatch.setattr(settings, "xai_api_key", "k")
    monkeypatch.setattr(settings, "voice_live_brain", "xai")
    assert grok_voice_enabled() is True
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    assert grok_voice_enabled() is False
    monkeypatch.setattr(settings, "xai_api_key", "")
    monkeypatch.setattr(settings, "voice_live_brain", "xai")
    assert grok_voice_enabled() is False


def test_grok_voice_url_pins_think_fast_2() -> None:
    url = grok_voice_url(
        model="grok-voice-think-fast-2.0",
        realtime_url="wss://api.x.ai/v1/realtime",
    )
    assert url.startswith("wss://api.x.ai/v1/realtime?")
    assert "grok-voice-think-fast-2.0" in url


def test_grok_session_update_is_16k_pcm_with_server_vad() -> None:
    body = grok_session_update(
        provider="xai",
        approved_tools=[
            {"name": "search_memory", "parameters": {"type": "object"}},
            {"name": "set_reminder", "parameters": {"type": "object"}},
        ],
        capability_manifest={
            "capabilities": [
                {
                    "name": "search_web",
                    "availability": "available",
                    "model_exposed": True,
                    "realtime_eligible": True,
                }
            ]
        },
    )
    assert body["type"] == "session.update"
    session = body["session"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert session["turn_detection"]["type"] == "server_vad"
    assert session["voice"]
    names = {tool.get("name") for tool in session["tools"] if tool.get("type") == "function"}
    types = {tool["type"] for tool in session["tools"]}
    assert "web_search" in types
    assert "search_memory" in names
    assert "set_reminder" in names
    assert "search_web" not in names


def test_xai_session_does_not_expose_provider_search_without_ev_search() -> None:
    body = grok_session_update(
        provider="xai",
        approved_tools=[],
        capability_manifest={
            "capabilities": [
                {
                    "name": "search_web",
                    "availability": "not_connected",
                    "model_exposed": True,
                    "realtime_eligible": True,
                }
            ]
        },
    )
    session = body["session"]
    assert session["tools"] == []
    assert session["tool_choice"] == "none"


def test_xai_session_keeps_ev_daily_functions_alongside_provider_search() -> None:
    from app.ev.tools import get_spec

    names = {
        "search_web",
        "calendar_add",
        "list_protocols",
        "get_health_trends",
        "get_gear_status",
        "brief_me",
    }
    approved = [get_spec(name) for name in sorted(names)]
    assert all(spec is not None for spec in approved)
    body = grok_session_update(
        provider="xai",
        approved_tools=[spec for spec in approved if spec is not None],
        capability_manifest={
            "capabilities": [
                {
                    "name": "search_web",
                    "availability": "available",
                    "model_exposed": True,
                    "realtime_eligible": True,
                }
            ]
        },
    )
    session = body["session"]
    function_names = {
        tool["name"] for tool in session["tools"] if tool.get("type") == "function"
    }
    assert names <= function_names
    assert any(tool.get("type") == "web_search" for tool in session["tools"])
    assert session["tool_choice"] == "auto"


def test_live_realtime_prefers_openai_when_both_keys(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "xai_api_key", "xai-test")
    assert live_realtime_provider() == "openai"
    monkeypatch.setattr(settings, "voice_live_brain", "xai")
    assert live_realtime_provider() == "xai"
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert live_realtime_provider() is None


def test_openai_realtime_url_pins_mini() -> None:
    url = openai_realtime_url(
        model="gpt-realtime-2.1-mini",
        realtime_url="wss://api.openai.com/v1/realtime",
    )
    assert url.startswith("wss://api.openai.com/v1/realtime?")
    assert "gpt-realtime-2.1-mini" in url


def test_openai_session_update_advertises_only_approved_function_tools() -> None:
    approved = [
        {
            "name": "calculate",
            "description": "Calculate safely.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        }
    ]
    body = grok_session_update(provider="openai", approved_tools=approved)
    session = body["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1-mini"
    assert "EVIE" in session["instructions"] or "EV" in session["instructions"]
    assert "do not use tools" not in session["instructions"].lower()
    assert [tool["name"] for tool in session["tools"]] == ["calculate"]
    assert session["tool_choice"] == "auto"
    assert "voice" not in session
    audio = session["audio"]
    assert audio["input"]["format"]["rate"] == 24000
    assert audio["input"]["turn_detection"]["type"] == "server_vad"
    assert audio["input"]["turn_detection"]["create_response"] is True
    assert audio["input"]["turn_detection"]["interrupt_response"] is False
    assert audio["output"]["voice"] == "marin"
    assert session["output_modalities"] == ["audio"]
    assert audio["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"


def test_openai_live_search_is_an_ev_function_not_only_provider_search() -> None:
    from app.ev.tools import get_spec

    search_spec = get_spec("search_web")
    assert search_spec is not None
    body = grok_session_update(provider="openai", approved_tools=[search_spec])
    session = body["session"]
    search_tools = [
        tool
        for tool in session["tools"]
        if tool.get("name") == "search_web"
    ]
    assert len(search_tools) == 1
    assert search_tools[0]["type"] == "function"
    assert search_tools[0]["parameters"]["required"] == ["query"]
    assert all(tool["type"] == "function" for tool in session["tools"])
    assert session["tool_choice"] == "auto"


def test_resample_pcm16_16k_to_24k_grows() -> None:
    pcm = b"\x00\x01" * 1600
    out = resample_pcm16(pcm, src_rate=16000, dst_rate=24000)
    assert abs(len(out) - 4800) < 8
    back = resample_pcm16(out, src_rate=24000, dst_rate=16000)
    assert abs(len(back) - 3200) < 8


def test_grok_voice_tools_are_flat_function_payloads() -> None:
    assert grok_voice_tools() == []
    tools = grok_voice_tools(
        [{"name": "search_memory", "description": "mem", "parameters": {"type": "object"}}]
    )
    assert tools == [
        {
            "type": "function",
            "name": "search_memory",
            "description": "mem",
            "parameters": {"type": "object"},
        }
    ]


def test_live_tool_projection_requires_current_available_capability() -> None:
    manifest = {
        "capabilities": [
            {
                "name": "calculate",
                "availability": "available",
                "parameters": {"type": "object"},
            },
            {
                "name": "set_reminder",
                "availability": "not_connected",
                "parameters": {"type": "object"},
            },
            {
                "name": "execute_command",
                "availability": "available",
                "parameters": {"type": "object"},
            },
        ]
    }
    assert [item["name"] for item in approved_live_tool_specs(manifest)] == ["calculate"]


def test_voice_pipeline_pins_xai_model() -> None:
    import inspect

    from app.voice import pipeline as voice_pipeline

    source = inspect.getsource(voice_pipeline.stream_chat_tts_pipeline)
    assert "xai_model" in source
    assert 'chat_provider == "xai"' in source


def _record_app(captured: list[dict]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        body = await request.json()
        captured.append(body)
        return {
            "id": "cmpl-xai",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "Clear tonight."}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }

    return app


def _patch_http(monkeypatch, app: FastAPI) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ),
    )


async def test_xai_chat_does_not_send_deepseek_thinking(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _record_app(captured))
    provider = XAIProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="grok-4.6",
    )
    result = await provider.chat([ChatMessage(role="user", content="weather")])
    assert result.text == "Clear tonight."
    assert captured[0]["model"] == "grok-4.6"
    assert "thinking" not in captured[0]


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


async def _ignore_event(event) -> None:
    del event


async def _wait_until(predicate, *, ticks: int = 100) -> None:
    """Let the bridge receive loop run without relying on wall-clock sleeps."""

    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


def _function_output_items(fake: _FakeRealtime) -> list[dict]:
    return [
        item["item"]
        for item in fake.sent
        if item.get("type") == "conversation.item.create"
        and item.get("item", {}).get("type") == "function_call_output"
    ]


def _function_spec(name: str) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": f"Run {name}.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }


async def _acknowledge_session(bridge: GrokVoiceBridge, fake: _FakeRealtime) -> None:
    """Deliver the provider's exact session.updated acknowledgement."""

    session = fake.sent[0]["session"]
    await bridge._handle_upstream(
        {
            "type": "session.updated",
            "session": {
                "id": "sess_test",
                "model": session.get("model"),
                "tools": session.get("tools", []),
                "audio": session.get("audio"),
            },
        }
    )


def test_realtime_tool_projection_is_exact_for_empty_one_and_multiple() -> None:
    empty_manifest = {
        # An explicit empty projection must win over a stale broad capability list.
        "live_tool_projection": [],
        "capabilities": [{"type": "function", "name": "start_timer"}],
    }
    assert approved_live_tool_specs(empty_manifest) == []
    empty_session = grok_session_update(
        provider="openai", capability_manifest=empty_manifest
    )["session"]
    assert empty_session["tools"] == []
    assert empty_session["tool_choice"] == "none"
    empty_xai_session = grok_session_update(
        provider="xai", capability_manifest=empty_manifest
    )["session"]
    assert empty_xai_session["tools"] == []
    assert empty_xai_session["tool_choice"] == "none"

    one = [_function_spec("start_timer")]
    one_session = grok_session_update(
        provider="openai", capability_manifest={"live_tool_projection": one}
    )["session"]
    assert [tool["name"] for tool in one_session["tools"]] == ["start_timer"]
    assert one_session["tool_choice"] == "auto"

    multiple = [_function_spec("start_timer"), _function_spec("get_weather")]
    multiple_session = grok_session_update(
        provider="openai", capability_manifest={"live_tool_projection": multiple}
    )["session"]
    assert [tool["name"] for tool in multiple_session["tools"]] == [
        "start_timer",
        "get_weather",
    ]
    assert multiple_session["tool_choice"] == "auto"


async def test_realtime_provider_ack_mismatch_is_nonfatal_and_explicit() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await bridge._handle_upstream(
            {
                "type": "session.updated",
                "session": {"tools": []},
            }
        )
        assert bridge.upstream_session_ready is True
        assert bridge.upstream_tool_names == ()
        mismatch = [
            event
            for event in events
            if isinstance(event, ErrorEvent) and event.code == "realtime_tools_rejected"
        ]
        assert len(mismatch) == 1
        assert mismatch[0].fatal is False
        assert (
            "expected ['start_timer']" in mismatch[0].message
            or "different schemas" in mismatch[0].message
        )
        if "different schemas" not in mismatch[0].message:
            assert "received []" in mismatch[0].message
    finally:
        bridge.close()


async def test_realtime_calls_distinguish_valid_malformed_unknown_and_unadvertised() -> None:
    events: list = []
    calls: list[tuple[str, dict, str]] = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        calls.append((name, arguments, call_id))
        return json.dumps({"ok": True, "name": name, "result": {"spoken": "done"}})

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "valid-call",
                    "arguments": json.dumps({"value": "tea"}),
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "malformed-call",
                    "arguments": "{not-json",
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "not_a_function",
                    "call_id": "unknown-call",
                    "arguments": "{}",
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "get_weather",
                    "call_id": "unadvertised-call",
                    "arguments": "{}",
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 4)

        outputs = {
            item["call_id"]: json.loads(item["output"])
            for item in _function_output_items(fake)
        }
        assert calls == [("start_timer", {"value": "tea"}, "valid-call")]
        assert outputs["valid-call"]["ok"] is True
        assert outputs["malformed-call"]["error"] == "invalid_arguments"
        assert outputs["unknown-call"]["error"] == "invalid_tool_call"
        assert outputs["unadvertised-call"]["error"] == "invalid_tool_call"
        assert all(body["ok"] is False for key, body in outputs.items() if key != "valid-call")

        error_codes = {
            event.code
            for event in events
            if isinstance(event, ErrorEvent)
        }
        assert "realtime_invalid_arguments" in error_codes
        assert "realtime_invalid_tool_call" in error_codes
        assert not any(
            isinstance(event, ErrorEvent) and event.fatal
            for event in events
        )
        continuations = [item for item in fake.sent if item.get("type") == "response.create"]
        assert len(continuations) == 4
    finally:
        bridge.close()


async def test_realtime_duplicate_call_id_dispatches_once() -> None:
    calls: list[tuple[str, dict, str]] = []
    fake = _FakeRealtime()
    called = asyncio.Event()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        calls.append((name, arguments, call_id))
        called.set()
        return json.dumps({"ok": True, "spoken": "done"})

    bridge = GrokVoiceBridge(
        on_event=_ignore_event,
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    event = {
        "type": "response.function_call_arguments.done",
        "name": "start_timer",
        "call_id": "same-call-id",
        "arguments": json.dumps({"value": "tea"}),
    }
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(json.dumps(event))
        await fake.incoming.put(json.dumps(event))
        await asyncio.wait_for(called.wait(), timeout=1)
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        assert calls == [("start_timer", {"value": "tea"}, "same-call-id")]
        assert len([item for item in fake.sent if item.get("type") == "response.create"]) == 1
    finally:
        bridge.close()


async def test_realtime_tool_failure_is_false_and_never_evidence() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def failing_tool(name: str, arguments: dict, call_id: str) -> str:
        del name, arguments, call_id
        raise RuntimeError("provider unavailable")

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=failing_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "failed-call",
                    "arguments": json.dumps({"value": "tea"}),
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        body = json.loads(_function_output_items(fake)[0]["output"])
        assert body["ok"] is False
        assert body["error"] == "tool_execution_failed"
        assert "evidence" not in body
        assert not any(isinstance(event, ReplyEvent) for event in events)
        assert any(
            isinstance(event, ErrorEvent) and event.code == "realtime_tool_failure"
            for event in events
        )
    finally:
        bridge.close()


async def test_realtime_confirmation_result_holds_without_success_claim() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def needs_confirmation(name: str, arguments: dict, call_id: str) -> str:
        del name, arguments, call_id
        return json.dumps(
            {
                "ok": False,
                "error": "confirmation_required",
                "confirmation_required": True,
                "hold": True,
                "result": {"spoken": "Please confirm."},
            }
        )

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=needs_confirmation,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "hold-call",
                    "arguments": json.dumps({"value": "tea"}),
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        body = json.loads(_function_output_items(fake)[0]["output"])
        assert body["ok"] is False
        assert body["confirmation_required"] is True
        assert body["hold"] is True
        assert bridge._pending_confirmation_calls == {"hold-call": "start_timer"}
        assert not any(isinstance(event, ReplyEvent) for event in events)
        assert len([item for item in fake.sent if item.get("type") == "response.create"]) == 1
    finally:
        bridge.close()


async def test_realtime_function_output_continues_to_final_spoken_reply() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        assert (name, arguments, call_id) == ("start_timer", {"value": "tea"}, "reply-call")
        return json.dumps(
            {
                "ok": True,
                "name": "start_timer",
                "result": {"spoken": "Timer set."},
                "evidence": {"source": "owner_timer", "observed": True},
                "spoken": "Timer set.",
            }
        )

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await bridge._handle_upstream(
            {"type": "session.updated", "session": {"tools": [_function_spec("start_timer")]}}
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "reply-call",
                    "response_id": "tool-response",
                    "arguments": json.dumps({"value": "tea"}),
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        output_index = fake.sent.index(
            next(item for item in fake.sent if item.get("type") == "response.create")
        )
        function_output_index = next(
            index
            for index, item in enumerate(fake.sent)
            if item.get("type") == "conversation.item.create"
            and item.get("item", {}).get("type") == "function_call_output"
        )
        assert function_output_index < output_index
        assert not any(isinstance(event, ReplyEvent) for event in events)

        pcm_24k = b"\x00\x01" * 2400
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio_transcript.delta",
                    "delta": "Timer set.",
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
        await _wait_until(
            lambda: any(isinstance(event, ReplyEvent) for event in events)
        )
        replies = [event for event in events if isinstance(event, ReplyEvent)]
        assert [reply.text for reply in replies] == ["Timer set."]
        assert any(isinstance(event, TtsChunkEvent) for event in events)
        assert not any(isinstance(event, ReplyEvent) and not event.text for event in events)
    finally:
        bridge.close()


async def test_realtime_trace_proves_tool_boundary_and_final_spoken_continuation(caplog) -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        assert (name, arguments, call_id) == (
            "start_timer",
            {"value": "private phrase"},
            "trace-call",
        )
        return json.dumps(
            {
                "ok": True,
                "name": "start_timer",
                "spoken": "Timer set.",
                "evidence": {"source": "owner_timer", "observed": True},
            }
        )

    caplog.set_level(logging.WARNING, logger="ev.voice.live.grok")
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "trace-call",
                    "arguments": json.dumps({"value": "private phrase"}),
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio_transcript.delta",
                    "delta": "Timer set.",
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(b"\x00\x01" * 2400).decode("ascii"),
                }
            )
        )
        await fake.incoming.put(json.dumps({"type": "response.done"}))
        await _wait_until(lambda: any(isinstance(event, ReplyEvent) for event in events))
    finally:
        bridge.close()

    trace = caplog.text
    for marker in (
        "provider.selected",
        "session.update.sent",
        "tool_schemas",
        "session.updated.received",
        "acknowledged_tool_schemas",
        "response.function_call_arguments.done",
        "function_call.validation",
        "function_call.dispatch",
        "function_call_output.sent",
        "response.create.continuation",
        "final_spoken_audio.chunk",
        "final_spoken_text",
        "response.continuation.completed",
    ):
        assert marker in trace
    assert "private phrase" not in trace
    assert "trace-call" not in trace


async def test_xai_realtime_uses_xai_transcript_event_and_function_continuation() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        assert (name, arguments, call_id) == ("start_timer", {"value": "tea"}, "xai-call")
        return json.dumps({"ok": True, "spoken": "Timer set."})

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="xai",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        assert fake.sent[0]["session"]["audio"]["input"]["format"] == {
            "type": "audio/pcm",
            "rate": 16000,
        }
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.updated",
                    "transcript": "set a timer",
                }
            )
        )
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "start_timer",
                    "call_id": "xai-call",
                    "arguments": json.dumps({"value": "tea"}),
                }
            )
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        await fake.incoming.put(
            json.dumps({"type": "response.audio_transcript.delta", "delta": "Timer set."})
        )
        await fake.incoming.put(json.dumps({"type": "response.done"}))
        await _wait_until(lambda: any(isinstance(event, ReplyEvent) for event in events))
        assert any(
            isinstance(event, PartialTranscriptEvent) and event.text == "set a timer"
            for event in events
        )
        assert any(isinstance(event, ReplyEvent) and event.text == "Timer set." for event in events)
    finally:
        bridge.close()


async def test_grok_voice_bridge_appends_pcm_and_emits_wav_chunks() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        assert "grok-voice-think-fast-2.0" in url
        assert additional_headers["Authorization"].startswith("Bearer ")
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        model="grok-voice-think-fast-2.0",
        provider="xai",
        now_ms=lambda: 10,
    )
    await bridge.start()
    assert fake.sent[0]["type"] == "session.update"
    pcm = b"\x00\x01" * 1600  # 100 ms at 16 kHz
    await bridge.append_pcm(pcm)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    assert fake.sent[-1]["audio"] == base64.b64encode(pcm).decode("ascii")

    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )
    )
    await fake.incoming.put(
        json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"})
    )
    await fake.incoming.put(json.dumps({"type": "response.done"}))
    await asyncio.sleep(0.05)
    kinds = [event.type for event in events]
    assert "tts_chunk" in kinds
    chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert chunk.content_type == "audio/pcm"
    assert chunk.sample_rate == 16000
    assert chunk.audio_b64
    raw = base64.b64decode(chunk.audio_b64)
    assert raw[:4] != b"RIFF"
    assert any(isinstance(event, FinalTranscriptEvent) and event.text == "hi" for event in events)
    bridge.close()


async def test_openai_item_done_nested_transcript_emits_final() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        now_ms=lambda: 10,
    )
    await bridge.start()
    await fake.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.done",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "transcript": "remember the lantern"}
                    ],
                },
            }
        )
    )
    await _wait_until(
        lambda: any(
            isinstance(event, FinalTranscriptEvent) and "lantern" in event.text
            for event in events
        )
    )
    bridge.close()


async def test_openai_realtime_bridge_resamples_and_uses_ga_session() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        assert "api.openai.com" in url
        assert "gpt-realtime-2.1-mini" in url
        assert additional_headers["Authorization"].startswith("Bearer ")
        assert "OpenAI-Beta" not in additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        model="gpt-realtime-2.1-mini",
        provider="openai",
        now_ms=lambda: 10,
        approved_tool_specs=[
            {
                "name": "calculate",
                "description": "Calculate safely.",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
    )
    await bridge.start()
    session = fake.sent[0]["session"]
    assert session["type"] == "realtime"
    assert "voice" not in session
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    assert session["audio"]["input"]["turn_detection"]["create_response"] is True
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is False
    assert {tool["name"] for tool in session["tools"]} == {"calculate"}
    assert session["tool_choice"] == "auto"
    assert session["output_modalities"] == ["audio"]
    pcm = b"\x00\x01" * 1600
    await bridge.append_pcm(pcm)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    appended = base64.b64decode(fake.sent[-1]["audio"])
    assert abs(len(appended) - 4800) < 16

    pcm_24k = b"\x00\x01" * 2400
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm_24k).decode("ascii"),
            }
        )
    )
    await fake.incoming.put(json.dumps({"type": "response.done"}))
    await asyncio.sleep(0.05)
    chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert chunk.sample_rate == 16000
    assert chunk.provider == "openai-realtime"
    assert chunk.content_type == "audio/pcm"
    chunks = [event for event in events if isinstance(event, TtsChunkEvent)]
    total = b"".join(base64.b64decode(event.audio_b64) for event in chunks)
    assert total[:4] != b"RIFF"
    assert abs(len(total) - 3200) < 64
    bridge.close()


async def test_realtime_receive_pump_keeps_reading_while_audio_playout_waits() -> None:
    """Provider reads must continue while the client/audio path is slow."""

    fake = _FakeRealtime()
    audio_started = asyncio.Event()
    release_audio = asyncio.Event()
    events: list = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_event(event) -> None:
        events.append(event)
        if isinstance(event, TtsChunkEvent):
            audio_started.set()
            await release_audio.wait()

    bridge = GrokVoiceBridge(
        on_event=on_event,
        connect=connect,
        api_key="test",
        provider="xai",
        now_ms=lambda: 1,
    )
    try:
        await bridge.start()
        pcm = b"\x00\x01" * 1920  # 120 ms at 16 kHz: one emitted chunk.
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(pcm).decode("ascii"),
                }
            )
        )
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        await fake.incoming.put(json.dumps({"type": "ping"}))
        await _wait_until(
            lambda: bridge._upstream_events is not None
            and bridge._upstream_events.qsize() >= 1
        )
        release_audio.set()
        await _wait_until(lambda: any(item.get("type") == "pong" for item in fake.sent))
    finally:
        release_audio.set()
        bridge.close()


async def test_realtime_late_audio_after_cancel_is_discarded() -> None:
    """A provider delta racing cancellation must not reopen the old turn."""

    fake = _FakeRealtime()
    events: list = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="xai",
        now_ms=lambda: 1,
    )
    try:
        await bridge.start()
        await fake.incoming.put(json.dumps({"type": "response.created"}))
        await _wait_until(lambda: bridge._response_active)
        await bridge.cancel()
        await fake.incoming.put(
            json.dumps(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(b"\x00\x01" * 1920).decode("ascii"),
                }
            )
        )
        await asyncio.sleep(0.05)
        assert not any(isinstance(event, TtsChunkEvent) for event in events)
    finally:
        bridge.close()


async def test_reconnect_refreshes_manifest_tools_and_accepts_provider_ack() -> None:
    """A restarted upstream gets the current capability projection again.

    This is the client-facing contract behind Mac's reconnect loop: the new
    realtime session must not inherit stale function names, and the provider
    must acknowledge the exact set EV advertised before diagnostics call it
    ready.
    """

    events: list = []
    fakes: list[_FakeRealtime] = []
    current = {
        "manifest": {
            "schema_version": "ev.capability-manifest.v1",
            "enabled": ["Calculator"],
            "live_tool_projection": [{"name": "calculate", "type": "function"}],
        },
        "tools": [
            {
                "name": "calculate",
                "description": "Calculate safely.",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = _FakeRealtime()
        fakes.append(fake)
        return fake

    async def load_manifest():
        return current["manifest"]

    async def load_tools():
        return current["tools"]

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        reconnect_delay_s=0.01,
        capability_manifest_loader=load_manifest,
        tool_specs_loader=load_tools,
    )
    await bridge.start()
    assert len(fakes) == 1
    first = fakes[0]
    assert [tool["name"] for tool in first.sent[0]["session"]["tools"]] == ["calculate"]

    await first.incoming.put(
        json.dumps(
            {
                "type": "session.updated",
                "session": {"tools": first.sent[0]["session"]["tools"]},
            }
        )
    )
    await asyncio.sleep(0.05)
    assert bridge.upstream_session_ready is True
    assert bridge.upstream_tool_names == ("calculate",)

    current["manifest"] = {
        "schema_version": "ev.capability-manifest.v1",
        "enabled": ["Reminders"],
        "live_tool_projection": [{"name": "set_reminder", "type": "function"}],
    }
    current["tools"] = [
        {
            "name": "set_reminder",
            "description": "Set a reminder.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    await first.incoming.put(None)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and len(fakes) < 2:
        await asyncio.sleep(0.01)
    assert len(fakes) >= 2
    second = fakes[1]
    assert [tool["name"] for tool in second.sent[0]["session"]["tools"]] == ["set_reminder"]
    assert bridge._capability_manifest["enabled"] == ["Reminders"]
    assert bridge.advertised_tool_names == ("set_reminder",)

    await second.incoming.put(
        json.dumps(
            {
                "type": "session.updated",
                "session": {"tools": second.sent[0]["session"]["tools"]},
            }
        )
    )
    await asyncio.sleep(0.05)
    assert bridge.upstream_session_ready is True
    assert bridge.upstream_tool_names == ("set_reminder",)
    diagnostics = bridge.diagnostics_snapshot()
    assert diagnostics["advertised_tool_names"] == ["set_reminder"]
    assert diagnostics["acknowledged_tool_names"] == ["set_reminder"]
    assert diagnostics["upstream_session_ready"] is True
    assert diagnostics["provider_mismatch"] is False
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "realtime_tools_rejected"
        for event in events
    )
    bridge.close()


async def test_empty_live_manifest_disables_realtime_function_tools() -> None:
    """An empty projection is observable and fail-closed, never static tools."""

    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="openai",
        capability_manifest={
            "schema_version": "ev.capability-manifest.v1",
            "enabled": [],
            "live_tool_projection": [],
        },
        approved_tool_specs=[],
    )
    await bridge.start()
    session = fake.sent[0]["session"]
    assert session["tools"] == []
    assert session["tool_choice"] == "none"

    await fake.incoming.put(
        json.dumps({"type": "session.updated", "session": {"tools": []}})
    )
    await asyncio.sleep(0.05)
    assert bridge.upstream_session_ready is True
    assert bridge.upstream_tool_names == ()
    diagnostics = bridge.diagnostics_snapshot()
    assert diagnostics["advertised_tool_names"] == []
    assert diagnostics["acknowledged_tool_names"] == []
    assert diagnostics["tool_choice"] == "none"
    assert diagnostics["provider_mismatch"] is False
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "realtime_no_tools"
        and event.fatal is False
        for event in events
    )
    bridge.close()


async def test_realtime_function_call_rejects_unknown_or_invalid_arguments() -> None:
    events: list = []
    calls: list[tuple[str, dict]] = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        del call_id
        calls.append((name, arguments))
        return json.dumps({"ok": True, "result": {"spoken": "done"}})

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[
            {
                "name": "calculate",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
    )
    await bridge.start()
    await _acknowledge_session(bridge, fake)
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "name": "not_approved",
                "call_id": "bad-name",
                "arguments": "{}",
            }
        )
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "name": "calculate",
                "call_id": "bad-args",
                "arguments": "{}",
            }
        )
    )
    await asyncio.sleep(0.05)
    assert calls == []
    outputs = [
        item["item"]["output"]
        for item in fake.sent
        if item.get("type") == "conversation.item.create"
        and item.get("item", {}).get("type") == "function_call_output"
    ]
    assert any(json.loads(output)["error"] == "invalid_tool_call" for output in outputs)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "realtime_invalid_tool_call"
        for event in events
    )
    bridge.close()


async def test_function_call_response_done_does_not_emit_empty_reply() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        del name, arguments, call_id
        return json.dumps({"ok": True, "result": {"spoken": "done"}})

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[
            {
                "name": "calculate",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
    )
    await bridge.start()
    await _acknowledge_session(bridge, fake)
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "name": "calculate",
                "call_id": "call-before-follow-up",
                "arguments": json.dumps({"expression": "1 + 1"}),
            }
        )
    )
    await fake.incoming.put(json.dumps({"type": "response.done"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, ReplyEvent) and not event.text for event in events)
    bridge.close()


async def test_grok_voice_speech_started_without_response_does_not_cancel() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, BargeInEvent) for event in events)
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    bridge.close()


async def test_grok_voice_does_not_cancel_during_reasoning() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    await fake.incoming.put(json.dumps({"type": "response.created"}))
    await asyncio.sleep(0.05)
    fake.sent.clear()
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    bridge.close()


async def test_benign_cancel_error_is_not_surfaced() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    await fake.incoming.put(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "code": "response_cancel_not_active",
                    "message": "Cancellation failed: no active response found",
                },
            }
        )
    )
    await asyncio.sleep(0.05)
    assert not any(
        isinstance(event, ErrorEvent) and event.code != "realtime_no_tools"
        for event in events
    )
    bridge.close()


async def test_live_session_with_grok_forwards_pcm_not_local_asr() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.handle_client(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    assert fake.sent
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    session.close()


async def test_live_mute_cancels_only_when_a_response_is_active() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.grok_voice.start()
    await fake.incoming.put(json.dumps({"type": "response.created"}))
    await asyncio.sleep(0.05)
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "mute"})
    types = [item["type"] for item in fake.sent]
    assert "input_audio_buffer.clear" in types
    assert "response.cancel" in types
    session.close()


async def test_grok_voice_start_returns_false_without_key() -> None:
    events: list = []
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        api_key="",
        now_ms=lambda: 1,
    )
    assert await bridge.start() is False
    assert any(isinstance(event, ErrorEvent) and event.code == "realtime_missing_key" for event in events)
    assert await bridge.start() is False


def test_live_session_attach_intelligence_sets_pipeline() -> None:
    session = LiveSession(backchannel_enabled=False)
    assert session._respond is None
    assert session.asr_feed is None

    async def respond(text: str, **kwargs):
        del text, kwargs
        return None

    session.attach_intelligence(respond=respond)
    assert session._respond is respond
    session.close()


async def test_live_mute_clears_realtime_input_buffer() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.grok_voice.start()
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "mute"})
    types = [item["type"] for item in fake.sent]
    assert "input_audio_buffer.clear" in types
    assert "response.cancel" not in types
    session.close()


async def test_live_attentive_rearms_realtime_input_after_mute() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=session.now,
    )
    await session.grok_voice.start()
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "attentive"})
    assert any(item["type"] == "input_audio_buffer.clear" for item in fake.sent)
    session.close()


async def test_realtime_voice_mutes_mic_while_speakers_play() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    await bridge.append_pcm(b"\x00\x01" * 800)
    assert fake.sent == []
    bridge.set_playback(False)
    bridge._echo_until = 0.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    bridge.close()


async def test_realtime_voice_hears_next_turn_after_stale_playback() -> None:
    """A client that never sends playback=false must not deafen turn two."""

    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = False
    bridge._assistant_open = False
    bridge._playback_since = time.monotonic() - 1.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    bridge.close()


async def test_realtime_voice_unblocks_mic_after_audio_done() -> None:
    """response.audio.done must drop the echo latch even without playback=false."""

    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    bridge._assistant_open = True
    await fake.incoming.put(json.dumps({"type": "response.output_audio.done"}))
    await _wait_until(lambda: bridge._assistant_open is False)
    assert bridge._response_active is False
    bridge._playback_since = time.monotonic() - 1.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(
        lambda: any(item["type"] == "input_audio_buffer.append" for item in fake.sent)
    )
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    bridge.close()


async def test_realtime_voice_does_not_cancel_on_echo_during_playback() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, BargeInEvent) for event in events)
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    bridge.close()


async def test_grok_voice_ignores_barge_in_while_assistant_is_speaking() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    pcm = b"\x00\x01" * 1600
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )
    )
    await asyncio.sleep(0.05)
    fake.sent.clear()
    events.clear()
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, BargeInEvent) for event in events)
    assert not any(item.get("type") == "response.cancel" for item in fake.sent)
    bridge.close()


async def test_grok_voice_pong_answers_ping() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GrokVoiceBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    await fake.incoming.put(json.dumps({"type": "ping"}))
    await asyncio.sleep(0.05)
    assert any(item.get("type") == "pong" for item in fake.sent)
    bridge.close()


def test_error_event_includes_text_alias_for_clients() -> None:
    payload = ErrorEvent(at_ms=1, code="xai_voice", message="Grok Voice connect failed").as_dict()
    assert payload["message"] == "Grok Voice connect failed"
    assert payload["text"] == "Grok Voice connect failed"
