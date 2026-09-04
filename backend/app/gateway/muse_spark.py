"""Muse Spark 1.3 Contributor — Meta Model API chat completions adapter.

One general-purpose cloud brain. Does not own Core, Policy, Executor, or TTS.
Fail closed when META_MODEL_API_KEY is missing. No silent provider substitution.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.contracts import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.gateway.muse import (
    MuseProviderUnavailable,
    muse_api_key,
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


_CHAT_MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")


def _sanitize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep Chat Completions history Meta-legal.

    Spark replies can include reasoning fields. Replaying those on the next
    turn is an unsupported parameter / malformed conversation (HTTP 400).
    """

    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = {key: message[key] for key in _CHAT_MESSAGE_KEYS if key in message}
        if "role" not in item:
            continue
        cleaned.append(item)
    return cleaned


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

    def _stream_headers(self) -> dict:
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        return headers

    def _resolve_model(self, model: str | None) -> str:
        """Spark is the only Meta chat model Evie may call.

        Leftover client IDs (Grok, Luna, DeepSeek) must not be forwarded;
        Meta would 400 and Talk would mute.
        """

        spark = (self.default_model or muse_spark_model()).strip()
        requested = (model or "").strip()
        if not requested:
            return spark
        if "muse-spark" in requested.lower():
            return requested
        return spark

    def _identified(self, result: ChatResult, requested: str | None) -> ChatResult:
        """Stamp Spark's id when Meta omits model or echoes a leftover name."""

        spark = self._resolve_model(requested)
        current = (result.model or "").strip()
        if current and "muse-spark" in current.lower():
            return result
        result.model = spark
        return result

    def _payload_extras(self) -> dict:
        return {"reasoning_effort": muse_spark_reasoning_effort()}

    def _raise_if_auth_rejected(self, exc: BaseException) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {401, 403}:
            raise MuseProviderUnavailable("Muse Spark credential was rejected") from exc

    def _conversation_for_meta(self, messages: Sequence[ChatMessage]) -> list[ChatMessage]:
        """Meta 400s orphan ``role=tool`` rows. Fold action receipts into user text."""

        legal: list[ChatMessage] = []
        open_ids: set[str] = set()
        for message in messages:
            role = (message.role or "").strip().lower()
            if role == "assistant":
                calls = list(message.tool_calls or [])
                open_ids = {
                    (call.id or "").strip()
                    for call in calls
                    if (call.id or "").strip()
                }
                legal.append(message)
                continue
            if role == "tool":
                tool_call_id = (message.tool_call_id or "").strip()
                if tool_call_id and tool_call_id in open_ids:
                    legal.append(message)
                    continue
                label = (message.name or "tool").strip() or "tool"
                legal.append(
                    ChatMessage(
                        role="user",
                        content=f"ACTION RESULT ({label}): {message.content}",
                    )
                )
                continue
            open_ids = set()
            legal.append(message)
        return legal

    def _message_payload(self, message: ChatMessage) -> dict:
        payload = super()._message_payload(message)
        calls = list(message.tool_calls or [])
        if calls:
            payload["tool_calls"] = [
                {
                    "id": ((call.id or f"call_{index}")[:64]),
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments or {}, default=str),
                    },
                }
                for index, call in enumerate(calls)
            ]
            if not (message.content or "").strip():
                payload["content"] = None
        tool_call_id = (message.tool_call_id or "").strip()
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id[:64]
        return payload

    async def _complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None,
        temperature: float,
        tools: Sequence[ToolSpec] | None = None,
    ) -> ChatResult:
        try:
            return await super()._complete(
                self._conversation_for_meta(messages),
                model=self._resolve_model(model),
                temperature=temperature,
                tools=tools,
            )
        except httpx.HTTPStatusError as exc:
            self._raise_if_auth_rejected(exc)
            raise

    def _apply_provider_payload(self, payload: dict, *, temperature: float) -> dict:
        # Spark samples at provider default (1.0). Do not send a competing
        # temperature; reasoning_effort=high is the one fixed intelligence knob.
        del temperature
        payload.pop("temperature", None)
        payload.pop("thinking", None)
        # Meta streaming examples send stream=true only. OpenAI's
        # stream_options.include_usage is not in the Chat Completions table and
        # an unsupported parameter is HTTP 400 — which would mute Talk.
        payload.pop("stream_options", None)
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
        return self._identified(result, model)

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
        return self._identified(result, model)

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator:
        try:
            async for chunk in super().stream_chat(
                self._conversation_for_meta(messages),
                model=self._resolve_model(model),
                temperature=temperature,
            ):
                if getattr(chunk, "done", False):
                    note_spark_call(
                        usage=getattr(chunk, "usage", None) or {},
                        model=getattr(chunk, "model", None),
                    )
                yield chunk
        except httpx.HTTPStatusError as exc:
            self._raise_if_auth_rejected(exc)
            raise

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
            "model": self._resolve_model(model),
            "messages": _sanitize_chat_messages(messages),
        }
        payload = self._apply_provider_payload(payload, temperature=1.0)
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        # Meta Chat Completions only accepts tool_choice=auto. Omit otherwise.
        if tool_choice is not None and str(tool_choice).strip().lower() == "auto":
            payload["tool_choice"] = "auto"
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
                    # Omit strict. Meta defaults false; TurnIntent does not
                    # list every property in required, which 400s under strict.
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
        return self._identified(
            ChatResult(
                text=choice.get("content") or "",
                tool_calls=tool_calls,
                usage=data.get("usage") or {},
                model=data.get("model"),
            ),
            model,
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
