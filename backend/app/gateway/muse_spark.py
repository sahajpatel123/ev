"""Muse Spark 1.3 Contributor provider.

Spark Contributor is served by the official Meta Model API OpenAI-compatible
Responses endpoint (``https://api.meta.ai/v1``). EV keeps the provider behind
the normal :class:`ChatProvider` contract so the rest of the application never
has to know whether a turn came from Responses, Chat Completions, or a local
test provider.

The Responses adapter is deliberately state-free: EV owns conversation
history, turn identity, policy, and execution. Constructor ``base_url=`` is
honored for hermetic tests; the factory always uses ``muse_spark_base_url()``,
which remaps leftover OpenCode Zen URLs to Meta.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.contracts import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.gateway.muse import (
    MuseProviderUnavailable,
    muse_spark_api_key,
    muse_spark_base_url,
    muse_spark_model,
    muse_spark_reasoning_effort,
    note_spark_call,
    require_muse_spark_key,
)
from app.gateway.reliability import (
    CIRCUIT_BREAKERS,
    CircuitOpenError,
    ProviderStreamError,
    http_timeout,
    http_timeout_for_media,
    is_transient,
    max_attempts,
    wait_for_retry,
)
from app.gateway.streaming import ChatStreamChunk, StreamingChatProvider

MUSE_SPARK_PROVIDER = "meta_muse_spark"
_MAX_ID_LENGTH = 128


def _clean_id(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    return raw[:_MAX_ID_LENGTH] or fallback


def _json_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        return {"raw": str(raw)}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"raw": raw[:16_000]}
    return dict(parsed) if isinstance(parsed, dict) else {"raw": parsed}


def _text_from_output(output: Sequence[Any]) -> str:
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text":
            chunks.append(str(item.get("text") or ""))
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    chunks.append(str(content.get("text") or ""))
    return "".join(part for part in chunks if part)


def _usage_for_ev(usage: Any) -> dict[str, Any]:
    """Normalize Responses usage while preserving provider detail fields."""

    if not isinstance(usage, dict):
        return {}
    result = dict(usage)
    input_tokens = int(
        usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input") or 0
    )
    output_tokens = int(
        usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("output") or 0
    )
    input_detail = usage.get("input_tokens_details")
    if not isinstance(input_detail, dict):
        input_detail = usage.get("prompt_tokens_details")
    output_detail = usage.get("output_tokens_details")
    if not isinstance(output_detail, dict):
        output_detail = usage.get("completion_tokens_details")
    cached = int(
        (input_detail or {}).get("cached_tokens")
        or (input_detail or {}).get("cache_read")
        or usage.get("cached_tokens")
        or 0
    )
    reasoning = int(
        (output_detail or {}).get("reasoning_tokens")
        or usage.get("reasoning_tokens")
        or 0
    )
    result.setdefault("input_tokens", input_tokens)
    result.setdefault("output_tokens", output_tokens)
    result.setdefault("prompt_tokens", input_tokens)
    result.setdefault("completion_tokens", output_tokens)
    result.setdefault("total_tokens", int(usage.get("total_tokens") or input_tokens + output_tokens))
    result.setdefault("cached_tokens", cached)
    result.setdefault("reasoning_tokens", reasoning)
    return result


def _tool_from_response(item: dict[str, Any], index: int) -> ToolCall:
    return ToolCall(
        id=_clean_id(item.get("call_id") or item.get("id") or item.get("item_id"), f"call_{index}"),
        name=str(item.get("name") or "").strip(),
        arguments=_json_arguments(item.get("arguments")),
    )


def _message_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Chat Completions-style calls from a raw assistant row."""

    calls: list[dict[str, Any]] = []
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        args = function.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or call.get("arguments") or {}, default=str)
        calls.append(
            {
                "id": _clean_id(call.get("id"), f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or call.get("name") or ""),
                    "arguments": args,
                },
            }
        )
    return calls


