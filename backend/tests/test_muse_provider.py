"""Muse Spark provider and role wiring (offline, hermetic)."""

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


def _enable_brain(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "http://local/v1")
    monkeypatch.setattr(settings, "brain_model", "meta/muse-spark-1.3-contributor")
    monkeypatch.setattr(settings, "brain_reasoning_effort", "high")
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_gemini")
    monkeypatch.setenv("EV_ALLOW_REMOTE_CHAT", "true")


def _completion_app(captured: list[dict], *, content: str = "Hi there.") -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        captured.append(await request.json())
        return {
            "id": "brain-test",
            "model": "meta/muse-spark-1.3-contributor",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "cost": 0.000002,
            },
        }

    return app


async def test_brain_chat_payload_has_reasoning_and_usage_include(
    db_session, monkeypatch
) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider

    captured: list[dict] = []
    _patch_http(monkeypatch, _completion_app(captured))
    _enable_brain(monkeypatch)

    result = await MuseSparkProvider().chat([ChatMessage(role="user", content="hello")])

    assert result.text == "Hi there."
    assert result.usage["cost"] == pytest.approx(0.000002)
    payload = captured[0]
    assert payload["model"] == "meta/muse-spark-1.3-contributor"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["usage"] == {"include": True}
    assert "thinking" not in payload
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


async def test_brain_chat_with_tools_parses_tool_calls(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        await request.json()
        return {
            "id": "brain-tools",
            "model": "meta/muse-spark-1.3-contributor",
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
    _enable_brain(monkeypatch)
    tool = ToolSpec(
        name="get_weather",
        description="Weather",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    result = await MuseSparkProvider().chat_with_tools(
        [ChatMessage(role="user", content="weather in Paris")], [tool]
    )
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "Paris"}


async def test_brain_stream_parses_deltas_and_tools(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> object:
        await request.json()
        from fastapi.responses import StreamingResponse

        async def gen():
            for piece in ("Hel", "lo"):
                yield "data: " + json.dumps(
                    {"model": "meta/muse-spark-1.3-contributor", "choices": [{"delta": {"content": piece}}]}
                ) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "model": "meta/muse-spark-1.3-contributor",
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
    _enable_brain(monkeypatch)
    chunks = [
        chunk
        async for chunk in MuseSparkProvider().stream_chat(
            [ChatMessage(role="user", content="status")]
        )
    ]
    text = "".join(chunk.text for chunk in chunks)
    assert text == "Hello"
    done = [chunk for chunk in chunks if chunk.done]
    assert done and done[0].tool_calls[0].name == "goal_status"
    assert done[0].usage["cost"] == pytest.approx(0.000001)


async def test_brain_structured_returns_json_text(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider

    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _completion_app(captured, content='{"route": "life", "operation": "send"}'),
    )
    _enable_brain(monkeypatch)
    result = await MuseSparkProvider().chat_structured(
        [ChatMessage(role="user", content="text mom")],
        schema={"type": "object", "properties": {"route": {"type": "string"}}},
        schema_name="turn_intent",
    )
    assert json.loads(result.text) == {"route": "life", "operation": "send"}
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[0]["response_format"]["json_schema"]["name"] == "turn_intent"


async def test_brain_egress_gate_and_missing_key(monkeypatch) -> None:
    from app.gateway.openrouter_muse import (
        BrainEgressDenied,
        BrainUnavailable,
        MuseSparkProvider,
    )

    _enable_brain(monkeypatch)
    monkeypatch.delenv("EV_ALLOW_REMOTE_CHAT", raising=False)
    with pytest.raises(BrainEgressDenied):
        await MuseSparkProvider().chat([ChatMessage(role="user", content="hi")])

    _enable_brain(monkeypatch)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(BrainUnavailable, match="EV_OPENROUTER_API_KEY"):
        await MuseSparkProvider().chat([ChatMessage(role="user", content="hi")])


def test_brain_mode_roles_and_single_brain(monkeypatch) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider
    from app.gateway.providers import get_chat_provider
    from app.gateway.roles import (
        resolve_code_brain,
        resolve_text_brain,
        resolve_voice_mouth,
        text_brain_active,
    )
    from app.gateway.routing import routing_candidates, select_provider

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "brain_model", "meta/muse-spark-1.3-contributor")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "chat_provider", "muse")

    assert text_brain_active() is True
    text = resolve_text_brain()
    assert (text.provider, text.model) == ("muse", "meta/muse-spark-1.3-contributor")
    code = resolve_code_brain()
    assert code.provider == "muse"
    assert resolve_voice_mouth().model == settings.gemini_live_model
    assert isinstance(get_chat_provider(), MuseSparkProvider)
    assert routing_candidates() == ["muse"]
    selected = select_provider(configured="muse", evidence=None)
    assert selected.provider == "muse"
    assert selected.reason == "single_brain_configured"


async def test_chat_structured_via_role_uses_brain(monkeypatch) -> None:
    from app.contracts import ChatResult
    from app.gateway import roles

    captured: dict = {}

    class _Stub:
        name = "muse"

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
        model="meta/muse-spark-1.3-contributor",
    )
    assert result.text == '{"ok": true}'
    assert captured["schema_name"] == "desk_act"
    assert captured["model"] == "meta/muse-spark-1.3-contributor"


