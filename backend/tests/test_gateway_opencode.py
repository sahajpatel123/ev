"""opencode provider: session lifecycle, cost mapping, honest degradation.

Every unit test here runs offline against a mocked opencode server (FLEET_LAW
§7). The single live test skips — never fails — when no `opencode serve` is
listening.

The mock is an ``httpx.MockTransport`` rather than an ASGI app because the
provider holds an SSE stream open while POSTing the prompt, and
``httpx.ASGITransport`` cannot serve a second request while a streaming
response is being consumed.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.config import settings
from app.contracts import ChatMessage, ToolSpec
from app.gateway.opencode import (
    OpenCodeProvider,
    OpenCodeUnavailableError,
    api_key_status,
)
from app.gateway.providers import PROVIDER_REGISTRY, get_chat_provider
from app.gateway.reliability import CIRCUIT_BREAKERS
from app.gateway.service import ModelGateway

BASE_URL = "http://opencode.test"

TOKENS = {
    "total": 260,
    "input": 90,
    "output": 20,
    "reasoning": 22,
    "cache": {"read": 128, "write": 0},
}
COST = 1.23592e-05


class FakeOpenCode:
    """Stand-in for the opencode routes the provider actually uses."""

    def __init__(
        self,
        *,
        text: str = "EV_OPENCODE_OK",
        healthy: bool = True,
        stream_deltas: tuple[str, ...] = ("Hello", " from", " opencode"),
        report_stream_usage: bool = True,
    ) -> None:
        self.text = text
        self.healthy = healthy
        self.stream_deltas = stream_deltas
        self.report_stream_usage = report_stream_usage
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.bodies: list[dict] = []
        self.sessions: list[str] = []
        self._events: asyncio.Queue[dict] | None = None

    # ------------------------------------------------------------------ events

    @property
    def events(self) -> asyncio.Queue[dict]:
        if self._events is None:
            self._events = asyncio.Queue()
        return self._events

    async def _sse(self) -> AsyncIterator[bytes]:
        yield b'data: {"type":"server.connected","properties":{}}\n\n'
        while True:
            event = await self.events.get()
            yield f"data: {json.dumps(event)}\n\n".encode()
            if event.get("type") == "session.idle":
                return

    def _script_stream(self, session_id: str) -> None:
        def put(event: dict) -> None:
            self.events.put_nowait(event)

        # A reasoning part streams first; its deltas must never reach the user.
        put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_reason", "sessionID": session_id,
                         "type": "reasoning", "text": ""},
            },
        })
        put({
            "type": "message.part.delta",
            "properties": {"sessionID": session_id, "partID": "prt_reason",
                           "field": "text", "delta": "internal deliberation"},
        })
        put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_text", "sessionID": session_id,
                         "type": "text", "text": ""},
            },
        })
        for delta in self.stream_deltas:
            put({
                "type": "message.part.delta",
                "properties": {"sessionID": session_id, "partID": "prt_text",
                               "field": "text", "delta": delta},
            })
        if self.report_stream_usage:
            put({
                "type": "message.updated",
                "properties": {
                    "sessionID": session_id,
                    "info": {"role": "assistant", "tokens": TOKENS, "cost": COST,
                             "modelID": "deepseek-v4-flash"},
                },
            })
        put({"type": "session.idle", "properties": {"sessionID": session_id}})

    # ----------------------------------------------------------------- routing

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/global/health":
            return httpx.Response(200, json={"healthy": self.healthy, "version": "1.18.12"})
        if method == "GET" and path == "/event":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=self._sse(),
            )
        if method == "GET" and path == "/session":
            return httpx.Response(200, json=[{"id": s, "title": "ev"} for s in self.sessions])
        if method == "POST" and path == "/session":
            body = json.loads(request.content)
            self.created.append(body)
            session_id = f"ses_fake{len(self.created)}"
            self.sessions.append(session_id)
            return httpx.Response(200, json={"id": session_id})
        if method == "DELETE" and path.startswith("/session/"):
            session_id = path.split("/")[2]
            self.deleted.append(session_id)
            if session_id in self.sessions:
                self.sessions.remove(session_id)
            return httpx.Response(200, json=True)
        if method == "POST" and path.endswith("/message"):
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "info": {"tokens": TOKENS, "cost": COST, "modelID": "deepseek-v4-flash",
                         "providerID": "opencode-go", "role": "assistant"},
                "parts": [
                    {"type": "step-start"},
                    {"type": "reasoning", "text": "internal deliberation"},
                    {"type": "text", "text": self.text},
                    {"type": "step-finish"},
                ],
            })
        if method == "POST" and path.endswith("/prompt_async"):
            self.bodies.append(json.loads(request.content))
            self._script_stream(path.split("/")[2])
            return httpx.Response(200, content=b"")
        return httpx.Response(404, json={"name": "NotFound", "path": path})  # pragma: no cover


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    CIRCUIT_BREAKERS.reset("opencode")


@pytest.fixture
def visible_key(monkeypatch, tmp_path) -> None:
    """A credential EV can see, without depending on the developer's machine."""

    monkeypatch.setattr(settings, "opencode_api_key", None)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test-key")
    monkeypatch.setattr(settings, "opencode_env_file", str(tmp_path / "absent.env"))


