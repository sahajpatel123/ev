"""DeepSeek V4.1 Flash main brain: provider, registry, auto-pick, sidecar (offline, hermetic)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request

from app.config import settings
from app.contracts import ChatMessage, ToolSpec

FLASH_MODEL = "deepseek/deepseek-v4.1-flash"


def _is_config_field(name: str) -> bool:
    return name in type(settings).model_fields


def _set_setting(monkeypatch, name: str, value) -> None:
    """Set a Settings field that may not exist until the config patch lands."""

    if _is_config_field(name):
        monkeypatch.setattr(settings, name, value)
    else:
        monkeypatch.setitem(settings.__dict__, name, value)


def _drop_setting(monkeypatch, name: str) -> None:
    """Remove a Settings override (or pre-patch extra) for one test."""

    if _is_config_field(name):
        monkeypatch.delattr(settings, name, raising=False)
    else:
        monkeypatch.delitem(settings.__dict__, name, raising=False)


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


def _enable_deepseek(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "http://local/v1")
    monkeypatch.setattr(settings, "openrouter_model", FLASH_MODEL, raising=False)
    # New settings until the shared config patch lands; the helpers keep
    # these green both before and after it does.
    _drop_setting(monkeypatch, "deepseek_flash_model")
    _set_setting(monkeypatch, "deepseek_reasoning_effort", "high")
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_gemini")
    monkeypatch.setenv("EV_ALLOW_REMOTE_CHAT", "true")


def _completion_app(captured: list[dict], *, content: str = "Hi there.") -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        captured.append(await request.json())
        return {
            "id": "deepseek-test",
            "model": FLASH_MODEL,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "cost": 0.000002,
            },
        }

    return app


async def test_deepseek_chat_payload_has_model_reasoning_usage(
    db_session, monkeypatch
) -> None:
    from app.gateway.openrouter_deepseek import DeepSeekFlashProvider

    captured: list[dict] = []
    _patch_http(monkeypatch, _completion_app(captured))
    _enable_deepseek(monkeypatch)

    result = await DeepSeekFlashProvider().chat([ChatMessage(role="user", content="hello")])

    assert result.text == "Hi there."
    assert result.usage["cost"] == pytest.approx(0.000002)
    payload = captured[0]
    assert payload["model"] == FLASH_MODEL
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["usage"] == {"include": True}
    assert "thinking" not in payload
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


def test_default_flash_model_prefers_dedicated_setting(monkeypatch) -> None:
    from app.gateway.openrouter_deepseek import default_flash_model

    _set_setting(monkeypatch, "deepseek_flash_model", "custom/flash")
    monkeypatch.setattr(settings, "openrouter_model", FLASH_MODEL, raising=False)
    assert default_flash_model() == "custom/flash"

    _drop_setting(monkeypatch, "deepseek_flash_model")
    assert default_flash_model() == FLASH_MODEL

    monkeypatch.setattr(settings, "openrouter_model", "", raising=False)
    assert default_flash_model() == FLASH_MODEL


async def test_deepseek_chat_with_tools_parses_tool_calls(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_deepseek import DeepSeekFlashProvider

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        await request.json()
        return {
            "id": "deepseek-tools",
            "model": FLASH_MODEL,
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
    _enable_deepseek(monkeypatch)
    tool = ToolSpec(
        name="get_weather",
        description="Weather",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    result = await DeepSeekFlashProvider().chat_with_tools(
        [ChatMessage(role="user", content="weather in Paris")], [tool]
    )
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "Paris"}


async def test_deepseek_structured_returns_json_text(db_session, monkeypatch) -> None:
    from app.gateway.openrouter_deepseek import _STRUCTURED_BREAKER, DeepSeekFlashProvider

    _STRUCTURED_BREAKER.reset()
    captured: list[dict] = []
    _patch_http(
        monkeypatch,
        _completion_app(captured, content='{"route": "life", "operation": "send"}'),
    )
    _enable_deepseek(monkeypatch)
    result = await DeepSeekFlashProvider().chat_structured(
        [ChatMessage(role="user", content="text mom")],
        schema={"type": "object", "properties": {"route": {"type": "string"}}},
        schema_name="turn_intent",
    )
    assert json.loads(result.text) == {"route": "life", "operation": "send"}
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[0]["response_format"]["json_schema"]["name"] == "turn_intent"


async def test_deepseek_egress_gate_and_missing_key(monkeypatch) -> None:
    from app.gateway.openrouter_deepseek import (
        DeepSeekEgressDenied,
        DeepSeekFlashProvider,
        DeepSeekUnavailable,
    )

    _enable_deepseek(monkeypatch)
    monkeypatch.delenv("EV_ALLOW_REMOTE_CHAT", raising=False)
    with pytest.raises(DeepSeekEgressDenied):
        await DeepSeekFlashProvider().chat([ChatMessage(role="user", content="hi")])

    _enable_deepseek(monkeypatch)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(DeepSeekUnavailable, match="EV_OPENROUTER_API_KEY"):
        await DeepSeekFlashProvider().chat([ChatMessage(role="user", content="hi")])


def test_deepseek_errors_subclass_mimo_errors_for_catch_sites() -> None:
    from app.gateway.openrouter_deepseek import DeepSeekEgressDenied, DeepSeekUnavailable
    from app.gateway.openrouter_mimo import MimoEgressDenied, MimoUnavailable

    assert issubclass(DeepSeekUnavailable, MimoUnavailable)
    assert issubclass(DeepSeekEgressDenied, MimoEgressDenied)


def test_registry_resolves_deepseek_to_flash(monkeypatch) -> None:
    from app.gateway.cloud_providers import DeepSeekProvider
    from app.gateway.openrouter_deepseek import DeepSeekFlashProvider
    from app.gateway.openrouter_mimo import MimoProvider
    from app.gateway.providers import PROVIDER_REGISTRY, get_chat_provider

    assert {"deepseek", "deepseek-direct", "mimo"} <= set(PROVIDER_REGISTRY)

    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    provider = get_chat_provider()
    assert isinstance(provider, DeepSeekFlashProvider)
    assert provider.default_model == FLASH_MODEL

    monkeypatch.setattr(settings, "chat_provider", "deepseek-direct")
    assert isinstance(get_chat_provider(), DeepSeekProvider)

    monkeypatch.setattr(settings, "chat_provider", "mimo")
    assert isinstance(get_chat_provider(), MimoProvider)


def test_roles_resolve_deepseek_main_brain(monkeypatch) -> None:
    from app.gateway.roles import (
        resolve_code_brain,
        resolve_text_brain,
        text_brain_active,
        text_role_available,
    )

    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.delattr(settings, "deepseek_flash_model", raising=False)

    assert text_brain_active() is True
    text = resolve_text_brain()
    assert (text.provider, text.model) == ("deepseek", FLASH_MODEL)
    code = resolve_code_brain()
    assert code.provider == "deepseek" and code.model == text.model
    assert text_role_available() is True

    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert text_role_available() is False

    # Explicit MiMo fallback keeps its identity.
    monkeypatch.setattr(settings, "chat_provider", "mimo")
    assert resolve_text_brain().provider == "mimo"


def test_brain_auto_pick_prefers_deepseek(monkeypatch) -> None:
    from app.agent.brain import (
        AUTO_ORDER,
        brain_status,
        build_agent_provider,
        resolve_agent_brain,
    )
    from app.gateway.openrouter_deepseek import DeepSeekFlashProvider

    assert AUTO_ORDER[0] == "deepseek"
    assert "deepseek-direct" in AUTO_ORDER

    monkeypatch.setattr(settings, "agent_provider", "auto")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key", raising=False)
    monkeypatch.setattr(settings, "openrouter_model", FLASH_MODEL, raising=False)
    monkeypatch.setattr(settings, "xai_api_key", "xai-key", raising=False)
    monkeypatch.setattr(settings, "deepseek_api_key", "ds-key", raising=False)
    monkeypatch.delattr(settings, "deepseek_flash_model", raising=False)

    choice = resolve_agent_brain()
    assert choice.provider == "deepseek"
    assert choice.model == FLASH_MODEL
    assert choice.available is True
    assert isinstance(build_agent_provider(choice), DeepSeekFlashProvider)

    # Without the OpenRouter key the remaining candidates still fall through.
    monkeypatch.setattr(settings, "openrouter_api_key", None, raising=False)
    assert resolve_agent_brain().provider == "xai"

    status = brain_status()
    assert status["auto_pick"]["provider"] == "xai"
    assert "deepseek-direct" in status["candidates"]


def _sidecar_namespace():
    import runpy
    from pathlib import Path

    return runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py")
    )


def test_sidecar_selects_deepseek_brain_slot(monkeypatch) -> None:
    namespace = _sidecar_namespace()

    monkeypatch.setenv("EV_CHAT_PROVIDER", "deepseek")
    assert namespace["deepseek_selected"]() is True
    assert namespace["mimo_selected"]() is False

    monkeypatch.setenv("EV_CHAT_PROVIDER", "mimo")
    assert namespace["deepseek_selected"]() is False
    assert namespace["mimo_selected"]() is True


def test_sidecar_refuses_deepseek_without_openrouter_key(monkeypatch) -> None:
    namespace = _sidecar_namespace()

    monkeypatch.setenv("EV_VAULT_KEY", "test-vault-key-long-enough")
    monkeypatch.setenv("EV_VOICE_LIVE_BRAIN", "")
    monkeypatch.setenv("EV_CHAT_PROVIDER", "deepseek")
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        namespace["refuse_brain_without_key"]()
    assert exc.value.code == 2

    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "or-key")
    namespace["refuse_brain_without_key"]()


def test_sidecar_forces_deepseek_brain_by_default() -> None:
    """main() execs uvicorn, so the forcing line itself is pinned by source.

    This is intentionally a source assertion: importing main() would replace
    the test process. The behavioral halves (selection + key gate) are
    covered by the tests above.
    """

    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py").read_text()
    assert 'os.environ.setdefault("EV_CHAT_PROVIDER", "deepseek")' in source
    assert (
        'os.environ.setdefault("EV_DEEPSEEK_FLASH_MODEL", "deepseek/deepseek-v4.1-flash")'
        in source
    )
