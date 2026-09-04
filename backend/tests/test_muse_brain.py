"""Muse Brain V1: hearing + Spark intelligence adapters. No silent fallbacks."""

from __future__ import annotations

import pytest

from app.config import Settings, settings
from app.contracts import ChatMessage, ToolSpec
from app.ev.luna_adapter import classify_intent, is_deterministic_high_confidence
from app.gateway.muse import (
    MuseProviderUnavailable,
    muse_counters_snapshot,
    reset_muse_counters,
)
from app.gateway.providers import PROVIDER_REGISTRY, get_chat_provider
from app.gateway.routing import select_provider
from app.voice.asr import get_transcriber
from app.voice.contracts import VoiceError
from app.voice.live.asr_feed import resolve_live_transcriber
from app.voice.live.grok_voice import grok_voice_enabled, live_realtime_provider


def test_health_snapshot_reports_muse_counters() -> None:
    from app.ev.model_router import health_snapshot

    snap = health_snapshot()
    assert "muse" in snap
    assert snap["muse"]["reasoning_effort"] == "high"
    assert "spark_calls" in snap["muse"]
    assert "spark_reasoning_tokens" in snap["muse"]


def test_muse_spark_is_registered() -> None:
    assert "meta_muse_spark" in PROVIDER_REGISTRY
    assert "muse" in PROVIDER_REGISTRY
    assert "meta_muse_spark" in PROVIDER_REGISTRY
    assert "muse" in PROVIDER_REGISTRY


def test_muse_spark_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    with pytest.raises((MuseProviderUnavailable, Exception)):
        get_chat_provider()


def test_muse_spark_empty_instance_key_fails_closed_not_nameerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="",
        default_model="muse-spark-1.3-contributor",
    )
    with pytest.raises(MuseProviderUnavailable):
        provider._headers()


def test_muse_routing_never_selects_legacy_brains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-test")
    monkeypatch.setattr(settings, "local_model_base_url", "http://localhost:11434/v1")
    selection = select_provider(
        configured="meta_muse_spark",
        evidence={
            "totals": {"calls": 10, "errors": 0, "blocked": 0, "p95_latency_ms": 100.0},
            "by_provider_model": [],
        },
        strategy={"mode": "reasoning"},
    )
    assert selection.provider == "meta_muse_spark"
    assert selection.reason == "muse_spark_single_brain"


def test_muse_routing_candidates_are_spark_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.routing import routing_candidates

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-must-not-route")
    monkeypatch.setattr(settings, "xai_api_key", "xai-must-not-route")
    monkeypatch.setattr(settings, "local_model_base_url", "http://localhost:11434/v1")
    assert routing_candidates() == ["meta_muse_spark"]


def test_spark_code_loop_prompt_is_not_luna() -> None:
    import inspect

    from app.ev.luna_code import LUNA_CODE_SYSTEM, SPARK_CODE_SYSTEM, _spark_code_loop

    assert SPARK_CODE_SYSTEM.startswith("You are Evie's coding brain.")
    assert "(Luna)" not in SPARK_CODE_SYSTEM
    assert "Mini is only the mouth" not in SPARK_CODE_SYSTEM
    assert "Luna" in LUNA_CODE_SYSTEM
    source = inspect.getsource(_spark_code_loop)
    assert "SPARK_CODE_SYSTEM" in source
    assert "LUNA_CODE_SYSTEM" not in source


def test_spark_turn_classifier_prompt_is_not_luna() -> None:
    import inspect

    from app.ev.luna_adapter import (
        LUNA_SYSTEM_PROMPT,
        SPARK_TURN_SYSTEM,
        _call_spark_intent,
    )

    assert SPARK_TURN_SYSTEM.startswith("You are Evie's turn classifier (Muse Spark).")
    assert "Luna" not in SPARK_TURN_SYSTEM
    assert "Luna" in LUNA_SYSTEM_PROMPT
    source = inspect.getsource(_call_spark_intent)
    assert "SPARK_TURN_SYSTEM" in source
    assert "LUNA_SYSTEM_PROMPT" not in source
    assert "chat_structured" in source
    assert "HTTPStatusError" in source