def _raw_input_items(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert EV chat rows or raw loop rows into Responses input."""

    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item_type = str(message.get("type") or "").strip()
        if item_type in {"function_call", "function_call_output"}:
            copied = dict(message)
            if item_type == "function_call":
                copied["call_id"] = _clean_id(copied.get("call_id") or copied.get("id"), "call_0")
            else:
                copied["call_id"] = _clean_id(copied.get("call_id"), "call_0")
            items.append(copied)
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        content = message.get("content")
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": _clean_id(call_id, "call_0"),
                        "output": str(content or ""),
                    }
                )
            else:
                label = str(message.get("name") or "tool").strip() or "tool"
                items.append({"role": "user", "content": f"ACTION RESULT ({label}): {content or ''}"})
            continue
        if role == "assistant":
            if content:
                items.append({"role": "assistant", "content": content})
            for call in _message_tool_calls(message):
                fn = call["function"]
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": fn["name"],
                        "arguments": fn["arguments"],
                    }
                )
            continue
        items.append({"role": role, "content": content or ""})
    return items


def responses_tools(specs: Sequence[ToolSpec | dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize EV and Chat Completions tool specs to Responses shape."""

    out: list[dict[str, Any]] = []
    for spec in specs:
        if isinstance(spec, ToolSpec):
            out.append(
                {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description or "",
                    "parameters": spec.parameters or {"type": "object", "properties": {}},
                }
            )
            continue
        if not isinstance(spec, dict):
            continue
        function = spec.get("function")
        if isinstance(function, dict):
            name = function.get("name") or spec.get("name")
            description = function.get("description") or spec.get("description") or ""
            parameters = function.get("parameters") or spec.get("parameters")
        else:
            name = spec.get("name")
            description = spec.get("description") or ""
            parameters = spec.get("parameters")
        if name:
            out.append(
                {
                    "type": "function",
                    "name": str(name),
                    "description": str(description),
                    "parameters": parameters or {"type": "object", "properties": {}},
                }
            )
    return out


def _payload_has_input_image(payload: dict[str, Any]) -> bool:
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            parts = content
        elif isinstance(content, dict):
            parts = [content]
        else:
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "input_image":
                return True
    return False


def _spark_timeout(payload: dict[str, Any] | None = None) -> httpx.Timeout:
    if payload and _payload_has_input_image(payload):
        return http_timeout_for_media()
    return http_timeout()


class MuseSparkProvider(StreamingChatProvider):
    """Provider-neutral EV adapter for ``muse-spark-1.3-contributor``."""

    name = MUSE_SPARK_PROVIDER
    supports_media = True
    supports_tools = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        provider_name: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.base_url = (base_url or muse_spark_base_url()).rstrip("/")
        self.api_key = api_key
        self.default_model = muse_spark_model()
        # The contributor model is a hard project decision. An old caller may
        # pass GPT/Luna/1.3; it must not change the active model.
        del default_model
        if provider_name:
            self.name = provider_name
        self.session_id = _clean_id(session_id, f"evie-{uuid.uuid4().hex}")

    def _credential(self) -> str:
        return (self.api_key or "").strip() or muse_spark_api_key()

    def _headers(self) -> dict[str, str]:
        key = self._credential()
        if not key:
            raise MuseProviderUnavailable(
                "Muse Spark is unavailable: META_MODEL_API_KEY is missing"
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "evie-muse-spark/1",
        }

    def _destination_is_zen(self) -> bool:
        low = (self.base_url or "").lower()
        return "opencode.ai" in low or "/zen/" in low

    def _note_call(self, *, usage: dict | None = None, model: str | None = None) -> None:
        note_spark_call(usage=usage, model=model, destination=self.base_url)

    def _unavailable_from_http(self, status: int, body: str = "") -> MuseProviderUnavailable:
        low = (body or "").lower()
        if self._destination_is_zen():
            if "creditserror" in low or "insufficient balance" in low:
                return MuseProviderUnavailable(
                    "Muse Spark is unavailable: OpenCode Zen has insufficient balance"
                )
            if "1010" in body:
                return MuseProviderUnavailable(
                    "Muse Spark is unavailable: OpenCode Zen blocked the client"
                )
            return MuseProviderUnavailable(
                "Muse Spark credential was rejected by OpenCode Zen"
            )
        if status == 402 or "billing" in low:
            return MuseProviderUnavailable(
                "Muse Spark is unavailable: official Meta Model API billing/"
                "account permission denied"
            )
        if status == 404 or "model_not_found" in low:
            return MuseProviderUnavailable(
                "Muse Spark is unavailable: official Meta Model API does not "
                "expose muse-spark-1.3-contributor for this credential "
                "(model_not_found)"
            )
        if status == 403:
            return MuseProviderUnavailable(
                "Muse Spark is unavailable: official Meta Model API key lacks "
                "permission for muse-spark-1.3-contributor"
            )
        if status == 401 or "invalid_api_key" in low or "authentication_error" in low:
            return MuseProviderUnavailable(
                "Muse Spark credential was rejected by the official Meta Model API"
            )
        return MuseProviderUnavailable(
            f"Muse Spark is unavailable: official Meta Model API HTTP {status}"
        )

    def _stream_headers(self) -> dict[str, str]:
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        return headers

    def _resolve_model(self, model: str | None) -> str:
        del model
        return muse_spark_model()

    def _http_timeout_kwargs(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = getattr(self, "_keepalive_client", None)
        if client is not None and type(client).__module__.startswith("httpx"):
            return {"timeout": _spark_timeout(payload)}
        return {}

    def _http(self) -> httpx.AsyncClient:
        """Reuse one client so tool-loop steps keep the TLS session warm."""

        client = getattr(self, "_keepalive_client", None)
        closed = True if client is None else bool(getattr(client, "is_closed", False))
        if closed:
            self._keepalive_client = httpx.AsyncClient(
                timeout=http_timeout(),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            )
        return self._keepalive_client

    def _identified(self, result: ChatResult, requested: str | None = None) -> ChatResult:
        del requested
        result.model = muse_spark_model()
        return result

    def _conversation_for_meta(self, messages: Sequence[ChatMessage]) -> list[ChatMessage]:
        """Compatibility normalizer used by old callers and unit fixtures."""

        legal: list[ChatMessage] = []
        open_ids: set[str] = set()
        for message in messages:
            role = (message.role or "").strip().lower()
            if role == "assistant":
                calls = list(message.tool_calls or [])
                open_ids = {(call.id or "").strip() for call in calls if call.id}
                legal.append(message)
            elif role == "tool" and (message.tool_call_id or "").strip() in open_ids:
                legal.append(message)
            elif role == "tool":
                label = (message.name or "tool").strip() or "tool"
                legal.append(ChatMessage(role="user", content=f"ACTION RESULT ({label}): {message.content}"))
            else:
                open_ids = set()
                legal.append(message)
        return legal

    def _message_payload(self, message: ChatMessage) -> dict[str, Any]:
        """Render an EV message into the Responses input-compatible shape.

        Text-only rows intentionally remain the compact string form. Media
        rows use the Responses content-part names so image/audio attachments
        survive the provider swap instead of being silently dropped.
        """

        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.media:
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "input_text", "text": message.content})
            for part in message.media:
                if part.kind == "image" and part.data_url:
                    parts.append({"type": "input_image", "image_url": part.data_url})
                elif part.kind == "audio" and part.data_url:
                    data = part.data_url
                    fmt = "wav"
                    if data.startswith("data:"):
                        header, _, encoded = data.partition(",")
                        data = encoded
                        if "audio/mpeg" in header:
                            fmt = "mp3"
                        elif "audio/mp4" in header or "audio/aac" in header:
                            fmt = "mp4"
                    parts.append({"type": "input_audio", "input_audio": {"data": data, "format": fmt}})
                elif part.text:
                    parts.append({"type": "input_text", "text": part.text})
            if parts:
                payload["content"] = parts
        if message.name:
            payload["name"] = message.name
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": _clean_id(call.id, f"call_{index}"),
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments or {}, default=str),
                    },
                }
                for index, call in enumerate(message.tool_calls)
            ]
            if not (message.content or "").strip():
                payload["content"] = None
        if message.tool_call_id:
            payload["tool_call_id"] = _clean_id(message.tool_call_id, "call_0")
        return payload

    def _responses_message(self, message: ChatMessage) -> dict[str, Any]:
        """Render an EV message as a Responses item, preserving media parts."""

        if not message.media:
            payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": _clean_id(call.id, f"call_{index}"),
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments or {}, default=str),
                        },
                    }
                    for index, call in enumerate(message.tool_calls)
                ]
                if not (message.content or "").strip():
                    payload["content"] = None
            if message.tool_call_id:
                payload["tool_call_id"] = _clean_id(message.tool_call_id, "call_0")
            return payload
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "input_text", "text": message.content})
        for media in message.media:
            if media.kind == "image" and media.data_url:
                parts.append({"type": "input_image", "image_url": media.data_url})
            elif media.kind in {"document", "text"} and media.data_url:
                parts.append({"type": "input_file", "file_data": media.data_url})
            elif media.text:
                parts.append({"type": "input_text", "text": media.text})
            elif media.ref:
                parts.append({"type": "input_text", "text": f"[media ref: {media.ref}]"})
        payload = {
            "role": message.role,
            "content": parts or [{"type": "input_text", "text": ""}],
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": _clean_id(call.id, f"call_{index}"),
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments or {}, default=str),
                    },
                }
                for index, call in enumerate(message.tool_calls)
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = _clean_id(message.tool_call_id, "call_0")
        return payload

    def _apply_provider_payload(
        self,
        payload: dict[str, Any],
        *,
        temperature: float,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        del temperature
        payload.pop("thinking", None)
        payload.pop("temperature", None)
        payload.pop("stream_options", None)
        effort = (reasoning_effort or "").strip().lower() or muse_spark_reasoning_effort()
        if "input" in payload:
            payload["reasoning"] = {"effort": effort}
        else:
            # Compatibility inspection value only; direct traffic uses the
            # Responses ``reasoning`` object above.
            payload["reasoning_effort"] = effort
        return payload

    def _text_format(self, response_format: dict[str, Any] | None) -> dict[str, Any] | None:
        if not response_format or response_format.get("type") != "json_schema":
            return None
        schema = response_format.get("json_schema") or {}
        return {
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.get("name") or "evie_response",
                    "schema": schema.get("schema") or {"type": "object"},
                    "strict": bool(schema.get("strict", False)),
                }
            }
        }

    def _payload(
        self,
        messages: Sequence[ChatMessage] | Sequence[dict[str, Any]],
        *,
        model: str | None,
        tools: Sequence[ToolSpec | dict[str, Any]] | None,
        stream: bool,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | dict | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        raw_messages = [self._responses_message(item) if isinstance(item, ChatMessage) else item for item in messages]
        payload: dict[str, Any] = {
            "model": self._resolve_model(model),
            "input": _raw_input_items(raw_messages),
            "stream": stream,
        }
        if tools:
            payload["tools"] = responses_tools(tools)
        payload.update(self._text_format(response_format) or {})
        if tool_choice is not None and (isinstance(tool_choice, dict) or str(tool_choice).strip().lower() in {"auto", "none", "required"}):
            payload["tool_choice"] = tool_choice
        return self._apply_provider_payload(
            payload, temperature=1.0, reasoning_effort=reasoning_effort
        )

    def _result_from_response(self, data: dict[str, Any]) -> ChatResult:
        # Accept a Chat Completions fixture/proxy at the adapter boundary, but
        # always expose the contributor model to EV.
        if isinstance(data.get("choices"), list) and not data.get("output"):
            choice = (data.get("choices") or [{}])[0].get("message") or {}
            calls: list[ToolCall] = []
            for index, call in enumerate(choice.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                calls.append(ToolCall(id=_clean_id(call.get("id"), f"call_{index}"), name=str(fn.get("name") or ""), arguments=_json_arguments(fn.get("arguments"))))
            return self._identified(ChatResult(text=str(choice.get("content") or ""), tool_calls=calls, usage=_usage_for_ev(data.get("usage")), model=data.get("model")))
        output = data.get("output") or []
        if not isinstance(output, list):
            output = []
        calls = [_tool_from_response(item, index) for index, item in enumerate(output) if isinstance(item, dict) and item.get("type") == "function_call"]
        text = str(data.get("output_text") or "") or _text_from_output(output)
        return self._identified(ChatResult(text=text, tool_calls=calls, usage=_usage_for_ev(data.get("usage")), model=data.get("model")))

    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        attempts = max_attempts()
        for attempt in range(attempts):
            try:
                client = self._http()
                post_kwargs: dict[str, Any] = {
                    "headers": self._headers(),
                    "json": payload,
                    **self._http_timeout_kwargs(payload),
                }
                response = await client.post(
                    f"{self.base_url}/responses",
                    **post_kwargs,
                )
                # A real httpx response always has ``status_code``. The
                # defensive default keeps small deterministic adapter
                # fixtures useful without weakening the real HTTP checks.
                status = getattr(response, "status_code", 200)
                if status in {401, 402, 403, 404}:
                    body = ""
                    try:
                        body = response.text or ""
                    except Exception:
                        body = ""
                    raise self._unavailable_from_http(status, body)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MuseProviderUnavailable("Muse Spark returned a non-object response")
                breaker.record_success()
                return data
            except MuseProviderUnavailable:
                breaker.record_failure()
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if is_transient(exc, status) and attempt + 1 < attempts:
                    breaker.record_failure()
                    await wait_for_retry(attempt)
                    continue
                breaker.record_failure()
                raise
        raise MuseProviderUnavailable("Muse Spark request failed")

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        result = self._result_from_response(
            await self._post_json(
                self._payload(
                    messages,
                    model=model,
                    tools=None,
                    stream=False,
                    reasoning_effort=reasoning_effort,
                )
            )
        )
        self._note_call(usage=result.usage, model=result.model)
        return result

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        result = self._result_from_response(
            await self._post_json(
                self._payload(
                    messages,
                    model=model,
                    tools=tools,
                    stream=False,
                    reasoning_effort=reasoning_effort,
                )
            )
        )
        self._note_call(usage=result.usage, model=result.model)
        return result

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict[str, Any]:
        """Raw Responses call with a compatibility ``choices`` projection."""

        data = await self._post_json(self._payload(messages, model=model, tools=tools, stream=False, response_format=response_format, tool_choice=tool_choice))
        result = self._result_from_response(data)
        self._note_call(usage=result.usage, model=result.model)
        projected = dict(data)
        projected["model"] = muse_spark_model()
        projected.setdefault("usage", result.usage)
        # Always project from parsed tool_calls. Meta Responses often returns
        # function_call items on `output` plus a prose `choices` message that
        # claims the work is done. The coding loop reads `choices`; if we keep
        # the provider's choices, Evie speaks success and never writes files.
        projected["choices"] = [
            {
                "message": {
                    "role": "assistant",
                    "content": result.text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments or {}, default=str),
                            },
                        }
                        for call in (result.tool_calls or [])
                        if call.name
                    ],
                }
            }
        ]
        return projected

    async def chat_structured(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str = "turn_intent",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        data = await self._post_json(
            self._payload(
                messages,
                model=model,
                tools=None,
                stream=False,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": False},
                },
                reasoning_effort=reasoning_effort,
            )
        )
        result = self._result_from_response(data)
        self._note_call(usage=result.usage, model=result.model)
        return result

    async def list_models(self) -> list[str]:
        """Satisfy the shared provider contract without a discovery call.

        Meta Model API exposes a fixed project-selected Contributor slot. A
        discovery request would add latency and could accidentally surface a
        different model to EV's health/API surfaces.
        """

        return [muse_spark_model()]

    @staticmethod
    def _stream_calls(buffers: dict[str, dict[str, str]]) -> list[ToolCall]:
        return [ToolCall(id=value["id"], name=value["name"], arguments=_json_arguments(value.get("arguments"))) for value in buffers.values() if value.get("name")]

    async def stream_chat(self, messages: Sequence[ChatMessage], *, model: str | None = None, temperature: float = 0.7, tools: Sequence[ToolSpec] | None = None) -> AsyncIterator[ChatStreamChunk]:
        """Stream Responses deltas and emit native function calls at the end."""

        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        payload = self._payload(messages, model=model, tools=tools, stream=True)
        attempts = max_attempts()
        attempt = 0
        emitted = False
        completed = False
        buffers: dict[str, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        resolved_model = muse_spark_model()
        try:
            while True:
                started_stream = False
                try:
                    client = self._http()
                    stream_kwargs: dict[str, Any] = {
                        "headers": self._stream_headers(),
                        "json": payload,
                        **self._http_timeout_kwargs(payload),
                    }
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/responses",
                        **stream_kwargs,
                    ) as response:
                            status = getattr(response, "status_code", 200)
                            if status in {401, 402, 403, 404}:
                                body = ""
                                try:
                                    body = await response.aread()
                                    body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
                                except Exception:
                                    body = ""
                                raise self._unavailable_from_http(status, body)
                            response.raise_for_status()
                            event_name = ""
                            async for line in response.aiter_lines():
                                if line.startswith("event:"):
                                    event_name = line[6:].strip()
                                    continue
                                if not line.startswith("data:"):
                                    continue
                                raw = line[5:].strip()
                                if not raw:
                                    continue
                                if raw == "[DONE]":
                                    completed = True
                                    break
                                try:
                                    event = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(event, dict):
                                    continue
                                started_stream = True
                                # A few existing local/test transports still
                                # emit Chat Completions SSE rows. Accept those
                                # rows at the edge while Meta Model API traffic
                                # remains Responses-native. This makes a
                                # provider migration safe for typed/live
                                # continuation fixtures and older proxies.
                                choices = event.get("choices")
                                if isinstance(choices, list) and choices:
                                    choice = choices[0] if isinstance(choices[0], dict) else {}
                                    delta = choice.get("delta") or {}
                                    if isinstance(delta, dict):
                                        text_delta = str(delta.get("content") or "")
                                        if text_delta:
                                            emitted = True
                                            yield ChatStreamChunk(text=text_delta, model=resolved_model)
                                        for index, raw_call in enumerate(delta.get("tool_calls") or []):
                                            if not isinstance(raw_call, dict):
                                                continue
                                            fn = raw_call.get("function") or {}
                                            key = _clean_id(raw_call.get("id"), f"call_{index}")
                                            current = buffers.setdefault(
                                                key,
                                                {"id": key, "name": "", "arguments": ""},
                                            )
                                            if fn.get("name"):
                                                current["name"] = str(fn["name"])
                                            if fn.get("arguments"):
                                                current["arguments"] += str(fn["arguments"])
                                    if isinstance(event.get("usage"), dict):
                                        usage = _usage_for_ev(event["usage"])
                                    if choice.get("finish_reason"):
                                        completed = True
                                    continue
                                kind = str(event.get("type") or event_name or "").strip()
                                if kind in {"error", "response.error", "response.failed"}:
                                    detail = event.get("error") or event.get("message") or "provider error"
                                    raise MuseProviderUnavailable(f"Muse Spark response failed: {detail}")
                                if kind == "response.output_text.delta":
                                    delta = str(event.get("delta") or "")
                                    if delta:
                                        emitted = True
                                        yield ChatStreamChunk(text=delta, model=resolved_model)
                                    continue
                                if kind in {"response.output_item.added", "response.output_item.done"}:
                                    item = event.get("item")
                                    if isinstance(item, dict) and item.get("type") == "function_call":
                                        key = _clean_id(item.get("call_id") or item.get("id") or event.get("item_id"), f"call_{len(buffers)}")
                                        current = buffers.setdefault(key, {"id": key, "name": "", "arguments": ""})
                                        current["name"] = str(item.get("name") or current["name"])
                                        if item.get("arguments") is not None:
                                            current["arguments"] = str(item.get("arguments") or "")
                                    continue
                                if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                                    key = _clean_id(event.get("call_id") or event.get("item_id"), f"call_{len(buffers)}")
                                    current = buffers.setdefault(key, {"id": key, "name": "", "arguments": ""})
                                    if event.get("name"):
                                        current["name"] = str(event["name"])
                                    if kind.endswith(".delta"):
                                        current["arguments"] += str(event.get("delta") or "")
                                    elif event.get("arguments") is not None:
                                        current["arguments"] = str(event.get("arguments") or "")
                                    continue
                                if kind == "response.completed":
                                    response_body = event.get("response")
                                    if isinstance(response_body, dict):
                                        usage = _usage_for_ev(response_body.get("usage"))
                                        output = response_body.get("output") or []
                                        for index, item in enumerate(output):
                                            if isinstance(item, dict) and item.get("type") == "function_call":
                                                call = _tool_from_response(item, index)
                                                current = buffers.setdefault(call.id, {"id": call.id, "name": call.name, "arguments": ""})
                                                current["name"] = call.name
                                                current["arguments"] = json.dumps(call.arguments, default=str)
                                        if not emitted:
                                            fallback_text = str(response_body.get("output_text") or "") or _text_from_output(output)
                                            if fallback_text:
                                                emitted = True
                                                yield ChatStreamChunk(text=fallback_text, model=resolved_model)
                                    completed = True
                            await response.aclose()
                    if not completed:
                        raise ProviderStreamError("Muse Spark stream ended before response.completed")
                    if not emitted and not self._stream_calls(buffers):
                        raise ProviderStreamError("Muse Spark completed with no text or tools")
                    breaker.record_success()
                    break
                except MuseProviderUnavailable:
                    breaker.record_failure()
                    raise
                except ProviderStreamError:
                    breaker.record_failure()
                    raise
                except httpx.HTTPStatusError as exc:
                    status = getattr(exc.response, "status_code", None)
                    if status in {401, 403}:
                        breaker.record_failure()
                        raise MuseProviderUnavailable(
                            "Muse Spark credential was rejected by the official Meta Model API"
                        ) from exc
                    if is_transient(exc, status) and attempt + 1 < attempts and not started_stream:
                        breaker.record_failure()
                        attempt += 1
                        await wait_for_retry(attempt - 1)
                        continue
                    breaker.record_failure()
                    raise
                except asyncio.CancelledError:
                    raise
                except httpx.TransportError as exc:
                    if is_transient(exc) and attempt + 1 < attempts and not started_stream:
                        breaker.record_failure()
                        attempt += 1
                        await wait_for_retry(attempt - 1)
                        continue
                    breaker.record_failure()
                    if started_stream:
                        raise ProviderStreamError(f"Muse Spark stream failed after partial output: {exc}") from exc
                    raise
        finally:
            if completed:
                self._note_call(usage=usage, model=resolved_model)
        yield ChatStreamChunk(text="", usage=usage, model=resolved_model, tool_calls=self._stream_calls(buffers), finish_reason="tool_calls" if buffers else "stop", done=True)


def responses_tools_to_chat_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility name; callers receive Responses-compatible functions."""

    return responses_tools(specs)


def muse_spark_provider() -> MuseSparkProvider:
    return MuseSparkProvider(
        base_url=muse_spark_base_url(),
        api_key=require_muse_spark_key(role="Muse Spark"),
        default_model=muse_spark_model(),
        provider_name=MUSE_SPARK_PROVIDER,
    )
