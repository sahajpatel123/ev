"""MiMo-V2.6-Flash provider and role wiring (offline, hermetic)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request

from app.config import settings
from app.contracts import ChatMessage, ToolSpec


def _patch_http(monkeypatch, app: FastAPI) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
        ),
    )


def _enable_mimo(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mimo_enabled", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "http://local/v1")
    monkeypatch.setattr(settings, "mimo_model", "xiaomi/mimo-v2.6-flash")
    monkeypatch.setattr(settings, "mimo_reasoning_effort", "high")
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setenv("EV_ALLOW_REMOTE_CHAT", "true")


def _completion_app(captured: list[dict], *, content: str = "Hi there.") -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        captured.append(await request.json())
        return {
            "id": "mimo-test",
            "model": "xiaomi/mimo-v2.6-flash",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "cost": 0.000002,
            },
        }

    return app


async def test_mimo_chat_payload_has_reasoning_and_usage_include(
    db_session, monkeypatch
) -> None:
    from app.gateway.openrouter_mimo import MimoProvider

    captured: list[dict] = []
    _patch_http(monkeypatch, _completion_app(captured))
    _enable_mimo(monkeypatch)

    result = await MimoProvider().chat([ChatMessage(role="user", content="hello")])

    assert result.text == "Hi there."
    assert result.usage["cost"] == pytest.approx(0.000002)
    payload = captured[0]
    assert payload["model"] == "xiaomi/mimo-v2.6-flash"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["usage"] == {"include": True}
    assert "thinking" not in payload
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


async def test_mimo_chat_with_tools_parses_tool_calls(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_mimo import MimoProvider

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        await request.json()
        return {
            "id": "mimo-tools",
            "model": "xiaomi/mimo-v2.6-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Paris"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "cost": 0.000003},
        }

    _patch_http(monkeypatch, app)
    _enable_mimo(monkeypatch)
    tool = ToolSpec(
        name="get_weather",
        description="Weather",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    result = await MimoProvider().chat_with_tools(
        [ChatMessage(role="user", content="weather in Paris")], [tool]
    )
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "Paris"}


async def test_mimo_stream_parses_deltas_and_tools(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_mimo import MimoProvider

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> object:
        await request.json()
        from fastapi.responses import StreamingResponse

        async def gen():
            for piece in ("Hel", "lo"):
                yield "data: " + json.dumps(
                    {"model": "xiaomi/mimo-v2.6-flash", "choices": [{"delta": {"content": piece}}]}
                ) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "model": "xiaomi/mimo-v2.6-flash",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-9",
                                        "function": {
                                            "name": "goal_status",
                                            "arguments": '{"x": 1}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.000001},
                }
            ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    _patch_http(monkeypatch, app)
    _enable_mimo(monkeypatch)
    chunks = [
        chunk
        async for chunk in MimoProvider().stream_chat(
            [ChatMessage(role="user", content="status")]
        )
    ]
    text = "".join(chunk.text for chunk in chunks)
    assert text == "Hello"
    done = [chunk for chunk in chunks if chunk.done]
    assert done and done[0].tool_calls[0].name == "goal_status"
    assert done[0].usage["cost"] == pytest.approx(0.000001)


async def test_mimo_structured_returns_json_text(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_mimo import MimoProvider

    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _completion_app(captured, content='{"route": "life", "operation": "send"}'),
    )
    _enable_mimo(monkeypatch)
    result = await MimoProvider().chat_structured(
        [ChatMessage(role="user", content="text mom")],
        schema={"type": "object", "properties": {"route": {"type": "string"}}},
        schema_name="turn_intent",
    )
    assert json.loads(result.text) == {"route": "life", "operation": "send"}
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[0]["response_format"]["json_schema"]["name"] == "turn_intent"


async def test_mimo_egress_gate_and_missing_key(monkeypatch) -> None:
    from app.gateway.openrouter_mimo import (
        MimoEgressDenied,
        MimoProvider,
        MimoUnavailable,
    )

    _enable_mimo(monkeypatch)
    monkeypatch.delenv("EV_ALLOW_REMOTE_CHAT", raising=False)
    with pytest.raises(MimoEgressDenied):
        await MimoProvider().chat([ChatMessage(role="user", content="hi")])

    _enable_mimo(monkeypatch)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(MimoUnavailable, match="EV_OPENROUTER_API_KEY"):
        await MimoProvider().chat([ChatMessage(role="user", content="hi")])


def test_mimo_mode_roles_and_single_brain(monkeypatch) -> None:
    from app.gateway.muse import configured_intelligence_provider, muse_brain_active
    from app.gateway.openrouter_mimo import MimoProvider
    from app.gateway.providers import get_chat_provider
    from app.gateway.roles import (
        resolve_code_brain,
        resolve_text_brain,
        resolve_voice_mouth,
    )
    from app.gateway.routing import routing_candidates, select_provider

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "mimo_model", "xiaomi/mimo-v2.6-flash")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")

    assert configured_intelligence_provider() == "mimo"
    assert muse_brain_active() is False
    text = resolve_text_brain()
    assert (text.provider, text.model) == ("mimo", "xiaomi/mimo-v2.6-flash")
    code = resolve_code_brain()
    assert code.provider == "mimo"
    assert resolve_voice_mouth().model == settings.openai_realtime_model
    assert isinstance(get_chat_provider(), MimoProvider)
    assert routing_candidates() == ["mimo"]
    selected = select_provider(configured="mimo", evidence=None)
    assert selected.provider == "mimo"
    assert selected.reason == "mimo_single_brain"


async def test_chat_structured_via_role_uses_mimo(monkeypatch) -> None:
    from app.contracts import ChatResult
    from app.gateway import roles

    captured: dict = {}

    class _Stub:
        name = "mimo"

        async def chat_structured(self, messages, *, schema, schema_name, model=None, **kwargs):
            captured["schema_name"] = schema_name
            captured["model"] = model
            return ChatResult(text='{"ok": true}')

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(roles, "require_text_provider", lambda: _Stub())
    result = await roles.chat_structured_via_role(
        [ChatMessage(role="user", content="x")],
        schema={"type": "object"},
        schema_name="desk_act",
        model="meta_muse_spark",
    )
    assert result.text == '{"ok": true}'
    assert captured["schema_name"] == "desk_act"
    assert captured["model"] is None


async def test_mimo_code_loop_executes_tool_calls(monkeypatch) -> None:
    from app.contracts import ChatResult, ToolCall
    from app.ev import luna_code

    class _StubMimo:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, messages, specs, *, model=None, temperature=0.7, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="list_projects", arguments={})],
                )
            return ChatResult(text="Listed the projects.")

    stub = _StubMimo()
    monkeypatch.setattr("app.gateway.openrouter_mimo.MimoProvider", lambda: stub)
    executed: list[str] = []

    def fake_execute(name, args):
        executed.append(name)
        return {"ok": True, "path": "x.py"}

    monkeypatch.setattr(luna_code, "execute_code_tool", fake_execute)
    monkeypatch.setattr(luna_code, "list_projects", lambda: [])

    result = await luna_code._mimo_code_loop(
        "list projects", model="xiaomi/mimo-v2.6-flash", budget_s=10, live=False
    )
    assert executed == ["list_projects"]
    assert stub.calls == 2
    assert result["spoken"] == "Listed the projects."
    assert result["brain"] == "xiaomi/mimo-v2.6-flash"


async def test_mimo_kernel_turn_uses_mimo_provider(monkeypatch, tmp_path) -> None:
    from app.cognitive import kernel, telemetry
    from app.cognitive.session_store import reset_for_tests
    from app.contracts import ChatResult
    from app.gateway import roles

    class _Scripted:
        name = "mimo"

        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.calls += 1
            return ChatResult(text="Doing well — glad you're here.")

    scripted = _Scripted()
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "mimo_enabled", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "laptop_files", False)
    monkeypatch.setattr(roles, "require_text_provider", lambda: scripted)
    reset_for_tests()
    telemetry.reset_for_tests()
    try:
        result = await kernel.handle_turn(
            transcript="Tell me how you're doing today.", modality="text"
        )
        snapshot = telemetry.snapshot()
    finally:
        reset_for_tests()
        telemetry.reset_for_tests()
        monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    assert result.kind == "muse"
    assert "glad" in result.spoken.lower()
    assert scripted.calls == 1
    assert snapshot["muse_turns"] >= 1