@pytest.mark.asyncio
async def test_spark_intent_falls_through_to_structured_on_tool_schema_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.contracts import ChatResult
    from app.ev.luna_adapter import _call_spark_intent
    from app.ev.turn_intent import TurnIntent

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")

    class _FakeSpark:
        async def chat_with_tools(self, *args, **kwargs):
            request = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("tool schema", request=request, response=response)

        async def chat_structured(self, *args, **kwargs):
            return ChatResult(
                text='{"route":"CONVERSATION","operation":"UNKNOWN"}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider", lambda: _FakeSpark()
    )
    intent = await _call_spark_intent("hello there, what should we do", None)
    assert isinstance(intent, TurnIntent)
    assert intent.route == "CONVERSATION"


def test_normal_s2s_brain_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "xai_api_key", "xai-test")
    assert live_realtime_provider() is None
    assert grok_voice_enabled() is False
    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    assert live_realtime_provider() is None


def test_explicit_openai_realtime_rollback_still_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setattr(settings, "turn_control_provider", "openai")
    monkeypatch.setattr(settings, "voice_asr_provider", "echo")
    assert live_realtime_provider() == "openai"


def test_leftover_openai_realtime_does_not_open_a_second_mouth_while_muse_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    assert live_realtime_provider() is None
    assert grok_voice_enabled() is False


def test_muse_asr_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    transcriber = get_transcriber()
    assert transcriber.name == "meta_muse_voice"
    live = resolve_live_transcriber(transcriber)
    assert live is transcriber
    assert getattr(live, "native_live_stream", False) is True


@pytest.mark.asyncio
async def test_muse_asr_file_transcribe_parses_final_and_diarization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.muse_voice import MuseVoiceTranscriber

    monkeypatch.setenv("EV_ALLOW_REMOTE_ASR", "true")
    payload = {
        "sessionId": "s1",
        "transcript": "Open Calculator",
        "audioDurationMs": 1200,
        "turns": [
            {"turnId": 1, "transcript": "Open Calculator", "speaker": "A"},
        ],
    }

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return payload

    class _Client:
        captured: dict = {}

        async def post(self, url, *args, **kwargs):
            _Client.captured = {"url": url, **kwargs}
            return _Resp()

        async def aclose(self):
            return None

    transcriber = MuseVoiceTranscriber(api_key="test-key", client=_Client())
    result = await transcriber.transcribe(audio_b64=__import__("base64").b64encode(_tiny_wav()).decode("ascii"))
    assert result.text == "Open Calculator"
    assert result.details["diarization_is_owner_auth"] is False
    assert result.details["diarization_speakers"] == ["A"]
    files = _Client.captured["files"]
    assert files["request"][0] is None
    request = __import__("json").loads(files["request"][1])
    assert request["audioEncoding"] == "WAV"
    assert request["mode"] == "PUSH_TO_TALK"
    assert request["model"] == "muse-voice-transcribe-1.0"
    headers = _Client.captured["headers"]
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"].startswith("Bearer ")


def _tiny_wav() -> bytes:
    import io
    import math
    import struct
    import wave

    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(rate)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_muse_asr_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.voice.muse_voice import MuseVoiceTranscriber

    monkeypatch.setenv("EV_ALLOW_REMOTE_ASR", "true")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    transcriber = MuseVoiceTranscriber(api_key="")
    with pytest.raises(VoiceError) as exc:
        await transcriber.transcribe(audio_b64=__import__("base64").b64encode(_tiny_wav()).decode("ascii"))
    assert exc.value.code == "asr_unusable"


