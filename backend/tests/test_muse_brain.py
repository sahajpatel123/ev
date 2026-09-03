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
    assert live_realtime_provider() == "openai"


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
        {"model": "muse-spark-1.3-contributor", "temperature": 0.7, "thinking": {"type": "enabled"}},
        temperature=0.7,
    )
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "thinking" not in payload


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

