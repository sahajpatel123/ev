"""Graph-worker effects are durable and read back before they count (R6).

Live catch (2026-10-24): three voice timers came back "Timer set for N
minutes." with a timer id and observed=true, yet none existed in the timers
table. Every graph worker ran its tools inside a session it never committed,
so each write was rolled back when the worker's session closed.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.cognitive import durability
from app.cognitive.graph import TaskNode, WorkerReceipt
from app.cognitive.worker import WorkerCtx, run_node
from app.contracts import ChatResult, ToolCall
from app.db import SessionLocal
from app.models import OwnerTimer


def _node(**overrides) -> TaskNode:
    base = {"id": "n1", "label": "Set a timer", "detail": "Three minutes.", "tier": "W"}
    base.update(overrides)
    return TaskNode.model_validate(base)


async def _timers() -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(OwnerTimer)) or 0)


def _durability(receipt: WorkerReceipt) -> list[dict]:
    return [entry for entry in receipt.evidence if entry.get("check") == durability.CHECK]


async def _insert_timer(session, name, args, **kwargs):
    from datetime import UTC, datetime, timedelta

    row = OwnerTimer(
        fire_at=datetime.now(UTC) + timedelta(minutes=3),
        payload={"text": "tea"}, status="pending", late=False,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return {"ok": True, "spoken": "Timer set for 3 minutes.", "timer_id": str(row.id),
            "evidence": [{"source": "owner_timer", "observed": True, "timer_id": str(row.id)}]}


class _OneCall:
    """Fake brain: one tool call, then a closing line."""

    model = "fake/brain"

    def __init__(self, *calls: ToolCall) -> None:
        self.calls = list(calls)

    async def chat_with_tools(self, messages, specs):
        if self.calls:
            return ChatResult(text="", tool_calls=[self.calls.pop(0)],
                              usage={"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.0001},
                              model="served/model-x")
        return ChatResult(text="Timer set.", usage={"prompt_tokens": 2, "completion_tokens": 1},
                          model="served/model-x")


async def test_static_worker_timer_is_saved_through_the_real_executor():
    node = _node(tool="timer.act", arguments={"op": "start", "seconds": 180, "label": "tea"})

    receipt = await run_node(node, WorkerCtx(), job_id="j")

    assert receipt.ok is True, receipt
    assert await _timers() == 1
    [check] = _durability(receipt)
    assert check["durable"] is True
    assert check["rows_verified"] >= 1


async def test_mimo_worker_timer_is_saved_and_names_the_serving_model():
    provider = _OneCall(ToolCall(id="c1", name="timer.act",
                                 arguments={"op": "start", "seconds": 120, "label": "eggs"}))

    receipt = await run_node(_node(tool=None), WorkerCtx(), job_id="j",
                             provider=provider)

    assert receipt.ok is True, receipt
    assert receipt.worker == "mimo"
    assert await _timers() == 1
    assert _durability(receipt)[0]["durable"] is True
    # Provenance is what the provider response reported, not the config.
    assert receipt.model == "served/model-x"
    assert receipt.usage["calls"] == 2
    assert receipt.usage["prompt_tokens"] == 9
    assert receipt.usage["cost"] == 0.0001


async def test_a_write_that_does_not_land_fails_the_node(monkeypatch):
    """The pre-fix failure mode: the tool flushes, the commit never lands."""

    async def _no_commit(session):
        return None

    monkeypatch.setattr(durability, "_commit", _no_commit)

    receipt = await run_node(_node(tool="timer.act"), WorkerCtx(), job_id="j",
                             execute_fn=_insert_timer)

    assert receipt.ok is False
    assert receipt.error == "not_persisted"
    assert "Timer set" not in receipt.spoken
    assert "didn't save" in receipt.spoken
    [check] = _durability(receipt)
    assert check["durable"] is False
    assert check["rows_missing"] == 1


async def test_a_raising_tool_rolls_back_and_the_next_call_still_saves():
    async def _flush_then_raise(session, name, args, **kwargs):
        await _insert_timer(session, name, args)
        raise RuntimeError("tool blew up after writing")

    calls = {"n": 0}

    async def _execute(session, name, args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return await _flush_then_raise(session, name, args)
        return await _insert_timer(session, name, args)

    provider = _OneCall(
        ToolCall(id="c1", name="timer.act", arguments={"op": "start", "seconds": 60}),
        ToolCall(id="c2", name="timer.act", arguments={"op": "start", "seconds": 90}),
    )

    receipt = await run_node(_node(tool=None), WorkerCtx(), job_id="j",
                             provider=provider, execute_fn=_execute)

    # The half-written first call is gone; the second call is durable. A
    # poisoned session would have failed the second call too.
    assert await _timers() == 1
    assert [r["ok"] for r in receipt.rounds] == [False, True]


async def test_status_says_who_did_each_step_and_whether_it_saved(monkeypatch):
    from uuid import uuid4

    from app.cognitive.delegation import delegate_proof, delegate_status_snapshot, status_brief
    from app.models import ResearchSession

    provider = _OneCall(ToolCall(id="c1", name="timer.act",
                                 arguments={"op": "start", "seconds": 120, "label": "eggs"}))
    saved = await run_node(_node(id="t1", tool=None), WorkerCtx(), job_id="j", provider=provider)

    async def _no_commit(session):
        return None

    monkeypatch.setattr(durability, "_commit", _no_commit)
    lost = await run_node(_node(id="t2", tool="timer.act"), WorkerCtx(), job_id="j",
                          execute_fn=_insert_timer)
    evidence = {"result": {"graph": {
        "receipts": [saved.model_dump(mode="json"), lost.model_dump(mode="json")],
        "verdicts": [{"node_id": "t1", "next": "accept", "state": "done"},
                     {"node_id": "t2", "next": "retry", "state": "failed"}],
    }}}

    steps = {s["node_id"]: s for s in status_brief(status="failed", budget={},
                                                   evidence=evidence)["steps"]}
    assert (steps["t1"]["worker"], steps["t1"]["model"], steps["t1"]["saved"]) == (
        "mimo", "served/model-x", True)
    assert steps["t2"]["saved"] is False

    proof = delegate_proof(evidence)
    assert proof["path"] == "graph"
    assert proof["served_by"] == ["served/model-x"]
    assert (proof["writes_checked"], proof["writes_lost"]) == (2, 1)
    assert proof["cost_usd"] == 0.0001

    row = ResearchSession(id=uuid4(), owner="master", mode="rt_delegate", status="failed",
                          question="q", goal="q", budget={}, evidence=evidence,
                          conclusion="I couldn't finish that request.")
    assert delegate_status_snapshot(row)["proof"] == proof


async def test_ev_tasks_shows_the_owner_a_lost_write(client, monkeypatch):
    from uuid import uuid4

    from app.models import ResearchSession
    from clients.cli import build_parser, delegate_tasks, format_task

    provider = _OneCall(ToolCall(id="c1", name="timer.act",
                                 arguments={"op": "start", "seconds": 120, "label": "eggs"}))
    saved = await run_node(_node(id="t1", tool=None), WorkerCtx(), job_id="j", provider=provider)

    async def _no_commit(session):
        return None

    monkeypatch.setattr(durability, "_commit", _no_commit)
    lost = await run_node(_node(id="t2", tool="timer.act"), WorkerCtx(), job_id="j",
                          execute_fn=_insert_timer)
    monkeypatch.undo()
    async with SessionLocal() as session:
        session.add(ResearchSession(
            id=uuid4(), owner="master", mode="rt_delegate", status="failed",
            question="Set two timers.", goal="Set two timers.", budget={},
            conclusion="I couldn't finish that request.",
            evidence={"result": {"graph": {
                "receipts": [saved.model_dump(mode="json"), lost.model_dump(mode="json")],
                "verdicts": [{"node_id": "t1", "next": "accept"},
                             {"node_id": "t2", "next": "retry"}],
            }}},
        ))
        await session.commit()

    [task] = (await delegate_tasks(client=client))["tasks"]
    text = "\n".join(format_task(task))

    assert "Set two timers." in text
    assert "served by served/model-x" in text
    assert "WARNING: 1 of 2 writes did NOT save" in text
    assert build_parser().parse_args(["tasks", "--limit", "3"]).limit == 3


def test_kernel_and_unstarted_jobs_claim_no_served_model():
    from app.cognitive.delegation import delegate_proof

    assert delegate_proof({}) == {"path": None}
    # The kernel turn only knows its configured model, and says so.
    assert delegate_proof({"result": {"kind": "answer", "mimo_model": "cfg/model"}}) == {
        "path": "kernel", "configured_model": "cfg/model"}
    assert delegate_proof({"result": {"lane": {}, "agent_task_id": "a1"}}) == {
        "path": "agent", "agent_task_id": "a1"}


async def test_reads_add_no_durability_noise():
    async def _read(session, name, args, **kwargs):
        return {"ok": True, "spoken": "No meetings today.", "events": []}

    receipt = await run_node(_node(tool="life.mail", tier="R"), WorkerCtx(), job_id="j",
                             execute_fn=_read)

    assert receipt.ok is True
    assert _durability(receipt) == []