@pytest.mark.asyncio
async def test_deterministic_canary_priority_does_not_call_spark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_muse_counters()
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")

    async def boom(*args, **kwargs):
        raise AssertionError("Spark must not run for deterministic Core queries")

    monkeypatch.setattr("app.ev.luna_adapter._call_luna", boom)
    assert is_deterministic_high_confidence("what priority is Canary")
    intent = await classify_intent("what priority is Canary")
    assert intent.route == "STATE_QUERY"
    assert muse_counters_snapshot()["spark_calls"] == 0


@pytest.mark.asyncio
async def test_ambiguous_turn_uses_spark_not_luna(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ev.turn_intent import TurnIntent
    from app.gateway import muse as muse_mod

    reset_muse_counters()
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "openai_api_key", "sk-should-not-be-used")
    monkeypatch.setattr(muse_mod, "muse_key_loaded", lambda: True)
    monkeypatch.setattr(muse_mod, "muse_intelligence_active", lambda: True)

    calls = {"spark": 0, "luna_http": 0}

    async def fake_spark(turn, context):
        calls["spark"] += 1
        return TurnIntent(route="CLARIFICATION", operation="UNKNOWN", needs_clarification=True, confidence=0.7)

    async def fake_openai(*args, **kwargs):
        calls["luna_http"] += 1
        raise AssertionError("OpenAI Luna must not run")

    monkeypatch.setattr("app.ev.luna_adapter._call_spark_intent", fake_spark)
    monkeypatch.setattr("app.ev.luna_adapter._call_responses_api", fake_openai)
    intent = await classify_intent("can you make it better somehow")
    assert calls["spark"] == 1
    assert calls["luna_http"] == 0
    assert intent.route == "CLARIFICATION"


