"""Local model provider (plan 4.4): OpenAI-compatible local server support."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request

from app.config import settings
from app.contracts import ChatMessage, ToolSpec
from app.gateway.providers import LocalModelProvider, get_chat_provider


def _provider_app(*, with_tools: bool = False) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        body = await request.json()
        message: dict = {"role": "assistant", "content": "local reply"}
        if with_tools and body.get("tools"):
            message["tool_calls"] = [
                {
                    "id": "call-local-1",
                    "type": "function",
                    "function": {
                        "name": "lookup_person",
                        "arguments": '{"name": "Maya"}',
                    },
                }
            ]
        return {
            "id": "cmpl-local",
            "model": body.get("model", "llama3"),
            "choices": [{"message": message}],
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


def _provider() -> LocalModelProvider:
    return LocalModelProvider(base_url="http://local/v1", default_model="llama3")


async def test_local_provider_chat(monkeypatch) -> None:
    _patch_http(monkeypatch, _provider_app())
    result = await _provider().chat([ChatMessage(role="user", content="hi")])
    assert result.text == "local reply"
    assert result.model == "llama3"
    assert result.usage["total_tokens"] == 7
    assert await _provider().list_models() == ["llama3"]


async def test_local_provider_tools(monkeypatch) -> None:
    _patch_http(monkeypatch, _provider_app(with_tools=True))
    spec = ToolSpec(
        name="lookup_person",
        description="Find a person",
        parameters={},
        sensitive=False,
        read_only=True,
        permission="memory:read",
        undoable=False,
        output={},
    )
    result = await _provider().chat_with_tools(
        [ChatMessage(role="user", content="who is Maya")],
        [spec],
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "lookup_person"
    assert result.tool_calls[0].arguments == {"name": "Maya"}


def test_local_provider_registered_and_defaults(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "local")
    provider = get_chat_provider()
    assert provider.name == "local"
    assert provider.default_model == "llama3"


def test_local_provider_uses_qwen_brain_when_configured(monkeypatch) -> None:
    """CORTEX local brain: Qwen3-1.7B Q4 via the EV_LOCAL_MODEL_NAME override."""

    monkeypatch.setenv("EV_LOCAL_MODEL_NAME", "qwen3:1.7b")
    provider = LocalModelProvider()
    assert provider.default_model == "qwen3:1.7b"
    assert provider.name == "local"
