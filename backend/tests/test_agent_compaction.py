"""A long run must not outgrow the model's context window.

The reference harness compacts at 85% of the window and leaves the compacted
turns on disk. Evie's loop now compacts too — factually, and never over the
recent part of the transcript.

These tests pin the three properties that matter:

- the owner's goal and the model's own turns survive compaction;
- a compacted tool result still says whether the call succeeded;
- compaction is not invented content, so a digest cannot claim something the
  tool never returned.
"""

from __future__ import annotations

import json
from collections import deque

import pytest

from app.agent import compaction
from app.agent.loop import AgentLimits, run_agent
from app.contracts import ChatMessage, ChatResult, ToolCall


class ScriptedProvider:
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


def _tool_message(name: str, payload: dict, call_id: str) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=json.dumps(payload),
        name=name,
        tool_call_id=call_id,
    )


def _transcript(turns: int = 8) -> list[ChatMessage]:
    messages = [
        ChatMessage(role="system", content="system contract"),
        ChatMessage(role="user", content="the owner's goal"),
    ]
    for index in range(turns):
        messages.append(
            ChatMessage(
                role="assistant",
                content=f"thinking {index}",
                tool_calls=[
                    ToolCall(id=f"c{index}", name="read_file", arguments={"path": f"{index}"})
                ],
            )
        )
        messages.append(
            _tool_message(
                "read_file",
                {"ok": True, "result": {"content": "x" * 4000}, "error": None},
                f"c{index}",
            )
        )
    return messages


# --------------------------------------------------------------------------- #
# The unit
# --------------------------------------------------------------------------- #


def test_a_short_transcript_is_left_alone() -> None:
    messages = _transcript(2)
    out, report = compaction.compact_messages(messages, keep_recent=12)
    assert report.compacted is False
    assert len(out) == len(messages)


def test_compaction_keeps_the_goal_and_the_recent_turns() -> None:
    messages = _transcript(8)
    out, report = compaction.compact_messages(messages, keep_recent=6)
    assert report.compacted is True
    assert out[0].content == "system contract"
    assert out[1].content == "the owner's goal"
    # The recent window is verbatim.
    assert out[-1].content == messages[-1].content
    assert out[-2].content == messages[-2].content


def test_a_compacted_tool_result_keeps_its_receipt() -> None:
    messages = _transcript(8)
    out, _ = compaction.compact_messages(messages, keep_recent=4)
    digests = [
        json.loads(m.content)
        for m in out
        if m.role == "tool" and m.content.startswith("{")
    ]
    assert digests, "older tool results must survive as digests"
    assert all("ok" in d for d in digests)
    assert any(d.get("ok") is True for d in digests)


def test_a_failed_tool_result_still_says_it_failed() -> None:
    messages = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="u"),
        ChatMessage(
            role="assistant",
            content="thinking",
            tool_calls=[ToolCall(id="c1", name="run_shell", arguments={})],
        ),
        _tool_message("run_shell", {"ok": False, "error": "denied by policy"}, "c1"),
    ]
    for _ in range(6):
        messages.append(
            ChatMessage(
                role="assistant",
                content="more",
                tool_calls=[ToolCall(id="x", name="read_file", arguments={})],
            )
        )
        messages.append(_tool_message("read_file", {"ok": True, "result": "y" * 3000}, "x"))
    out, _ = compaction.compact_messages(messages, keep_recent=4)
    digests = [
        json.loads(m.content) for m in out if m.role == "tool" and m.content.startswith("{")
    ]
    assert any(d.get("error") == "denied by policy" for d in digests)


def test_the_digest_never_adds_a_claim_the_tool_did_not_make() -> None:
    messages = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="u"),
        ChatMessage(
            role="assistant",
            content="thinking",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={})],
        ),
        _tool_message("read_file", {"ok": True, "result": {"content": "needle"}}, "c1"),
    ]
    for _ in range(6):
        messages.append(
            ChatMessage(
                role="assistant",
                content="more",
                tool_calls=[ToolCall(id="x", name="read_file", arguments={})],
            )
        )
        messages.append(_tool_message("read_file", {"ok": True, "result": "y" * 3000}, "x"))
    out, _ = compaction.compact_messages(messages, keep_recent=4)
    text = " ".join(m.content for m in out)
    assert "shortened" in text
    # The digest must not have grown a summary the tool never produced.
    assert "summary" not in text.lower()


def test_the_budget_check_accounts_for_the_tool_surface() -> None:
    messages = _transcript(8)
    alone = compaction.estimate_tokens(messages)
    assert compaction.context_budget_exceeded(
        messages, context_tokens=1000, threshold_percent=85
    )
    assert not compaction.context_budget_exceeded(
        messages, context_tokens=100_000, threshold_percent=85
    )
    # The same transcript overflows a small window once the tool surface is
    # counted, because that surface is sent on every request.
    assert compaction.context_budget_exceeded(
        messages,
        context_tokens=alone + 100,
        threshold_percent=85,
        tool_schema_tokens=10_000,
    )


# --------------------------------------------------------------------------- #
# In the loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_run_compacts_its_own_transcript(tmp_path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "agent_context_tokens", 900, raising=False)
    monkeypatch.setattr(settings, "agent_compact_keep_recent", 4, raising=False)

    # A real file with real bulk in it: the tool result has to be big for
    # compaction to have anything to compact.
    big = tmp_path / "big.txt"
    big.write_text("needle\n" + ("filler line of text\n" * 2000))

    calls = [
        ToolCall(id=f"c{index}", name="read_file", arguments={"path": "big.txt"})
        for index in range(6)
    ]
    provider = ScriptedProvider([ChatResult(text="", tool_calls=[c], usage={}, model="scripted")
                                 for c in calls])
    events: list[dict] = []
    result = await run_agent(
        "read the big file",
        root=tmp_path,
        provider=provider,
        on_event=events.append,
        limits=AgentLimits(max_steps=8, max_tool_calls=10, max_seconds=60),
    )
    compacted = [e for e in events if e.get("kind") == "context_compacted"]
    assert compacted, "the run must compact when its transcript outgrows the window"
    report = compacted[0]
    assert report["tool_results_digested"] >= 1
    assert report["tokens_after"] < report["tokens_before"]
    assert result.status in {"completed", "capped", "failed"}