def _patch_http(monkeypatch, fake: FakeOpenCode) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(fake.handler), **kwargs),
    )


def _provider(**kwargs) -> OpenCodeProvider:
    defaults: dict[str, Any] = {
        "base_url": BASE_URL,
        "provider_id": "opencode-go",
        "model": "deepseek-v4-flash",
        "agent": "ev-minimal",
        "session_reuse": False,
        "tool_emulation": False,
    }
    defaults.update(kwargs)
    return OpenCodeProvider(**defaults)


async def test_chat_creates_ephemeral_session_and_extracts_text(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    result = await _provider().chat([ChatMessage(role="user", content="say EV_OPENCODE_OK")])

    assert result.text == "EV_OPENCODE_OK"  # reasoning parts are not user text
    assert result.model == "deepseek-v4-flash"
    assert fake.created == [
        {"title": settings.opencode_session_title,
         "model": {"providerID": "opencode-go", "id": "deepseek-v4-flash"}}
    ]
    assert fake.deleted == ["ses_fake1"], "ephemeral session must be disposed"
    assert fake.sessions == [], "no session may survive the request"
    body = fake.bodies[0]
    assert body["agent"] == "ev-minimal"
    assert body["tools"] == {}
    assert body["parts"] == [{"type": "text", "text": "say EV_OPENCODE_OK"}]
    assert "format" not in body


async def test_system_prompt_and_transcript_flattening(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    await _provider().chat(
        [
            ChatMessage(role="system", content="You are EV."),
            ChatMessage(role="user", content="who am I"),
            ChatMessage(role="assistant", content="You are the owner."),
            ChatMessage(role="tool", content='{"count": 0}', name="search_memory"),
            ChatMessage(role="user", content="and now"),
        ]
    )
    body = fake.bodies[0]
    assert body["system"] == "You are EV."
    text = body["parts"][0]["text"]
    assert text.startswith("User: who am I")
    assert 'Tool result (search_memory): {"count": 0}' in text
    assert text.endswith("User: and now")


async def test_cost_and_tokens_come_from_opencode(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    result = await _provider().chat([ChatMessage(role="user", content="hi")])

    # prompt = input + cached read; completion = output + reasoning.
    assert result.usage["prompt_tokens"] == 218
    assert result.usage["completion_tokens"] == 42
    assert result.usage["total_tokens"] == 260
    assert result.usage["cached_prompt_tokens"] == 128
    assert result.usage["cost_usd"] == pytest.approx(COST)
    assert result.usage["cost_source"] == "opencode_reported"


async def test_temperature_is_reported_not_silently_dropped(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    result = await _provider().chat([ChatMessage(role="user", content="hi")], temperature=0.1)
    assert result.usage["temperature_requested"] == 0.1
    assert result.usage["temperature_applied"] == settings.opencode_agent_temperature


async def test_session_reuse_keeps_one_session(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    provider = _provider(session_reuse=True)
    await provider.chat([ChatMessage(role="user", content="one")])
    await provider.chat([ChatMessage(role="user", content="two")])
    assert len(fake.created) == 1
    assert fake.deleted == []


async def test_tools_without_emulation_degrade_loudly(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    spec = ToolSpec(name="search_memory", description="search", parameters={})
    result = await _provider().chat_with_tools([ChatMessage(role="user", content="hi")], [spec])

    assert result.text == "EV_OPENCODE_OK"
    assert result.tool_calls == []
    assert result.usage["degraded"] is True
    assert result.usage["degradation"]["kind"] == "tools_unsupported"
    assert "format" not in fake.bodies[0], "no structured output unless emulation is on"


async def test_tool_emulation_parses_and_normalises_calls(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode(
        text=json.dumps({"tool_calls": [{"name": "search_memory", "query": "gym"}]})
    )
    _patch_http(monkeypatch, fake)
    spec = ToolSpec(
        name="search_memory",
        description="search",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    result = await _provider(tool_emulation=True).chat_with_tools(
        [ChatMessage(role="user", content="gym decision?")], [spec]
    )

    assert [c.name for c in result.tool_calls] == ["search_memory"]
    assert result.tool_calls[0].arguments == {"query": "gym"}
    assert "normalised" in result.usage["tool_emulation_problem"]
    assert fake.bodies[0]["format"]["type"] == "json_schema"
    assert "TOOL PROTOCOL" in fake.bodies[0]["system"]


async def test_tool_emulation_recovers_envelope_wrapped_in_reasoning(
    monkeypatch, visible_key
) -> None:
    """Observed live failure: deepseek-v4-flash prefixes an <analysis> block.

    Without brace scanning the whole envelope leaked to the user as the reply.
    """

    envelope = json.dumps(
        {"reply": "", "tool_calls": [{"name": "search_memory", "arguments": {"query": "gym {x}"}}]}
    )
    fake = FakeOpenCode(text=f"<analysis>\nI should search memory.\n</analysis>\n\n{envelope}\n")
    _patch_http(monkeypatch, fake)
    spec = ToolSpec(name="search_memory", description="search", parameters={})
    result = await _provider(tool_emulation=True).chat_with_tools(
        [ChatMessage(role="user", content="gym decision?")], [spec]
    )

    assert [c.name for c in result.tool_calls] == ["search_memory"]
    assert result.tool_calls[0].arguments == {"query": "gym {x}"}
    assert result.text == ""
    assert "recovered by brace scan" in result.usage["tool_emulation_problem"]
    assert "degraded" not in result.usage


async def test_tool_emulation_unparsable_envelope_marks_degraded(
    monkeypatch, visible_key
) -> None:
    fake = FakeOpenCode(text="I cannot search memory right now.")
    _patch_http(monkeypatch, fake)
    spec = ToolSpec(name="search_memory", description="search", parameters={})
    result = await _provider(tool_emulation=True).chat_with_tools(
        [ChatMessage(role="user", content="gym decision?")], [spec]
    )

    assert result.tool_calls == []
    assert result.text == "I cannot search memory right now."
    assert result.usage["degraded"] is True
    assert result.usage["degradation"]["kind"] == "tool_emulation_unparsed"


async def test_tool_emulation_result_still_passes_gateway_validation(
    monkeypatch, visible_key
) -> None:
    """The provider never dispatches: the gateway validates every emulated call."""

    fake = FakeOpenCode(
        text=json.dumps({"reply": "", "tool_calls": [{"name": "not_a_tool", "arguments": {}}]})
    )
    _patch_http(monkeypatch, fake)
    spec = ToolSpec(name="search_memory", description="search", parameters={})
    gateway = ModelGateway(_provider(tool_emulation=True))
    call = await gateway.chat([ChatMessage(role="user", content="hi")], tools=[spec])

    assert call.status == "ok"
    assert [v.status for v in call.tool_validation] == ["rejected"]
    assert "unknown tool 'not_a_tool'" in call.tool_validation[0].issues[0]


async def test_unreachable_server_fails_closed_with_command(monkeypatch, visible_key) -> None:
    real = httpx.AsyncClient

    def _blackhole(**kwargs):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _blackhole)
    with pytest.raises(OpenCodeUnavailableError) as exc:
        await _provider().chat([ChatMessage(role="user", content="hi")])
    message = str(exc.value)
    assert "opencode serve --hostname 127.0.0.1 --port 4096" in message
    assert "launchctl kickstart -k gui/$UID/ev.opencode" in message


async def test_unhealthy_server_fails_closed(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode(healthy=False)
    _patch_http(monkeypatch, fake)
    with pytest.raises(OpenCodeUnavailableError) as exc:
        await _provider().chat([ChatMessage(role="user", content="hi")])
    assert "unhealthy" in str(exc.value)
    assert fake.created == []


async def test_missing_api_key_fails_closed(monkeypatch, tmp_path) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    monkeypatch.setattr(settings, "opencode_api_key", None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(settings, "opencode_env_file", str(tmp_path / "absent.env"))
    assert api_key_status() == (False, "not found")

    with pytest.raises(OpenCodeUnavailableError) as exc:
        await _provider().chat([ChatMessage(role="user", content="hi")])
    assert "OPENCODE_API_KEY" in str(exc.value)
    assert fake.created == [], "no session may be created without a credential"


async def test_api_key_env_file_is_a_valid_source(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "opencode.env"
    env_file.write_text("# comment\nOPENCODE_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setattr(settings, "opencode_api_key", None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(settings, "opencode_env_file", str(env_file))
    present, source = api_key_status()
    assert present is True
    assert source == str(env_file)


async def test_stream_chat_yields_incremental_text_then_usage(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    chunks = [
        chunk
        async for chunk in _provider().stream_chat([ChatMessage(role="user", content="hi")])
    ]

    deltas = [c.text for c in chunks if c.text]
    assert deltas == ["Hello", " from", " opencode"], "reasoning deltas must not leak"
    terminal = chunks[-1]
    assert terminal.done is True
    assert terminal.usage["cost_usd"] == pytest.approx(COST)
    assert terminal.usage["prompt_tokens"] == 218
    assert fake.deleted == ["ses_fake1"], "streamed sessions are disposed too"


async def test_stream_without_reported_usage_is_marked_degraded(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode(report_stream_usage=False)
    _patch_http(monkeypatch, fake)
    chunks = [
        chunk
        async for chunk in _provider().stream_chat([ChatMessage(role="user", content="hi")])
    ]
    terminal = chunks[-1]
    assert terminal.done is True
    assert terminal.usage["degraded"] is True
    assert terminal.usage["degradation"]["kind"] == "usage_missing"


async def test_gateway_stream_aggregates_provider_deltas(monkeypatch, visible_key) -> None:
    fake = FakeOpenCode()
    _patch_http(monkeypatch, fake)
    gateway = ModelGateway(_provider())
    events = [
        event
        async for event in gateway.stream_chat([ChatMessage(role="user", content="hi")])
    ]
    done = events[-1]
    assert done.kind == "done"
    assert done.call is not None
    assert done.call.result.text == "Hello from opencode"
    assert done.call.first_token_ms is not None


def test_registered_in_provider_registry(monkeypatch) -> None:
    """Exercise the real factory entry point, not a re-implementation."""

    assert "opencode" in PROVIDER_REGISTRY
    monkeypatch.setattr(settings, "chat_provider", "opencode")
    provider = get_chat_provider()
    assert provider.name == "opencode"
    assert isinstance(provider, OpenCodeProvider)
    assert provider.supports_tools is False


def _server_up() -> bool:
    host = settings.opencode_base_url.split("//", 1)[-1]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname, int(port or 80)), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _server_up(), reason="no opencode server on EV_OPENCODE_BASE_URL")
async def test_live_opencode_round_trip() -> None:
    """Real call against a running server (skipped, never failed, when absent)."""

    provider = OpenCodeProvider()
    result = await provider.chat(
        [
            ChatMessage(role="system", content="Reply with exactly the token requested."),
            ChatMessage(role="user", content="Reply with EV_OPENCODE_OK and nothing else."),
        ]
    )
    assert "EV_OPENCODE_OK" in result.text
    assert result.usage["prompt_tokens"] > 0
    assert result.usage["cost_usd"] > 0
    assert result.usage["cost_source"] == "opencode_reported"
    # The ephemeral session must be gone: opencode keeps no EV conversation.
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{settings.opencode_base_url}/session")
        titles = [s.get("title") for s in response.json()]
    assert settings.opencode_session_title not in titles