async def test_brain_code_loop_executes_tool_calls(monkeypatch) -> None:
    from app.ev import luna_code

    class _StubBrain:
        name = "muse"

        def __init__(self) -> None:
            self.calls = 0

        async def complete_raw(self, messages, *, tools=None, model=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "run_command",
                                            "arguments": '{"argv": ["python3", "-m", "pytest"]}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Tests pass."}}
                ]
            }

    stub = _StubBrain()
    monkeypatch.setattr("app.gateway.roles.require_code_provider", lambda: stub)
    executed: list[str] = []

    def fake_execute(name, args):
        executed.append(name)
        return {"ok": True, "argv": ["python3", "-m", "pytest"], "exit_code": 0, "stdout": "3 passed"}

    monkeypatch.setattr(luna_code, "execute_code_tool", fake_execute)
    monkeypatch.setattr(luna_code, "list_projects", lambda: [])

    result = await luna_code._brain_code_loop(
        "run the tests", model="meta/muse-spark-1.3-contributor", budget_s=10, live=False
    )
    assert executed == ["run_command"]
    assert stub.calls == 2
    assert result["spoken"] == "Tests pass."
    assert result["brain"] == "meta/muse-spark-1.3-contributor"


async def test_mimo_kernel_turn_uses_brain_provider(monkeypatch, tmp_path) -> None:
    from app.cognitive import kernel, telemetry
    from app.cognitive.session_store import reset_for_tests
    from app.contracts import ChatResult
    from app.gateway import roles

    class _Scripted:
        name = "muse"

        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.calls += 1
            return ChatResult(text="Doing well — glad you're here.")

    scripted = _Scripted()
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "chat_provider", "muse")
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
        monkeypatch.setattr(settings, "cognitive_mode", "legacy_gemini")
    assert result.kind == "brain"
    assert "glad" in result.spoken.lower()
    assert scripted.calls == 1
    assert snapshot["brain_turns"] >= 1


# --- two-model guarantees -------------------------------------------------


def test_default_brain_model_is_the_verified_openrouter_id() -> None:
    """The shipped default is the id read from OpenRouter's /api/v1/models."""

    from app.config import Settings
    from app.gateway.openrouter_muse import default_brain_model

    assert Settings.model_fields["brain_model"].default == "meta/muse-spark-1.3-contributor"
    assert default_brain_model() == settings.brain_model


def test_default_brain_model_follows_the_setting(monkeypatch) -> None:
    from app.gateway.openrouter_muse import MuseSparkProvider, default_brain_model

    monkeypatch.setattr(settings, "brain_model", "meta/some-other-listed-id")
    assert default_brain_model() == "meta/some-other-listed-id"
    assert MuseSparkProvider().default_model == "meta/some-other-listed-id"


def test_registry_has_only_the_offline_doubles_and_the_brain() -> None:
    from app.gateway.providers import PROVIDER_REGISTRY

    assert set(PROVIDER_REGISTRY) == {"echo", "mock", "muse"}


@pytest.mark.parametrize(
    "retired",
    ["deepseek", "mimo", "xai", "openai", "openrouter", "deepseek-direct", "local"],
)
def test_retired_brains_are_refused_not_remapped(monkeypatch, retired) -> None:
    from app.gateway.providers import UnknownProviderError, get_chat_provider

    monkeypatch.setattr(settings, "chat_provider", retired)
    with pytest.raises(UnknownProviderError, match="EV_CHAT_PROVIDER"):
        get_chat_provider()


async def test_brain_rejects_audio_input_before_any_request(monkeypatch) -> None:
    """The listing declares text/image/video/file input; audio must fail loudly."""

    from app.contracts import MediaPart
    from app.gateway.openrouter_muse import BrainUnavailable, MuseSparkProvider

    captured: list[dict] = []
    _patch_http(monkeypatch, _completion_app(captured))
    _enable_brain(monkeypatch)
    message = ChatMessage(
        role="user",
        content="what is this",
        media=[MediaPart(kind="audio", content_type="audio/wav", data_url="data:audio/wav;base64,AAAA")],
    )
    with pytest.raises(BrainUnavailable, match="audio"):
        await MuseSparkProvider().chat([message])
    assert captured == []


async def test_brain_accepts_image_input(db_session, monkeypatch) -> None:
    from app.contracts import MediaPart
    from app.gateway.openrouter_muse import MuseSparkProvider

    captured: list[dict] = []
    _patch_http(monkeypatch, _completion_app(captured))
    _enable_brain(monkeypatch)
    message = ChatMessage(
        role="user",
        content="what is this",
        media=[MediaPart(kind="image", content_type="image/png", data_url="data:image/png;base64,AAAA")],
    )
    await MuseSparkProvider().chat([message])
    parts = captured[0]["messages"][0]["content"]
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}} in parts


