"""Gemini Live speech path: gemini-3.8-live over the Live API.

Live speech assertions target the Gemini Live wire protocol: ``{"setup": ...}``,
``realtimeInput`` audio, ``clientContent`` turns, ``toolCall`` /
``toolResponse`` functions, and ``serverContent`` transcripts/audio.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from app.config import settings
from app.voice.live.events import (
    BargeInEvent,
    ErrorEvent,
    FinalTranscriptEvent,
    PartialTranscriptEvent,
    ReplyEvent,
    TtsChunkEvent,
)
from app.voice.live.gemini_live import (
    _MOUTH_SPEAK_INSTRUCTIONS,
    _REALTIME_WS_PING_INTERVAL,
    _REALTIME_WS_PING_TIMEOUT,
    GeminiLiveBridge,
    approved_live_tool_specs,
    gemini_live_enabled,
    gemini_live_setup,
    gemini_live_tools,
    gemini_live_url,
    gemini_live_ws_url,
    live_speech_provider,
    resample_pcm16,
)
from app.voice.live.session import LiveSession
from tests._live_fakes import _acknowledge_session, _FakeRealtime


def test_gemini_live_enabled_follows_google_key_not_typed_provider(monkeypatch) -> None:
    """Live Gemini Live is independent of EV_CHAT_PROVIDER (MiMo typed chat)."""

    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    monkeypatch.setattr(settings, "chat_provider", "mimo")
    monkeypatch.setattr(settings, "google_api_key", "k")
    assert gemini_live_enabled() is True
    monkeypatch.setattr(settings, "chat_provider", "echo")
    assert gemini_live_enabled() is True
    monkeypatch.setattr(settings, "google_api_key", "")
    assert gemini_live_enabled() is False
    monkeypatch.setattr(settings, "google_api_key", "k")
    monkeypatch.setattr(settings, "voice_live_brain", "gemini")
    assert gemini_live_enabled() is True
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    assert gemini_live_enabled() is False
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "voice_live_brain", "gemini")
    assert gemini_live_enabled() is False


def test_gemini_live_url_is_bidi_endpoint_with_key_param() -> None:
    url = gemini_live_url()
    assert url.startswith("wss://generativelanguage.googleapis.com/ws/")
    assert "BidiGenerateContent" in url
    authed = gemini_live_ws_url("test-key")
    assert authed.startswith(url + "?")
    assert "key=test-key" in authed
    custom = gemini_live_url(realtime_url="wss://proxy.local/live/")
    assert custom == "wss://proxy.local/live"


def test_gemini_live_setup_is_audio_session_with_declarations() -> None:
    body = gemini_live_setup(
        provider="gemini",
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
    assert set(body) == {"setup"}
    setup = body["setup"]
    assert setup["model"] == "models/gemini-3.8-live-extended-thinking"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "inputAudioTranscription" in setup
    assert "outputAudioTranscription" in setup
    voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice["voiceName"] == "Aoede"
    declarations = setup["tools"][0]["functionDeclarations"]
    names = {tool.get("name") for tool in declarations}
    assert names == {"search_memory", "set_reminder"}
    assert all(tool.get("behavior") == "NON_BLOCKING" for tool in declarations)
    # Web search is an EV function (search_web), never a provider-side tool.
    assert "search_web" not in names
    # Live API shape: generation controls nest under generationConfig, and
    # tool schemas drop JSON-Schema keys the server rejects (live 1007s).
    assert "responseModalities" not in setup
    assert "speechConfig" not in setup
    # The default mouth is the Extended Thinking model: thinking depth rides
    # in thinkingConfig.
    assert setup["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    blob = json.dumps(declarations)
    assert "additionalProperties" not in blob


def test_gemini_session_omits_tools_key_for_empty_projection() -> None:
    body = gemini_live_setup(
        provider="gemini",
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
    setup = body["setup"]
    assert "tools" not in setup


def test_gemini_session_keeps_ev_daily_functions_as_declarations() -> None:
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
    body = gemini_live_setup(
        provider="gemini",
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
    setup = body["setup"]
    declarations = setup["tools"][0]["functionDeclarations"]
    function_names = {tool["name"] for tool in declarations}
    assert names <= function_names
    # No provider-side search tool: every entry is an EV function declaration.
    assert all("functionDeclarations" in block for block in setup["tools"])


def test_live_speech_provider_is_gemini_or_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    monkeypatch.setattr(settings, "google_api_key", "google-test")
    monkeypatch.setattr(settings, "chat_provider", "mimo")
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    assert live_speech_provider() == "gemini"
    # Legacy brain names still map to Gemini (with a warning), never to a ghost.
    monkeypatch.setattr(settings, "voice_live_brain", "xai")
    assert live_speech_provider() == "gemini"
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    assert live_speech_provider() == "gemini"
    monkeypatch.setattr(settings, "google_api_key", "")
    assert live_speech_provider() is None
    monkeypatch.setattr(settings, "google_api_key", "google-test")
    assert live_speech_provider() == "gemini"
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    assert live_speech_provider() is None


def test_gemini_setup_advertises_only_approved_function_tools() -> None:
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
    body = gemini_live_setup(provider="gemini", approved_tools=approved)
    setup = body["setup"]
    assert setup["model"] == "models/gemini-3.8-live-extended-thinking"
    system_text = setup["systemInstruction"]["parts"][0]["text"]
    assert "EVIE" in system_text or "EV" in system_text
    assert "do not use tools" not in system_text.lower()
    declarations = setup["tools"][0]["functionDeclarations"]
    assert [tool["name"] for tool in declarations] == ["calculate"]
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice["voiceName"] == "Aoede"
    assert "contextWindowCompression" in setup
    assert "sessionResumption" in setup


def test_gemini_live_search_is_an_ev_function_declaration() -> None:
    from app.ev.tools import get_spec

    search_spec = get_spec("search_web")
    assert search_spec is not None
    body = gemini_live_setup(provider="gemini", approved_tools=[search_spec])
    setup = body["setup"]
    declarations = setup["tools"][0]["functionDeclarations"]
    search_tools = [
        tool
        for tool in declarations
        if tool.get("name") == "search_web"
    ]
    assert len(search_tools) == 1
    assert search_tools[0]["behavior"] == "NON_BLOCKING"
    assert search_tools[0]["parameters"]["required"] == ["query"]


def test_resample_pcm16_16k_to_24k_grows() -> None:
    pcm = b"\x00\x01" * 1600
    out = resample_pcm16(pcm, src_rate=16000, dst_rate=24000)
    assert abs(len(out) - 4800) < 8
    back = resample_pcm16(out, src_rate=24000, dst_rate=16000)
    assert abs(len(back) - 3200) < 8


def test_stream_resampler_chunked_matches_one_shot() -> None:
    """AUDIO FIDELITY: chunked streaming SRC must not lose anchors at chunk
    boundaries. The old implementation dropped one input sample per feed
    (~10 waveform discontinuities/second = audible scratching)."""
    import random

    from app.voice.live.gemini_live import _StreamResampler

    rng = random.Random(7)
    src_rate, dst_rate = 24000, 16000
    total_in = 24000 * 2  # two seconds of audio
    # Strictly increasing ramp: any anchor skip shows as a length deficit.
    pcm = b"".join(
        int(i * 32767 / total_in).to_bytes(2, "little", signed=False)
        for i in range(total_in)
    )
    one_shot = len(resample_pcm16(pcm, src_rate=src_rate, dst_rate=dst_rate))

    stream = _StreamResampler(src_rate, dst_rate)
    out = bytearray()
    offset = 0
    while offset < len(pcm):
        take = min(len(pcm) - offset, rng.randrange(480, 2400) * 2)
        out.extend(stream.feed(pcm[offset : offset + take]))
        offset += take
    out.extend(stream.feed(b"", flush=True))

    expected = one_shot / 2  # bytes -> samples vs one-shot samples
    assert abs(len(out) / 2 - expected) <= 4, (
        f"chunked SRC lost samples: {len(out) / 2:.0f} vs one-shot {expected:.0f}"
    )


def test_gemini_live_tools_are_non_blocking_declarations() -> None:
    assert gemini_live_tools() == []
    tools = gemini_live_tools(
        [{"name": "search_memory", "description": "mem", "parameters": {"type": "object"}}]
    )
    assert tools == [
        {
            "name": "search_memory",
            "description": "mem",
            "parameters": {"type": "object"},
            "behavior": "NON_BLOCKING",
        }
    ]
    # Outside the live allowlist: dropped, never advertised.
    assert gemini_live_tools([{"name": "not_a_live_tool"}]) == []


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
    """Flatten every FunctionResponse the bridge sent upstream."""

    responses: list[dict] = []
    for item in fake.sent:
        tool_response = item.get("toolResponse")
        if not isinstance(tool_response, dict):
            continue
        entries = tool_response.get("functionResponses")
        if isinstance(entries, list):
            responses.extend(entry for entry in entries if isinstance(entry, dict))
    return responses


def _tool_call_message(name: str, call_id: str, arguments) -> str:
    """One Live API toolCall server message (args may be a dict or raw JSON)."""

    return json.dumps(
        {
            "toolCall": {
                "functionCalls": [
                    {"id": call_id, "name": name, "args": arguments}
                ]
            }
        }
    )


def _model_audio_message(pcm: bytes) -> str:
    return json.dumps(
        {
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/pcm;rate=24000",
                                "data": base64.b64encode(pcm).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        }
    )


def _output_transcript_message(text: str) -> str:
    return json.dumps({"serverContent": {"outputTranscription": {"text": text}}})


def _input_transcript_message(text: str) -> str:
    return json.dumps({"serverContent": {"inputTranscription": {"text": text}}})


def _turn_complete_message() -> str:
    return json.dumps({"serverContent": {"turnComplete": True}})


def _mic_audio_items(fake: _FakeRealtime) -> list[dict]:
    return [
        item["realtimeInput"]["audio"]
        for item in fake.sent
        if isinstance(item.get("realtimeInput"), dict)
        and isinstance(item["realtimeInput"].get("audio"), dict)
    ]


def _client_turns(fake: _FakeRealtime) -> list[dict]:
    return [
        item["clientContent"]
        for item in fake.sent
        if isinstance(item.get("clientContent"), dict)
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


def _declaration_names(setup: dict) -> list[str]:
    blocks = setup.get("tools") or []
    names: list[str] = []
    for block in blocks:
        declarations = block.get("functionDeclarations") or []
        names.extend(tool["name"] for tool in declarations)
    return names


def test_realtime_tool_projection_is_exact_for_empty_one_and_multiple() -> None:
    empty_manifest = {
        # An explicit empty projection must win over a stale broad capability list.
        "live_tool_projection": [],
        "capabilities": [{"type": "function", "name": "start_timer"}],
    }
    assert approved_live_tool_specs(empty_manifest) == []
    empty_setup = gemini_live_setup(
        provider="gemini", capability_manifest=empty_manifest
    )["setup"]
    assert "tools" not in empty_setup
    # Legacy provider aliases still build the same setup (with a warning).
    empty_legacy = gemini_live_setup(
        provider="xai", capability_manifest=empty_manifest
    )["setup"]
    assert "tools" not in empty_legacy

    one = [_function_spec("start_timer")]
    one_setup = gemini_live_setup(
        provider="gemini", capability_manifest={"live_tool_projection": one}
    )["setup"]
    assert _declaration_names(one_setup) == ["start_timer"]

    multiple = [_function_spec("start_timer"), _function_spec("get_weather")]
    multiple_setup = gemini_live_setup(
        provider="gemini", capability_manifest={"live_tool_projection": multiple}
    )["setup"]
    assert _declaration_names(multiple_setup) == [
        "start_timer",
        "get_weather",
    ]


async def test_setup_complete_marks_advertised_projection_ready() -> None:
    """setupComplete carries no tool echo: the advertised set is authoritative."""

    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        assert bridge.upstream_session_ready is False
        await bridge._handle_upstream({"setupComplete": {}})
        assert bridge.upstream_session_ready is True
        assert bridge.upstream_tool_names == ("start_timer",)
        assert bridge.diagnostics_snapshot()["provider_mismatch"] is False
        assert not any(
            isinstance(event, ErrorEvent) and event.code == "realtime_tools_rejected"
            for event in events
        )
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(_tool_call_message("start_timer", "valid-call", {"value": "tea"}))
        await fake.incoming.put(_tool_call_message("start_timer", "malformed-call", "{not-json"))
        await fake.incoming.put(_tool_call_message("not_a_function", "unknown-call", {}))
        await fake.incoming.put(_tool_call_message("get_weather", "unadvertised-call", {}))
        await _wait_until(lambda: len(_function_output_items(fake)) == 4)

        outputs = {
            item["id"]: item["response"]
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
        # Continuations are implicit after a toolResponse: no explicit turn.
        assert _client_turns(fake) == []
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

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    event = _tool_call_message("start_timer", "same-call-id", {"value": "tea"})
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(event)
        await fake.incoming.put(event)
        await asyncio.wait_for(called.wait(), timeout=1)
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        assert calls == [("start_timer", {"value": "tea"}, "same-call-id")]
        assert _client_turns(fake) == []
    finally:
        bridge.close()


async def test_gemini_skips_explicit_continuation_while_response_active() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        del name, arguments, call_id
        return json.dumps({"ok": True, "spoken": "done", "verified": True})

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        fake.sent.clear()
        bridge._response_active = True
        await fake.incoming.put(
            _tool_call_message("start_timer", "active-resp", {"value": "tea"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        assert _client_turns(fake) == []
        assert bridge._continuation_sent is True
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=failing_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            _tool_call_message("start_timer", "failed-call", {"value": "tea"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        body = _function_output_items(fake)[0]["response"]
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=needs_confirmation,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            _tool_call_message("start_timer", "hold-call", {"value": "tea"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        body = _function_output_items(fake)[0]["response"]
        assert body["ok"] is False
        assert body["confirmation_required"] is True
        assert body["hold"] is True
        assert bridge._pending_confirmation_calls == {"hold-call": "start_timer"}
        assert not any(isinstance(event, ReplyEvent) for event in events)
        # The hold speaks through the implicit continuation — no explicit turn.
        assert _client_turns(fake) == []
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            _tool_call_message("start_timer", "reply-call", {"value": "tea"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        responses = _function_output_items(fake)
        assert responses[0]["id"] == "reply-call"
        assert responses[0]["name"] == "start_timer"
        # No scheduling key: the deployed model closes the session (1007)
        # when FunctionResponse.scheduling is present.
        assert "scheduling" not in responses[0]
        # The continuation is implicit: the toolResponse alone triggers speech.
        assert _client_turns(fake) == []
        assert not any(isinstance(event, ReplyEvent) for event in events)

        pcm_24k = b"\x00\x01" * 2400
        await fake.incoming.put(_output_transcript_message("Timer set."))
        await fake.incoming.put(_model_audio_message(pcm_24k))
        await fake.incoming.put(_turn_complete_message())
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

    caplog.set_level(logging.WARNING, logger="ev.voice.live.gemini")
    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            _tool_call_message("start_timer", "trace-call", {"value": "private phrase"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        await fake.incoming.put(_output_transcript_message("Timer set."))
        await fake.incoming.put(_model_audio_message(b"\x00\x01" * 2400))
        await fake.incoming.put(_turn_complete_message())
        await _wait_until(lambda: any(isinstance(event, ReplyEvent) for event in events))
    finally:
        bridge.close()

    trace = caplog.text
    for marker in (
        "provider.selected",
        "setup.sent",
        "tool_schemas",
        "setup.complete",
        "tool_call.received",
        "function_call.validation",
        "function_call.dispatch",
        "function_response.sent",
        "tool.continuation_implicit",
        "final_spoken_audio.chunk",
        "final_spoken_text",
        "turn.continuation.completed",
    ):
        assert marker in trace
    assert "private phrase" not in trace
    assert "trace-call" not in trace


async def test_gemini_realtime_uses_transcript_events_and_function_continuation() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        assert (name, arguments, call_id) == ("start_timer", {"value": "tea"}, "gemini-call")
        return json.dumps({"ok": True, "spoken": "Timer set."})

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        setup = fake.sent[0]["setup"]
        assert setup["model"] == "models/gemini-3.8-live-extended-thinking"
        assert "inputAudioTranscription" in setup
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(_input_transcript_message("set a timer"))
        await fake.incoming.put(
            _tool_call_message("start_timer", "gemini-call", {"value": "tea"})
        )
        await _wait_until(lambda: len(_function_output_items(fake)) == 1)
        await fake.incoming.put(_output_transcript_message("Timer set."))
        await fake.incoming.put(_turn_complete_message())
        await _wait_until(lambda: any(isinstance(event, ReplyEvent) for event in events))
        assert any(
            isinstance(event, PartialTranscriptEvent) and event.text == "set a timer"
            for event in events
        )
        assert any(isinstance(event, ReplyEvent) and event.text == "Timer set." for event in events)
    finally:
        bridge.close()


async def test_gemini_live_bridge_appends_pcm_and_emits_native_chunks() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        assert "BidiGenerateContent" in url
        assert "key=test" in url
        assert additional_headers == {}
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        model="gemini-3.8-live",
        provider="gemini",
        now_ms=lambda: 10,
    )
    await bridge.start()
    assert set(fake.sent[0]) == {"setup"}
    pcm = b"\x00\x01" * 1600  # 100 ms at 16 kHz
    await bridge.append_pcm(pcm)
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    mic = _mic_audio_items(fake)[-1]
    assert mic["mimeType"] == "audio/pcm;rate=16000"
    # Native 16 kHz in: no resample, bytes pass through untouched.
    assert mic["data"] == base64.b64encode(pcm).decode("ascii")

    await fake.incoming.put(_model_audio_message(pcm))
    await fake.incoming.put(_input_transcript_message("hi"))
    await fake.incoming.put(_turn_complete_message())
    await asyncio.sleep(0.05)
    kinds = [event.type for event in events]
    assert "tts_chunk" in kinds
    chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    assert chunk.content_type == "audio/pcm"
    assert chunk.sample_rate == 24000
    assert chunk.provider == "gemini-live"
    assert chunk.audio_b64
    raw = base64.b64decode(chunk.audio_b64)
    assert raw[:4] != b"RIFF"
    assert any(isinstance(event, FinalTranscriptEvent) and event.text == "hi" for event in events)
    bridge.close()


async def test_gemini_input_transcript_finalizes_on_turn_complete() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="gemini",
        now_ms=lambda: 10,
    )
    await bridge.start()
    await fake.incoming.put(_input_transcript_message("remember the lantern"))
    await fake.incoming.put(_turn_complete_message())
    await _wait_until(
        lambda: any(
            isinstance(event, FinalTranscriptEvent) and "lantern" in event.text
            for event in events
        )
    )
    bridge.close()


async def test_repeated_question_finalizes_every_turn() -> None:
    """Asking the same question twice must yield two final transcripts.

    The exactly-once finalize guard compares against the previous turn's
    text; without a per-turn dedup reset the repeat finalizes to nothing
    (no transcript event, no kernel turn, no reply)."""

    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="gemini",
        now_ms=lambda: 10,
    )

    def finals():
        return [
            event
            for event in events
            if isinstance(event, FinalTranscriptEvent) and event.text == "what time is it"
        ]

    await bridge.start()
    try:
        await fake.incoming.put(_input_transcript_message("what time is it"))
        await fake.incoming.put(_turn_complete_message())
        await _wait_until(lambda: len(finals()) == 1)
        await fake.incoming.put(_input_transcript_message("what time is it"))
        await fake.incoming.put(_turn_complete_message())
        await _wait_until(lambda: len(finals()) == 2)
    finally:
        bridge.close()


async def test_gemini_realtime_bridge_emits_native_rate_and_live_setup() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        assert "generativelanguage.googleapis.com" in url
        assert "BidiGenerateContent" in url
        assert "key=sk-test" in url
        assert additional_headers == {}
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        model="gemini-3.8-live",
        provider="gemini",
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
    setup = fake.sent[0]["setup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice["voiceName"]
    assert _declaration_names(setup) == ["calculate"]
    pcm = b"\x00\x01" * 1600
    await bridge.append_pcm(pcm)
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    appended = base64.b64decode(_mic_audio_items(fake)[-1]["data"])
    # Native 16 kHz in: no upsample to 24 kHz.
    assert appended == pcm

    pcm_24k = b"\x00\x01" * 2400
    await fake.incoming.put(_model_audio_message(pcm_24k))
    await fake.incoming.put(_turn_complete_message())
    await asyncio.sleep(0.05)
    chunk = next(event for event in events if isinstance(event, TtsChunkEvent))
    # AUDIO FIDELITY LAW: native provider rate, honestly declared — no
    # lossy server-side downsample between the model and the client.
    assert chunk.sample_rate == 24000
    assert chunk.provider == "gemini-live"
    assert chunk.content_type == "audio/pcm"
    chunks = [event for event in events if isinstance(event, TtsChunkEvent)]
    total = b"".join(base64.b64decode(event.audio_b64) for event in chunks)
    assert total[:4] != b"RIFF"
    assert abs(len(total) - 4800) < 64
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

    bridge = GeminiLiveBridge(
        on_event=on_event,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    try:
        await bridge.start()
        # 160 ms at 24 kHz: one emitted first chunk (7680-byte threshold).
        pcm = b"\x00\x01" * 3840
        await fake.incoming.put(_model_audio_message(pcm))
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        await fake.incoming.put(json.dumps({"type": "ping"}))
        await _wait_until(lambda: any(item.get("type") == "pong" for item in fake.sent))
        assert not release_audio.is_set()
        release_audio.set()
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    try:
        await bridge.start()
        # Small first slice: activates the turn without emitting a chunk.
        await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
        await _wait_until(lambda: bridge._response_active)
        await bridge.cancel()
        await fake.incoming.put(_model_audio_message(b"\x00\x01" * 3840))
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="gemini",
        reconnect_delay_s=0.01,
        capability_manifest_loader=load_manifest,
        tool_specs_loader=load_tools,
    )
    await bridge.start()
    assert len(fakes) == 1
    first = fakes[0]
    assert _declaration_names(first.sent[0]["setup"]) == ["calculate"]

    await first.incoming.put(json.dumps({"setupComplete": {}}))
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
    assert _declaration_names(second.sent[0]["setup"]) == ["set_reminder"]
    assert bridge._capability_manifest["enabled"] == ["Reminders"]
    assert bridge.advertised_tool_names == ("set_reminder",)

    await second.incoming.put(json.dumps({"setupComplete": {}}))
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="sk-test",
        provider="gemini",
        capability_manifest={
            "schema_version": "ev.capability-manifest.v1",
            "enabled": [],
            "live_tool_projection": [],
        },
        approved_tool_specs=[],
    )
    await bridge.start()
    setup = fake.sent[0]["setup"]
    assert "tools" not in setup

    await fake.incoming.put(json.dumps({"setupComplete": {}}))
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
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
    await fake.incoming.put(_tool_call_message("not_approved", "bad-name", {}))
    await fake.incoming.put(_tool_call_message("calculate", "bad-args", {}))
    await asyncio.sleep(0.05)
    assert calls == []
    outputs = [entry["response"] for entry in _function_output_items(fake)]
    assert any(output["error"] == "invalid_tool_call" for output in outputs)
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

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
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
        _tool_call_message("calculate", "call-before-follow-up", {"expression": "1 + 1"})
    )
    await fake.incoming.put(_turn_complete_message())
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, ReplyEvent) and not event.text for event in events)
    bridge.close()


async def test_gemini_live_ignores_unknown_events_without_response() -> None:
    """Legacy VAD events are not part of the Live API: ignored, never fatal."""

    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
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
    assert fake.sent == []
    bridge.close()


async def test_gemini_live_ignores_unknown_events_during_response() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: bridge._response_active)
    fake.sent.clear()
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert fake.sent == []
    assert bridge._response_active is True
    bridge.close()


async def test_benign_cancel_error_is_not_surfaced() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
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


async def test_live_session_with_live_forwards_pcm_not_local_asr() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.gemini_live = GeminiLiveBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.handle_client(b"\x00\x01" * 800)
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    assert fake.sent
    assert "audio" in fake.sent[-1]["realtimeInput"]
    session.close()


async def test_live_mute_ends_stream_and_cancels_locally_when_response_active() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.gemini_live = GeminiLiveBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.gemini_live.start()
    await fake.incoming.put(_model_audio_message(b"\x00\x01" * 500))
    await _wait_until(lambda: session.gemini_live._response_active)
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "mute"})
    # Mute flushes the server-side audio buffer; cancelling is local-only —
    # the Live API has no response.cancel message.
    assert any(
        isinstance(item.get("realtimeInput"), dict)
        and "audioStreamEnd" in item["realtimeInput"]
        for item in fake.sent
    )
    assert session.gemini_live._response_active is False
    assert session.gemini_live._audio_accepting is False
    session.close()


async def test_gemini_live_start_returns_false_without_key() -> None:
    events: list = []
    bridge = GeminiLiveBridge(
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
    session.gemini_live = GeminiLiveBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
    )
    await session.gemini_live.start()
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "mute"})
    assert any(
        isinstance(item.get("realtimeInput"), dict)
        and "audioStreamEnd" in item["realtimeInput"]
        for item in fake.sent
    )
    # Cancelling is local-only: nothing else goes upstream on mute.
    assert all("clientContent" not in item for item in fake.sent)
    session.close()


async def test_live_attentive_rearms_realtime_input_after_mute() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(backchannel_enabled=False)
    session.gemini_live = GeminiLiveBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=session.now,
    )
    await session.gemini_live.start()
    fake.sent.clear()
    await session.handle_client({"type": "control", "action": "attentive"})
    assert any(
        isinstance(item.get("realtimeInput"), dict)
        and "audioStreamEnd" in item["realtimeInput"]
        for item in fake.sent
    )
    session.close()


async def test_realtime_voice_mutes_mic_while_speakers_play() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
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
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    assert "audio" in fake.sent[-1]["realtimeInput"]
    bridge.close()


async def test_realtime_voice_hears_next_turn_after_stale_playback() -> None:
    """A client that never sends playback=false must not deafen turn two."""

    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = False
    bridge._assistant_open = False
    bridge._playback_since = time.monotonic() - 1.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    assert "audio" in fake.sent[-1]["realtimeInput"]
    bridge.close()


async def test_realtime_voice_unblocks_mic_after_turn_complete() -> None:
    """turnComplete must drop the echo latch even without playback=false."""

    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    bridge._assistant_open = True
    await fake.incoming.put(_turn_complete_message())
    await _wait_until(lambda: bridge._assistant_open is False)
    assert bridge._response_active is False
    bridge._playback_since = time.monotonic() - 1.0
    await bridge.append_pcm(b"\x00\x01" * 800)
    await _wait_until(lambda: len(_mic_audio_items(fake)) == 1)
    assert "audio" in fake.sent[-1]["realtimeInput"]
    bridge.close()


async def test_realtime_voice_does_not_cancel_on_echo_during_playback() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    bridge.set_playback(True)
    bridge._response_active = True
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, BargeInEvent) for event in events)
    assert fake.sent == []
    bridge.close()


async def test_gemini_live_ignores_barge_in_while_assistant_is_speaking() -> None:
    events: list = []
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key="test",
        now_ms=lambda: 1,
    )
    await bridge.start()
    pcm = b"\x00\x01" * 1600
    await fake.incoming.put(_model_audio_message(pcm))
    await asyncio.sleep(0.05)
    fake.sent.clear()
    events.clear()
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, BargeInEvent) for event in events)
    assert fake.sent == []
    bridge.close()


async def test_slow_tool_does_not_block_pcm_event_pump() -> None:
    events: list = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        del name, arguments, call_id
        started.set()
        await release.wait()
        return json.dumps({"ok": True, "spoken": "done"})

    fake = _FakeRealtime()
    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="gemini",
        approved_tool_specs=[_function_spec("start_timer")],
    )
    try:
        await bridge.start()
        await _acknowledge_session(bridge, fake)
        await fake.incoming.put(
            _tool_call_message("start_timer", "slow-call", {"value": "tea"})
        )
        await _wait_until(lambda: started.is_set(), ticks=400)
        # 7680+ bytes: crosses the first-chunk threshold without a flush.
        pcm = b"\x00\x01" * 4000
        await fake.incoming.put(_model_audio_message(pcm))
        await _wait_until(
            lambda: any(isinstance(event, TtsChunkEvent) for event in events),
            ticks=400,
        )
        assert not any(
            entry.get("id") == "slow-call"
            for entry in _function_output_items(fake)
        )
        release.set()
        await _wait_until(lambda: len(_function_output_items(fake)) == 1, ticks=400)
    finally:
        release.set()
        bridge.close()


def test_realtime_ws_keepalive_does_not_kill_long_speak() -> None:
    """Client protocol pings + ping_timeout=40 closed the socket mid-speak."""

    assert _REALTIME_WS_PING_INTERVAL is None
    assert _REALTIME_WS_PING_TIMEOUT is None
    assert "one or two short sentences" not in _MOUTH_SPEAK_INSTRUCTIONS
    assert "verbatim" in _MOUTH_SPEAK_INSTRUCTIONS.lower()
    assert "never switch languages" in _MOUTH_SPEAK_INSTRUCTIONS.lower()


async def test_speak_supplied_text_is_verbatim_mouth_not_brevity_law() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
        now_ms=lambda: 1,
    )
    await bridge.start()
    fake.sent.clear()
    spoken = " ".join(["detail"] * 200)
    assert await bridge.speak_supplied_text(spoken)
    turns = _client_turns(fake)
    assert len(turns) == 1
    assert turns[0]["turnComplete"] is True
    text = turns[0]["turns"][0]["parts"][0]["text"]
    assert spoken in text
    assert text.endswith(_MOUTH_SPEAK_INSTRUCTIONS)
    assert "EV SPEECH CONTRACT" not in text
    assert "one or two short sentences" not in text
    bridge.close()


async def test_gemini_live_pong_answers_ping() -> None:
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
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
    payload = ErrorEvent(at_ms=1, code="gemini_live", message="Gemini Live connect failed").as_dict()
    assert payload["message"] == "Gemini Live connect failed"
    assert payload["text"] == "Gemini Live connect failed"


def _activity_markers(fake: _FakeRealtime) -> list[str]:
    markers = []
    for item in fake.sent:
        node = item.get("realtimeInput") if isinstance(item, dict) else None
        if not isinstance(node, dict):
            continue
        if "activityStart" in node:
            markers.append("start")
        elif "activityEnd" in node:
            markers.append("end")
    return markers


async def _manual_vad_bridge(monkeypatch, fake: _FakeRealtime):
    import app.voice.live.gemini_live as live_mod

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    monkeypatch.setattr(live_mod, "_mouth_coprocessor", lambda: True)
    monkeypatch.setattr(live_mod, "_MANUAL_VAD_SILENCE_S", 0.05)
    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
    )
    await bridge.start()
    assert bridge._manual_vad is True
    await bridge._handle_upstream({"setupComplete": {}})
    return bridge


async def test_manual_vad_sends_no_activity_at_setup(monkeypatch) -> None:
    """setupComplete must not open an activity: the server only delivers
    inputTranscription after activityEnd, so a window opened at setup and
    never closed yields no transcripts for the whole session."""

    fake = _FakeRealtime()
    bridge = await _manual_vad_bridge(monkeypatch, fake)
    try:
        assert _activity_markers(fake) == []
    finally:
        bridge.close()


async def test_manual_vad_brackets_each_utterance(monkeypatch) -> None:
    """Speech opens the activity, 0.7 s of silence closes it, and the next
    utterance opens a fresh window (per-turn transcripts for the kernel)."""

    fake = _FakeRealtime()
    bridge = await _manual_vad_bridge(monkeypatch, fake)
    loud = (3000).to_bytes(2, "little", signed=True) * 1600
    silence = b"\x00" * 3200
    try:
        fake.sent.clear()
        await bridge.append_pcm(silence)
        assert _activity_markers(fake) == []
        for _ in range(4):
            await bridge.append_pcm(loud)
        assert _activity_markers(fake) == ["start"]
        await asyncio.sleep(0.1)
        await bridge.append_pcm(silence)
        assert _activity_markers(fake) == ["start", "end"]
        for _ in range(4):
            await bridge.append_pcm(loud)
        await asyncio.sleep(0.1)
        await bridge.append_pcm(silence)
        assert _activity_markers(fake) == ["start", "end", "start", "end"]
    finally:
        bridge.close()


async def test_manual_vad_short_blip_stays_open_and_merges(monkeypatch) -> None:
    """A sub-0.25 s noise blip opens a window but does not close it: the
    next real utterance continues the same window instead of fragmenting."""

    fake = _FakeRealtime()
    bridge = await _manual_vad_bridge(monkeypatch, fake)
    loud = (3000).to_bytes(2, "little", signed=True) * 1600
    silence = b"\x00" * 3200
    try:
        fake.sent.clear()
        await bridge.append_pcm(loud)
        await asyncio.sleep(0.1)
        await bridge.append_pcm(silence)
        assert _activity_markers(fake) == ["start"]
        for _ in range(4):
            await bridge.append_pcm(loud)
        await asyncio.sleep(0.1)
        await bridge.append_pcm(silence)
        assert _activity_markers(fake) == ["start", "end"]
    finally:
        bridge.close()


async def test_auto_vad_sends_no_activity_markers() -> None:
    """Automatic-VAD sessions never bracket: the provider owns turn-taking."""

    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=_ignore_event,
        connect=connect,
        api_key="test",
        provider="gemini",
    )
    loud = (3000).to_bytes(2, "little", signed=True) * 1600
    try:
        await bridge.start()
        assert bridge._manual_vad is False
        await bridge._handle_upstream({"setupComplete": {}})
        fake.sent.clear()
        for _ in range(4):
            await bridge.append_pcm(loud)
        await bridge.append_pcm(b"\x00" * 3200)
        assert _activity_markers(fake) == []
    finally:
        bridge.close()