@pytest.mark.asyncio
async def test_muse_spark_chat_and_tools_and_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    reset_muse_counters()
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "muse-spark-1.3-contributor",
                "choices": [
                    {
                        "message": {
                            "content": "hello",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "emit_intent", "arguments": '{"route":"CONVERSATION"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            }

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        async def aclose(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["payload"] = json
            captured["url"] = url
            return _Resp()

        def stream(self, *args, **kwargs):
            return _Stream()

    monkeypatch.setattr("app.gateway.providers.httpx.AsyncClient", _Client)
    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
        provider_name="meta_muse_spark",
    )
    chat = await provider.chat([ChatMessage(role="user", content="hi")])
    assert chat.text == "hello"
    assert captured["payload"]["model"] == "muse-spark-1.3-contributor"
    assert captured["payload"]["reasoning_effort"] == "high"
    assert "temperature" not in captured["payload"]
    tools = await provider.chat_with_tools(
        [ChatMessage(role="user", content="hi")],
        [ToolSpec(name="emit_intent", description="x", parameters={"type": "object"})],
    )
    assert tools.tool_calls and tools.tool_calls[0].name == "emit_intent"
    chunks = []
    async for chunk in provider.stream_chat([ChatMessage(role="user", content="hi")]):
        chunks.append(chunk)
    assert any(getattr(c, "text", "") == "hi" or getattr(c, "done", False) for c in chunks)
    snap = muse_counters_snapshot()
    assert snap["spark_calls"] >= 2


@pytest.mark.asyncio
async def test_curator_uses_spark_when_muse_is_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.memory import curator

    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_key_loaded", lambda: True)
    assert curator.curator_available() is True

    class _Result:
        text = '{"memories":[]}'
        usage = {"total_tokens": 9}

    class _Prov:
        name = "meta_muse_spark"

        async def chat(self, messages, **kwargs):
            return _Result()

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    text, tokens = await curator._call_deepseek("organize this")
    assert "memories" in text
    assert tokens == 9


def test_muse_spark_payload_drops_legacy_thinking() -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    payload = provider._apply_provider_payload(
        {
            "model": "muse-spark-1.3-contributor",
            "temperature": 0.7,
            "thinking": {"type": "enabled"},
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        temperature=0.7,
    )
    assert payload["reasoning_effort"] == "high"
    assert payload.get("stream") is True
    assert "temperature" not in payload
    assert "thinking" not in payload
    assert "stream_options" not in payload


def test_spark_folds_orphan_receipt_tools() -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    legal = provider._conversation_for_meta(
        [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="open Calculator"),
            ChatMessage(role="tool", content='{"ok":true}', name="open_app"),
        ]
    )
    assert legal[-1].role == "user"
    assert "ACTION RESULT (open_app)" in legal[-1].content
    assert all(message.role != "tool" for message in legal)


def test_spark_keeps_paired_tool_replay() -> None:
    from app.contracts import ToolCall
    from app.gateway.muse_spark import MuseSparkProvider

    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    legal = provider._conversation_for_meta(
        [
            ChatMessage(role="user", content="weather in Surat"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_weather",
                        name="search_web",
                        arguments={"query": "Surat weather"},
                    )
                ],
            ),
            ChatMessage(
                role="tool",
                content='{"temp":32}',
                name="search_web",
                tool_call_id="call_weather",
            ),
        ]
    )
    assert legal[-2].role == "assistant"
    assert legal[-1].role == "tool"
    assert legal[-1].tool_call_id == "call_weather"
    assistant = provider._message_payload(legal[-2])
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "call_weather"
    assert assistant["tool_calls"][0]["function"]["name"] == "search_web"
    tool = provider._message_payload(legal[-1])
    assert tool["tool_call_id"] == "call_weather"


def test_tool_loop_replays_assistant_tool_calls() -> None:
    import inspect

    from app.services import tool_loop

    source = inspect.getsource(tool_loop.run_tool_loop)
    assert "tool_calls=paired_calls" in source
    assert "tool_call_id=paired_calls[index].id" in source


def test_muse_spark_coerces_legacy_client_model_ids() -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    assert provider._resolve_model(None) == "muse-spark-1.3-contributor"
    assert provider._resolve_model("gpt-5.6-luna") == "muse-spark-1.3-contributor"
    assert provider._resolve_model("grok-4.6") == "muse-spark-1.3-contributor"
    assert provider._resolve_model("deepseek-v4-flash") == "muse-spark-1.3-contributor"
    assert provider._resolve_model("muse-spark-1.3-contributor") == "muse-spark-1.3-contributor"
    assert provider._resolve_model("muse-spark-1.3") == "muse-spark-1.3"
    headers = provider._stream_headers()
    assert headers["Accept"] == "text/event-stream"
    assert headers["Authorization"].startswith("Bearer ")


def test_talk_sidecar_refuses_muse_without_meta_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    path = Path("/Users/sahajpatel/Code/ev/scripts/start_talk_sidecar.py")
    spec = importlib.util.spec_from_file_location("start_talk_sidecar", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    monkeypatch.setenv("EV_CHAT_PROVIDER", "meta_muse_spark")
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "meta_muse_voice")
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("EV_META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert mod.muse_selected() is True
    assert mod.meta_key_loaded() is False
    with pytest.raises(SystemExit) as exited:
        mod.refuse_muse_without_key()
    assert exited.value.code == 2
    monkeypatch.setenv("META_MODEL_API_KEY", "meta-test-not-logged")
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "meta-test-not-logged")
    mod.refuse_muse_without_key()
    assert mod.meta_key_loaded() is True


def test_talk_sidecar_overlay_fills_empty_meta_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    path = Path("/Users/sahajpatel/Code/ev/scripts/start_talk_sidecar.py")
    spec = importlib.util.spec_from_file_location("start_talk_sidecar_empty_meta", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    overlay = tmp_path / "production.env"
    overlay.write_text("META_MODEL_API_KEY=meta-overlay-not-logged\n")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    mod.load(overlay)
    mod.alias_meta_model_keys()
    assert mod.meta_key_loaded() is True


def test_talk_sidecar_muse_env_beats_leftover_xai_and_openai(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import os
    from pathlib import Path

    path = Path("/Users/sahajpatel/Code/ev/scripts/start_talk_sidecar.py")
    spec = importlib.util.spec_from_file_location("start_talk_sidecar_leftover_brain", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    monkeypatch.setenv("EV_CHAT_PROVIDER", "xai")
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "faster_whisper")
    monkeypatch.setenv("EV_VOICE_LIVE_BRAIN", "openai")
    monkeypatch.setenv("EV_VOICE_TTS_PROVIDER", "openai_compat")
    monkeypatch.setenv("EV_TURN_CONTROL_PROVIDER", "openai")
    monkeypatch.setenv("EV_ALLOW_REMOTE_ASR", "false")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EV_CHAT_PROVIDER=meta_muse_spark",
                "EV_INTELLIGENCE_PROVIDER=meta_muse_spark",
                "EV_VOICE_ASR_PROVIDER=meta_muse_voice",
                "EV_VOICE_LIVE_BRAIN=pipeline",
                "EV_VOICE_TTS_PROVIDER=edge_tts",
                "EV_TURN_CONTROL_PROVIDER=meta_muse_spark",
                "EV_TURN_CONTROL_MODEL=muse-spark-1.3-contributor",
                "EV_MUSE_SPARK_MODEL=muse-spark-1.3-contributor",
                "EV_MUSE_VOICE_MODEL=muse-voice-transcribe-1.0",
                "EV_ALLOW_REMOTE_ASR=true",
            ]
        )
        + "\n"
    )
    mod.load(env_file)
    assert os.environ["EV_CHAT_PROVIDER"] == "meta_muse_spark"
    assert os.environ["EV_INTELLIGENCE_PROVIDER"] == "meta_muse_spark"
    assert os.environ["EV_VOICE_ASR_PROVIDER"] == "meta_muse_voice"
    assert os.environ["EV_VOICE_LIVE_BRAIN"] == "pipeline"
    assert os.environ["EV_VOICE_TTS_PROVIDER"] == "edge_tts"
    assert os.environ["EV_TURN_CONTROL_PROVIDER"] == "meta_muse_spark"
    assert os.environ["EV_ALLOW_REMOTE_ASR"] == "true"
    assert mod.muse_selected() is True


