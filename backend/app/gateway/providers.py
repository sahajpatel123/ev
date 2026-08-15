from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence

import httpx

from app.config import settings
from app.contracts import ChatMessage, ChatProvider, ChatResult, ToolCall, ToolSpec
from app.gateway.reliability import (
    CIRCUIT_BREAKERS,
    CircuitOpenError,
    ProviderStreamError,
    http_timeout,
    is_transient,
    max_attempts,
    wait_for_retry,
)
from app.gateway.routing import ProviderSelection
from app.gateway.streaming import ChatStreamChunk, StreamingChatProvider

logger = logging.getLogger("ev.gateway.providers")


class UnknownProviderError(ValueError):
    """The configured chat provider is not registered.

    Raised instead of silently falling back to ``echo``: a system that
    misconfigures its brain must say so loudly rather than lie about itself.
    """


def _stream_text_chunks(text: str, *, size: int = 24, model: str | None) -> list[ChatStreamChunk]:
    """Deterministic offline stream used by echo/mock providers."""

    return [
        ChatStreamChunk(text=text[i : i + size], model=model)
        for i in range(0, len(text), size)
    ] or [ChatStreamChunk(text="", model=model)]


def _offline_reply(user_text: str, *, kind: str) -> str:
    """Deterministic offline reply used only when the gateway is echo/mock."""

    if kind == "echo":
        return f"EV: I heard you. (echo provider — '{user_text[:120]}')"
    return f"EV: Mock reply. Last user message: {user_text[:100]}"


class EchoProvider(StreamingChatProvider):
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
            text=_offline_reply(user_text, kind="echo"),
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

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatStreamChunk]:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        text = _offline_reply(user_text, kind="echo")
        for chunk in _stream_text_chunks(text, model=model or self.model):
            yield chunk
        yield ChatStreamChunk(
            usage={
                "prompt_tokens": sum(len(m.content) // 4 for m in messages),
                "completion_tokens": 8,
            },
            model=model or self.model,
            done=True,
        )

    async def list_models(self) -> list[str]:
        return [self.model]


class MockProvider(StreamingChatProvider):
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
            text=_offline_reply(user_text, kind="mock"),
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

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatStreamChunk]:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        text = _offline_reply(user_text, kind="mock")
        for chunk in _stream_text_chunks(text, model=model or self.model):
            yield chunk
        yield ChatStreamChunk(
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            model=model or self.model,
            done=True,
        )

    async def list_models(self) -> list[str]:
        return [self.model]


