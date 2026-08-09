"""Neutral model gateway: envelope contract, provider call, validation, audit data.

The gateway is the only place EV talks to a reasoning provider. It carries the
complete request envelope (strategy, memories, request id, metadata), measures
and reports every call, and pre-validates model tool invocations before anything
can be executed. Swapping the provider remains a configuration change; EV's
identity and behavior live outside this module.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from app.contracts import (
    ChatMessage,
    ChatProvider,
    ChatResult,
    RequestEnvelope,
    ToolSpec,
)
from app.gateway.validation import ValidatedToolCall, validate_tool_calls
from app.security.boundary import ModelBoundaryViolation, guard_model_payload


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

    @property
    def model(self) -> str | None:
        return self.result.model

    def usage(self) -> dict:
        return self.result.usage

    def tool_calls_dict(self) -> list[dict]:
        return [v.to_dict() for v in self.tool_validation]


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

    def __init__(self, provider: ChatProvider) -> None:
        self.provider = provider

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
            )
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
        )
