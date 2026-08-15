"""Neutral model gateway: envelope contract, provider call, validation, audit data.

The gateway is the only place EV talks to a reasoning provider. It carries the
complete request envelope (strategy, memories, request id, metadata), measures
and reports every call, and pre-validates model tool invocations before anything
can be executed. Swapping the provider remains a configuration change; EV's
identity and behavior live outside this module.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from app.contracts import (
    ChatMessage,
    ChatProvider,
    ChatResult,
    RequestEnvelope,
    ToolCall,
    ToolSpec,
)
from app.ev.actions import life_agency_prompt
from app.gateway.costs import CostCapExceeded
from app.gateway.reliability import CircuitOpenError, ProviderStreamError
from app.gateway.routing import ProviderSelection
from app.gateway.streaming import StreamingChatProvider
from app.gateway.validation import ValidatedToolCall, validate_tool_calls
from app.security.boundary import ModelBoundaryViolation, guard_model_payload

LIFE_TOOL_PERMISSIONS = frozenset(
    {
        "message:send",
        "message:read",
        "phone:act",
        "mail:read",
        "mail:act",
        "contacts:read",
        "life:open_url",
        "life:reminder",
    }
)


_YOU_ARE_RE = re.compile(r"You are ([^,\n]+)")


def _spoken_name_for_agency(
    messages: list[ChatMessage],
    envelope: RequestEnvelope | None = None,
) -> str:
    """Prefer envelope spoken_name, then the identity prefix, then EVIE."""

    from app.ev.assistant import spoken_name

    meta = (envelope.metadata if envelope is not None else None) or {}
    explicit = meta.get("spoken_name")
    if explicit:
        return spoken_name(str(explicit))
    for message in messages:
        if message.role != "system":
            continue
        match = _YOU_ARE_RE.search(message.content or "")
        if match:
            return spoken_name(match.group(1).strip())
    return spoken_name(None)


def _with_life_agency_prompt(
    messages: list[ChatMessage],
    tool_specs: list[ToolSpec],
    envelope: RequestEnvelope | None = None,
) -> list[ChatMessage]:
    """Attach the life-agency block when life tools are offered.

    This rides the existing system-message path, so it applies to the DeepSeek
    provider and the OpenCode ev-minimal agent alike (the minimal agent is
    instructed to follow the system instructions supplied with the request).
    """

    if not tool_specs or not any(
        spec.permission in LIFE_TOOL_PERMISSIONS for spec in tool_specs
    ):
        return messages
    block = life_agency_prompt(_spoken_name_for_agency(messages, envelope))
    result = list(messages)
    for index in range(len(result) - 1, -1, -1):
        if result[index].role == "system":
            if block not in result[index].content:
                existing = result[index]
                result[index] = ChatMessage(
                    role="system",
                    content=f"{existing.content}\n\n{block}",
                    name=existing.name,
                    media=list(existing.media),
                )
            return result
    result.insert(0, ChatMessage(role="system", content=block))
    return result


@dataclass
class GatewayCall:
    """Result of one auditable model call."""

    provider: str
    request_id: str
    envelope: RequestEnvelope
    result: ChatResult
    tool_validation: list[ValidatedToolCall] = field(default_factory=list)
    latency_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    first_token_ms: float | None = None
    selection: dict | None = None
    degraded: bool = False
    degradation: dict | None = None

    @property
    def model(self) -> str | None:
        return self.result.model

    def usage(self) -> dict:
        return self.result.usage

    def tool_calls_dict(self) -> list[dict]:
        return [v.to_dict() for v in self.tool_validation]


@dataclass
class GatewayStreamEvent:
    """One event from :meth:`ModelGateway.stream_chat`."""

    kind: str  # delta | done | error
    text: str = ""
    model: str | None = None
    call: GatewayCall | None = None
    error: str | None = None


def tool_specs_from_dicts(specs: Sequence[dict]) -> list[ToolSpec]:
    """Convert declarative tool specs (as used by the registry/API) to contracts."""

    converted: list[ToolSpec] = []
    for spec in specs:
        converted.append(
            ToolSpec(
                name=spec["name"],
                description=spec.get("description", ""),
                parameters=spec.get("parameters") or {},
                sensitive=bool(spec.get("sensitive", False)),
                read_only=bool(spec.get("read_only", True)),
                permission=str(spec.get("permission", "memory:read")),
                undoable=bool(spec.get("undoable", False)),
                output=spec.get("output") or {},
            )
        )
    return converted


class ModelGateway:
    """Provider-agnostic chat gateway with envelope + validation + audit payloads."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        selection: ProviderSelection | None = None,
        cost_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.provider = provider
        self.cost_guard = cost_guard
        self.selection = selection or ProviderSelection(
            provider=provider.name,
            reason="configured_provider",
        )

    def _record_selection(self, envelope: RequestEnvelope) -> None:
        envelope.metadata.setdefault("provider_selection", self.selection.to_dict())

    def _degraded_call(
        self,
        *,
        envelope: RequestEnvelope,
        started: float,
        error: str,
        degradation: dict,
        model: str | None,
        status: str = "degraded",
    ) -> GatewayCall:
        envelope.metadata.setdefault("degradation", degradation)
        return GatewayCall(
            provider=self.provider.name,
            request_id=envelope.request_id,
            envelope=envelope,
            result=ChatResult(text="", usage={}, model=model),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            status=status,
            error=error,
            selection=self.selection.to_dict(),
            degraded=True,
            degradation=degradation,
        )

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        envelope: RequestEnvelope | None = None,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        allow_sensitive_tools: bool = False,
    ) -> GatewayCall:
        envelope = envelope or RequestEnvelope(request_id=str(uuid4()), strategy={})
        self._record_selection(envelope)
        tool_specs = list(tools or [])
        started = time.perf_counter()
        error: str | None = None
        status: str = "ok"
        try:
            safe_messages = guard_model_payload(list(messages), envelope)
        except ModelBoundaryViolation as exc:
            return GatewayCall(
                provider=self.provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked",
                error=str(exc),
                selection=self.selection.to_dict(),
            )
        if self.cost_guard is not None:
            try:
                await self.cost_guard()
            except CostCapExceeded as exc:
                return self._degraded_call(
                    envelope=envelope,
                    started=started,
                    error=str(exc),
                    degradation={"kind": "cost_cap", "provider": self.provider.name},
                    model=model,
                    status="error",
                )
        safe_messages = _with_life_agency_prompt(safe_messages, tool_specs, envelope)
        try:
            if tool_specs:
                result = await self.provider.chat_with_tools(
                    safe_messages,
                    tool_specs,
                    model=model,
                    temperature=temperature,
                )
            else:
                result = await self.provider.chat(
                    safe_messages,
                    model=model,
                    temperature=temperature,
                )
        except CircuitOpenError as exc:
            return self._degraded_call(
                envelope=envelope,
                started=started,
                error=str(exc),
                degradation={
                    "kind": "circuit_open",
                    "provider": self.provider.name,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - gateway boundary; errors are audited
            result = ChatResult(text="", usage={}, model=model)
            error = f"{type(exc).__name__}: {exc}"
            status = "error"

        validated = validate_tool_calls(
            result.tool_calls,
            tool_specs,
            sensitive_allowed=allow_sensitive_tools,
        )
        return GatewayCall(
            provider=self.provider.name,
            request_id=envelope.request_id,
            envelope=envelope,
            result=result,
            tool_validation=validated,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            status=status,
            error=error,
            selection=self.selection.to_dict(),
        )

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        envelope: RequestEnvelope | None = None,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        allow_sensitive_tools: bool = False,
        chunk_interceptor: Callable[[str], str | None] | None = None,
    ) -> AsyncIterator[GatewayStreamEvent]:
        """Stream a provider response as delta events, then one done event.

        The returned generator is cancellation-safe: cancelling it (or a
        dropped SSE client) propagates into the provider generator, whose
        ``finally`` closes the upstream HTTP stream. ``chunk_interceptor`` is
        the Agent 16 filter seam: it sees every raw text delta before it is
        emitted and may return ``None`` to suppress the chunk.
        """

        envelope = envelope or RequestEnvelope(request_id=str(uuid4()), strategy={})
        self._record_selection(envelope)
        tool_specs = list(tools or [])
        started = time.perf_counter()
        first_token_ms: float | None = None
        text_parts: list[str] = []
        usage: dict = {}
        tool_calls: list[ToolCall] = []
        result_model: str | None = model

        try:
            safe_messages = guard_model_payload(list(messages), envelope)
        except ModelBoundaryViolation as exc:
            call = GatewayCall(
                provider=self.provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked",
                error=str(exc),
                selection=self.selection.to_dict(),
            )
            yield GatewayStreamEvent(kind="error", error=str(exc))
            yield GatewayStreamEvent(kind="done", call=call)
            return
        if self.cost_guard is not None:
            try:
                await self.cost_guard()
            except CostCapExceeded as exc:
                call = self._degraded_call(
                    envelope=envelope,
                    started=started,
                    error=str(exc),
                    degradation={"kind": "cost_cap", "provider": self.provider.name},
                    model=model,
                    status="error",
                )
                yield GatewayStreamEvent(kind="error", error=str(exc))
                yield GatewayStreamEvent(kind="done", call=call)
                return
        safe_messages = _with_life_agency_prompt(safe_messages, tool_specs, envelope)

        try:
            if isinstance(self.provider, StreamingChatProvider):
                async for chunk in self.provider.stream_chat(
                    safe_messages,
                    model=model,
                    temperature=temperature,
                ):
                    if chunk.error:
                        raise RuntimeError(chunk.error)
                    if chunk.text:
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter() - started) * 1000, 1)
                        interceptor_result: str | None = chunk.text
                        if chunk_interceptor is not None:
                            interceptor_result = chunk_interceptor(chunk.text)
                            if inspect.isawaitable(interceptor_result):
                                interceptor_result = await interceptor_result
                        if interceptor_result:
                            text_parts.append(interceptor_result)
                            yield GatewayStreamEvent(
                                kind="delta",
                                text=interceptor_result,
                                model=chunk.model or result_model,
                            )
                    if chunk.usage:
                        usage = chunk.usage
                    if chunk.tool_calls:
                        tool_calls.extend(chunk.tool_calls)
                    if chunk.model:
                        result_model = chunk.model
                    if chunk.done:
                        break
            else:
                if tool_specs:
                    result = await self.provider.chat_with_tools(
                        safe_messages,
                        tool_specs,
                        model=model,
                        temperature=temperature,
                    )
                else:
                    result = await self.provider.chat(
                        safe_messages,
                        model=model,
                        temperature=temperature,
                    )
                if result.text:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 1)
                    text_parts.append(result.text)
                    yield GatewayStreamEvent(
                        kind="delta",
                        text=result.text,
                        model=result.model or model,
                    )
                usage = result.usage
                tool_calls = list(result.tool_calls)
                result_model = result.model or model
        except CircuitOpenError as exc:
            call = self._degraded_call(
                envelope=envelope,
                started=started,
                error=str(exc),
                degradation={
                    "kind": "circuit_open",
                    "provider": self.provider.name,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                model=model,
            )
            yield GatewayStreamEvent(kind="error", error=str(exc))
            yield GatewayStreamEvent(kind="done", call=call)
            return
        except ProviderStreamError as exc:
            call = GatewayCall(
                provider=self.provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(
                    text="".join(text_parts), usage=usage, model=result_model
                ),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="error",
                error=str(exc),
                first_token_ms=first_token_ms,
                selection=self.selection.to_dict(),
            )
            yield GatewayStreamEvent(kind="error", error=str(exc))
            yield GatewayStreamEvent(kind="done", call=call)
            return
        except Exception as exc:  # noqa: BLE001 - gateway boundary; errors are audited
            call = GatewayCall(
                provider=self.provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="".join(text_parts), usage=usage, model=result_model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                first_token_ms=first_token_ms,
                selection=self.selection.to_dict(),
            )
            yield GatewayStreamEvent(kind="error", error=str(exc))
            yield GatewayStreamEvent(kind="done", call=call)
            return

        validated = validate_tool_calls(
            tool_calls,
            tool_specs,
            sensitive_allowed=allow_sensitive_tools,
        )
        call = GatewayCall(
            provider=self.provider.name,
            request_id=envelope.request_id,
            envelope=envelope,
            result=ChatResult(
                text="".join(text_parts),
                tool_calls=tool_calls,
                usage=usage,
                model=result_model,
            ),
            tool_validation=validated,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            status="ok",
            first_token_ms=first_token_ms,
            selection=self.selection.to_dict(),
        )
        yield GatewayStreamEvent(kind="done", call=call)
