"""Official DeepSeek provider: Flash model, thinking off, native tools."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request

from app.config import Settings, settings
from app.contracts import ChatMessage
from app.gateway.providers import DeepSeekProvider, LocalModelProvider


def test_voice_pipeline_pins_official_flash() -> None:
    import inspect

    from app.voice import pipeline as voice_pipeline

    source = inspect.getsource(voice_pipeline.stream_chat_tts_pipeline)
    assert "deepseek_model" in source
    assert 'chat_provider == "deepseek"' in source
    field = Settings.model_fields["deepseek_model"]
    assert field.default == "deepseek-v4-flash"
    thinking = Settings.model_fields["deepseek_thinking"]
    assert thinking.default is False


def test_deepseek_provider_declares_native_tools() -> None:
    provider = DeepSeekProvider(
        base_url="https://api.deepseek.com",
        api_key="test",
        default_model="deepseek-v4-flash",
    )
    assert provider.supports_tools is True
    assert provider.name == "deepseek"


def _record_app(captured: list[dict], *, with_reasoning: bool = False) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        body = await request.json()
        captured.append(body)
        message: dict = {
            "role": "assistant",
            "content": "Two plus two is four.",
        }
        if with_reasoning:
            message["reasoning_content"] = "Let me think step by step about arithmetic."
        return {
            "id": "cmpl-ds",
            "model": body.get("model"),
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
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


async def test_deepseek_payload_disables_thinking_and_uses_flash(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deepseek_thinking", False)
    captured: list[dict] = []
    _patch_http(monkeypatch, _record_app(captured))
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-v4-flash",
    )
    result = await provider.chat([ChatMessage(role="user", content="what's 2+2?")])
    assert result.text == "Two plus two is four."
    assert captured
    body = captured[0]
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}


async def test_deepseek_does_not_speak_reasoning_content(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _record_app(captured, with_reasoning=True))
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-v4-flash",
    )
    result = await provider.chat([ChatMessage(role="user", content="what's 2+2?")])
    assert result.text == "Two plus two is four."
    assert "step by step" not in result.text


async def test_local_provider_does_not_send_thinking_field(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _record_app(captured))
    provider = LocalModelProvider(base_url="http://local/v1", default_model="llama3")
    await provider.chat([ChatMessage(role="user", content="hi")])
    assert captured
    assert "thinking" not in captured[0]
