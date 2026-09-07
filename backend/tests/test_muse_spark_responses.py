"""Deterministic contract tests for the Muse Spark Contributor Responses seam."""

from __future__ import annotations

import json

import pytest

from app.contracts import ChatMessage, RequestEnvelope, ToolCall, ToolSpec
from app.gateway.muse import muse_spark_api_key, muse_spark_key_loaded
from app.gateway.muse_spark import MuseSparkProvider, responses_tools
from app.gateway.reliability import CIRCUIT_BREAKERS
from app.gateway.service import ModelGateway


def _tool() -> ToolSpec:
    return ToolSpec(
        name="open_app",
        description="Open an approved local app.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        permission="apps:act",
    )


def test_spark_uses_dedicated_opencode_key_not_meta_voice_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "meta_model_api_key", "meta-only-key")
    monkeypatch.setattr(settings, "opencode_api_key", None)
    monkeypatch.setattr(settings, "opencode_env_file", "")
    monkeypatch.setenv("EV_OPENCODE_API_KEY", "")
    monkeypatch.setenv("OPENCODE_API_KEY", "")
    assert muse_spark_api_key() == ""
    assert muse_spark_key_loaded() is False


def test_responses_history_and_tool_schema_are_provider_neutral() -> None:
    provider = MuseSparkProvider(
        base_url="https://opencode.ai/zen/go/v1",
        api_key="test-key",
        default_model="gpt-5.6-luna",
    )
    messages = [
        ChatMessage(role="system", content="You are Evie."),
        ChatMessage(role="user", content="Open Calculator."),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="open_app", arguments={"name": "Calculator"})],
        ),
        ChatMessage(
            role="tool",
            name="open_app",
            tool_call_id="call_1",
            content='{"ok":true}',
        ),
    ]
    payload = provider._payload(messages, model="grok-4.6", tools=[_tool()], stream=False)

    assert payload["model"] == "muse-spark-1.3-contributor"
    assert payload["input"] == [
        {"role": "system", "content": "You are Evie."},
        {"role": "user", "content": "Open Calculator."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "open_app",
            "arguments": '{"name": "Calculator"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'},
    ]
    assert payload["tools"] == responses_tools([_tool()])
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["name"] == "open_app"
    assert payload["reasoning"] == {"effort": "high"}
    assert "messages" not in payload
    assert "temperature" not in payload
    low = provider._payload(
        messages, model="grok-4.6", tools=[_tool()], stream=False, reasoning_effort="low"
    )
    assert low["reasoning"] == {"effort": "low"}


def test_chat_pipeline_does_not_stream_away_offered_tools() -> None:
    import inspect

    from app.api.core import run_chat_pipeline

    source = inspect.getsource(run_chat_pipeline)
    assert "not write_needed and not tool_specs" in source
    assert 'call.status in {"error", "degraded"}' in source
    assert "muse_empty_response" in source


@pytest.mark.asyncio
async def test_responses_json_and_sse_keep_typed_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    CIRCUIT_BREAKERS.reset("meta_muse_spark")
    calls: list[dict] = []

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            events = [
                ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Opening "}),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "open_app",
                            "arguments": "",
                        },
                    },
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "call_id": "call_1",
                        "delta": '{"name":"Calculator"}',
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "call_1",
                        "name": "open_app",
                        "arguments": '{"name":"Calculator"}',
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "model": "muse-spark-1.3-contributor",
                            "usage": {"input_tokens": 14, "output_tokens": 6},
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "fc_1",
                                    "call_id": "call_1",
                                    "name": "open_app",
                                    "arguments": '{"name":"Calculator"}',
                                }
                            ],
                        },
                    },
                ),
            ]
            for event_name, event in events:
                yield f"event: {event_name}"
                yield f"data: {json.dumps(event)}"
            yield "data: [DONE]"

        async def aclose(self):
            return None

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "resp_1",
                "model": "muse-spark-1.3-contributor",
                "output_text": "Opening Calculator.",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "open_app",
                        "arguments": '{"name":"Calculator"}',
                    }
                ],
                "usage": {"input_tokens": 14, "output_tokens": 6},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "payload": json})
            return _Response()

        def stream(self, method, url, headers=None, json=None):
            calls.append({"method": method, "url": url, "headers": headers, "payload": json})
            return _Stream()

    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(
        base_url="https://opencode.ai/zen/go/v1",
        api_key="test-key",
        default_model="gpt-5.6-luna",
    )

    result = await provider.chat_with_tools(
        [ChatMessage(role="user", content="Open Calculator.")], [_tool()]
    )
    assert result.text == "Opening Calculator."
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].arguments == {"name": "Calculator"}
    assert calls[0]["url"].endswith("/responses")

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="Open Calculator.")], tools=[_tool()]
        )
    ]
    assert "".join(chunk.text for chunk in chunks if chunk.text) == "Opening "
    done = chunks[-1]
    assert done.done is True
    assert done.tool_calls[0].name == "open_app"
    assert done.tool_calls[0].arguments == {"name": "Calculator"}
    assert done.usage["input_tokens"] == 14


@pytest.mark.asyncio
async def test_gateway_stream_forwards_tools_to_responses_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    CIRCUIT_BREAKERS.reset("meta_muse_spark")
    captured: dict = {}

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'event: response.output_text.delta'
            yield 'data: {"type":"response.output_text.delta","delta":"done"}'
            yield 'event: response.completed'
            yield 'data: {"type":"response.completed","response":{"model":"muse-spark-1.3-contributor","output":[],"usage":{}}}'

        async def aclose(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured.update({"method": method, "url": url, "headers": headers, "payload": json})
            return _Stream()

    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(base_url="https://opencode.ai/zen/go/v1", api_key="test-key")
    gateway = ModelGateway(provider)
    events = [
        event
        async for event in gateway.stream_chat(
            [ChatMessage(role="user", content="Open Calculator.")],
            envelope=RequestEnvelope(request_id="req-1", strategy={}),
            tools=[_tool()],
        )
    ]

    assert events[-1].kind == "done"
    assert events[-1].call is not None
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/responses")
    assert captured["payload"]["tools"][0]["name"] == "open_app"


@pytest.mark.asyncio
async def test_spark_empty_completed_stream_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.reliability import ProviderStreamError

    CIRCUIT_BREAKERS.reset("meta_muse_spark")

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "event: response.completed"
            yield (
                'data: {"type":"response.completed","response":'
                '{"model":"muse-spark-1.3-contributor","output":[],"usage":{}}}'
            )

        async def aclose(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, headers=None, json=None):
            return _Stream()

    monkeypatch.setattr("app.gateway.muse_spark.httpx.AsyncClient", _Client)
    provider = MuseSparkProvider(base_url="https://opencode.ai/zen/go/v1", api_key="test-key")
    with pytest.raises(ProviderStreamError, match="no text or tools"):
        async for _event in provider.stream_chat(
            [ChatMessage(role="user", content="Are you there?")]
        ):
            pass
