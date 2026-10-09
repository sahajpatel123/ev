"""Track B worker pins: brain budgets, loop guardrails, fence, focus shape."""
from __future__ import annotations

import pytest

from app.cognitive.graph import TaskNode, WorkerReceipt
from app.cognitive.worker import (
    WorkerCtx,
    _is_stub,
    _mimo_worker,
    _worker_budget,
    _worker_effort,
)
from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.ev.computer import _shape_lifecycle
from app.ev.computer_executor import (
    ComputerExecutionRequest,
    ComputerExecutor,
    SideEffectState,
    mark_fence,
    reset_fence,
    shadow_validate_tool,
)


def _node(**overrides):
    base = {"id": "n1", "label": "Do it", "detail": "Do it well.", "tier": "R"}
    base.update(overrides)
    return TaskNode.model_validate(base)


# --------------------------------------------------------------------------- #
# Brain budgets: effort tiers and clamped reserves
# --------------------------------------------------------------------------- #


def test_worker_effort_fast_first_retry_thinks(monkeypatch):
    assert _worker_effort(1) == "low"
    monkeypatch.setattr(settings, "graph_retry_reasoning_effort", "high")
    assert _worker_effort(2) == "high"
    monkeypatch.setattr(settings, "graph_retry_reasoning_effort", "bogus")
    assert _worker_effort(2) == "medium"


def test_worker_budget_clamps_rounds_and_reserve(monkeypatch):
    monkeypatch.setattr(settings, "graph_worker_max_rounds", 99)
    monkeypatch.setattr(settings, "graph_worker_reserve_seconds", 99.0)
    cap, reserve, limit = _worker_budget(_node(timeout_seconds=60.0))
    assert cap == 8
    assert reserve == limit / 2.0
    monkeypatch.setattr(settings, "graph_worker_max_rounds", 4)
    monkeypatch.setattr(settings, "graph_worker_reserve_seconds", 8.0)
    cap, reserve, limit = _worker_budget(_node(timeout_seconds=5.0))
    assert cap == 4
    assert reserve == 2.5  # short budgets still attempt work


# --------------------------------------------------------------------------- #
# Loop guardrails with a scripted brain (no model calls)
# --------------------------------------------------------------------------- #


class _ScriptedProvider:
    def __init__(self, replies: list[ChatResult]):
        self.replies = list(replies)
        self.reasoning_effort = "low"

    async def chat_with_tools(self, messages, specs):
        if self.replies:
            return self.replies.pop(0)
        return ChatResult(text="")


def _call(tool_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=tool_id, name=name, arguments=arguments or {})


async def test_loop_runs_identical_tool_call_once():
    calls: list[str] = []

    async def fake_execute(session, tool, arguments, **kwargs):
        calls.append(tool)
        return {"ok": True, "rows": [{"a": 1}], "spoken": "Got it."}

    provider = _ScriptedProvider([
        ChatResult(text="", tool_calls=[
            _call("c1", "memory.search", {"query": "x"}),
            _call("c2", "memory.search", {"query": "x"}),
        ]),
        ChatResult(text="Done."),
    ])
    receipt = await _mimo_worker(
        _node(), ctx=WorkerCtx(), provider=provider, execute_fn=fake_execute)
    assert calls == ["memory.search"]
    assert receipt.ok is True
    assert receipt.evidence != []


async def test_loop_bare_assertion_yields_no_evidence():
    async def fake_execute(session, tool, arguments, **kwargs):
        return {"ok": True, "spoken": "Trust me."}

    provider = _ScriptedProvider([
        ChatResult(text="", tool_calls=[_call("c1", "memory.search")]),
        ChatResult(text=""),
    ])
    receipt = await _mimo_worker(
        _node(), ctx=WorkerCtx(), provider=provider, execute_fn=fake_execute)
    assert receipt.evidence == []  # supervisor must escalate, never accept


async def test_loop_prose_is_deliverable_on_first_read_attempt():
    provider = _ScriptedProvider([ChatResult(text="Line one.\nLine two.")])
    prior = [
        WorkerReceipt.model_validate({"node_id": "a", "ok": True, "worker": "s",
                                       "spoken": "A said this."}),
    ]
    receipt = await _mimo_worker(
        _node(), ctx=WorkerCtx(), provider=provider, execute_fn=None, prior=prior)
    assert receipt.ok is True
    assert receipt.artifacts != []


async def test_loop_retry_prose_without_action_is_honest():
    provider = _ScriptedProvider([ChatResult(text="Still thinking.")])
    receipt = await _mimo_worker(
        _node(), ctx=WorkerCtx(), provider=provider, execute_fn=None, attempt=2)
    assert receipt.ok is False
    assert receipt.error == "no_action_taken"


def test_stub_detector_rejects_header_only_synthesis():
    prior = [
        WorkerReceipt.model_validate({"node_id": "a", "ok": True, "worker": "s",
                                       "spoken": "x"}),
        WorkerReceipt.model_validate({"node_id": "b", "ok": True, "worker": "s",
                                       "spoken": "y"}),
    ]
    assert _is_stub("Emails", prior) is True
    assert _is_stub("Mom \u2014 meeting moved to 3pm.\nDad \u2014 called about the trip.", prior) is False
    assert _is_stub("Emails", prior[:1]) is False


async def test_loop_without_provider_fails_closed(monkeypatch):
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: False)
    with pytest.raises(Exception, match="worker unavailable"):
        await _mimo_worker(_node(), ctx=WorkerCtx(), execute_fn=None)


# --------------------------------------------------------------------------- #
# Executor fence: a retried mutation identity is refused, never repeated
# --------------------------------------------------------------------------- #


async def test_fence_blocks_mutation_retry():
    reset_fence()
    try:
        request = ComputerExecutionRequest(
            primitive="act", operation="ui_action",
            args={"verb": "click", "target": "OK"})
        request.execution_id = "track-b-fence-1"
        mark_fence(request, SideEffectState.EFFECT_OBSERVED)
        result = await ComputerExecutor(live=None, actor="master").execute(request)
        assert result.ok is False
        assert result.executed is False
        assert result.error_code == "fence_blocked_mutation_retry"
    finally:
        reset_fence()


async def test_shadow_mode_never_executes_mutations():
    record = await shadow_validate_tool(
        "ui_action", {"verb": "click", "target": "OK"}, live=None)
    assert record is not None
    assert record["mutating"] is True
    assert record["executed"] is False


def test_executor_declared_default_is_shadow():
    # Conftest forces "off" at runtime for unrelated suites; the shipped
    # default is shadow (behavior-preserving, diagnostics accumulating).
    field = type(settings).model_fields["computer_executor_v2"]
    assert field.default == "shadow"


# --------------------------------------------------------------------------- #
# One focus shape on computer receipts
# --------------------------------------------------------------------------- #


def test_lifecycle_receipt_defaults_to_background():
    shaped = _shape_lifecycle("open_app", {"name": "Music"},
                              {"ok": True, "name": "Music"}, source="t")
    assert shaped["activated"] is False
    assert shaped["focus_theft"] == 0


def test_lifecycle_receipt_reports_activation():
    shaped = _shape_lifecycle("activate_app", {"name": "Music"},
                              {"ok": True, "name": "Music"}, source="t")
    assert shaped["activated"] is True


def test_lifecycle_receipt_preserves_reported_focus_theft():
    shaped = _shape_lifecycle("open_app", {"name": "Music"},
                              {"ok": True, "name": "Music",
                               "activated": True, "focus_theft": 2}, source="t")
    assert shaped["activated"] is True
    assert shaped["focus_theft"] == 2
