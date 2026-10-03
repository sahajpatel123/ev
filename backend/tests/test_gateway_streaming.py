"""Real token streaming: providers, gateway, SSE endpoint, cancellation.

CORTEX (Agent 10) acceptance: first token is measured, the filter can
intercept chunks, the RequestEnvelope hash stays auditable, cancellation
provably stops the upstream generator, and ``curl -N`` sees progressive
``delta`` events before ``done``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.contracts import ChatMessage, ChatResult, RequestEnvelope, ToolSpec
from app.gateway.costs import CostCapExceeded, monthly_cost_usd
from app.gateway.muse import configured_intelligence_provider, muse_brain_active
from app.gateway.openrouter_jev import (
    JevQuestion,
    OpenRouterEgressDenied,
    OpenRouterJevDisabled,
    OpenRouterJevProvider,
    OpenRouterJevUnavailable,
    reported_openrouter_cost,
)
from app.gateway.providers import (
    DeepSeekProvider,
    LocalModelProvider,
    MockProvider,
    get_chat_provider,
    get_decision_provider,
)
from app.gateway.reliability import CIRCUIT_BREAKERS
from app.gateway.roles import choose_with_jev
from app.gateway.routing import routing_candidates, select_provider
from app.gateway.service import GatewayCall, ModelGateway
from app.gateway.streaming import ChatStreamChunk, StreamingChatProvider
from app.services.model_call import log_model_call
from app.utils.text import utcnow


@pytest.mark.parametrize(
    ("transcript", "expected_effort", "expected_sort"),
    [
        ("Tell me how you're doing today.", "low", "latency"),
        ("Make a packing list on my Desktop", "medium", "throughput"),
    ],
)
async def test_mimo_kernel_sends_turn_effort_instead_of_global_high(
    monkeypatch, tmp_path, db_session, transcript, expected_effort, expected_sort
) -> None:
    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.gateway.openrouter_mimo import MimoProvider

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "mimo_reasoning_effort", "high")
    # Compare two policies explicitly; do not inherit the owner's work effort.
    monkeypatch.setattr(settings, "cognitive_work_reasoning_effort", "medium")
    monkeypatch.setattr(settings, "cognitive_conversation_reasoning_effort", "low")
    monkeypatch.setattr(settings, "laptop_files", False)
    reset_for_tests()
    captured: list[dict] = []
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse({
            "model": "xiaomi/mimo-v2.6-flash",
            "choices": [{"message": {"content": "Here is the answer."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        })

    _patch_http(monkeypatch, app)
    provider = MimoProvider(base_url="http://mimo-test/v1", api_key="offline-test")

    async def authorized_for_offline_transport() -> None:
        pass

    monkeypatch.setattr(provider, "_authorize", authorized_for_offline_transport)
    monkeypatch.setattr(kernel, "_text_brain_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: provider)
    CIRCUIT_BREAKERS.reset()
    try:
        result = await kernel.handle_turn(transcript=transcript, session=db_session)
        assert result.spoken == "Here is the answer."
        assert len(captured) == 1
        assert captured[0]["reasoning"] == {"effort": expected_effort}
        assert captured[0]["provider"]["sort"] == expected_sort
    finally:
        reset_for_tests()
        CIRCUIT_BREAKERS.reset()


def test_mimo_factory_preserves_default_effort_without_kernel_override(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "mimo")
    monkeypatch.setattr(settings, "mimo_reasoning_effort", "high")
    provider = get_chat_provider()
    assert provider.reasoning_effort is None
    assert provider._payload_extras()["reasoning"] == {"effort": "high"}
    assert provider._payload_extras()["provider"]["sort"] == "throughput"


@pytest.mark.parametrize("structured", [False, True])
async def test_mimo_role_adapter_honors_explicit_effort(monkeypatch, structured) -> None:
    from app.gateway.openrouter_mimo import MimoProvider
    from app.gateway.roles import chat_structured_via_role, chat_via_role

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "mimo_reasoning_effort", "high")
    provider = MimoProvider(api_key="offline-test")
    seen: list[dict] = []

    async def reply(*args, **kwargs) -> ChatResult:
        seen.append(provider._payload_extras())
        return ChatResult(text="{}" if structured else "Answer.")

    monkeypatch.setattr(provider, "chat", reply)
    monkeypatch.setattr(provider, "chat_structured", reply)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: provider)
    messages = [ChatMessage(role="user", content="A question")]
    if structured:
        await chat_structured_via_role(messages, schema={"type": "object"}, reasoning_effort="low")
    else:
        await chat_via_role(messages, reasoning_effort="low")
    assert seen[0]["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("active_work", [False, True])
async def test_mimo_hello_avoids_provider_even_during_active_work(
    monkeypatch, tmp_path, db_session, active_work
) -> None:
    from app.cognitive import kernel
    from app.cognitive.session_store import current, reset_for_tests, save

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()

    def unexpected_provider():
        pytest.fail("A bare greeting must not call the reasoning provider")

    monkeypatch.setattr("app.gateway.roles.require_text_provider", unexpected_provider)
    if active_work:
        row = current()
        row.semantic_objective = "inspect the repo"
        row.focused_goal_id = "existing-goal"
        save(row)
    try:
        for transcript in ("hello", "hello Evie", "Evie, hi!"):
            result = await kernel.handle_turn(transcript=transcript, session=db_session)
            assert result.kind == "reflex:greeting"
            assert result.spoken == "Hello!"
    finally:
        reset_for_tests()


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: ").strip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def _stream_app(*, with_tools: bool = False, captured: list[dict] | None = None) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> StreamingResponse:
        body = await request.json()
        if captured is not None:
            captured.append(body)
        model = body.get("model", "mock-local")

        async def gen() -> AsyncIterator[str]:
            pieces = ["Hello", " from", " the", " local", " brain."]
            for index, piece in enumerate(pieces):
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "cmpl-s",
                            "model": model,
                            "choices": [{"delta": {"content": piece}, "finish_reason": None}],
                        }
                    )
                    + "\n\n"
                )
                if index == 0 and with_tools:
                    continue
            if with_tools:
                tool_deltas = [
                    {"index": 0, "id": "call-1", "function": {"name": "lookup_person", "arguments": ""}},
                    {"index": 0, "function": {"arguments": '{"name": "Maya"}'}},
                ]
                for delta in tool_deltas:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "id": "cmpl-s",
                                "model": model,
                                "choices": [{"delta": {"tool_calls": [delta]}, "finish_reason": None}],
                            }
                        )
                        + "\n\n"
                    )
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": "cmpl-s",
                        "model": model,
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 9, "total_tokens": 13},
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _patch_http(monkeypatch, app: FastAPI) -> None:
    real = httpx.AsyncClient

    def _client(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client,
    )


async def test_mock_provider_streams_deltas_then_done() -> None:
    provider = MockProvider()
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="hello world")]
        )
    ]
    assert "".join(chunk.text for chunk in chunks if not chunk.done) == (
        "EV: Mock reply. Last user message: hello world"
    )
    done = [chunk for chunk in chunks if chunk.done]
    assert len(done) == 1
    assert done[0].usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert done[0].model == "mock-model"


async def test_gateway_stream_yields_deltas_then_done_with_audit() -> None:
    gateway = ModelGateway(MockProvider())
    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="hello")],
            envelope=RequestEnvelope(
                request_id="stream-1",
                strategy={},
                metadata={"envelope_hash": "hash-abc"},
            ),
        )
    ]
    kinds = [event.kind for event in events]
    assert kinds[-1] == "done"
    assert all(kind in ("delta", "done") for kind in kinds)

    call = events[-1].call
    assert call is not None
    assert call.status == "ok"
    assert call.request_id == "stream-1"
    assert call.first_token_ms is not None and call.first_token_ms >= 0
    assert call.latency_ms >= 0
    assert "".join(event.text for event in events if event.kind == "delta") == call.result.text
    assert call.envelope.metadata["envelope_hash"] == "hash-abc"
    assert call.selection == {"provider": "mock", "reason": "configured_provider", "evidence": {}}


async def test_gateway_stream_interceptor_sees_and_transforms_every_chunk() -> None:
    gateway = ModelGateway(MockProvider())
    seen: list[str] = []

    async def interceptor(text: str) -> str | None:
        seen.append(text)
        return text.upper()

    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="hello")],
            chunk_interceptor=interceptor,  # type: ignore[arg-type]
        )
    ]
    assert seen
    assert all(event.text == event.text.upper() for event in events if event.kind == "delta")
    assert events[-1].call is not None
    assert events[-1].call.result.text == "".join(seen).upper()


async def test_gateway_stream_interceptor_can_suppress_a_chunk() -> None:
    class TwoChunkProvider(StreamingChatProvider):
        name = "two-chunk"

        async def stream_chat(
            self,
            messages,
            *,
            model=None,
            temperature=0.7,
        ) -> AsyncIterator[ChatStreamChunk]:
            yield ChatStreamChunk(text="alpha", model=model or "two")
            yield ChatStreamChunk(text="beta", model=model or "two")
            yield ChatStreamChunk(
                text="",
                usage={"prompt_tokens": 1, "completion_tokens": 2},
                model=model or "two",
                done=True,
            )

    gateway = ModelGateway(TwoChunkProvider())
    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="hello")],
            chunk_interceptor=lambda text: None if text == "alpha" else text,
        )
    ]
    assert [event.text for event in events if event.kind == "delta"] == ["beta"]
    assert events[-1].call is not None
    assert events[-1].call.result.text == "beta"


async def test_gateway_stream_blocked_payload_never_calls_provider(monkeypatch) -> None:
    provider = MockProvider()
    gateway = ModelGateway(provider)
    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="never_send_to_model secret")],
            envelope=RequestEnvelope(request_id="blocked-1", strategy={}),
        )
    ]
    assert events[0].kind == "error"
    assert events[1].kind == "done"
    assert events[1].call is not None
    assert events[1].call.status == "blocked"
    assert "never_send_to_model" in (events[1].call.error or "")


async def _offline_authorized() -> None:
    """Local HTTP doubles bypass the remote-egress gate, like the MiMo tests."""

    return None


async def test_deepseek_provider_parses_sse_stream(monkeypatch) -> None:
    _patch_http(monkeypatch, _stream_app())
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-test",
    )
    provider._authorize = _offline_authorized
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="hi")]
        )
    ]
    assert "".join(chunk.text for chunk in chunks) == "Hello from the local brain."
    done = [chunk for chunk in chunks if chunk.done]
    assert len(done) == 1
    assert done[0].usage["completion_tokens"] == 9
    assert done[0].model == "deepseek-test"
    assert done[0].finish_reason == "stop"


async def test_deepseek_provider_accumulates_streamed_tool_calls(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _stream_app(with_tools=True, captured=captured))
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-test",
    )
    provider._authorize = _offline_authorized
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="who is Maya")],
            tools=[
                ToolSpec(
                    name="lookup_person",
                    description="Look up one person.",
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                )
            ],
        )
    ]
    assert captured[0]["tools"][0]["function"]["name"] == "lookup_person"
    done = [chunk for chunk in chunks if chunk.done][0]
    assert len(done.tool_calls) == 1
    assert done.tool_calls[0].name == "lookup_person"
    assert done.tool_calls[0].arguments == {"name": "Maya"}


class SlowStreamProvider(StreamingChatProvider):
    """Slow mock whose upstream generator records whether it was closed."""

    name = "slow"
    supports_media = False

    def __init__(self) -> None:
        self.closed = False

    async def stream_chat(
        self,
        messages,
        *,
        model=None,
        temperature=0.7,
    ) -> AsyncIterator[ChatStreamChunk]:
        try:
            yield ChatStreamChunk(text="first", model=model or "slow")
            while True:
                yield ChatStreamChunk(text="more", model=model or "slow")
                await asyncio.sleep(0.02)
        finally:
            self.closed = True


async def _collect_events(
    gateway: ModelGateway,
    out: list,
) -> None:
    async for event in gateway.stream_chat(
        [ChatMessage(role="user", content="hello")]
    ):
        out.append(event)


async def test_cancellation_provably_stops_upstream_slow_mock() -> None:
    provider = SlowStreamProvider()
    gateway = ModelGateway(provider)
    events: list = []
    task = asyncio.create_task(_collect_events(gateway, events))
    for _ in range(200):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events and events[0].kind == "delta"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The upstream generator's finally ran: the slow call was stopped, not
    # abandoned to keep streaming in the background.
    assert provider.closed is True
    assert all(event.kind == "delta" for event in events)


async def test_provider_stream_generator_closes_cleanly(monkeypatch) -> None:
    _patch_http(monkeypatch, _stream_app())
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-test",
    )
    provider._authorize = _offline_authorized
    agen = provider.stream_chat([ChatMessage(role="user", content="hi")])
    first = await anext(agen)
    assert first.text == "Hello"
    await agen.aclose()  # must not raise: upstream stream is torn down


async def test_gateway_stream_endpoint_sse_and_audit(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/gateway/stream",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "request_id": "sse-req-1",
            "strategy": {"mode": "quick", "intent": "chat"},
            "context": {"context_tokens": 12, "envelope_hash": "sse-hash-1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    events = _sse_events(resp.text)
    names = [name for name, _data in events]
    assert names[-1] == "done"
    assert "delta" in names
    assert "error" not in names

    done = events[-1][1]
    assert done["request_id"] == "sse-req-1"
    assert done["provider"] == "mock"
    assert done["status"] == "ok"
    assert done["envelope_hash"] == "sse-hash-1"
    assert done["first_token_ms"] is not None
    assert done["provider_selection"]["provider"] == "mock"
    assert done["provider_selection"]["reason"] == "single_provider_routing_noop"

    audit = await client.get("/v1/gateway/calls", params={"request_id": "sse-req-1"})
    assert audit.status_code == 200, audit.text
    rows = audit.json()
    assert len(rows) == 1
    assert rows[0]["provider"] == "mock"
    assert rows[0]["envelope_hash"] == "sse-hash-1"
    assert rows[0]["envelope"]["metadata"]["provider_selection"]["provider"] == "mock"


async def test_local_provider_defaults_to_qwen_via_env(monkeypatch) -> None:
    monkeypatch.setenv("EV_LOCAL_MODEL_NAME", "qwen3:1.7b")
    provider = LocalModelProvider()
    assert provider.default_model == "qwen3:1.7b"


async def test_unknown_provider_is_loud(monkeypatch) -> None:
    from app.gateway.providers import UnknownProviderError, get_chat_provider

    original = settings.chat_provider
    try:
        settings.chat_provider = "does-not-exist"
        with pytest.raises(UnknownProviderError, match="unknown chat provider"):
            get_chat_provider()
    finally:
        settings.chat_provider = original


def _flaky_app(*, failures: int, status_code: int = 503) -> tuple[FastAPI, dict]:
    state = {"calls": 0}
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions() -> JSONResponse:
        state["calls"] += 1
        if state["calls"] <= failures:
            return JSONResponse(status_code=status_code, content={"error": "overloaded"})
        return JSONResponse(
            {
                "id": "cmpl",
                "model": "deepseek-test",
                "choices": [{"message": {"role": "assistant", "content": "recovered"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )

    return app, state


async def test_deepseek_provider_retries_transient_failures_with_backoff(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "model_max_retries", 2)
    monkeypatch.setattr(settings, "model_retry_base_seconds", 0.01)
    monkeypatch.setattr(settings, "model_retry_max_seconds", 0.05)
    app, state = _flaky_app(failures=2)
    _patch_http(monkeypatch, app)
    provider = DeepSeekProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="deepseek-test",
    )
    provider._authorize = _offline_authorized
    result = await provider.chat([ChatMessage(role="user", content="hi")])
    assert result.text == "recovered"
    assert state["calls"] == 3


async def test_circuit_breaker_trips_and_gateway_degrades(monkeypatch) -> None:
    monkeypatch.setattr(settings, "model_max_retries", 0)
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    monkeypatch.setattr(settings, "circuit_cooldown_seconds", 300.0)
    CIRCUIT_BREAKERS.reset("deepseek")
    try:
        app, state = _flaky_app(failures=10, status_code=500)
        _patch_http(monkeypatch, app)
        provider = DeepSeekProvider(
            base_url="http://local/v1",
            api_key="test-key",
            default_model="deepseek-test",
        )
        provider._authorize = _offline_authorized
        gateway = ModelGateway(provider)
        for _ in range(2):
            call = await gateway.chat([ChatMessage(role="user", content="hi")])
            assert call.status == "error"
        assert state["calls"] == 2

        call = await gateway.chat([ChatMessage(role="user", content="hi")])
        assert call.status == "degraded"
        assert call.degraded is True
        assert call.degradation["kind"] == "circuit_open"
        assert call.degradation["provider"] == "deepseek"
        assert call.degradation["retry_after_seconds"] > 0
        assert "circuit breaker is open" in (call.error or "")
        assert state["calls"] == 2  # fast-fail: no upstream attempt
    finally:
        CIRCUIT_BREAKERS.reset("deepseek")


class BrokenStream(httpx.AsyncByteStream):
    """Delivers one SSE chunk, then fails mid-stream (simulated connection loss)."""

    def __init__(self) -> None:
        self.chunks = [
            (
                "data: "
                + json.dumps(
                    {
                        "id": "cmpl-broken",
                        "model": "deepseek-test",
                        "choices": [{"delta": {"content": "first"}, "finish_reason": None}],
                    }
                )
                + "\n\n"
            ).encode()
        ]
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        raise RuntimeError("mid-stream boom")

    async def aclose(self) -> None:
        self.closed = True


def _patch_mock_http(monkeypatch, stream: httpx.AsyncByteStream) -> None:
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.MockTransport(handler),
            base_url="http://local",
        ),
    )


async def test_mid_stream_error_surfaces_as_typed_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "model_max_retries", 0)
    CIRCUIT_BREAKERS.reset("deepseek")
    try:
        stream = BrokenStream()
        _patch_mock_http(monkeypatch, stream)
        provider = DeepSeekProvider(
            base_url="http://local/v1",
            api_key="test-key",
            default_model="deepseek-test",
        )
        provider._authorize = _offline_authorized
        gateway = ModelGateway(provider)
        events = [
            event
            async for event in gateway.stream_chat(
                [ChatMessage(role="user", content="hi")]
            )
        ]
        assert any(event.kind == "delta" for event in events)
        assert any(event.kind == "error" for event in events)
        done = events[-1]
        assert done.call is not None
        assert done.call.status == "error"
        assert "mid-stream boom" in (done.call.error or "")
        assert stream.closed is True  # upstream stream torn down, no leak
    finally:
        CIRCUIT_BREAKERS.reset("deepseek")


async def test_gateway_cost_guard_refuses_over_cap() -> None:
    async def guard() -> None:
        raise CostCapExceeded(
            "monthly cost cap exceeded: $40.00 used + ~$1.00 projected > $40.00 cap"
        )

    gateway = ModelGateway(MockProvider(), cost_guard=guard)
    call = await gateway.chat(
        [ChatMessage(role="user", content="hi")],
        envelope=RequestEnvelope(request_id="cap-1", strategy={}),
    )
    assert call.status == "error"
    assert call.degraded is True
    assert call.degradation == {"kind": "cost_cap", "provider": "mock"}
    assert "cost cap" in (call.error or "")
    assert call.envelope.metadata["degradation"]["kind"] == "cost_cap"

    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="hi")],
            envelope=RequestEnvelope(request_id="cap-2", strategy={}),
        )
    ]
    assert events[0].kind == "error"
    assert events[-1].call is not None
    assert events[-1].call.status == "error"
    assert events[-1].call.degradation["kind"] == "cost_cap"


# --- Agent 10 JEV/OpenRouter decision-provider coverage ---------------------


def _jev_provider_app(
    captured: list[dict],
    *,
    answers: dict | None = None,
    usage: dict | None = None,
    response_model: str = "typesafe/jev-1.13-20261001",
    request_urls: list[str] | None = None,
    request_auth: list[str | None] | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/alpha/decisions")
    async def decisions(request: Request) -> dict:
        payload = await request.json()
        if request_urls is not None:
            request_urls.append(str(request.url))
        if request_auth is not None:
            request_auth.append(request.headers.get("authorization"))
        captured.append(payload)
        requested_questions = payload.get("questions") or {}
        typed_answers: dict = {}
        if answers is None:
            for question_id, question in requested_questions.items():
                if question.get("type") == "choice":
                    labels = question["criteria"]
                    choice = "read_only" if "read_only" in labels else next(iter(labels))
                    typed_answers[question_id] = {"type": "choice", "choice": choice}
                elif question.get("type") == "score":
                    typed_answers[question_id] = {"type": "score", "score": 0}
                else:
                    typed_answers[question_id] = {"type": "noul", "noul": 0.91}
        else:
            typed_answers = dict(answers)
        return {
            "id": "jev-test",
            "model": response_model,
            "answers": typed_answers,
            "usage": usage
            if usage is not None
            else {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "cost": 0.000001,
            },
        }

    return app


def _enable_jev(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "jev_model", "typesafe/jev-1.13")
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setattr(settings, "chat_provider", "mock")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setenv("EV_ALLOW_REMOTE_CHAT", "true")


async def _grant_jev_chat_egress(session) -> None:
    from app.models import ConsentRecord

    session.add(
        ConsentRecord(
            track="chat_egress",
            purpose="synthetic OpenRouter provider test",
            scope={"synthetic": True},
            source="test",
        )
    )
    await session.commit()


def _jev_route_question() -> dict[str, JevQuestion]:
    return {
        "route": JevQuestion(
            type="choice",
            instructions="Choose the safe route for the request.",
            criteria={"read_only": "Only read data", "clarify": "The request is ambiguous"},
        )
    }


async def test_jev_provider_uses_native_typed_decisions_endpoint(
    db_session, monkeypatch
) -> None:
    captured: list[dict] = []
    request_urls: list[str] = []
    request_auth: list[str | None] = []
    _patch_http(
        monkeypatch,
        _jev_provider_app(
            captured,
            request_urls=request_urls,
            request_auth=request_auth,
        ),
    )
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)

    result = await OpenRouterJevProvider().decide(
        {"request": "Choose the faster safe route."},
        {
            "route": JevQuestion(
                type="choice",
                instructions="Which route fits the request?",
                criteria={"read_only": "No writes are needed", "clarify": "The request is ambiguous"},
            )
        },
    )

    assert result.answers["route"].choice == "read_only"
    assert result.model == "typesafe/jev-1.13-20261001"
    assert result.usage["cost_source"] == "openrouter_reported"
    assert result.response_id == "jev-test"
    payload = captured[0]
    assert payload["model"] == "typesafe/jev-1.13"
    assert set(payload) == {"model", "state", "questions"}
    assert payload["state"] == {"request": "Choose the faster safe route."}
    assert payload["questions"]["route"] == {
        "type": "choice",
        "instructions": "Which route fits the request?",
        "criteria": {
            "read_only": "No writes are needed",
            "clarify": "The request is ambiguous",
        },
    }
    assert request_urls == ["https://openrouter.ai/api/alpha/decisions"]
    assert request_auth == ["Bearer test-openrouter-key"]


async def test_jev_typed_choice_uses_gateway_validation(monkeypatch, db_session) -> None:
    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _jev_provider_app(
            captured,
            answers={
                "route": {
                    "type": "choice",
                    "choice": "read_only",
                    "probabilities": {
                        "read_only": 1.0,
                        "clarify": 0.0,
                        "refuse": 0.0,
                    },
                    "confidence": 1.0,
                }
            },
        ),
    )
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    monkeypatch.setattr(settings, "chat_provider", "openrouter")
    decision = await choose_with_jev(
        ModelGateway(get_decision_provider()),
        [ChatMessage(role="user", content="Read the visible settings only.")],
        envelope=RequestEnvelope(request_id="jev-choice", strategy={}),
        session=db_session,
        actor="test",
        question_id="route",
        choices=["read_only", "clarify", "refuse"],
        instructions="Select the next safe route.",
    )
    assert decision.choice == "read_only"
    assert decision.validation == "ok"
    assert decision.call.result.text == ""
    assert set(captured[0]) == {"model", "state", "questions"}
    assert decision.call.response_id == "jev-test"


async def test_jev_kernel_runs_only_a_typed_read_only_tool_turn(
    db_session, monkeypatch
) -> None:
    from sqlalchemy import select

    from app.cognitive import kernel
    from app.cognitive.session_store import CognitiveSession
    from app.gateway.openrouter_jev import JevDecisionResult, normalize_questions, validate_answer
    from app.models import ModelCallLog

    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "chat_provider", "openrouter")
    monkeypatch.setattr(settings, "intelligence_provider", "openrouter")
    monkeypatch.setattr(settings, "cost_cap_enabled", False)
    monkeypatch.setattr(kernel, "should_prefetch_memory", lambda **kwargs: False)
    monkeypatch.setattr(kernel, "compile_context", lambda **kwargs: "system context")
    monkeypatch.setattr(
        "app.ev.computer_runtime.computer_prompt_state",
        lambda **kwargs: ({}, True),
    )

    seen: list[dict] = []

    async def fake_decide(self, state, questions, *, model=None):
        seen.append({"state": state, "questions": questions, "model": model})
        normalized = normalize_questions(questions)
        answer = validate_answer(
            "kernel_action",
            normalized["kernel_action"],
            {
                "type": "choice",
                "choice": "goal_status",
                "probabilities": {"goal_status": 1.0, "unsupported": 0.0},
                "confidence": 1.0,
            },
        )
        return JevDecisionResult(
            model=model or self.default_model,
            answers={"kernel_action": answer},
            usage={"input_tokens": 8, "output_tokens": 1, "cost": 0.000001},
            response_id="jev-kernel-test",
            latency_ms=1.0,
        )

    async def _async_none(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(kernel, "_dispatch_kernel_code", _async_none)
    monkeypatch.setattr(OpenRouterJevProvider, "decide", fake_decide)
    cognition = CognitiveSession(
        session_id="jev-kernel-session",
        focused_goal_id="goal-test",
        semantic_objective="Finish the notes",
    )
    result = await kernel._muse_turn(
        db_session,
        text="What am I working on?",
        live_session_id=None,
        device_id=None,
        modality="text",
        actor="test",
        cognition=cognition,
        started=time.perf_counter(),
    )

    assert result.kind == "decision_tool"
    assert result.last_tool == "goal.status"
    assert result.spoken == "Working on Finish the notes."
    assert seen and seen[0]["questions"]["kernel_action"]["type"] == "choice"
    assert seen[0]["state"]["available_deterministic_actions"] == ["goal_status"]
    audit_rows = list(
        (
            await db_session.execute(
                select(ModelCallLog).where(ModelCallLog.provider == "openrouter")
            )
        )
        .scalars()
        .all()
    )
    assert audit_rows
    metadata = audit_rows[-1].envelope["metadata"]
    assert metadata["jev_decision"]["response_id"] == "jev-kernel-test"
    assert "transcript" not in json.dumps(metadata["jev_decision"])
    assert audit_rows[-1].envelope["metadata"]["cost_source"] == "openrouter_reported"


async def test_gateway_rejects_jev_choice_outside_owner_enum(monkeypatch, db_session) -> None:
    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _jev_provider_app(
            captured,
            answers={
                "route": {
                    "type": "choice",
                    "choice": "delete_everything",
                    "probabilities": {"read_only": 0.1, "clarify": 0.1, "refuse": 0.8},
                    "confidence": 0.8,
                }
            },
        ),
    )
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    monkeypatch.setattr(settings, "chat_provider", "openrouter")
    decision = await choose_with_jev(
        ModelGateway(get_decision_provider()),
        [ChatMessage(role="user", content="Clean up my workspace.")],
        envelope=RequestEnvelope(request_id="jev-invalid-choice", strategy={}),
        session=db_session,
        actor="test",
        question_id="route",
        choices=["read_only", "clarify", "refuse"],
        instructions="Select the next safe route.",
    )
    assert decision.choice is None
    assert decision.validation == "error"
    assert any("outside the requested choices" in issue.lower() for issue in decision.issues)


async def test_jev_does_not_claim_chat_or_tool_call_support(db_session, monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured))
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setattr(settings, "chat_provider", "openrouter")
    from app.gateway.providers import UnknownProviderError

    with pytest.raises(UnknownProviderError, match="only supports typed decisions"):
        get_chat_provider()
    assert captured == []


async def test_jev_requires_env_and_database_consent(monkeypatch) -> None:
    _enable_jev(monkeypatch)
    monkeypatch.delenv("EV_ALLOW_REMOTE_CHAT", raising=False)
    with pytest.raises(OpenRouterEgressDenied):
        await OpenRouterJevProvider().decide("hello", _jev_route_question())

    _enable_jev(monkeypatch)
    with pytest.raises(OpenRouterEgressDenied, match="no active chat_egress"):
        await OpenRouterJevProvider().decide("hello", _jev_route_question())


async def test_revoked_jev_chat_egress_consent_blocks_request(db_session, monkeypatch) -> None:
    from app.models import ConsentRecord

    db_session.add(
        ConsentRecord(
            track="chat_egress",
            purpose="revoked synthetic provider test",
            scope={"synthetic": True},
            source="test",
            revoked_at=utcnow(),
            revoked_reason="test revocation",
        )
    )
    await db_session.commit()
    _enable_jev(monkeypatch)
    with pytest.raises(OpenRouterEgressDenied, match="no active chat_egress"):
        await OpenRouterJevProvider().decide("hello", _jev_route_question())


async def test_jev_refuses_missing_key_raw_pixels_and_model_override(db_session, monkeypatch) -> None:
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(OpenRouterJevUnavailable, match="EV_OPENROUTER_API_KEY"):
        await OpenRouterJevProvider().decide("hello", _jev_route_question())

    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    with pytest.raises(OpenRouterJevUnavailable, match="derived text only"):
        await OpenRouterJevProvider().decide(
            {"image": "data:image/png;base64,AA=="}, _jev_route_question()
        )
    with pytest.raises(OpenRouterJevUnavailable, match="model overrides"):
        await OpenRouterJevProvider().decide(
            "hello", _jev_route_question(), model="some-other-model"
        )


async def test_jev_rejects_untrusted_destination_before_any_request(monkeypatch) -> None:
    _enable_jev(monkeypatch)
    provider = OpenRouterJevProvider(base_url="https://openrouter.ai.attacker.invalid/api/v1")
    with pytest.raises(OpenRouterJevUnavailable, match="trusted HTTPS OpenRouter"):
        await provider.decide("hello", _jev_route_question())


async def test_jev_accepts_derived_ocr_text_but_not_pixels(db_session, monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured))
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    result = await OpenRouterJevProvider().decide(
        {"screen_text": "Derived OCR: Save button"}, _jev_route_question()
    )
    assert result.answers["route"].choice == "read_only"
    assert captured[0]["state"]["screen_text"] == "Derived OCR: Save button"


@pytest.mark.parametrize(
    "dated_model",
    ["typesafe/jev-1.13-20261001", "typesafe/jev-1.13-2026-10-01"],
)
def test_jev_accepts_compact_and_iso_dated_model_ids(dated_model: str) -> None:
    assert OpenRouterJevProvider._valid_model_id(dated_model, "typesafe/jev-1.13")


@pytest.mark.parametrize(
    "dated_model",
    [
        "typesafe/jev-1.13-20260230",
        "typesafe/jev-1.13-2026-13-01",
        "typesafe/jev-1.13-2026101",
        "typesafe/jev-1.13-today",
    ],
)
def test_jev_rejects_malformed_dated_model_ids(dated_model: str) -> None:
    assert not OpenRouterJevProvider._valid_model_id(
        dated_model, "typesafe/jev-1.13"
    )


async def test_jev_gateway_blocks_forbidden_envelope_and_raw_media(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured))
    _enable_jev(monkeypatch)
    gateway = ModelGateway(OpenRouterJevProvider())
    blocked = await gateway.decide(
        {"request": "choose safely"},
        _jev_route_question(),
        envelope=RequestEnvelope(
            request_id="jev-privacy",
            strategy={"source": "never_send_to_model"},
        ),
    )
    assert blocked.status == "blocked"
    assert captured == []

    media = await gateway.decide(
        {"image": "data:image/png;base64,AA=="},
        _jev_route_question(),
        envelope=RequestEnvelope(request_id="jev-media", strategy={}),
    )
    assert media.status == "blocked"
    assert captured == []


async def test_jev_cost_cap_uses_exact_sanitized_decision_payload(
    db_session, monkeypatch,
) -> None:
    del db_session
    from app.gateway import service as gateway_service

    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured))
    _enable_jev(monkeypatch)
    monkeypatch.setattr(settings, "cost_cap_enabled", True)
    guarded: list[list[ChatMessage]] = []

    async def reject_actual_payload(session, *, provider, messages):
        del session
        assert provider == "openrouter"
        guarded.append(list(messages))
        raise CostCapExceeded("exact JEV request exceeds cap")

    monkeypatch.setattr(gateway_service, "check_cost_cap", reject_actual_payload)
    call = await ModelGateway(OpenRouterJevProvider()).decide(
        {"request": "synthetic state"},
        _jev_route_question(),
        envelope=RequestEnvelope(request_id="jev-cap", strategy={}),
    )
    assert call.status == "error"
    assert call.degradation == {"kind": "cost_cap", "provider": "openrouter"}
    assert len(guarded) == 1
    sent_shape = json.loads(guarded[0][0].content)
    assert set(sent_shape) == {"model", "state", "questions"}
    assert sent_shape["state"] == {"request": "synthetic state"}
    assert captured == []


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({}, "exactly one answer"),
        (
            {
                "route": {
                    "type": "choice",
                    "choice": "read_only",
                },
                "injected": {"type": "noul", "noul": 0.5},
            },
            "unexpected fields",
        ),
        (
            {
                "route": {
                    "type": "choice",
                    "choice": "read_only",
                    "explanation": "not part of the typed answer",
                }
            },
            "unexpected fields",
        ),
    ],
)
async def test_jev_rejects_missing_extra_or_malformed_answers(
    answers: dict, message: str, db_session, monkeypatch
) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured, answers=answers))
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    call = await ModelGateway(OpenRouterJevProvider()).decide(
        {"request": "Choose a safe route."},
        _jev_route_question(),
        envelope=RequestEnvelope(request_id="jev-answer-validation", strategy={}),
    )
    assert call.status == "error"
    assert message in (call.error or "").lower()


async def test_jev_gateway_decision_is_audited_without_raw_state(db_session, monkeypatch) -> None:
    from sqlalchemy import select

    from app.models import ModelCallLog

    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured))
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    call = await ModelGateway(get_decision_provider()).decide(
        {"synthetic_request": "read the visible settings only"},
        _jev_route_question(),
        envelope=RequestEnvelope(request_id="jev-audit", strategy={}),
    )
    assert call.status == "ok"
    assert call.model == "typesafe/jev-1.13-20261001"
    assert call.response_id == "jev-test"
    assert call.result.text == ""
    assert captured and captured[0]["model"] == "typesafe/jev-1.13"
    assert call.envelope.metadata["jev_decision"]["question_ids"] == ["route"]
    assert "synthetic_request" not in json.dumps(call.envelope.metadata["jev_decision"])

    expected_hash = hashlib.sha256(
        json.dumps(captured[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert call.envelope.metadata["jev_decision"]["payload_sha256"] == expected_hash
    row = await log_model_call(db_session, call=call, actor="test")
    assert row.model == "typesafe/jev-1.13-20261001"
    assert row.latency_ms == call.latency_ms
    assert row.envelope["metadata"]["jev_decision"]["response_id"] == "jev-test"
    assert row.envelope["metadata"]["cost_source"] == "openrouter_reported"
    calls = list(
        (
            await db_session.execute(
                select(ModelCallLog).where(ModelCallLog.provider == "openrouter")
            )
        )
        .scalars()
        .all()
    )
    assert calls and calls[0].envelope["metadata"]["jev_decision"]["payload_sha256"] == expected_hash


async def test_jev_registry_stays_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jev_enabled", False)
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setattr(settings, "chat_provider", "openrouter")

    with pytest.raises(OpenRouterJevDisabled):
        get_decision_provider()
    from app.gateway.providers import UnknownProviderError

    with pytest.raises(UnknownProviderError, match="typed decisions"):
        get_chat_provider()


def test_jev_single_brain_mode_overrides_muse_without_silent_routing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "deepseek_api_key", "configured-but-not-a-candidate")
    assert configured_intelligence_provider() == "openrouter"
    assert muse_brain_active() is False
    from app.gateway.providers import UnknownProviderError

    with pytest.raises(UnknownProviderError, match="typed decisions"):
        get_chat_provider()
    assert isinstance(get_decision_provider(), OpenRouterJevProvider)
    assert routing_candidates() == ["openrouter"]
    selected = select_provider(configured="openrouter", evidence=None)
    assert selected.provider == "openrouter"
    assert selected.reason == "jev_single_brain"


async def test_jev_mode_keeps_spark_on_the_code_lane(tmp_path, monkeypatch) -> None:
    from app.ev import luna_code

    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "chat_provider", "openrouter")
    monkeypatch.setattr(settings, "intelligence_provider", "openrouter")
    monkeypatch.setattr(settings, "turn_control_provider", "openrouter")
    monkeypatch.setattr(settings, "code_model", "muse-spark-1.3-contributor")
    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects", "")
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    async def spark_code_loop(*args, **kwargs):
        return {
            "ok": True,
            "spoken": "Wrote a script and ran it.",
            "files_changed": ["hello.py"],
            "runs": [{"ok": True, "argv": ["python3", "hello.py"], "exit_code": 0}],
            "brain": "muse-spark-1.3-contributor",
            "workspace": str(tmp_path),
            "degraded": False,
        }

    async def luna_code_loop(*args, **kwargs):
        raise AssertionError("JEV must not take the code lane")

    monkeypatch.setattr(luna_code, "_spark_code_loop", spark_code_loop)
    monkeypatch.setattr(luna_code, "_luna_loop", luna_code_loop)
    result = await luna_code.run_code_job("write a python script that prints hello")
    assert result["ok"] is True
    assert result["brain"] == "muse-spark-1.3-contributor"


def test_openrouter_reported_cost_is_never_fabricated() -> None:
    assert reported_openrouter_cost({"cost": 0.000012}) == pytest.approx(0.000012)
    assert reported_openrouter_cost({"cost": -1}) is None
    assert reported_openrouter_cost({"cost": "unknown"}) is None
    assert reported_openrouter_cost({"cost": True}) is None
    assert reported_openrouter_cost({}) is None


async def test_openrouter_receipt_cost_is_audited_and_used_for_month_total(db_session) -> None:
    call = GatewayCall(
        provider="openrouter",
        request_id="jev-cost-test",
        envelope=RequestEnvelope(request_id="jev-cost-test", strategy={}),
        result=ChatResult(
            text="",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "cost": 0.000001,
                "cost_source": "openrouter_reported",
            },
            model="typesafe/jev-1.13",
        ),
        latency_ms=7.0,
    )
    row = await log_model_call(db_session, call=call, actor="test")
    assert row.envelope["metadata"]["cost_usd"] == pytest.approx(0.000001)
    assert row.envelope["metadata"]["cost_source"] == "openrouter_reported"
    assert await monthly_cost_usd(db_session, now=utcnow()) == pytest.approx(0.000001)


async def test_missing_openrouter_usage_is_logged_as_a_conservative_estimate(
    db_session, monkeypatch
) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _jev_provider_app(captured, usage={}))
    _enable_jev(monkeypatch)
    await _grant_jev_chat_egress(db_session)
    call = await ModelGateway(get_decision_provider()).decide(
        {"request": "Choose one of the safe options."},
        _jev_route_question(),
        envelope=RequestEnvelope(request_id="jev-missing-usage", strategy={}, context_tokens=40),
    )
    row = await log_model_call(db_session, call=call, actor="test")
    assert row.envelope["metadata"]["cost_usd"] > 0
    assert row.envelope["metadata"]["cost_source"] == "conservative_estimate_usage_missing"
    assert await monthly_cost_usd(db_session, now=utcnow()) == pytest.approx(
        row.envelope["metadata"]["cost_usd"]
    )


async def test_jev_role_rejects_arbitrary_structured_generation(db_session, monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _jev_provider_app(captured),
    )
    _enable_jev(monkeypatch)
    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    await _grant_jev_chat_egress(db_session)

    from app.gateway.roles import chat_structured_via_role

    schema = {
        "type": "object",
        "properties": {
            "route": {"type": "string"},
            "operation": {"type": "string"},
        },
        "required": ["route", "operation"],
    }
    with pytest.raises(OpenRouterJevUnavailable, match="cannot generate structured output"):
        await chat_structured_via_role(
            [ChatMessage(role="user", content="Text Mom I'm late.")],
            schema=schema,
            schema_name="turn_intent",
        )
    assert captured == []


async def test_jev_streaming_path_fails_closed_without_fake_tokens(monkeypatch) -> None:
    captured: list[dict] = []
    _enable_jev(monkeypatch)
    gateway = ModelGateway(OpenRouterJevProvider())
    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="say hi")],
            envelope=RequestEnvelope(request_id="jev-stream", strategy={}),
        )
    ]

    deltas = [event for event in events if event.kind == "delta"]
    assert deltas == []
    errors = [event for event in events if event.kind == "error"]
    assert errors
    assert events[-1].kind == "done"
    assert events[-1].call is not None
    assert events[-1].call.status == "error"
    assert events[-1].call.result.text == ""
    assert captured == []


def test_model_lanes_are_separate_and_explicit(monkeypatch) -> None:
    from app.gateway.muse import MUSE_SPARK_MODEL
    from app.gateway.roles import (
        resolve_code_brain,
        resolve_text_brain,
        resolve_voice_mouth,
    )

    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_model", "typesafe/jev-1.13")
    monkeypatch.setattr(settings, "openai_realtime_model", "gpt-realtime-2.1-mini")

    text = resolve_text_brain()
    assert (text.provider, text.model) == ("openrouter", "typesafe/jev-1.13")

    code = resolve_code_brain()
    assert code.provider == "meta_muse_spark"
    assert code.model == MUSE_SPARK_MODEL
    assert code.reason == "code_lane_is_always_spark"

    mouth = resolve_voice_mouth()
    assert (mouth.provider, mouth.model) == ("openai-realtime", "gpt-realtime-2.1-mini")
    assert mouth.role == "mouth"

    # Code never follows the text role, even outside jev_kernel.
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    assert resolve_code_brain().provider == "meta_muse_spark"


async def test_realtime_delegation_admission_does_not_wait_for_kernel(monkeypatch):
    from app.cognitive import delegation, kernel
    from app.cognitive.mode import cognitive_mode, realtime_delegate_active

    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    started, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    async def blocked_kernel(**kwargs):
        assert cognitive_mode() == "mimo_kernel"
        seen.append(kwargs["transcript"])
        started.set()
        await release.wait()
        return kernel.KernelResult(spoken="The verified answer.")

    async def receive(receipt):
        assert realtime_delegate_active()
        assert receipt["status"] == "answered"
        delivered.set()

    monkeypatch.setattr(kernel, "handle_turn", blocked_kernel)
    try:
        receipt = await asyncio.wait_for(delegation.submit_delegate(
            task="model-invented yes", owner_transcript="What is the requested analysis?",
            owner_turn_id="actual-owner-turn", request_id="first-tool-call",
            live_session_id="test-delegate", on_complete=receive,
        ), timeout=1)
        assert receipt["status"] == "queued"
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not delivered.is_set()
        duplicate = await delegation.submit_delegate(
            task="another model paraphrase", owner_transcript="What is the requested analysis?",
            owner_turn_id="actual-owner-turn", request_id="second-tool-call",
            live_session_id="test-delegate", on_complete=receive,
        )
        assert duplicate["job_id"] == receipt["job_id"]
        release.set()
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert seen == ["What is the requested analysis?"]
        final = await delegation.get_delegate(receipt["job_id"])
        assert final["status"] == "answered"
        assert final["spoken"] == "The verified answer."
    finally:
        release.set()
        for future in list(delegation._tasks.values()):
            await future


async def test_realtime_delegation_cancel_delivers_receipt(monkeypatch):
    from app.cognitive import delegation, kernel
    from app.cognitive.mode import realtime_delegate_active

    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    started, release = asyncio.Event(), asyncio.Event()
    deliveries = []

    async def blocked_kernel(**kwargs):
        started.set()
        await release.wait()
        return kernel.KernelResult(spoken="Must not arrive after cancellation.")

    async def receive(receipt):
        assert realtime_delegate_active()
        deliveries.append(receipt["status"])

    monkeypatch.setattr(kernel, "handle_turn", blocked_kernel)
    receipt = await delegation.submit_delegate(
        task="Analyze the current report", request_id="cancel-test", on_complete=receive,
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    future = delegation._tasks[receipt["job_id"]]
    cancelled = await delegation.cancel_delegate(receipt["job_id"])
    await future
    assert cancelled["status"] == "cancelled"
    assert deliveries == ["cancelled"]
    assert (await delegation.get_delegate(receipt["job_id"]))["status"] == "cancelled"


@pytest.mark.parametrize(
    ("confidence", "evidence", "expected"),
    [("UNKNOWN", {"receipt": "partial"}, "answered"),
     ("COMPLETED_VERIFIED", {}, "answered"),
     ("COMPLETED_VERIFIED", {"receipt": "verified"}, "completed")],
)
async def test_delegation_goal_completion_requires_verification(confidence, evidence, expected):
    from app.cognitive import delegation
    from app.db import SessionLocal
    from app.models import PresenceContract, ResearchSession

    async with SessionLocal() as db:
        goal = PresenceContract(state="COMPLETED", confidence=confidence, evidence=evidence)
        job = ResearchSession(mode="rt_delegate", question="Analyze report", status="waiting")
        db.add_all([goal, job])
        await db.commit()
        goal_id, job_id = str(goal.id), job.id
    receipt = await delegation._monitor_goal(job_id, goal_id)
    assert receipt["status"] == expected
    assert receipt["result"]["verified"] is (expected == "completed")