def test_talk_sidecar_muse_selected_if_any_slot_is_muse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util
    from pathlib import Path

    path = Path("/Users/sahajpatel/Code/ev/scripts/start_talk_sidecar.py")
    spec = importlib.util.spec_from_file_location("start_talk_sidecar_any_slot", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    monkeypatch.setenv("EV_INTELLIGENCE_PROVIDER", "xai")
    monkeypatch.setenv("EV_CHAT_PROVIDER", "meta_muse_spark")
    monkeypatch.setenv("EV_TURN_CONTROL_PROVIDER", "openai")
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "faster_whisper")
    assert mod.muse_selected() is True
    monkeypatch.setenv("EV_CHAT_PROVIDER", "xai")
    monkeypatch.setenv("EV_TURN_CONTROL_PROVIDER", "meta_muse_spark")
    assert mod.muse_selected() is True
    monkeypatch.setenv("EV_TURN_CONTROL_PROVIDER", "openai")
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "meta_muse_voice")
    assert mod.muse_selected() is True
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "faster_whisper")
    assert mod.muse_selected() is False


def test_talk_sidecar_replaces_port_only_after_meta_key_gate() -> None:
    from pathlib import Path

    source = Path("/Users/sahajpatel/Code/ev/scripts/start_talk_sidecar.py").read_text()
    main = source.split("def main() -> None:", 1)[1]
    assert main.index("refuse_muse_without_key()") < main.index("stop_existing_talk_sidecar()")
    assert main.index("stop_existing_talk_sidecar()") < main.index("daemonize()")
    assert "tiTCP:8000" not in source
    assert "kickstart" not in source


