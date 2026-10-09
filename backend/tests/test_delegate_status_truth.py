"""Status truth: verdict-gated completion, per-step briefs, no silent drops."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.cognitive.delegation import (
    _TAG,
    FINAL_DELEGATE_STATES,
    dispatch_delegate_control,
    mark_delegate_announced,
    status_brief,
)
from app.cognitive.graph import (
    NodeState,
    SupervisorVerdict,
    TaskNode,
    VerdictNext,
    WorkerReceipt,
    join_outcome,
    shift_report,
)
from app.db import SessionLocal
from app.models import ResearchSession


def _verdict(node_id: str, nxt: VerdictNext, **overrides) -> SupervisorVerdict:
    base: dict = {"node_id": node_id, "state": NodeState.DONE, "ok": True,
                  "score": 0.9, "reasons": [], "next": nxt}
    base.update(overrides)
    return SupervisorVerdict(**base)


def _receipt(node_id: str = "n1", **overrides) -> WorkerReceipt:
    base: dict = {"node_id": node_id, "ok": True, "worker": "static",
                  "evidence": [{"tool": "memory.search", "ok": True}]}
    base.update(overrides)
    return WorkerReceipt.model_validate(base)


def _node(node_id: str) -> TaskNode:
    return TaskNode.model_validate(
        {"id": node_id, "label": f"Step {node_id}", "detail": "Do it.", "tier": "R"}
    )


# --------------------------------------------------------------------------- #
# Single completion vocabulary
# --------------------------------------------------------------------------- #


def test_final_states_cover_every_terminal_and_parked_state():
    for state in ("answered", "completed", "complete", "waiting",
                  "failed", "cancelled", "interrupted"):
        assert state in FINAL_DELEGATE_STATES
    assert "queued" not in FINAL_DELEGATE_STATES
    assert "running" not in FINAL_DELEGATE_STATES


# --------------------------------------------------------------------------- #
# Verdict-gated completion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([VerdictNext.ACCEPT, VerdictNext.ACCEPT], "answered"),
        ([VerdictNext.ACCEPT, VerdictNext.ESCALATE], "failed"),
        ([VerdictNext.ACCEPT, VerdictNext.ASK_OWNER], "waiting"),
        ([VerdictNext.ESCALATE], "failed"),
        ([], "failed"),
    ],
)
def test_answered_requires_every_node_accepted(verdicts, expected):
    made = [_verdict(f"n{i}", nxt) for i, nxt in enumerate(verdicts)]
    receipts = [_receipt(f"n{i}") for i in range(len(made))]
    out = join_outcome(job_id="j", receipts=receipts, verdicts=made,
                       nodes_total=len(made))
    assert out.status == expected


def test_join_outcome_speaks_retried_node_once():
    verdict = _verdict("n1", VerdictNext.ACCEPT)
    out = join_outcome(
        job_id="j",
        receipts=[_receipt(spoken="First try."), _receipt(spoken="Fixed it.")],
        verdicts=[verdict], nodes_total=1,
    )
    assert out.status == "answered"
    assert out.spoken == "Fixed it."


def test_shift_report_marks_match_verdict_fates():
    nodes = [_node("a"), _node("b"), _node("c")]
    receipts = [_receipt("a"), _receipt("b", ok=False), _receipt("c", ok=False)]
    verdicts = [
        _verdict("a", VerdictNext.ACCEPT),
        _verdict("b", VerdictNext.ASK_OWNER, state=NodeState.BLOCKED, ok=None,
                 score=0.0, reasons=["Approve?"]),
        _verdict("c", VerdictNext.ESCALATE, state=NodeState.FAILED, ok=False),
    ]
    report = shift_report(receipts, verdicts, nodes)
    assert "- DONE: Step a" in report
    assert "- NEEDS_OWNER: Step b" in report
    assert "- NOT_DONE: Step c" in report


# --------------------------------------------------------------------------- #
# Per-step status briefs (pure builder)
# --------------------------------------------------------------------------- #


def _graph_evidence() -> dict:
    return {"result": {"graph": {
        "receipts": [_receipt("a").model_dump(mode="json"),
                     _receipt("b", ok=False).model_dump(mode="json")],
        "verdicts": [_verdict("a", VerdictNext.ACCEPT).model_dump(mode="json"),
                     _verdict("b", VerdictNext.ESCALATE, state=NodeState.FAILED,
                              ok=False).model_dump(mode="json")],
    }}}


def test_status_brief_marks_steps_from_verdicts():
    brief = status_brief(
        status="failed",
        budget={"status_events": [{"text": "Planned 2 steps"}, {"text": "a: done"}]},
        evidence=_graph_evidence(),
        conclusion="I couldn't finish that request.",
    )
    assert brief["state"] == "failed"
    assert [(s["node_id"], s["mark"]) for s in brief["steps"]] == [
        ("a", "DONE"), ("b", "NOT_DONE")]
    assert brief["nodes_accepted"] == 1
    assert brief["recent_events"] == ["Planned 2 steps", "a: done"]
    assert brief["announced"] is False
    assert brief["waiting_question"] is None


def test_status_brief_without_graph_invents_no_steps():
    brief = status_brief(status="running", budget={}, evidence={})
    assert brief["state"] == "working"
    assert brief["steps"] == []
    assert brief["nodes_total"] == 0


def test_status_brief_waiting_carries_question():
    brief = status_brief(status="waiting", budget={}, evidence={},
                         conclusion="Approve delete?")
    assert brief["state"] == "waiting_owner"
    assert brief["waiting_question"] == "Approve delete?"


# --------------------------------------------------------------------------- #
# Durable delivery: announced flag + status-op redelivery
# --------------------------------------------------------------------------- #


async def _store_row(**overrides) -> str:
    job_id = uuid4()
    base: dict = {"id": job_id, "question": "Do delegated work.", "owner": "master",
                  "mode": _TAG, "status": "answered", "conclusion": "Did it.",
                  "budget": {"live_session_id": "live-1", "device_id": "dev-1"},
                  "evidence": {}}
    base.update(overrides)
    async with SessionLocal() as db:
        db.add(ResearchSession(**base))
        await db.commit()
    return str(job_id)


async def test_mark_announced_first_channel_wins():
    job_id = await _store_row()
    await mark_delegate_announced(job_id, channel="speech")
    await mark_delegate_announced(job_id, channel="status")
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, UUID(job_id))
        assert row is not None
        assert (row.budget or {}).get("delivery") == {
            "announced": True, "channel": "speech"}


async def test_mark_announced_ignores_bad_ids():
    await mark_delegate_announced("not-a-uuid", channel="speech")


async def test_status_op_resurfaces_unannounced_terminal_once():
    job_id = await _store_row(evidence=_graph_evidence())
    first = await dispatch_delegate_control(
        operation="status", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id)
    assert first["ok"] is True
    assert first["spoken"].startswith("You missed this result: ")
    assert first["tasks"][0]["brief"]["announced"] is True
    assert [(s["node_id"], s["mark"]) for s in first["tasks"][0]["brief"]["steps"]] == [
        ("a", "DONE"), ("b", "NOT_DONE")]
    second = await dispatch_delegate_control(
        operation="status", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id)
    assert not second["spoken"].startswith("You missed this result: ")


async def test_status_op_empty_conversation_keeps_honest_empty():
    result = await dispatch_delegate_control(
        operation="status", live_session_id="nope", device_id="nope", actor="master")
    assert result["tasks"] == []
    assert result["spoken"] == "There are no assigned tasks for this conversation."
