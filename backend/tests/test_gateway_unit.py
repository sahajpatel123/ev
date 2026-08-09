"""Unit tests for the neutral model gateway, envelope, and tool validation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.config import settings
from app.contracts import (
    ChatMessage,
    ChatResult,
    MemoryRef,
    RequestEnvelope,
    ToolCall,
    ToolSpec,
)
from app.gateway.providers import (
    PROVIDER_REGISTRY,
    get_chat_provider,
    register_provider,
)
from app.gateway.service import ModelGateway, tool_specs_from_dicts
from app.gateway.validation import validate_tool_calls


def test_tool_validation_rectifies_defaults_and_rejects_bad_calls() -> None:
    specs = [
        ToolSpec(
            name="search_memory",
            description="Search memory.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        )
    ]
    rectified = validate_tool_calls(
        [ToolCall(id="1", name="search_memory", arguments={"query": "kyoto"})],
        specs,
    )
    assert rectified[0].status == "rectified"
    assert rectified[0].rectified_arguments == {"query": "kyoto", "k": 10}

    rejected = validate_tool_calls(
        [ToolCall(id="2", name="search_memory", arguments={"query": 123})],
        specs,
    )
    assert rejected[0].status == "rejected"
    assert any("must be string" in issue for issue in rejected[0].issues)

    unknown = validate_tool_calls([ToolCall(id="3", name="nope", arguments={})], specs)
    assert unknown[0].status == "rejected"
    assert "unknown tool" in unknown[0].issues[0]


def test_sensitive_tool_requires_permission_gate() -> None:
    spec = ToolSpec(
        name="get_health_trends",
        description="Health trends.",
        parameters={"type": "object", "properties": {}},
        sensitive=True,
    )
    blocked = validate_tool_calls(
        [ToolCall(id="1", name="get_health_trends", arguments={})],
        [spec],
    )
    assert blocked[0].status == "rejected"
    assert "requires explicit permission" in blocked[0].issues[0]

    allowed = validate_tool_calls(
        [ToolCall(id="2", name="get_health_trends", arguments={})],
        [spec],
        sensitive_allowed=True,
    )
    assert allowed[0].status == "ok"


@dataclass
class RecordingProvider:
    """Test provider that records the messages/tools it receives."""

    name: str = "recording"
    tool_calls: list[ToolCall] | None = None
    seen_messages: list[ChatMessage] | None = None
    seen_tools: list[ToolSpec] | None = None
    fail: bool = False

    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=0.7,
    ) -> ChatResult:
        self.seen_messages = list(messages)
        if self.fail:
            raise RuntimeError("provider down")
        return ChatResult(text="EV: ok", usage={"prompt_tokens": 5, "completion_tokens": 2}, model=model)

    async def chat_with_tools(
        self,
        messages,
        tools,
        *,
        model=None,
        temperature=0.7,
    ) -> ChatResult:
        self.seen_messages = list(messages)
        self.seen_tools = list(tools)
        if self.fail:
            raise RuntimeError("provider down")
        return ChatResult(
            text="",
            tool_calls=list(self.tool_calls or []),
            usage={"prompt_tokens": 8, "completion_tokens": 3},
            model=model,
        )

    async def list_models(self) -> list[str]:
        return ["recording-model"]


@pytest.mark.asyncio
async def test_gateway_carries_envelope_and_validates_tool_calls() -> None:
    provider = RecordingProvider(
        tool_calls=[
            ToolCall(id="c1", name="calculate", arguments={"expression": "2+2"}),
            ToolCall(id="c2", name="get_health_trends", arguments={"metric": "hrv_ms"}),
        ]
    )
    gateway = ModelGateway(provider)
    envelope = RequestEnvelope(
        request_id="req-123",
        strategy={"mode": "technical", "intent": "question"},
        memories=[MemoryRef(memory_id="m1", memory_type="fact", text="Kyoto", score=0.9)],
        conversation_id="conv-1",
        context_tokens=120,
        metadata={"context_depth": "standard"},
    )
    call = await gateway.chat(
        [ChatMessage(role="user", content="what is 2+2?")],
        envelope=envelope,
        tools=tool_specs_from_dicts(
            [
                {
                    "name": "calculate",
                    "description": "Arithmetic.",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
                {
                    "name": "get_health_trends",
                    "description": "Health trends.",
                    "parameters": {
                        "type": "object",
                        "properties": {"metric": {"type": "string"}},
                        "required": ["metric"],
                    },
                    "sensitive": True,
                },
            ]
        ),
        model="recording-model",
        temperature=0.2,
    )
    assert call.request_id == "req-123"
    assert call.provider == "recording"
    assert call.model == "recording-model"
    assert call.latency_ms >= 0
    assert call.envelope.strategy["mode"] == "technical"
    assert call.envelope.memories[0].memory_id == "m1"
    assert provider.seen_messages[0].content == "what is 2+2?"
    assert [t.name for t in provider.seen_tools] == ["calculate", "get_health_trends"]

    by_name = {v.call.name: v for v in call.tool_validation}
    assert by_name["calculate"].status == "ok"
    assert by_name["get_health_trends"].status == "rejected"
    assert "requires explicit permission" in by_name["get_health_trends"].issues[0]


@pytest.mark.asyncio
async def test_gateway_audits_provider_errors() -> None:
    gateway = ModelGateway(RecordingProvider(fail=True))
    call = await gateway.chat(
        [ChatMessage(role="user", content="hi")],
        envelope=RequestEnvelope(request_id="req-err", strategy={}),
    )
    assert call.status == "error"
    assert call.error == "RuntimeError: provider down"
    assert call.result.text == ""


@pytest.mark.asyncio
async def test_provider_swap_is_registry_config_change() -> None:
    register_provider("recording", RecordingProvider)
    original = settings.chat_provider
    try:
        settings.chat_provider = "recording"
        provider = get_chat_provider()
        assert isinstance(provider, RecordingProvider)
        assert provider.name == "recording"
        assert "recording" in PROVIDER_REGISTRY
    finally:
        settings.chat_provider = original
        PROVIDER_REGISTRY.pop("recording", None)
