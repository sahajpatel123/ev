"""Neutral model gateway: envelope contract, provider call, validation, audit data.

The gateway is the only place EV talks to a reasoning provider. It carries the
complete request envelope (strategy, memories, request id, metadata), measures
and reports every call, and pre-validates model tool invocations before anything
can be executed. Swapping the provider remains a configuration change; EV's
identity and behavior live outside this module.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from app.config import settings
from app.contracts import (
    ChatMessage,
    ChatProvider,
    ChatResult,
    RequestEnvelope,
    ToolCall,
    ToolSpec,
)
from app.ev.actions import life_agency_prompt
from app.gateway.costs import CostCapExceeded, check_cost_cap
from app.gateway.reliability import CircuitOpenError, ProviderStreamError
from app.gateway.routing import ProviderSelection
from app.gateway.streaming import StreamingChatProvider
from app.gateway.validation import ValidatedToolCall, validate_tool_calls
from app.security.boundary import ModelBoundaryViolation, guard_model_payload

if TYPE_CHECKING:
    from app.gateway.openrouter_jev import DecisionProvider

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
        "apps:act",
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
    decision_answers: dict[str, Any] | None = None
    response_id: str | None = None
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

    def decision_answers_dict(self) -> dict[str, dict] | None:
        if self.decision_answers is None:
            return None
        return {
            question_id: answer.to_dict() if hasattr(answer, "to_dict") else dict(answer)
            for question_id, answer in self.decision_answers.items()
        }


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
                version=str(spec.get("version", "1")),
                required_scopes=[str(scope) for scope in spec.get("required_scopes", [])],
                risk_class=str(spec.get("risk_class", "R0")),
                confirmation=str(spec.get("confirmation", "none")),
                target_ownership=str(spec.get("target_ownership", "owner")),
                provider=str(spec.get("provider", "local")),
                fallback=spec.get("fallback"),
                evidence=[str(item) for item in spec.get("evidence", [])],
                idempotency=str(spec.get("idempotency", "natural")),
                timeout_seconds=int(spec.get("timeout_seconds", 10)),
                cancellation=str(spec.get("cancellation", "not_applicable")),
                audit_event=spec.get("audit_event"),
            )
        )
    return converted


class ModelGateway:
    """Provider-agnostic chat gateway with envelope + validation + audit payloads."""

    def __init__(
        self,
        provider: ChatProvider | DecisionProvider,
        *,
        selection: ProviderSelection | None = None,
        cost_guard: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.provider: Any = provider
        self.cost_guard = cost_guard
        self.selection = selection or ProviderSelection(
            provider=provider.name,
            reason="configured_provider",
        )

    def _record_selection(self, envelope: RequestEnvelope) -> None:
        envelope.metadata.setdefault("provider_selection", self.selection.to_dict())

    @staticmethod
    def _cost_guard_call_mode(guard: Callable[..., Awaitable[None]]) -> str:
        """Choose how to pass payload to additive, legacy-compatible guards."""

        try:
            parameters = list(inspect.signature(guard).parameters.values())
        except (TypeError, ValueError):
            return "none"
        if any(
            parameter.name == "messages"
            and parameter.kind != inspect.Parameter.POSITIONAL_ONLY
            for parameter in parameters
        ) or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return "keyword"
        if any(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
            for parameter in parameters
        ):
            return "positional"
        return "none"

    async def _run_cost_guard(
        self,
        messages: Sequence[ChatMessage],
        *,
        require_exact_payload_cap: bool = False,
    ) -> None:
        """Run injected policy and optionally cap the exact provider payload."""

        if self.cost_guard is not None:
            mode = self._cost_guard_call_mode(self.cost_guard)
            if mode == "keyword":
                await self.cost_guard(messages=messages)
            elif mode == "positional":
                await self.cost_guard(messages)
            else:
                await self.cost_guard()

        if require_exact_payload_cap and settings.cost_cap_enabled:
            # Older route closures capture their original chat messages. Run
            # the shared cap against this precise sanitized JEV request too.
            from app.db import SessionLocal

            async with SessionLocal() as session:
                await check_cost_cap(
                    session,
                    provider=self.provider.name,
                    messages=messages,
                )

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
        tool_choice: dict | str | None = None,
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
                await self._run_cost_guard(safe_messages)
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
                if tool_choice is None:
                    result = await self.provider.chat_with_tools(
                        safe_messages,
                        tool_specs,
                        model=model,
                        temperature=temperature,
                    )
                else:
                    try:
                        signature = inspect.signature(self.provider.chat_with_tools)
                        accepts_choice = "tool_choice" in signature.parameters or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in signature.parameters.values()
                        )
                    except (TypeError, ValueError):
                        accepts_choice = False
                    if not accepts_choice:
                        raise ValueError(
                            f"{self.provider.name} does not support forced tool_choice"
                        )
                    result = await cast(Any, self.provider).chat_with_tools(
                        safe_messages,
                        tool_specs,
                        model=model,
                        temperature=temperature,
                        tool_choice=tool_choice,
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
                await self._run_cost_guard(safe_messages)
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
                # ``tools`` was added after the original streaming contract.
                # Pass it only to providers that advertise the additive
                # parameter so third-party/test providers remain compatible;
                # Muse Spark receives the complete native Responses tool
                # surface instead of silently streaming a no-tool turn.
                try:
                    stream_signature = inspect.signature(self.provider.stream_chat)
                    accepts_tools = "tools" in stream_signature.parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in stream_signature.parameters.values()
                    )
                except (TypeError, ValueError):
                    accepts_tools = False
                if accepts_tools:
                    stream = self.provider.stream_chat(
                        safe_messages,
                        model=model,
                        temperature=temperature,
                        tools=tool_specs,
                    )
                else:
                    stream = self.provider.stream_chat(
                        safe_messages,
                        model=model,
                        temperature=temperature,
                    )
                async for chunk in stream:
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

    async def decide(
        self,
        state: object,
        questions: Mapping[str, Any],
        *,
        envelope: RequestEnvelope | None = None,
        model: str | None = None,
    ) -> GatewayCall:
        """Run one typed decision call through the same privacy/cost/audit seam.

        Decisions-only providers expose ``decision_payload`` and ``decide``;
        they are intentionally not adapted to the prose ``ChatProvider`` API.
        The cost estimate and audit digest cover the exact sanitized payload
        shape sent to the provider, while raw state is never copied to audit
        metadata.
        """

        from app.gateway.openrouter_jev import (
            JevAnswer,
            JevDecisionResult,
            OpenRouterEgressDenied,
            OpenRouterJevDisabled,
            OpenRouterJevUnavailable,
            validate_answer,
        )

        envelope = envelope or RequestEnvelope(request_id=str(uuid4()), strategy={})
        self._record_selection(envelope)
        started = time.perf_counter()
        provider = self.provider
        payload_builder = getattr(provider, "decision_payload", None)
        decide = getattr(provider, "decide", None)
        if not callable(payload_builder) or not callable(decide):
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="error",
                error="configured provider does not implement typed decisions",
                selection=self.selection.to_dict(),
            )

        try:
            payload = payload_builder(state, questions, model=model)
            if not isinstance(payload, dict) or set(payload) != {"model", "state", "questions"}:
                raise OpenRouterJevUnavailable("decision provider built an invalid request payload")
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            guarded = guard_model_payload(
                [ChatMessage(role="user", content=canonical)], envelope
            )
            safe_payload = json.loads(guarded[0].content)
            if (
                not isinstance(safe_payload, dict)
                or set(safe_payload) != {"model", "state", "questions"}
                or not isinstance(safe_payload.get("model"), str)
                or not isinstance(safe_payload.get("questions"), dict)
            ):
                raise OpenRouterJevUnavailable("sanitized decision payload has an invalid shape")
            safe_payload["questions"] = {
                question_id: _normalize_decision_question(question)
                for question_id, question in safe_payload["questions"].items()
            }
            canonical = json.dumps(
                safe_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except ModelBoundaryViolation as exc:
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked",
                error=str(exc),
                selection=self.selection.to_dict(),
            )
        except OpenRouterJevUnavailable as exc:
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked",
                error=str(exc),
                selection=self.selection.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - malformed provider payloads fail closed
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=model),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked" if isinstance(exc, ValueError) else "error",
                error=f"{type(exc).__name__}: {exc}",
                selection=self.selection.to_dict(),
            )

        payload_message = ChatMessage(role="user", content=canonical)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope.metadata["jev_decision"] = {
            "payload_sha256": payload_hash,
            "question_ids": sorted(safe_payload["questions"]),
        }
        try:
            await self._run_cost_guard(
                [payload_message], require_exact_payload_cap=True
            )
        except CostCapExceeded as exc:
            return self._degraded_call(
                envelope=envelope,
                started=started,
                error=str(exc),
                degradation={"kind": "cost_cap", "provider": provider.name},
                model=safe_payload["model"],
                status="error",
            )
        except Exception as exc:  # noqa: BLE001 - budget service failures fail closed
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=safe_payload["model"]),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="error",
                error=f"cost guard failed: {type(exc).__name__}: {exc}",
                selection=self.selection.to_dict(),
            )

        try:
            decision: JevDecisionResult = await decide(
                safe_payload["state"],
                safe_payload["questions"],
                model=safe_payload["model"],
            )
            if not isinstance(decision, JevDecisionResult):
                raise OpenRouterJevUnavailable("decision provider returned an invalid result")
            valid_model_id = getattr(provider, "_valid_model_id", None)
            model_matches = (
                bool(valid_model_id(decision.model, safe_payload["model"]))
                if callable(valid_model_id)
                else decision.model == safe_payload["model"]
            )
            if not model_matches:
                raise OpenRouterJevUnavailable(
                    "decision provider response model did not match the configured model"
                )
            if (
                not isinstance(decision.response_id, str)
                or not decision.response_id.strip()
                or len(decision.response_id) > 256
            ):
                raise OpenRouterJevUnavailable("decision provider returned an invalid response id")
            expected_ids = set(safe_payload["questions"])
            if set(decision.answers) != expected_ids:
                raise OpenRouterJevUnavailable(
                    "JEV did not return exactly one answer for each requested question"
                )
            validated = {
                question_id: validate_answer(
                    question_id,
                    safe_payload["questions"][question_id],
                    answer.to_dict() if isinstance(answer, JevAnswer) else answer,
                )
                for question_id, answer in decision.answers.items()
            }
            envelope.metadata["jev_decision"].update({
                "response_id": decision.response_id,
                "payload_sha256": decision.payload_sha256 or payload_hash,
                "answers": {
                    question_id: answer.to_dict()
                    for question_id, answer in validated.items()
                },
                "latency_ms": decision.latency_ms,
            })
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(
                    text="", usage=dict(decision.usage), model=decision.model
                ),
                decision_answers=validated,
                response_id=decision.response_id,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="ok",
                selection=self.selection.to_dict(),
            )
        except CircuitOpenError as exc:
            return self._degraded_call(
                envelope=envelope,
                started=started,
                error=str(exc),
                degradation={
                    "kind": "circuit_open",
                    "provider": provider.name,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                model=safe_payload["model"],
            )
        except (OpenRouterEgressDenied, OpenRouterJevDisabled) as exc:
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=safe_payload["model"]),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="blocked",
                error=str(exc),
                selection=self.selection.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - typed answer failures are audited
            return GatewayCall(
                provider=provider.name,
                request_id=envelope.request_id,
                envelope=envelope,
                result=ChatResult(text="", usage={}, model=safe_payload["model"]),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                selection=self.selection.to_dict(),
            )


def _normalize_decision_question(question: Any) -> dict[str, Any]:
    """Revalidate a guarded question without importing provider-private state."""

    from app.gateway.openrouter_jev import normalize_question

    return normalize_question(question)