class DeepSeekProvider(StreamingChatProvider):
    """DeepSeek via the OpenAI-compatible chat completions API."""

    name = "deepseek"
    supports_media = True
    supports_tools = True

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

    def _thinking_payload(self) -> dict | None:
        """Official V4 thinking toggle. Voice stays non-thinking for latency.

        Local OpenAI-compatible servers (Ollama) must not receive this field.
        """

        if self.name != "deepseek":
            return None
        return {"type": "enabled" if settings.deepseek_thinking else "disabled"}

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
        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        payload: dict = {
            "model": model or self.default_model,
            "messages": [self._message_payload(m) for m in messages],
            "temperature": temperature,
        }
        thinking = self._thinking_payload()
        if thinking is not None:
            payload["thinking"] = thinking
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
        attempts = max_attempts()
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=http_timeout()) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    resp.raise_for_status()
                breaker.record_success()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                transient = is_transient(exc, status_code)
                if transient:
                    breaker.record_failure()
                    if attempt + 1 < attempts:
                        await wait_for_retry(attempt)
                        continue
                raise
        else:
            raise RuntimeError(f"{self.name} request failed after {attempts} attempts")
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
        # Spoken/text replies use ``content`` only. Chain-of-thought lives in
        # ``reasoning_content`` and must never be read aloud.
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

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatStreamChunk]:
        """Stream one OpenAI-compatible completion, delta by delta.

        The upstream ``httpx`` stream is closed in ``finally`` (and by the
        ``async with`` exit), so cancelling this generator — including a client
        disconnect — tears down the upstream connection instead of leaking it.
        Connection failures retry with jittered backoff until the first byte;
        a mid-stream upstream failure is raised as a typed
        :class:`ProviderStreamError` instead of truncating success.
        """

        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        resolved_model = model or self.default_model
        payload: dict = {
            "model": resolved_model,
            "messages": [self._message_payload(m) for m in messages],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        thinking = self._thinking_payload()
        if thinking is not None:
            payload["thinking"] = thinking
        tool_buffers: dict[int, dict[str, str]] = {}
        final_usage: dict = {}
        finish_reason: str | None = None
        attempts = max_attempts()
        attempt = 0
        started_stream = False
        while True:
            try:
                async with (
                    httpx.AsyncClient(timeout=http_timeout()) as client,
                    client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    ) as resp,
                ):
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        status_code = getattr(exc.response, "status_code", None)
                        transient = is_transient(exc, status_code)
                        if transient:
                            breaker.record_failure()
                            if attempt + 1 < attempts:
                                attempt += 1
                                await wait_for_retry(attempt - 1)
                                continue
                        raise
                    try:
                        async for line in resp.aiter_lines():
                            started_stream = True
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                break
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choice = (event.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            chunk_model = event.get("model") or resolved_model
                            # Never stream chain-of-thought; TTS would speak it.
                            text = delta.get("content")
                            if text:
                                yield ChatStreamChunk(text=text, model=chunk_model)
                            for tool_delta in delta.get("tool_calls") or []:
                                index = int(tool_delta.get("index", 0))
                                buf = tool_buffers.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if tool_delta.get("id"):
                                    buf["id"] = tool_delta["id"]
                                fn = tool_delta.get("function") or {}
                                if fn.get("name"):
                                    buf["name"] = fn["name"]
                                buf["arguments"] += fn.get("arguments") or ""
                            if event.get("usage"):
                                final_usage = event["usage"]
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                    finally:
                        await resp.aclose()
                breaker.record_success()
                break
            except Exception as exc:  # noqa: BLE001 - typed mid-stream boundary
                if started_stream:
                    breaker.record_failure()
                    raise ProviderStreamError(
                        f"upstream stream failed after partial output: {exc}"
                    ) from exc
                if isinstance(exc, (httpx.TransportError, httpx.RemoteProtocolError)):
                    breaker.record_failure()
                    if attempt + 1 < attempts:
                        attempt += 1
                        await wait_for_retry(attempt - 1)
                        continue
                raise
        tool_calls: list[ToolCall] = []
        for index, buf in tool_buffers.items():
            arguments: dict = {}
            if buf["arguments"]:
                try:
                    arguments = json.loads(buf["arguments"])
                except json.JSONDecodeError:
                    arguments = {"raw": buf["arguments"]}
            tool_calls.append(
                ToolCall(
                    id=buf["id"] or f"call-{index}",
                    name=buf["name"],
                    arguments=arguments,
                )
            )
        yield ChatStreamChunk(
            usage=final_usage,
            model=resolved_model,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            done=True,
        )

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
        resolved_base = (
            base_url
            or os.getenv("EV_LOCAL_MODEL_BASE_URL")
            or settings.local_model_base_url
            or "http://localhost:11434/v1"
        )
        resolved_model = (
            default_model
            or os.getenv("EV_LOCAL_MODEL_NAME")
            or settings.local_model_name
            # CORTEX local brain: Qwen3-1.7B Q4 via Ollama. The env var is the
            # supported override today; the settings default stays untouched
            # (shared config) pending the Agent 2 registry/default change.
            or "qwen3:1.7b"
        )
        super().__init__(
            base_url=resolved_base,
            api_key=None,
            default_model=resolved_model,
        )


class XAIProvider(DeepSeekProvider):
    """Official xAI chat completions (Grok 4.6). OpenAI-compatible.

    Grok Voice Think Fast 2.0 is *not* this provider — that model is
    speech-to-speech on ``wss://api.x.ai/v1/realtime``. Typed chat, HUD, and
    the tool loop use Grok 4.6 here.
    """

    name = "xai"
    supports_media = True
    supports_tools = True


def _deepseek_factory() -> DeepSeekProvider:
    return DeepSeekProvider(
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        default_model=settings.deepseek_model,
    )


def _xai_factory() -> XAIProvider:
    return XAIProvider(
        base_url=settings.xai_base_url,
        api_key=settings.xai_api_key,
        default_model=settings.xai_model,
    )


def _local_factory() -> LocalModelProvider:
    return LocalModelProvider()


# Provider registry: model swap is a configuration change (EV_CHAT_PROVIDER).
PROVIDER_REGISTRY: dict[str, Callable[[], ChatProvider]] = {
    "echo": EchoProvider,
    "mock": MockProvider,
    "deepseek": _deepseek_factory,
    "xai": _xai_factory,
    "local": _local_factory,
}


def register_provider(name: str, factory: Callable[[], ChatProvider]) -> None:
    """Register a provider factory (used by tests and future local providers)."""

    PROVIDER_REGISTRY[name] = factory


# --- AGENT OPENCODE (append-only) -------------------------------------------
# `opencode serve` speaks a session API, not OpenAI chat completions, so the
# provider lives in its own module. Imported lazily inside the factory so a
# broken/absent opencode install can never break importing this registry.
def _opencode_factory() -> ChatProvider:
    from app.gateway.opencode import OpenCodeProvider

    return OpenCodeProvider()


register_provider("opencode", _opencode_factory)
# --- END AGENT OPENCODE ---


def get_chat_provider() -> ChatProvider:
    name = settings.chat_provider
    factory = PROVIDER_REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        logger.error(
            "EV_CHAT_PROVIDER=%r is not a registered provider (known: %s); "
            "refusing to run instead of silently degrading to echo",
            name,
            known,
        )
        raise UnknownProviderError(
            f"unknown chat provider {name!r}; set EV_CHAT_PROVIDER to one of: {known}"
        )
    return factory()


def provider_from_selection(selection: ProviderSelection) -> ChatProvider:
    """Instantiate the provider chosen by the routing policy."""

    factory = PROVIDER_REGISTRY.get(selection.provider)
    if factory is None:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        logger.error(
            "routing selected provider %r which is not registered (known: %s)",
            selection.provider,
            known,
        )
        raise UnknownProviderError(
            f"routing selected unknown provider {selection.provider!r}; known: {known}"
        )
    return factory()
