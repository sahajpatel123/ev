"""OpenCode CLI server as an EV chat provider (session API — NOT OpenAI-compatible).

The locally installed ``opencode`` binary can run headless
(``opencode serve --hostname 127.0.0.1 --port 4096``) and reach hosted models
with the owner's ``OPENCODE_API_KEY``, so EV can use e.g.
``opencode-go/deepseek-v4-flash`` without a separate DeepSeek key.

What this provider has to work around, measured against opencode 1.18.12:

* The server exposes **no** ``/v1`` routes. There is no
  ``/v1/chat/completions``. The only reasoning path is session based:
  ``POST /session`` then ``POST /session/{id}/message`` (buffered) or
  ``POST /session/{id}/prompt_async`` + ``GET /event`` (incremental).
* Every built-in agent injects a large coding preamble: measured 14,666 input
  tokens (``plan``), 9,761 (``general``), 6,706 (``build``) for a three-word
  prompt. EV ships its own minimal agent (``.opencode/agents/ev-minimal.md``,
  no tools, one-line prompt) which measures **95–223 input tokens** for the
  same prompt, so EV's own ~20k budgeted context is not inflated.
* The session API accepts no OpenAI-style function definitions. Tool calling
  is therefore **not supported by default**: ``chat_with_tools`` runs the
  no-tools path and marks the result degraded (never silently). Structured
  output emulation via the server's ``format`` field is available behind
  ``EV_OPENCODE_TOOL_EMULATION`` and still goes through the gateway's existing
  ``validate_tool_calls``.
* opencode sessions accumulate history and would corrupt EV's own conversation
  state, so the default is one ephemeral session per request, deleted in a
  ``finally``. ``EV_OPENCODE_SESSION_REUSE`` opts into a sticky session.

Cost and tokens come from opencode's own ``info`` block (real reported cost),
not from an estimate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.contracts import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.gateway.reliability import (
    CIRCUIT_BREAKERS,
    CircuitOpenError,
    ProviderStreamError,
    http_timeout,
    is_transient,
    max_attempts,
    wait_for_retry,
)
from app.gateway.streaming import ChatStreamChunk

logger = logging.getLogger("ev.gateway.opencode")

#: Exact remediation printed with every unreachable-server failure.
START_SERVER_HINT = (
    "start the opencode server: `launchctl kickstart -k gui/$UID/ev.opencode` "
    "(installed by launchd/ev.opencode.plist) or, in the foreground, "
    "`opencode serve --hostname 127.0.0.1 --port 4096`"
)

#: Exact remediation printed when no OPENCODE_API_KEY is visible.
API_KEY_HINT = (
    "OPENCODE_API_KEY is not set where EV or the opencode server can see it. "
    "launchd does not read ~/.zshrc, so put `OPENCODE_API_KEY=sk-...` in "
    "~/.config/ev/opencode.env (chmod 600) or in EV's .env, then "
    "`launchctl kickstart -k gui/$UID/ev.opencode`"
)

#: Structured-output schema used only when tool emulation is enabled.
TOOL_EMULATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "tool_calls"],
    "properties": {
        "reply": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "arguments"],
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
            },
        },
    },
}

_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
}


class OpenCodeUnavailableError(RuntimeError):
    """The opencode server cannot serve this request; the message says how to fix it."""


def api_key_status() -> tuple[bool, str]:
    """Is an ``OPENCODE_API_KEY`` visible, and from where?

    EV cannot read the credential the server process holds, so this reports
    only what this process can see: EV settings, the environment, or the
    operator env file the launchd job also sources.
    """

    if settings.opencode_api_key:
        return True, "EV_OPENCODE_API_KEY"
    if os.getenv("OPENCODE_API_KEY"):
        return True, "OPENCODE_API_KEY (environment)"
    env_file = Path(settings.opencode_env_file).expanduser()
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("OPENCODE_API_KEY=") and stripped.split("=", 1)[1]:
                return True, str(env_file)
    except OSError:
        pass
    return False, "not found"


def _system_prompt(messages: Sequence[ChatMessage]) -> str:
    """EV's own system prompt(s), joined. opencode takes these verbatim."""

    return "\n\n".join(m.content for m in messages if m.role == "system" and m.content)