def test_roles_resolve_the_brain_and_report_availability(monkeypatch) -> None:
    from app.gateway.roles import (
        resolve_code_brain,
        resolve_text_brain,
        text_brain_active,
        text_role_available,
    )

    monkeypatch.setattr(settings, "chat_provider", "muse")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")

    assert text_brain_active() is True
    text = resolve_text_brain()
    assert (text.provider, text.model) == ("muse", settings.brain_model)
    code = resolve_code_brain()
    assert code.provider == "muse" and code.model == text.model
    assert text_role_available() is True

    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert text_role_available() is False


def test_offline_doubles_never_report_the_brain_available(monkeypatch) -> None:
    from app.gateway.roles import resolve_text_brain, text_role_available

    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "chat_provider", "mock")
    assert resolve_text_brain().provider == "mock"
    assert text_role_available() is False


def test_agent_brain_auto_pick(monkeypatch) -> None:
    from app.agent.brain import AUTO_ORDER, brain_status, build_agent_provider, resolve_agent_brain
    from app.gateway.openrouter_muse import MuseSparkProvider
    from app.gateway.providers import EchoProvider

    assert AUTO_ORDER == ("muse",)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")

    choice = resolve_agent_brain()
    assert (choice.provider, choice.model, choice.available) == ("muse", settings.brain_model, True)
    assert isinstance(build_agent_provider(choice), MuseSparkProvider)

    # No key: auto falls through to the offline double and says so.
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    echo = resolve_agent_brain()
    assert (echo.provider, echo.degraded) == ("echo", True)
    assert isinstance(build_agent_provider(echo), EchoProvider)
    assert brain_status()["auto_pick"]["provider"] == "echo"
    assert set(brain_status()["candidates"]) == {"muse"}


def test_agent_brain_explicit_muse_without_key_is_unavailable_not_swapped(monkeypatch) -> None:
    from app.agent.brain import resolve_agent_brain

    monkeypatch.setattr(settings, "openrouter_api_key", None)
    choice = resolve_agent_brain("muse")
    assert (choice.provider, choice.available) == ("muse", False)


@pytest.mark.parametrize("retired", ["deepseek", "mimo", "xai", "openai", "openrouter", "local"])
def test_agent_brain_refuses_retired_names(retired) -> None:
    from app.agent.brain import resolve_agent_brain

    with pytest.raises(ValueError, match="unknown agent brain"):
        resolve_agent_brain(retired)


def _sidecar_namespace():
    import runpy
    from pathlib import Path

    return runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py")
    )


def test_sidecar_selects_the_brain_slot(monkeypatch) -> None:
    namespace = _sidecar_namespace()

    monkeypatch.setenv("EV_CHAT_PROVIDER", "muse")
    assert namespace["brain_selected"]() is True

    monkeypatch.setenv("EV_CHAT_PROVIDER", "echo")
    assert namespace["brain_selected"]() is False


def test_sidecar_refuses_brain_without_openrouter_key(monkeypatch) -> None:
    namespace = _sidecar_namespace()

    monkeypatch.setenv("EV_VAULT_KEY", "test-vault-key-long-enough")
    monkeypatch.setenv("EV_VOICE_LIVE_BRAIN", "")
    monkeypatch.setenv("EV_CHAT_PROVIDER", "muse")
    monkeypatch.delenv("EV_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        namespace["refuse_brain_without_key"]()
    assert exc.value.code == 2

    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "or-key")
    namespace["refuse_brain_without_key"]()


@pytest.mark.parametrize("retired", ["deepseek", "mimo", "xai", "openai", "openrouter"])
def test_sidecar_refuses_a_retired_brain_by_name(monkeypatch, capsys, retired) -> None:
    namespace = _sidecar_namespace()

    monkeypatch.setenv("EV_VAULT_KEY", "test-vault-key-long-enough")
    monkeypatch.setenv("EV_VOICE_LIVE_BRAIN", "")
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("EV_AGENT_PROVIDER", raising=False)
    monkeypatch.setenv("EV_CHAT_PROVIDER", retired)
    with pytest.raises(SystemExit) as exc:
        namespace["refuse_brain_without_key"]()
    assert exc.value.code == 2
    assert f"EV_CHAT_PROVIDER={retired}" in capsys.readouterr().err


def test_sidecar_forces_the_brain_by_default() -> None:
    """main() execs uvicorn, so the forcing line itself is pinned by source.

    This is intentionally a source assertion: importing main() would replace
    the test process. The behavioral halves (selection + key gate) are
    covered by the tests above. The model id is deliberately not set here: it
    is the brain_model setting, so the sidecar can never drift from the API.
    """

    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py").read_text()
    assert 'os.environ.setdefault("EV_CHAT_PROVIDER", "muse")' in source
    assert "muse-spark" not in source.split("def main")[-1]
