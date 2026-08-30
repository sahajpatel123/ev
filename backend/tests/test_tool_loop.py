"""Tests for the bounded chat tool loop (M3.1): execute, feed back, cap, deny."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.contracts import ChatMessage, ChatProvider, ChatResult, ToolCall, ToolSpec
from app.models import AccessLog


class ToolCallProvider(ChatProvider):
    """Deterministic provider that emits a scripted sequence of tool calls."""

    name = "tooltest"
    supports_media = False

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = list(calls)
        self.rounds = 0

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return ChatResult(text="fallback", usage={}, model=model or "tooltest")

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        self.rounds += 1
        if self.calls:
            return ChatResult(
                text="",
                tool_calls=[self.calls.pop(0)],
                usage={},
                model=model or "tooltest",
            )
        tool_text = next(
            (m.content for m in reversed(messages) if m.role == "tool"),
            "",
        )
        return ChatResult(
            text=f"Final answer using tool results: {tool_text[:80]}",
            usage={},
            model=model or "tooltest",
        )

    async def list_models(self) -> list[str]:
        return ["tooltest"]


async def _tool_calls_for(name: str, db_session) -> int:
    rows = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "tool_call")
        )
    ).scalars().all()
    return sum(1 for row in rows if row.resource_ids and row.resource_ids[0] == name)


async def test_chat_tool_loop_executes_and_feeds_results_back(
    client, db_session, monkeypatch
) -> None:
    from app.api import core as core_api

    provider = ToolCallProvider(
        [ToolCall(id="c1", name="calculate", arguments={"expression": "2 + 3 * 4"})]
    )
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: provider)

    resp = await client.post(
        "/v1/chat",
        json={"message": "what is 2 + 3 * 4?"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Final answer using tool results" in body["reply"]
    assert "14.0" in body["reply"]
    assert provider.rounds == 2
    assert await _tool_calls_for("calculate", db_session) >= 1


async def test_tool_loop_is_bounded_to_three_rounds(client, db_session, monkeypatch) -> None:
    from app.api import core as core_api

    provider = ToolCallProvider([])
    provider.calls = [
        ToolCall(id=str(i), name="calculate", arguments={"expression": "1 + 1"})
        for i in range(10)
    ]
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: provider)

    resp = await client.post(
        "/v1/chat",
        json={"message": "keep calculating"},
    )
    assert resp.status_code == 200, resp.text
    assert "tool-call limit" in resp.json()["reply"]
    assert await _tool_calls_for("calculate", db_session) == 3
    assert provider.rounds == 4  # initial call + 3 re-entries


async def test_sensitive_tool_call_is_denied_without_permission(
    client, db_session, monkeypatch
) -> None:
    from app.api import core as core_api

    provider = ToolCallProvider(
        [
            ToolCall(
                id="h1",
                name="get_health_trends",
                arguments={"metric": "sleep_hours"},
            )
        ]
    )
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: provider)

    resp = await client.post(
        "/v1/chat",
        json={"message": "check my sleep"},
    )
    assert resp.status_code == 200, resp.text
    assert "tool-call limit" in resp.json()["reply"]
    assert await _tool_calls_for("get_health_trends", db_session) == 0


async def test_chat_can_allow_sensitive_tools_explicitly(
    client, db_session, monkeypatch
) -> None:
    from app.api import core as core_api

    provider = ToolCallProvider(
        [
            ToolCall(
                id="h2",
                name="get_health_trends",
                arguments={"metric": "sleep_hours"},
            )
        ]
    )
    monkeypatch.setattr(core_api, "get_chat_provider", lambda: provider)

    resp = await client.post(
        "/v1/chat",
        json={"message": "check my sleep", "allow_sensitive_tools": True},
    )
    assert resp.status_code == 200, resp.text
    assert await _tool_calls_for("get_health_trends", db_session) >= 1