def _media_note(message: ChatMessage) -> str:
    """Derived (never raw) representation of attached media.

    The session API's file parts are not wired up, so raw data URLs are never
    sent through this provider. Derived text is included when the boundary
    already produced it; anything else is named, not transmitted.
    """

    notes: list[str] = []
    for part in message.media:
        if part.text:
            notes.append(f"[{part.kind}] {part.text}")
        else:
            notes.append(f"[{part.kind} attachment not sent to this provider: {part.ref or 'unnamed'}]")
    return "\n".join(notes)


def _conversation_text(messages: Sequence[ChatMessage]) -> str:
    """Flatten EV's turn into one prompt part.

    A single user message is sent verbatim (cheapest, no framing). Multi-turn
    input is rendered as a labelled transcript because the session API has one
    prompt slot and EV — not opencode — owns conversation state.
    """

    body = [m for m in messages if m.role != "system"]
    if not body:
        return ""
    if len(body) == 1 and body[0].role == "user" and not body[0].media:
        return body[0].content
    lines: list[str] = []
    for message in body:
        label = _ROLE_LABELS.get(message.role, message.role.capitalize())
        if message.name:
            label = f"{label} ({message.name})"
        content = message.content
        note = _media_note(message)
        if note:
            content = f"{content}\n{note}" if content else note
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


def _usage_from_info(info: dict) -> dict:
    """Map opencode's reported tokens/cost into EV's usage shape.

    ``cost_usd`` is opencode's own measured number, so the audit trail and the
    monthly cap work off real spend instead of a table estimate.
    """

    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    cache_read = int(cache.get("read") or 0)
    cache_write = int(cache.get("write") or 0)
    reasoning = int(tokens.get("reasoning") or 0)
    prompt_tokens = int(tokens.get("input") or 0) + cache_read + cache_write
    completion_tokens = int(tokens.get("output") or 0) + reasoning
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(tokens.get("total") or (prompt_tokens + completion_tokens)),
        "cached_prompt_tokens": cache_read,
        "reasoning_tokens": reasoning,
    }
    cost = info.get("cost")
    if cost is not None:
        usage["cost_usd"] = float(cost)
        usage["cost_source"] = "opencode_reported"
    return usage


def _text_from_parts(parts: Sequence[dict]) -> str:
    return "".join(part.get("text") or "" for part in parts if part.get("type") == "text")


def _mark_degraded(usage: dict, *, kind: str, detail: str) -> None:
    """Record an honest degradation marker on the result's usage payload."""

    usage["degraded"] = True
    usage["degradation"] = {"kind": kind, "provider": "opencode", "detail": detail}


def _tool_protocol(tools: Sequence[ToolSpec]) -> str:
    """System-prompt block describing EV's tools for structured-output emulation."""

    declared = [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in tools
    ]
    return (
        "TOOL PROTOCOL. This provider has no native function calling, so reply "
        "ONLY with a JSON object of the form "
        '{"reply": "<prose for the user>", "tool_calls": []}. '
        "To call tools, put objects of the form "
        '{"name": "<tool>", "arguments": {<named arguments>}} in `tool_calls` '
        "and leave `reply` empty. Never invent a tool that is not listed. "
        "Emit no prose, reasoning or tags outside the JSON object. "
        "Available tools:\n" + json.dumps(declared)
    )


def _json_object_span(text: str) -> str | None:
    """Return the first balanced top-level JSON object found in ``text``.

    deepseek-v4-flash routinely wraps the envelope in reasoning prose (observed:
    a leading ``<analysis>`` block), which plain ``json.loads`` rejects. Scanning
    for a balanced object recovers the envelope without guessing at content:
    braces inside strings and escaped quotes are tracked so a ``}`` in a tool
    argument cannot end the span early.
    """

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


class EmulatedReply:
    """Outcome of parsing one structured-output envelope.

    ``parsed`` is False when no JSON envelope could be recovered at all, which
    the caller must report as a degradation: tools were offered and none could
    be read back.
    """

    __slots__ = ("calls", "parsed", "problem", "text")

    def __init__(
        self,
        *,
        text: str,
        calls: list[ToolCall],
        problem: str | None,
        parsed: bool,
    ) -> None:
        self.text = text
        self.calls = calls
        self.problem = problem
        self.parsed = parsed


