from __future__ import annotations

import os
from collections.abc import Callable, Sequence

import httpx

from app.config import settings
from app.contracts import ChatMessage, ChatProvider, ChatResult, ToolCall, ToolSpec


class EchoProvider:
    """Offline echo provider for zero-config local runs."""

    name = "echo"
    supports_media = False

    def __init__(self, model: str = "echo-local") -> None:
        self.model = model

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return ChatResult(
            text=f"EV: I heard you. (echo provider — '{user_text[:120]}')",
            usage={"prompt_tokens": sum(len(m.content) // 4 for m in messages), "completion_tokens": 8},
            model=model or self.model,
        )

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return await self.chat(messages, model=model, temperature=temperature)

    async def list_models(self) -> list[str]:
        return [self.model]


class MockProvider:
    """Deterministic provider for tests."""

    name = "mock"
    supports_media = False

    def __init__(self, model: str = "mock-model") -> None:
        self.model = model

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return ChatResult(
            text=f"EV: Mock reply. Last user message: {user_text[:100]}",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            model=model or self.model,
        )

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return await self.chat(messages, model=model, temperature=temperature)

    async def list_models(self) -> list[str]:
        return [self.model]


class DeepSeekProvider:
    """DeepSeek via the OpenAI-compatible chat completions API."""

    name = "deepseek"
    supports_media = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        default_model: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model

    def _headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _message_payload(self, message: ChatMessage) -> dict:
        """Render one message, using OpenAI-style content parts for media."""
        if not message.media:
            return {"role": message.role, "content": message.content}
        parts: list[dict] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        for part in message.media:
            if part.kind == "image" and part.data_url:
                parts.append(
                    {"type": "image_url", "image_url": {"url": part.data_url}}
                )
            elif part.kind == "audio" and part.data_url:
                data = part.data_url
                fmt = "wav"
                if data.startswith("data:"):
                    header, _, b64 = data.partition(",")
                    data = b64
                    if "audio/mpeg" in header:
                        fmt = "mp3"
                    elif "audio/mp4" in header or "audio/aac" in header:
                        fmt = "mp4"
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data, "format": fmt},
                    }
                )
            elif part.text:
                parts.append({"type": "text", "text": part.text})
        return {"role": message.role, "content": parts}

    async def _complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None,
        temperature: float,
        tools: Sequence[ToolSpec] | None = None,
    ) -> ChatResult:
        payload: dict = {
            "model": model or self.default_model,
            "messages": [self._message_payload(m) for m in messages],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]
        tool_calls = []
        for call in choice.get("tool_calls") or []:
            fn = call["function"]
            import json

            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"raw": fn.get("arguments")}
            tool_calls.append(ToolCall(id=call.get("id", ""), name=fn["name"], arguments=args))
        return ChatResult(
            text=choice.get("content") or "",
            tool_calls=tool_calls,
            usage=data.get("usage") or {},
            model=data.get("model"),
        )

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return await self._complete(messages, model=model, temperature=temperature)

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return await self._complete(messages, model=model, temperature=temperature, tools=tools)

    async def list_models(self) -> list[str]:
        return [self.default_model]


class LocalModelProvider(DeepSeekProvider):
    """OpenAI-compatible local model server (Ollama/llama.cpp) — plan 4.4.

    The model runs on the user's machine or LAN; no API key is required.
    Point ``EV_LOCAL_MODEL_BASE_URL`` at the server's OpenAI-compatible
    endpoint (Ollama default: ``http://localhost:11434/v1``).
    """

    name = "local"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        resolved_base = base_url or os.getenv("EV_LOCAL_MODEL_BASE_URL") or (
            "http://localhost:11434/v1"
        )
        resolved_model = default_model or os.getenv("EV_LOCAL_MODEL_NAME") or "llama3"
        super().__init__(
            base_url=resolved_base,
            api_key=None,
            default_model=resolved_model,
        )


def _deepseek_factory() -> DeepSeekProvider:
    return DeepSeekProvider(
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        default_model=settings.deepseek_model,
    )


def _local_factory() -> LocalModelProvider:
    return LocalModelProvider()


# Provider registry: model swap is a configuration change (EV_CHAT_PROVIDER).
PROVIDER_REGISTRY: dict[str, Callable[[], ChatProvider]] = {
    "echo": EchoProvider,
    "mock": MockProvider,
    "deepseek": _deepseek_factory,
    "local": _local_factory,
}


def register_provider(name: str, factory: Callable[[], ChatProvider]) -> None:
    """Register a provider factory (used by tests and future local providers)."""

    PROVIDER_REGISTRY[name] = factory


def get_chat_provider() -> ChatProvider:
    name = settings.chat_provider
    factory = PROVIDER_REGISTRY.get(name)
    if factory is None:
        return PROVIDER_REGISTRY["echo"]()
    return factory()