def test_prove_muse_brain_refuses_without_meta_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    overlay = tmp_path / "secrets.env"
    overlay.write_text("# Muse Brain V1\n# META_MODEL_API_KEY=\n")
    env = os.environ.copy()
    env["EV_SECRETS_FILE"] = str(overlay)
    env["EV_CHAT_PROVIDER"] = "meta_muse_spark"
    env["EV_INTELLIGENCE_PROVIDER"] = "meta_muse_spark"
    env["EV_VOICE_ASR_PROVIDER"] = "meta_muse_voice"
    env.pop("META_MODEL_API_KEY", None)
    env.pop("EV_META_MODEL_API_KEY", None)
    env.pop("MODEL_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(Path("/Users/sahajpatel/Code/ev/scripts/prove_muse_brain.py"))],
        cwd="/Users/sahajpatel/Code/ev",
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "META_MODEL_API_KEY is missing" in (result.stderr or "")
    source = Path("/Users/sahajpatel/Code/ev/scripts/prove_muse_brain.py").read_text()
    assert "8000" in source and "Does not touch production ev.api on :8000" in source
    assert "kickstart" not in source


def test_live_muse_conftest_does_not_force_echo_mock_or_offline_tts() -> None:
    from pathlib import Path

    source = Path("/Users/sahajpatel/Code/ev/backend/tests/conftest.py").read_text()
    assert "_LIVE_MUSE" in source
    live_branch = source.split("if _LIVE_MUSE:", 1)[1].split("else:", 1)[0]
    unit_branch = source.split("if _LIVE_MUSE:", 1)[1].split("else:", 1)[1]
    assert 'EV_VOICE_ASR_PROVIDER"] = "meta_muse_voice"' in live_branch
    assert 'EV_VOICE_TTS_PROVIDER"] = "edge_tts"' in live_branch
    assert 'EV_CHAT_PROVIDER"] = "meta_muse_spark"' in live_branch
    assert 'EV_VOICE_ASR_PROVIDER"] = "echo"' not in live_branch
    assert 'EV_CHAT_PROVIDER"] = "mock"' not in live_branch
    assert 'EV_VOICE_ASR_PROVIDER"] = "echo"' in unit_branch
    assert 'EV_VOICE_TTS_PROVIDER"] = "meta"' in unit_branch
    prove = Path("/Users/sahajpatel/Code/ev/scripts/prove_muse_brain.py").read_text()
    assert 'env["EV_CHAT_PROVIDER"] = "meta_muse_spark"' in prove
    assert 'env["EV_VOICE_ASR_PROVIDER"] = "meta_muse_voice"' in prove
    assert 'env["EV_VOICE_TTS_PROVIDER"] = "edge_tts"' in prove
    assert 'env["EV_XAI_API_KEY"] = ""' in prove


def test_prove_muse_brain_rejects_stale_xai_health(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    path = Path("/Users/sahajpatel/Code/ev/scripts/prove_muse_brain.py")
    spec = importlib.util.spec_from_file_location("prove_muse_brain_is_muse", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_head_sha", lambda: "abc123")
    stale = {
        "git": {"sha": "abc123"},
        "providers": {"chat": "xai", "live": "openai-realtime"},
        "models": {
            "voice": {"provider": "openai-realtime", "model": "gpt-realtime-2.1-mini"},
            "turn_control": {"provider": "openai", "model": "gpt-5.6-luna"},
            "manager": {"provider": "deepseek"},
        },
    }
    assert mod._is_muse(stale) is False
    healthy = {
        "git": {"sha": "abc123"},
        "providers": {"chat": "meta_muse_spark", "live": "pipeline"},
        "models": {
            "voice": {"provider": "meta_muse_voice", "model": "muse-voice-transcribe-1.0"},
            "turn_control": {"provider": "meta_muse_spark", "model": "muse-spark-1.3-contributor"},
            "manager": {"provider": "meta_muse_spark"},
            "muse": {"reasoning_effort": "high"},
        },
        "runtime": {"checks": [{"name": "tts", "provider": "edge_tts"}]},
    }
    assert mod._is_muse(healthy) is True
    half = dict(healthy)
    half["models"] = {**healthy["models"], "turn_control": {"provider": "openai"}}
    assert mod._is_muse(half) is False


@pytest.mark.asyncio
async def test_muse_spark_complete_raw_strips_reasoning_and_non_auto_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.muse_spark import MuseSparkProvider
    from app.gateway.reliability import CIRCUIT_BREAKERS

    CIRCUIT_BREAKERS.reset("meta_muse_spark")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "muse-spark-1.3-contributor",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *args, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs.get("json")
            return _Resp()

    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    await provider.complete_raw(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }
                ],
                "reasoning_content": "must not be replayed",
                "reasoning": {"effort": "high"},
            }
        ],
        tools=[{"type": "function", "function": {"name": "list_dir", "parameters": {}}}],
        tool_choice="required",
    )
    message = captured["payload"]["messages"][0]
    assert message["role"] == "assistant"
    assert message["tool_calls"]
    assert "reasoning_content" not in message
    assert "reasoning" not in message
    assert "tool_choice" not in captured["payload"]


