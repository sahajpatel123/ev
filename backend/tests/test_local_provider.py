"""Tests for the local model provider (Ollama/llama.cpp, plan 4.4)."""

from __future__ import annotations

from app.config import settings
from app.contracts import ChatMessage, ToolSpec
from app.gateway import providers
from app.gateway.providers import LocalModelProvider, get_chat_provider


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


def _fake_client(captured: list, data: dict):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self._data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, **kwargs) -> _FakeResponse:
            captured.append((url, kwargs))
            return _FakeResponse(self._data)

    return FakeAsyncClient


async def test_local_provider_chat_maps_request_and_response(monkeypatch) -> None:
    captured: list = []
    data = {
        "choices": [{"message": {"role": "assistant", "content": "hello from local", "tool_calls": []}}],
        "usage": {"total_tokens": 5},
        "model": "llama3",
    }
    monkeypatch.setattr(providers.httpx, "AsyncClient", _fake_client(captured, data))
    provider = LocalModelProvider(base_url="http://localhost:11434/v1", default_model="llama3")

    result = await provider.chat([ChatMessage(role="user", content="hi")])

    assert provider.name == "local"
    assert result.text == "hello from local"
    assert result.model == "llama3"
    assert result.usage == {"total_tokens": 5}
    url, kwargs = captured[0]
    assert url == "http://localhost:11434/v1/chat/completions"
    assert kwargs["json"]["model"] == "llama3"
    assert kwargs["json"]["messages"][0]["content"] == "hi"
    assert "Authorization" not in kwargs["headers"]


async def test_local_provider_tool_calls_passthrough(monkeypatch) -> None:
    captured: list = []
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search_memory", "arguments": '{"query":"EV"}'},
                        }
                    ],
                }
            }
        ],
        "usage": {},
        "model": "llama3",
    }
    monkeypatch.setattr(providers.httpx, "AsyncClient", _fake_client(captured, data))
    provider = LocalModelProvider(base_url="http://localhost:11434/v1", default_model="llama3")
    tool = ToolSpec(
        name="search_memory",
        description="search",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    result = await provider.chat_with_tools([ChatMessage(role="user", content="find")], [tool])

    assert result.tool_calls[0].name == "search_memory"
    assert result.tool_calls[0].arguments == {"query": "EV"}
    assert captured[0][1]["json"]["tools"][0]["function"]["name"] == "search_memory"


async def test_local_provider_list_models() -> None:
    provider = LocalModelProvider(base_url="http://localhost:11434/v1", default_model="llama3")
    assert await provider.list_models() == ["llama3"]


async def test_local_provider_factory_and_defaults(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "local")
    provider = get_chat_provider()
    assert isinstance(provider, LocalModelProvider)
    assert provider.default_model == "llama3"
    assert provider.base_url == "http://localhost:11434/v1"

    monkeypatch.setenv("EV_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("EV_LOCAL_MODEL_NAME", "qwen2.5:7b")
    overridden = LocalModelProvider()
    assert overridden.base_url == "http://127.0.0.1:8080/v1"
    assert overridden.default_model == "qwen2.5:7b"
