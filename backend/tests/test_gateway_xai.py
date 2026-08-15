"""Official xAI provider: Grok 4.6 for chat, Grok Voice for live."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
from fastapi import FastAPI, Request

from app.config import Settings, settings
from app.contracts import ChatMessage
from app.gateway.providers import XAIProvider, get_chat_provider
from app.voice.live.events import BargeInEvent, ErrorEvent, FinalTranscriptEvent, TtsChunkEvent
from app.voice.live.grok_voice import (
    GrokVoiceBridge,
    grok_session_update,
    grok_voice_enabled,
    grok_voice_tools,
    grok_voice_url,
)
from app.voice.live.session import LiveSession


def test_xai_settings_pin_grok_46_and_voice_think_fast() -> None:
    assert Settings.model_fields["xai_model"].default == "grok-4.6"
    assert Settings.model_fields["xai_voice_model"].default == "grok-voice-think-fast-2.0"
    assert Settings.model_fields["xai_voice_voice"].default == "eve"
    assert Settings.model_fields["voice_live_brain"].default == "auto"


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
    body = grok_session_update()
    assert body["type"] == "session.update"
    session = body["session"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert session["turn_detection"]["type"] == "server_vad"
    assert session["voice"]
    names = {tool["name"] for tool in session["tools"]}
    assert "search_memory" in names
    assert "set_reminder" in names
    assert all(tool["type"] == "function" for tool in session["tools"])


def test_grok_voice_tools_are_flat_function_payloads() -> None:
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
        now_ms=lambda: 10,
    )
    await bridge.start()
    assert fake.sent[0]["type"] == "session.update"
    pcm = b"\x00\x01" * 1600  # 100 ms at 16 kHz
    await bridge.append_pcm(pcm)
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
    assert chunk.content_type == "audio/wav"
    assert chunk.audio_b64
    assert any(isinstance(event, FinalTranscriptEvent) and event.text == "hi" for event in events)
    bridge.close()


async def test_grok_voice_speech_started_emits_barge_in_and_cancels() -> None:
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
    await fake.incoming.put(json.dumps({"type": "input_audio_buffer.speech_started"}))
    await asyncio.sleep(0.05)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert any(item.get("type") == "response.cancel" for item in fake.sent)
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
    assert fake.sent
    assert fake.sent[-1]["type"] == "input_audio_buffer.append"
    session.close()


async def test_grok_voice_start_returns_false_without_key() -> None:
    events: list = []
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        api_key="",
        now_ms=lambda: 1,
    )
    assert await bridge.start() is False
    assert any(isinstance(event, ErrorEvent) and event.code == "xai_missing_key" for event in events)
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