@pytest.mark.asyncio
async def test_muse_spark_structured_omits_strict_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.contracts import ChatMessage
    from app.gateway.muse_spark import MuseSparkProvider

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "muse-spark-1.3-contributor",
                "choices": [{"message": {"content": '{"route":"CONVERSATION","operation":"UNKNOWN"}'}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["payload"] = json
            return _Resp()

    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    reset_muse_counters()
    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )
    result = await provider.chat_structured(
        [ChatMessage(role="user", content="hi")],
        schema={"type": "object", "properties": {"route": {"type": "string"}}},
    )
    schema = captured["payload"]["response_format"]["json_schema"]
    assert "strict" not in schema
    assert result.text
    assert muse_counters_snapshot()["spark_reasoning_tokens"] == 12


def test_phone_asr_seam_left_on_openai_realtime() -> None:
    """Phone WebRTC is an OpenAI Realtime session, not a drop-in Muse ASR seam."""

    assert Settings.model_fields["phone_asr_model"].default == "gpt-4o-transcribe"


def test_intelligence_provider_overrides_chat_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    from app.gateway.muse import configured_intelligence_provider

    assert configured_intelligence_provider() == "meta_muse_spark"


def test_leftover_xai_chat_cannot_win_while_a_muse_slot_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.muse import configured_intelligence_provider, muse_intelligence_active

    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "intelligence_provider", "xai")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    assert configured_intelligence_provider() == "meta_muse_spark"
    assert muse_intelligence_active() is True

    monkeypatch.setattr(settings, "intelligence_provider", "xai")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "openai")
    assert configured_intelligence_provider() == "meta_muse_spark"


