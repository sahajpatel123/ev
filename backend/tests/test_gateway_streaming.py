"""Real token streaming: providers, gateway, SSE endpoint, cancellation.

CORTEX (Agent 10) acceptance: first token is measured, the filter can
intercept chunks, the RequestEnvelope hash stays auditable, cancellation
provably stops the upstream generator, and ``curl -N`` sees progressive
``delta`` events before ``done``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.contracts import ChatMessage, ChatResult, RequestEnvelope, ToolSpec
from app.gateway.costs import (
    CostCapExceeded,
    monthly_cost_usd,
    reported_openrouter_cost,
)
from app.gateway.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    get_chat_provider,
)
from app.gateway.reliability import CIRCUIT_BREAKERS
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


async def test_compat_provider_parses_sse_stream(monkeypatch) -> None:
    _patch_http(monkeypatch, _stream_app())
    provider = OpenAICompatibleProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="stream-test",
        provider_name="stream-test",
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
    assert done[0].model == "stream-test"
    assert done[0].finish_reason == "stop"


async def test_compat_provider_accumulates_streamed_tool_calls(monkeypatch) -> None:
    captured: list[dict] = []
    _patch_http(monkeypatch, _stream_app(with_tools=True, captured=captured))
    provider = OpenAICompatibleProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="stream-test",
        provider_name="stream-test",
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
    provider = OpenAICompatibleProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="stream-test",
        provider_name="stream-test",
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
    assert done["provider_selection"]["reason"] == "single_brain_configured"

    audit = await client.get("/v1/gateway/calls", params={"request_id": "sse-req-1"})
    assert audit.status_code == 200, audit.text
    rows = audit.json()
    assert len(rows) == 1
    assert rows[0]["provider"] == "mock"
    assert rows[0]["envelope_hash"] == "sse-hash-1"
    assert rows[0]["envelope"]["metadata"]["provider_selection"]["provider"] == "mock"


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
                "model": "stream-test",
                "choices": [{"message": {"role": "assistant", "content": "recovered"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )

    return app, state


async def test_compat_provider_retries_transient_failures_with_backoff(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "model_max_retries", 2)
    monkeypatch.setattr(settings, "model_retry_base_seconds", 0.01)
    monkeypatch.setattr(settings, "model_retry_max_seconds", 0.05)
    app, state = _flaky_app(failures=2)
    _patch_http(monkeypatch, app)
    provider = OpenAICompatibleProvider(
        base_url="http://local/v1",
        api_key="test-key",
        default_model="stream-test",
        provider_name="stream-test",
    )
    provider._authorize = _offline_authorized
    result = await provider.chat([ChatMessage(role="user", content="hi")])
    assert result.text == "recovered"
    assert state["calls"] == 3


async def test_circuit_breaker_trips_and_gateway_degrades(monkeypatch) -> None:
    monkeypatch.setattr(settings, "model_max_retries", 0)
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    monkeypatch.setattr(settings, "circuit_cooldown_seconds", 300.0)
    CIRCUIT_BREAKERS.reset("stream-test")
    try:
        app, state = _flaky_app(failures=10, status_code=500)
        _patch_http(monkeypatch, app)
        provider = OpenAICompatibleProvider(
            base_url="http://local/v1",
            api_key="test-key",
            default_model="stream-test",
        provider_name="stream-test",
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
        assert call.degradation["provider"] == "stream-test"
        assert call.degradation["retry_after_seconds"] > 0
        assert "circuit breaker is open" in (call.error or "")
        assert state["calls"] == 2  # fast-fail: no upstream attempt
    finally:
        CIRCUIT_BREAKERS.reset("stream-test")


class BrokenStream(httpx.AsyncByteStream):
    """Delivers one SSE chunk, then fails mid-stream (simulated connection loss)."""

    def __init__(self) -> None:
        self.chunks = [
            (
                "data: "
                + json.dumps(
                    {
                        "id": "cmpl-broken",
                        "model": "stream-test",
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
    CIRCUIT_BREAKERS.reset("stream-test")
    try:
        stream = BrokenStream()
        _patch_mock_http(monkeypatch, stream)
        provider = OpenAICompatibleProvider(
            base_url="http://local/v1",
            api_key="test-key",
            default_model="stream-test",
        provider_name="stream-test",
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
        CIRCUIT_BREAKERS.reset("stream-test")


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


def test_openrouter_reported_cost_is_never_fabricated() -> None:
    assert reported_openrouter_cost({"cost": 0.000012}) == pytest.approx(0.000012)
    assert reported_openrouter_cost({"cost": -1}) is None
    assert reported_openrouter_cost({"cost": "unknown"}) is None
    assert reported_openrouter_cost({"cost": True}) is None
    assert reported_openrouter_cost({}) is None


async def test_openrouter_receipt_cost_is_audited_and_used_for_month_total(db_session) -> None:
    call = GatewayCall(
        provider="mimo",
        request_id="mimo-cost-test",
        envelope=RequestEnvelope(request_id="mimo-cost-test", strategy={}),
        result=ChatResult(
            text="",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "cost": 0.000001,
                "cost_source": "openrouter_reported",
            },
            model="xiaomi/mimo-v2.6-flash",
        ),
        latency_ms=7.0,
    )
    row = await log_model_call(db_session, call=call, actor="test")
    assert row.envelope["metadata"]["cost_usd"] == pytest.approx(0.000001)
    assert row.envelope["metadata"]["cost_source"] == "openrouter_reported"
    assert await monthly_cost_usd(db_session, now=utcnow()) == pytest.approx(0.000001)
