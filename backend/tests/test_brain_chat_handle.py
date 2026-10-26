"""The primary chat surface carries the task handle (review fix for 0.4).

`ChatResponse` used to drop `task_id`/`task_status`, so the asking channel
heard progress and then silence. Both kernel-path legs (pure-question
`handle_task` and `arun_kernel_turn`) now return the handle.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from app.config import settings
from app.contracts import ChatResult, ToolCall


class ScriptedProvider:
    name = "scripted"

    def __init__(self, turns: list[ChatResult], *, delay: float = 0.0) -> None:
        self.turns = deque(turns)
        self.delay = delay

    async def chat_with_tools(self, messages, tools, **kwargs):
        del messages, tools, kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.turns:
            return self.turns.popleft()
        return ChatResult(text="nothing left", model="scripted")


def _finish(summary: str) -> ToolCall:
    return ToolCall(
        id="finish-1", name="finish", arguments={"summary": summary, "ok": True}
    )


def _install_provider(monkeypatch, provider: ScriptedProvider) -> None:
    import app.agent.loop as loop
    from app.agent.brain import BrainChoice

    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)
    monkeypatch.setattr(loop, "build_agent_provider", lambda c: provider)


async def test_chat_pure_question_returns_task_handle(
    client, monkeypatch, tmp_path: Path
) -> None:
    from app.agent import ledger, runner

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(settings, "agent_root", str(workspace))
    monkeypatch.setattr(settings, "chat_use_kernel", True)
    ledger.init()

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        del title, body, kind

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)
    _install_provider(
        monkeypatch,
        ScriptedProvider([
            ChatResult(
                text="",
                tool_calls=[_finish("Ada Lovelace wrote the first algorithm.")],
            )
        ]),
    )
    try:
        resp = await client.post(
            "/v1/chat", json={"message": "Who is Ada Lovelace, in one sentence?"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "Lovelace" in body["reply"]
        assert body["task_id"], "chat must carry the background task handle"
        assert body["task_status"] in {"completed", "running"}
        assert body["conversation_id"]
    finally:
        runner._MIRRORS.clear()


async def test_chat_task_turn_returns_task_handle(
    client, monkeypatch, tmp_path: Path
) -> None:
    from app.agent import ledger, runner

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "old-report.txt").write_text("stale\n")
    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(settings, "agent_root", str(workspace))
    monkeypatch.setattr(settings, "chat_use_kernel", True)
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "agent_chat_inline_seconds", 1.0)
    ledger.init()

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        del title, body, kind

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)
    _install_provider(
        monkeypatch,
        ScriptedProvider(
            [
                ChatResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="search_files",
                            arguments={"query": "old report"},
                        )
                    ],
                ),
                ChatResult(
                    text="",
                    tool_calls=[_finish("Cleaned up the old report.")],
                ),
            ],
            delay=2.0,
        ),
    )
    import app.agent.intake as intake_module

    async def _route_to_task(raw: str, **kwargs) -> str:
        del raw, kwargs
        return "task"

    monkeypatch.setattr(intake_module, "route_goal", _route_to_task)
    try:
        resp = await client.post(
            "/v1/chat", json={"message": "Clean up the old report."}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "On it" in body["reply"]
        assert body["task_id"], "chat must carry the background task handle"
        assert body["task_status"] == "running"
    finally:
        runner._MIRRORS.clear()


async def test_delegate_worker_installs_a_guard(monkeypatch) -> None:
    # Review hole: the delegated worker called handle_turn unguarded.
    import asyncio

    from app.cognitive import delegation
    from app.cognitive.kernel import KernelResult
    from app.gateway.turn_guard import current_guard

    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "chat_provider", "muse")
    monkeypatch.setattr(settings, "brain_model", "meta/muse-spark-1.3-contributor")
    monkeypatch.setattr(settings, "gemini_live_model", "gemini-3.8-live")

    seen: list[bool] = []
    gate = asyncio.Event()

    async def fake_handle_turn(**kwargs):
        seen.append(current_guard() is not None)
        await gate.wait()
        return KernelResult(spoken="The report is on your desk.", kind="brain")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", fake_handle_turn)
    delivered = asyncio.Event()

    async def on_complete(receipt: dict) -> None:
        del receipt
        delivered.set()

    receipt = await delegation.submit_delegate(
        task="Summarize my inbox",
        request_id="guard-probe-1",
        live_session_id="live-1",
        on_complete=on_complete,
    )
    assert receipt["accepted"] is True
    gate.set()
    await asyncio.wait_for(delivered.wait(), 10)
    assert seen == [True]