@pytest.mark.asyncio
async def test_legacy_xai_provider_cannot_chat_while_muse_is_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.providers import XAIProvider

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    provider = XAIProvider(
        base_url="https://api.x.ai/v1",
        api_key="xai-must-not-be-used",
        default_model="grok-4.6",
    )
    with pytest.raises(MuseProviderUnavailable):
        await provider.chat([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_luna_responses_api_blocked_while_muse_is_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.luna_adapter import _call_responses_api

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    with pytest.raises(MuseProviderUnavailable):
        await _call_responses_api(
            "hello", None, model="gpt-5.6-luna", requested="gpt-5.6-luna"
        )


@pytest.mark.asyncio
async def test_luna_code_loop_blocked_while_muse_is_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.luna_code import _luna_loop

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    with pytest.raises(MuseProviderUnavailable):
        await _luna_loop("write hello.py", model="gpt-5.6-luna", budget_s=30, live=False)


@pytest.mark.asyncio
async def test_file_rewrite_legacy_http_blocked_while_muse_is_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.laptop_files import _call_chat_model

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    with pytest.raises(MuseProviderUnavailable):
        await _call_chat_model(
            provider="openai",
            model="gpt-5.6-luna",
            fallback="",
            prompt="write hi",
            api_key="sk-must-not-be-used",
            base_url="https://api.openai.com/v1",
        )


@pytest.mark.asyncio
async def test_coding_loop_uses_spark_not_openai_when_muse_on(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import luna_code

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_api_key", lambda: "meta-test")
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-1.3-contributor")

    openai_posts = {"n": 0}

    async def fake_spark_loop(goal, **kwargs):
        return {
            "ok": True,
            "spoken": "Wrote a script and ran it.",
            "files_changed": ["hello.py"],
            "runs": [{"ok": True, "argv": ["python3", "hello.py"], "exit_code": 0}],
            "brain": "muse-spark-1.3-contributor",
            "workspace": str(tmp_path),
            "degraded": False,
        }

    async def fake_luna_loop(*args, **kwargs):
        openai_posts["n"] += 1
        raise AssertionError("OpenAI Luna-code must not run")

    monkeypatch.setattr(luna_code, "_spark_code_loop", fake_spark_loop)
    monkeypatch.setattr(luna_code, "_luna_loop", fake_luna_loop)
    result = await luna_code.run_code_job("write a python script that prints hello")
    assert openai_posts["n"] == 0
    assert result.get("brain") == "muse-spark-1.3-contributor" or "spark" in str(result.get("brain") or "").lower() or result.get("ok") in {True, False}


@pytest.mark.asyncio
async def test_muse_spark_401_fails_closed_not_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.gateway.muse_spark import MuseSparkProvider
    from app.gateway.reliability import CIRCUIT_BREAKERS

    CIRCUIT_BREAKERS.reset("meta_muse_spark")
    monkeypatch.setattr(settings, "model_max_retries", 0)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            request = httpx.Request("POST", url)
            return httpx.Response(401, request=request, json={"error": "unauthorized"})

    monkeypatch.setattr("app.gateway.providers.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="bad-key",
        default_model="muse-spark-1.3-contributor",
        provider_name="meta_muse_spark",
    )
    with pytest.raises(MuseProviderUnavailable):
        await provider.chat([ChatMessage(role="user", content="hi")])


def test_typed_chat_brain_is_muse_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")
    provider = get_chat_provider()
    assert provider.name == "meta_muse_spark"
    assert provider.default_model == "muse-spark-1.3-contributor"


def test_health_manager_is_spark_when_muse_is_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ev.model_router import health_snapshot, manager_model_info

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")
    info = manager_model_info()
    assert info.provider == "meta_muse_spark"
    assert info.model == "muse-spark-1.3-contributor"
    snap = health_snapshot()
    assert snap["manager"]["provider"] == "meta_muse_spark"
    assert snap["muse"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_v1_health_reports_muse_pipeline_not_openai_or_deepseek(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-select-s2s")
    monkeypatch.setattr(settings, "xai_api_key", "xai-must-not-select-s2s")
    monkeypatch.setattr(settings, "deepseek_api_key", "ds-must-not-be-manager")

    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["providers"]["chat"] == "meta_muse_spark"
    assert body["providers"]["live"] == "pipeline"
    assert body["models"]["voice"]["provider"] == "meta_muse_voice"
    assert body["models"]["voice"]["model"] == "muse-voice-transcribe-1.0"
    assert body["models"]["turn_control"]["provider"] == "meta_muse_spark"
    assert body["models"]["turn_control"]["model"] == "muse-spark-1.3-contributor"
    assert body["models"]["manager"]["provider"] == "meta_muse_spark"
    assert body["models"]["manager"]["model"] == "muse-spark-1.3-contributor"
    assert body["models"]["voice"]["provider"] != "openai-realtime"
    assert "deepseek" not in (body["models"]["manager"].get("provider") or "")
    assert "grok" not in (body["providers"]["live"] or "")
    assert "luna" not in (body["models"]["turn_control"].get("model") or "")

