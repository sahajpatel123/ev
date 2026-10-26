"""The agent's tool surface is the capability graph, not a command table.

These tests pin the shape the reference harnesses use and the shape this
workspace had been drifting away from:

- Every semantic capability reaches the model as its own typed tool, so a
  capability the owner invents a use for needs no Python edit.
- ``capability.discover`` — the tool that answers "what can I do?" — is
  itself callable.
- The self-referential capability (``agent.run``) is not offered inside the
  lane it owns.
- The model keeps a plan (``todo_write``) and can offer the owner a choice
  (``ask_owner`` with options).
- Policy is read off each capability's own metadata, and a message body that
  merely mentions a secret word is not a secret path.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from app.agent import tools as agent_tools
from app.agent.capability_bridge import capability_names, capability_tools
from app.agent.loop import AgentLimits, _classify, run_agent
from app.agent.tools import ToolContext
from app.config import settings
from app.contracts import ChatResult, ToolCall


class ScriptedProvider:
    """Deterministic stand-in for a chat provider with tool calls."""

    name = "scripted"
    supports_media = False

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = deque(turns)
        self.seen: list[list] = []

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
        del tools, model, temperature
        self.seen.append(list(messages))
        if self.turns:
            return self.turns.popleft()
        return ChatResult(text="nothing left to do", model="scripted")

    async def chat(self, messages, *, model=None, temperature=0.7):
        return await self.chat_with_tools(messages, [])


def _turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(text=text, tool_calls=list(calls), usage={}, model="scripted")


def _finish(summary: str = "done") -> ToolCall:
    return ToolCall(id="finish-1", name="finish", arguments={"summary": summary})


def _spec_names() -> set[str]:
    return {spec.name for spec in agent_tools.agent_tool_specs()}


# --------------------------------------------------------------------------- #
# The capability graph is the tool surface
# --------------------------------------------------------------------------- #


def test_every_semantic_capability_is_offered_as_a_tool() -> None:
    offered = _spec_names()
    missing = [name for name in capability_names() if name not in offered]
    # ``agent.run`` is the one deliberate exception: it owns the lane.
    assert missing == ["agent.run"], f"capabilities not reachable: {missing}"


def test_capability_discover_is_itself_callable() -> None:
    """The old mac_call refused it, so the model could not ask what it could do."""
    assert "capability.discover" in _spec_names()
    tool = agent_tools.REGISTRY["capability.discover"]
    assert tool.spec.read_only is True


def test_the_capability_tools_carry_their_own_schemas() -> None:
    by_name = {tool.spec.name: tool for tool in capability_tools()}
    send = by_name["life.send"]
    assert send.spec.parameters["required"] == ["to", "text"]
    assert send.spec.read_only is False
    assert send.spec.permission == "messaging:send"
    assert "to" in send.spec.parameters["properties"]


@pytest.mark.asyncio
async def test_a_capability_tool_reaches_the_real_executor(monkeypatch) -> None:
    """One capability call must land in execute_semantic, with its arguments."""
    seen: dict = {}

    async def fake_execute(session, name, arguments, **kwargs):
        del session, kwargs
        seen["name"] = name
        seen["arguments"] = dict(arguments)
        return {"ok": True, "spoken": "Ada Lovelace, 3 messages", "count": 3}

    import app.cognitive.executor as executor

    monkeypatch.setattr(executor, "execute_semantic", fake_execute)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    import app.db as db

    monkeypatch.setattr(db, "SessionLocal", lambda: _Session())

    tool = agent_tools.REGISTRY["life.messages"]
    receipt = await tool.run({"query": "who do I talk to"}, ToolContext(root=Path("/tmp")))
    assert receipt["ok"] is True
    assert receipt["verified"] is True
    assert seen["name"] == "life.messages"
    assert seen["arguments"]["query"] == "who do I talk to"
    assert receipt["result"]["count"] == 3


@pytest.mark.asyncio
async def test_a_capability_failure_is_reported_not_hidden(monkeypatch) -> None:
    async def fake_execute(session, name, arguments, **kwargs):
        del session, name, arguments, kwargs
        return {"ok": False, "error": "NOT_CONNECTED", "spoken": "Not Connected"}

    import app.cognitive.executor as executor

    monkeypatch.setattr(executor, "execute_semantic", fake_execute)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    import app.db as db

    monkeypatch.setattr(db, "SessionLocal", lambda: _Session())

    tool = agent_tools.REGISTRY["life.messages"]
    receipt = await tool.run({"query": "mail"}, ToolContext(root=Path("/tmp")))
    assert receipt["ok"] is False
    assert receipt["error"] == "NOT_CONNECTED"


# --------------------------------------------------------------------------- #
# The plan and the question
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_todo_write_records_the_plan_and_publishes_it() -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="t1", name="todo_write", arguments={
            "todos": [
                {"content": "find the largest file", "status": "in_progress"},
                {"content": "write the answer", "status": "pending"},
            ],
        })),
        _turn(_finish()),
    ])
    events: list[dict] = []
    result = await run_agent(
        "largest file and the answer",
        root=Path("/tmp"),
        provider=provider,
        on_event=events.append,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.status == "completed"
    assert result.steps[0].tool == "todo_write"
    assert result.steps[0].ok is True
    plan_events = [e for e in events if e.get("kind") == "plan_updated"]
    assert plan_events, "the plan must be published on the run's event stream"
    assert plan_events[0]["todos"][0]["content"] == "find the largest file"


@pytest.mark.asyncio
async def test_todo_write_rejects_a_status_it_does_not_understand() -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="t1", name="todo_write", arguments={
            "todos": [{"content": "x", "status": "maybe"}],
        })),
        _turn(_finish()),
    ])
    result = await run_agent(
        "plan it",
        root=Path("/tmp"),
        provider=provider,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.steps[0].ok is False
    assert "status" in (result.steps[0].error or "")


@pytest.mark.asyncio
async def test_ask_owner_carries_the_offered_options() -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="q1", name="ask_owner", arguments={
            "question": "Which account should I send from?",
            "options": [
                {"label": "Work", "description": "the work address"},
                {"label": "Personal", "description": "the personal address"},
            ],
        })),
    ])
    result = await run_agent(
        "send the reply",
        root=Path("/tmp"),
        provider=provider,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.status == "needs_input"
    assert result.needs_input is True
    assert result.question == "Which account should I send from?"
    assert [o["label"] for o in result.options] == ["Work", "Personal"]


# --------------------------------------------------------------------------- #
# Policy comes from the capability's own row
# --------------------------------------------------------------------------- #


def test_read_only_capabilities_run_without_asking() -> None:
    tool = agent_tools.REGISTRY["memory.search"]
    assert _classify(tool, {"query": "Ada"}, approved=False) == "AUTO"


def test_mutating_capabilities_ask_first() -> None:
    tool = agent_tools.REGISTRY["life.send"]
    assert _classify(tool, {"to": "ada@example.com", "text": "hi"}, approved=False) == "CONFIRM"
    # R7/D4 risk floor: a send asks even in a pre-approved run.
    assert _classify(tool, {"to": "ada@example.com", "text": "hi"}, approved=True) == "CONFIRM"


def test_a_secret_word_in_a_message_body_is_not_a_secret_path() -> None:
    """The guard is about secret *paths*. Free text must not trip it."""
    tool = agent_tools.REGISTRY["life.send"]
    verdict = _classify(
        tool,
        {"to": "ada@example.com", "text": "the token is in the vault, rotate it"},
        approved=False,
    )
    assert verdict == "CONFIRM", "a body mentioning a secret word must still ask, not deny"


def test_a_secret_path_is_still_denied() -> None:
    tool = agent_tools.REGISTRY["files.act"]
    verdict = _classify(tool, {"op": "read", "path": "~/.ssh/id_rsa.key"}, approved=True)
    assert verdict == "DENY"


def test_capability_policy_reads_the_spec_permission() -> None:
    from app.agent.loop import _action_class

    assert _action_class(agent_tools.REGISTRY["life.send"]) == "send"
    assert _action_class(agent_tools.REGISTRY["shell.run"]) == "shell"
    assert _action_class(agent_tools.REGISTRY["files.act"]) == "run"


# --------------------------------------------------------------------------- #
# A read operation is not an effect
# --------------------------------------------------------------------------- #


def test_a_read_operation_of_a_polymorphic_capability_does_not_ask() -> None:
    """owner.profile op=get reads the name; op=set writes it. Same tool."""
    tool = agent_tools.REGISTRY["owner.profile"]
    assert _classify(tool, {"op": "get"}, approved=False) == "AUTO"


def test_the_write_operation_of_the_same_capability_still_asks() -> None:
    tool = agent_tools.REGISTRY["owner.profile"]
    assert _classify(tool, {"op": "set", "name": "Ada"}, approved=False) == "CONFIRM"


def test_a_file_read_does_not_ask_but_a_file_delete_does() -> None:
    tool = agent_tools.REGISTRY["files.act"]
    assert _classify(tool, {"op": "read", "path": "notes.txt"}, approved=False) == "AUTO"
    assert _classify(tool, {"op": "list", "path": "."}, approved=False) == "AUTO"
    assert _classify(tool, {"op": "delete", "path": "notes.txt"}, approved=False) == "CONFIRM"


def test_a_gmail_read_does_not_ask_but_a_gmail_send_does() -> None:
    tool = agent_tools.REGISTRY["digital.act"]
    assert _classify(
        tool, {"service": "gmail", "operation": "search"}, approved=False
    ) == "AUTO"
    assert _classify(
        tool, {"service": "gmail", "operation": "send"}, approved=False
    ) == "CONFIRM"
    # A service whose reads this lane does not declare stays an effect.
    assert _classify(
        tool, {"service": "whatsapp", "operation": "read"}, approved=False
    ) == "CONFIRM"


def test_a_read_operation_vocabulary_is_the_owning_lane_s() -> None:
    """The file lane owns its read vocabulary; the bridge must not copy it."""
    from app.agent.capability_bridge import read_operations
    from app.ev.file_sandbox import READ_OPS

    assert read_operations("files.act") is READ_OPS


def test_an_unknown_operation_is_an_effect() -> None:
    from app.agent.capability_bridge import is_read_call

    assert is_read_call("owner.profile", {"op": "set"}) is False
    assert is_read_call("owner.profile", {}) is False
    assert is_read_call("not.a.capability", {"op": "get"}) is False


# --------------------------------------------------------------------------- #
# A failed capability must say why
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_capability_names_the_reason(monkeypatch) -> None:
    """ok=False with no error is not something a model can adapt to."""
    async def fake_execute(session, name, arguments, **kwargs):
        del session, name, arguments, kwargs
        return {
            "ok": False,
            "degraded": True,
            "grounding": "no_reliable_record",
            "spoken": "I cannot find that particular record.",
            "count": 0,
        }

    import app.cognitive.executor as executor

    monkeypatch.setattr(executor, "execute_semantic", fake_execute)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    import app.db as db

    monkeypatch.setattr(db, "SessionLocal", lambda: _Session())

    tool = agent_tools.REGISTRY["memory.search"]
    receipt = await tool.run({"query": "Ada Lovelace"}, ToolContext(root=Path("/tmp")))
    assert receipt["ok"] is False
    assert receipt["error"], "a failed call must carry a reason"
    assert "degraded" in receipt["error"]
    assert receipt["verified"] is False
    # The model must be able to see that the lane degraded.
    assert receipt["result"]["degraded"] is True
    assert receipt["result"]["grounding"] == "no_reliable_record"


# --------------------------------------------------------------------------- #
# The agent lane owns the agent capability
# --------------------------------------------------------------------------- #


def test_the_agent_lane_does_not_offer_itself() -> None:
    assert "agent.run" not in _spec_names()


def test_the_autonomy_setting_still_decides_whether_to_ask(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_autonomy", "trusted", raising=False)
    # Autonomy decides ordinary effects. A send sits under the risk floor and
    # always asks (tests/test_risk_floor.py).
    tool = agent_tools.REGISTRY["owner.profile"]
    assert _classify(tool, {"op": "set", "name": "Ada"}, approved=True) == "AUTO"
    send = agent_tools.REGISTRY["life.send"]
    assert _classify(send, {"to": "a@b.c", "text": "hi"}, approved=True) == "CONFIRM"
    monkeypatch.setattr(settings, "agent_autonomy", "ask", raising=False)
