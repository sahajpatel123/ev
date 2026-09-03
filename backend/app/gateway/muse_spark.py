"""Muse Spark 1.3 Contributor — Meta Model API chat completions adapter.

One general-purpose cloud brain. Does not own Core, Policy, Executor, or TTS.
Fail closed when META_MODEL_API_KEY is missing. No silent provider substitution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.contracts import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.gateway.muse import (
    MuseProviderUnavailable,
    muse_base_url,
    muse_spark_model,
    muse_spark_reasoning_effort,
    note_spark_call,
    require_muse_key,
)
from app.gateway.providers import DeepSeekProvider
from app.gateway.reliability import (
    CIRCUIT_BREAKERS,
    CircuitOpenError,
    is_transient,
    max_attempts,
    wait_for_retry,
    http_timeout,
)


class MuseSparkProvider(DeepSeekProvider):
    """OpenAI-compatible Chat Completions against api.meta.ai."""

    name = "meta_muse_spark"
    supports_media = True
    supports_tools = True

    def _thinking_payload(self) -> dict | None:
        return None

    def _credential(self) -> str:
        return (self.api_key or "").strip() or muse_api_key()

    def _headers(self) -> dict:
        key = self._credential()
        if not key:
            raise MuseProviderUnavailable(
                "Muse Spark is unavailable: META_MODEL_API_KEY is missing"
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _payload_extras(self) -> dict:
        return {"reasoning_effort": muse_spark_reasoning_effort()}

    def _apply_provider_payload(self, payload: dict, *, temperature: float) -> dict:
        # Spark samples at provider default (1.0). Do not send a competing
        # temperature; reasoning_effort=high is the one fixed intelligence knob.
        del temperature
        payload.pop("temperature", None)
        payload.pop("thinking", None)
        payload.update(self._payload_extras())
        return payload

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        result = await super().chat(messages, model=model, temperature=temperature)
        note_spark_call(usage=result.usage, model=result.model)
        return result

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        result = await super().chat_with_tools(
            messages, tools, model=model, temperature=temperature
        )
        note_spark_call(usage=result.usage, model=result.model)
        return result

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator:
        async for chunk in super().stream_chat(
            messages, model=model, temperature=temperature
        ):
            if getattr(chunk, "done", False):
                note_spark_call(
                    usage=getattr(chunk, "usage", None) or {},
                    model=getattr(chunk, "model", None),
                )
            yield chunk

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict[str, Any]:
        """POST chat/completions with already-shaped OpenAI messages.

        Used by the coding tool loop, which must round-trip ``tool_calls``.
        """

        if not self._credential():
            raise MuseProviderUnavailable(
                "Muse Spark is unavailable: META_MODEL_API_KEY is missing"
            )
        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        payload: dict[str, Any] = {
            "model": model or self.default_model or muse_spark_model(),
            "messages": messages,
        }
        payload = self._apply_provider_payload(payload, temperature=1.0)
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
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
                data = resp.json()
                usage = data.get("usage") if isinstance(data, dict) else {}
                note_spark_call(
                    usage=usage if isinstance(usage, dict) else {},
                    model=data.get("model") if isinstance(data, dict) else None,
                )
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {401, 403}:
                    raise MuseProviderUnavailable("Muse Spark credential was rejected") from exc
                transient = is_transient(exc, status_code)
                if transient:
                    breaker.record_failure()
                    if attempt + 1 < attempts:
                        await wait_for_retry(attempt)
                        continue
                raise
        raise MuseProviderUnavailable("Muse Spark request failed")

    async def chat_structured(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str = "turn_intent",
        model: str | None = None,
    ) -> ChatResult:
        data = await self.complete_raw(
            [self._message_payload(m) for m in messages],
            model=model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        choice = (data.get("choices") or [{}])[0].get("message") or {}
        tool_calls = []
        for call in choice.get("tool_calls") or []:
            fn = call.get("function") or {}
            import json

            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"raw": fn.get("arguments")}
            tool_calls.append(ToolCall(id=call.get("id", ""), name=fn.get("name", ""), arguments=args))
        return ChatResult(
            text=choice.get("content") or "",
            tool_calls=tool_calls,
            usage=data.get("usage") or {},
            model=data.get("model"),
        )


def responses_tools_to_chat_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI Responses function specs to Chat Completions tools."""

    out: list[dict[str, Any]] = []
    for spec in specs:
        if spec.get("type") == "function" and "function" in spec:
            out.append(spec)
            continue
        name = spec.get("name")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description") or "",
                    "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def muse_spark_provider() -> MuseSparkProvider:
    return MuseSparkProvider(
        base_url=muse_base_url(),
        api_key=require_muse_key(role="Muse Spark"),
        default_model=muse_spark_model(),
        provider_name="meta_muse_spark",
    )