def _parse_emulated_tool_calls(text: str) -> EmulatedReply:
    """Parse an emulated structured reply into text plus validated-later calls.

    Shapes the model actually produces are normalised deterministically (the
    observed failure modes are a reasoning preamble around the envelope and
    arguments inlined next to ``name``); nothing is invented, and every call
    still goes through the gateway's ``validate_tool_calls`` before dispatch.
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        _, _, stripped = stripped.partition("\n")
    wrapped = False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        candidate = _json_object_span(stripped)
        if candidate is None:
            return EmulatedReply(
                text=text,
                calls=[],
                problem=f"structured output was not JSON: {exc}",
                parsed=False,
            )
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as inner:
            return EmulatedReply(
                text=text,
                calls=[],
                problem=f"structured output was not JSON: {inner}",
                parsed=False,
            )
        wrapped = True
    if not isinstance(payload, dict):
        return EmulatedReply(
            text=text,
            calls=[],
            problem="structured output was not a JSON object",
            parsed=False,
        )
    reply = payload.get("reply")
    raw_calls = payload.get("tool_calls")
    if raw_calls is None and "name" in payload:
        raw_calls = [payload]
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        return EmulatedReply(
            text=reply if isinstance(reply, str) else text,
            calls=[],
            problem="tool_calls was not a list",
            parsed=False,
        )
    calls: list[ToolCall] = []
    problems: list[str] = []
    if wrapped:
        problems.append("envelope was wrapped in prose and recovered by brace scan")
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            problems.append(f"tool_calls[{index}] was not an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"tool_calls[{index}] had no tool name")
            continue
        arguments = item.get("arguments")
        normalised = False
        if not isinstance(arguments, dict):
            arguments = {k: v for k, v in item.items() if k not in ("name", "arguments")}
            normalised = bool(arguments)
        if normalised:
            problems.append(f"tool_calls[{index}] arguments were inlined and normalised")
        calls.append(ToolCall(id=f"opencode-{index}", name=name, arguments=arguments))
    return EmulatedReply(
        text=reply if isinstance(reply, str) else "",
        calls=calls,
        problem="; ".join(problems) or None,
        parsed=True,
    )


class OpenCodeProvider:
    """Chat provider backed by a local ``opencode serve`` instance.

    Implements the full :class:`app.contracts.ChatProvider` protocol plus
    :class:`app.gateway.streaming.StreamingChatProvider`.
    """

    name = "opencode"
    supports_media = False
    #: Native OpenAI-style tool calling does not exist on this transport.
    supports_tools = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        session_reuse: bool | None = None,
        tool_emulation: bool | None = None,
        require_api_key: bool | None = None,
    ) -> None:
        self.base_url = (base_url or settings.opencode_base_url).rstrip("/")
        self.provider_id = provider_id or settings.opencode_provider_id
        self.model = model or settings.opencode_model
        self.agent = agent or settings.opencode_agent
        self.session_reuse = (
            settings.opencode_session_reuse if session_reuse is None else session_reuse
        )
        self.tool_emulation = (
            settings.opencode_tool_emulation if tool_emulation is None else tool_emulation
        )
        self.require_api_key = (
            settings.opencode_require_api_key if require_api_key is None else require_api_key
        )
        self._sticky_session: str | None = None
        self._session_lock = asyncio.Lock()

    # ---------------------------------------------------------------- transport

    def _timeout(self) -> httpx.Timeout:
        """EV's shared timeout policy, with the model round trip allowed longer."""

        base = http_timeout()
        return httpx.Timeout(
            connect=base.connect,
            read=max(float(base.read or 60.0), settings.opencode_read_timeout_seconds),
            write=base.write,
            pool=base.pool,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout())

    def _unavailable(self, detail: str) -> OpenCodeUnavailableError:
        key_present, source = api_key_status()
        hint = START_SERVER_HINT if key_present else f"{START_SERVER_HINT}. Also: {API_KEY_HINT}"
        return OpenCodeUnavailableError(
            f"opencode provider unavailable at {self.base_url}: {detail} "
            f"(api key visible to EV: {source}). {hint}"
        )

    def _require_key(self) -> None:
        if not self.require_api_key:
            return
        key_present, _ = api_key_status()
        if not key_present:
            raise OpenCodeUnavailableError(
                f"opencode provider refuses to run without a credential: {API_KEY_HINT}. "
                "Set EV_OPENCODE_REQUIRE_API_KEY=false only if the server process "
                "holds the key somewhere EV cannot see."
            )

    async def health(self) -> dict:
        """Raw ``GET /global/health`` payload; raises when unreachable."""

        async with self._client() as client:
            return await self._health(client)

    async def _health(self, client: httpx.AsyncClient) -> dict:
        try:
            response = await client.get(f"{self.base_url}/global/health")
            response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise self._unavailable(f"health check failed ({exc})") from exc
        payload = response.json()
        if not payload.get("healthy"):
            raise self._unavailable(f"server reports unhealthy: {payload}")
        return dict(payload)

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict,
        *,
        retry_transient: bool = True,
    ) -> Any:
        """POST with EV's retry policy; connection failures become actionable errors."""

        attempts = max_attempts() if retry_transient else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await client.post(f"{self.base_url}{path}", json=payload)
                response.raise_for_status()
                body = response.content
                return json.loads(body) if body else None
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                detail = exc.response.text[:400]
                if is_transient(exc, status) and attempt + 1 < attempts:
                    last_exc = exc
                    await wait_for_retry(attempt)
                    continue
                raise self._unavailable(f"POST {path} -> HTTP {status}: {detail}") from exc
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await wait_for_retry(attempt)
                    continue
                raise self._unavailable(f"POST {path} failed ({exc})") from exc
        raise self._unavailable(f"POST {path} failed after {attempts} attempts ({last_exc})")

    # ------------------------------------------------------------- session life

    async def _create_session(self, client: httpx.AsyncClient, model: str) -> str:
        payload = {
            "title": settings.opencode_session_title,
            "model": {"providerID": self.provider_id, "id": model},
        }
        data = await self._post(client, "/session", payload)
        session_id = (data or {}).get("id")
        if not session_id:
            raise self._unavailable(f"session create returned no id: {data}")
        return str(session_id)

    async def _acquire_session(self, client: httpx.AsyncClient, model: str) -> str:
        """A fresh ephemeral session per request unless reuse is configured."""

        if not self.session_reuse:
            return await self._create_session(client, model)
        async with self._session_lock:
            if self._sticky_session is None:
                self._sticky_session = await self._create_session(client, model)
                logger.warning(
                    "EV_OPENCODE_SESSION_REUSE is on: opencode session %s will "
                    "accumulate history and cost outside EV's conversation model",
                    self._sticky_session,
                )
            return self._sticky_session

    async def _release_session(self, client: httpx.AsyncClient, session_id: str) -> bool:
        """Delete an ephemeral session so opencode keeps no conversation memory."""

        if self.session_reuse:
            return False
        try:
            response = await client.delete(f"{self.base_url}/session/{session_id}")
            response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            logger.warning("opencode session %s was not disposed: %s", session_id, exc)
            return False
        return True

    def _message_body(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
    ) -> dict:
        system = _system_prompt(messages)
        if tools:
            system = f"{system}\n\n{_tool_protocol(tools)}" if system else _tool_protocol(tools)
        body: dict[str, Any] = {
            "model": {"providerID": self.provider_id, "modelID": model},
            "agent": self.agent,
            "tools": {},
            "parts": [{"type": "text", "text": _conversation_text(messages)}],
        }
        if system:
            body["system"] = system
        if tools:
            body["format"] = {
                "type": "json_schema",
                "schema": TOOL_EMULATION_SCHEMA,
                "retryCount": settings.opencode_format_retries,
            }
        return body

    # ------------------------------------------------------------------- chat

    async def _complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> ChatResult:
        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        self._require_key()
        resolved_model = model or self.model
        try:
            async with self._client() as client:
                await self._health(client)
                session_id = await self._acquire_session(client, resolved_model)
                try:
                    data = await self._post(
                        client,
                        f"/session/{session_id}/message",
                        self._message_body(messages, model=resolved_model, tools=tools),
                        # A read timeout may leave a billed generation running
                        # server-side; retrying would pay for it twice.
                        retry_transient=False,
                    )
                finally:
                    await self._release_session(client, session_id)
        except OpenCodeUnavailableError:
            breaker.record_failure()
            raise
        except CircuitOpenError:
            raise
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()
        info = (data or {}).get("info") or {}
        parts = (data or {}).get("parts") or []
        text = _text_from_parts(parts)
        usage = _usage_from_info(info)
        tool_calls: list[ToolCall] = []
        if tools:
            parsed = _parse_emulated_tool_calls(text)
            text, tool_calls = parsed.text, parsed.calls
            usage["tool_calling"] = "emulated_structured_output"
            if parsed.problem:
                usage["tool_emulation_problem"] = parsed.problem
                logger.warning("opencode tool emulation problem: %s", parsed.problem)
            if not parsed.parsed:
                _mark_degraded(
                    usage,
                    kind="tool_emulation_unparsed",
                    detail=(
                        f"{len(tools)} tool(s) were offered and the model ignored the "
                        f"structured-output envelope ({parsed.problem}), so no tool call "
                        "could be read back from this turn"
                    ),
                )
        return ChatResult(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            model=info.get("modelID") or resolved_model,
        )

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        """One buffered turn. ``temperature`` is not honoured — see below.

        The session API has no temperature field, so sampling is whatever the
        configured agent declares (``.opencode/agents/ev-minimal.md``). The
        requested value is reported back in usage rather than silently dropped.
        """

        result = await self._complete(messages, model=model)
        if abs(temperature - settings.opencode_agent_temperature) > 1e-9:
            result.usage["temperature_requested"] = temperature
            result.usage["temperature_applied"] = settings.opencode_agent_temperature
            result.usage["temperature_source"] = f"opencode agent {self.agent}"
        return result

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        """Tools are not native here; degrade loudly or emulate on request."""

        if not tools:
            return await self.chat(messages, model=model, temperature=temperature)
        if self.tool_emulation:
            return await self._complete(messages, model=model, tools=tools)
        logger.warning(
            "opencode provider was asked for %d tools but the session API has no "
            "function calling; answering without tools and marking degraded "
            "(set EV_OPENCODE_TOOL_EMULATION=true for structured-output emulation)",
            len(tools),
        )
        result = await self.chat(messages, model=model, temperature=temperature)
        _mark_degraded(
            result.usage,
            kind="tools_unsupported",
            detail=(
                f"{len(tools)} tool(s) were offered; the opencode session API accepts "
                "no function definitions, so this turn ran without tools"
            ),
        )
        return result

    async def list_models(self) -> list[str]:
        """The configured model. The full catalogue is ``opencode models``."""

        return [self.model]

    # -------------------------------------------------------------- streaming

    async def _stream_cleanup(
        self,
        client: httpx.AsyncClient,
        session_id: str | None,
        tasks: Sequence[asyncio.Task[Any] | None],
    ) -> None:
        """Tear down one streamed turn: tasks, ephemeral session, connection.

        Runs to completion even when the consumer disconnected mid-stream, so a
        dropped SSE client cannot leave a session behind on the server.
        """

        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        live = [task for task in tasks if task is not None]
        if live:
            await asyncio.gather(*live, return_exceptions=True)
        try:
            if session_id is not None:
                await self._release_session(client, session_id)
        finally:
            await client.aclose()

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatStreamChunk]:
        """Real incremental streaming over ``prompt_async`` + ``GET /event``.

        opencode emits ``message.part.delta`` events per token group; only
        deltas belonging to a *text* part are forwarded (reasoning parts are not
        user-facing text). The terminal chunk carries opencode's reported tokens
        and cost. Cancelling this generator — including an SSE client that hangs
        up mid-answer — cancels the prompt task, disposes the ephemeral session
        and closes the connection.
        """

        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        self._require_key()
        resolved_model = model or self.model
        usage: dict[str, Any] = {}
        emitted_chars = 0
        started = False
        # The client is deliberately not held by `async with`: cleanup must be
        # able to outlive a cancellation, and closing the client here would
        # cancel the disposal request with it.
        client = self._client()
        session_id: str | None = None
        prompt_task: asyncio.Task[Any] | None = None
        reader_task: asyncio.Task[None] | None = None
        queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
        try:
            try:
                await self._health(client)
                session_id = await self._acquire_session(client, resolved_model)
                async with client.stream(
                    "GET",
                    f"{self.base_url}/event",
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()

                    async def pump(stream: httpx.Response = response) -> None:
                        try:
                            async for line in stream.aiter_lines():
                                await queue.put(line)
                        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
                            await queue.put(exc)
                        finally:
                            await queue.put(None)

                    reader_task = asyncio.create_task(pump())
                    prompt_task = asyncio.create_task(
                        self._post(
                            client,
                            f"/session/{session_id}/prompt_async",
                            self._message_body(messages, model=resolved_model),
                            retry_transient=False,
                        )
                    )
                    emitted: dict[str, int] = {}
                    pending: dict[str, list[str]] = {}
                    snapshots: dict[str, str] = {}
                    # messageID -> role, from message.updated events. Used to
                    # keep the user's own prompt (also a text part) out of the
                    # assistant output: without this the tail replay echoes the
                    # prompt back at the end of every answer.
                    message_roles: dict[str, str] = {}
                    deadline = time.monotonic() + settings.opencode_stream_timeout_seconds
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ProviderStreamError(
                                "opencode stream exceeded "
                                f"{settings.opencode_stream_timeout_seconds:.0f}s"
                            )
                        try:
                            item = await asyncio.wait_for(
                                queue.get(), timeout=min(remaining, 5.0)
                            )
                        except TimeoutError:
                            # No events for a while: surface a failed prompt POST
                            # instead of waiting for the deadline.
                            failure = prompt_task.exception() if prompt_task.done() else None
                            if failure is not None:
                                raise failure from None
                            continue
                        if item is None:
                            break
                        if isinstance(item, BaseException):
                            raise item
                        if not item.startswith("data:"):
                            continue
                        try:
                            event = json.loads(item[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        kind = event.get("type")
                        props = event.get("properties") or {}
                        if props.get("sessionID") not in (None, session_id):
                            continue
                        if kind == "message.part.updated":
                            part = props.get("part") or {}
                            part_id = str(part.get("id") or "")
                            if part.get("type") != "text" or not part_id:
                                continue
                            mid = part.get("messageID")
                            if mid is not None:
                                role = message_roles.get(str(mid))
                                if role is not None and role != "assistant":
                                    # The user's own prompt is also a text part;
                                    # it must never be replayed as a reply.
                                    continue
                            snapshots[part_id] = part.get("text") or ""
                            if part_id not in emitted:
                                emitted[part_id] = 0
                                for delta in pending.pop(part_id, []):
                                    emitted[part_id] += len(delta)
                                    emitted_chars += len(delta)
                                    started = True
                                    yield ChatStreamChunk(text=delta, model=resolved_model)
                        elif kind == "message.part.delta":
                            if props.get("field") != "text":
                                continue
                            part_id = str(props.get("partID") or "")
                            delta = props.get("delta") or ""
                            if not part_id or not delta:
                                continue
                            if part_id in emitted:
                                emitted[part_id] += len(delta)
                                emitted_chars += len(delta)
                                started = True
                                yield ChatStreamChunk(text=delta, model=resolved_model)
                            else:
                                # A delta can arrive before its part is announced.
                                pending.setdefault(part_id, []).append(delta)
                        elif kind == "message.updated":
                            info = props.get("info") or {}
                            role = info.get("role")
                            mid = info.get("id")
                            if mid is not None and role is not None:
                                message_roles[str(mid)] = str(role)
                            if role == "assistant" and (
                                (info.get("tokens") or {}).get("total")
                            ):
                                usage = _usage_from_info(info)
                                if info.get("modelID"):
                                    resolved_model = str(info["modelID"])
                        elif kind == "session.idle":
                            break
                    # Emit any tail the delta stream missed (late subscription),
                    # so the aggregated text always matches the server snapshot.
                    for part_id, snapshot in snapshots.items():
                        if len(snapshot) > emitted.get(part_id, 0):
                            tail = snapshot[emitted.get(part_id, 0):]
                            emitted_chars += len(tail)
                            yield ChatStreamChunk(text=tail, model=resolved_model)
                    prompt_failure = prompt_task.exception() if prompt_task.done() else None
                    if prompt_failure is not None:
                        raise prompt_failure
            except (CircuitOpenError, OpenCodeUnavailableError, ProviderStreamError):
                breaker.record_failure()
                raise
            except Exception as exc:  # noqa: BLE001 - typed mid-stream boundary
                breaker.record_failure()
                if started:
                    raise ProviderStreamError(
                        f"opencode stream failed after partial output: {exc}"
                    ) from exc
                raise
        finally:
            cleanup = asyncio.ensure_future(
                self._stream_cleanup(client, session_id, (reader_task, prompt_task))
            )
            # A cancelled consumer must not cancel disposal: shield lets the
            # cleanup task finish detached.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
        breaker.record_success()
        if not usage:
            _mark_degraded(
                usage,
                kind="usage_missing",
                detail=(
                    "opencode reported no token/cost totals for this stream, so the "
                    "cost meter has nothing measured to record for it"
                ),
            )
            logger.warning("opencode stream finished without reported usage")
        if emitted_chars == 0:
            logger.warning("opencode stream produced no text parts")
        yield ChatStreamChunk(
            usage=usage,
            model=resolved_model,
            finish_reason="stop",
            done=True,
        )


def opencode_factory() -> OpenCodeProvider:
    return OpenCodeProvider()
