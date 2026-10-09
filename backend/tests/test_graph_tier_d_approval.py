"""Graph tier-D approvals: park on the ledger, resume by action_id, one receipt."""
from __future__ import annotations

from uuid import UUID, uuid4

from app.cognitive.delegation import _TAG, dispatch_delegate_control
from app.cognitive.graph import (
    NodeState,
    SupervisorVerdict,
    TaskNode,
    TaskTier,
    VerdictNext,
    WorkerReceipt,
)
from app.cognitive.supervisor import decide_next, local_verdict, supervise
from app.cognitive.worker import WorkerCtx, run_node
from app.db import SessionLocal
from app.ev.messaging.approval import (
    approve_graph_action,
    delivery_receipt,
    park_graph_action,
    question_for_action,
)
from app.models import ApprovedAction, ResearchSession


def _node(**overrides) -> TaskNode:
    base = {"id": "send-it", "label": "Send the update", "detail": "Send it.",
            "tier": "D"}
    base.update(overrides)
    return TaskNode.model_validate(base)


def _receipt(**overrides) -> WorkerReceipt:
    base: dict = {"node_id": "send-it", "ok": True, "worker": "approved",
                  "evidence": [{"tool": "life.send", "sent": True}]}
    base.update(overrides)
    return WorkerReceipt.model_validate(base)


# --------------------------------------------------------------------------- #
# Parking: idempotent per ticket, distinct tickets coexist
# --------------------------------------------------------------------------- #


async def test_park_refreshes_identical_ticket(db_session):
    first = await park_graph_action(
        db_session, node_id="send-it", label="Send the update",
        tool="life.send", arguments={"to": "Mom", "text": "hi"},
        question='Should I send "hi" to Mom?', actor="graph",
        live_session_id="live-1", device_id="dev-1")
    await db_session.commit()
    second = await park_graph_action(
        db_session, node_id="send-it", label="Send the update",
        tool="life.send", arguments={"to": "Mom", "text": "hi"},
        question='Should I send "hi" to Mom?', actor="graph",
        live_session_id="live-1", device_id="dev-1")
    await db_session.commit()
    assert second.id == first.id
    assert second.status == "pending"


async def test_park_keeps_distinct_tickets(db_session):
    one = await park_graph_action(
        db_session, node_id="a", label="A", tool="life.send",
        arguments={"to": "Mom", "text": "hi"}, question="Q1?")
    other = await park_graph_action(
        db_session, node_id="b", label="B", tool="phone.call",
        arguments={"to": "Dad"}, question="Q2?")
    await db_session.commit()
    assert one.id != other.id
    assert one.status == other.status == "pending"


async def test_question_for_action_prefers_stored_question(db_session):
    action = await park_graph_action(
        db_session, node_id="a", label="A", tool="life.send",
        arguments={"to": "Mom", "text": "hi"}, question="Stored?")
    assert question_for_action(action) == "Stored?"


# --------------------------------------------------------------------------- #
# Worker tier-D branch: park when specified, block honestly when not
# --------------------------------------------------------------------------- #


async def test_run_node_parks_single_tool_tier_d():
    node = _node(tool="life.send", arguments={"to": "Mom", "text": "hi"})
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.ok is False
    assert receipt.error == "confirmation_required"
    assert receipt.spoken == 'Should I send "hi" to Mom?'
    assert receipt.resumption is not None
    assert receipt.resumption["op"] == "approved_action"
    action_id = receipt.resumption["params"]["action_id"]
    async with SessionLocal() as db:
        action = await db.get(ApprovedAction, UUID(action_id))
        assert action is not None
        assert action.status == "pending"
        assert action.action_type == "send_message"


async def test_run_node_blocks_tool_less_tier_d_without_parking():
    receipt = await run_node(_node(), WorkerCtx(actor="master"), job_id="j")
    assert receipt.ok is False
    assert receipt.error == "tier_d_requires_approval"
    assert receipt.resumption is None


async def test_run_node_blocks_read_only_tool_tier_d():
    node = _node(tool="life.mail", arguments={"query": "inbox"})
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "tier_d_requires_approval"


# --------------------------------------------------------------------------- #
# Approval executes through the runtime exactly once
# --------------------------------------------------------------------------- #


async def test_approve_executes_and_writes_delivery_receipt(db_session, monkeypatch):
    calls: list[str] = []

    async def fake_decide(session, action_id, *, actor, decision, reason=None,
                          device_id=None, reverify_token=None):
        calls.append(decision)
        action = await session.get(ApprovedAction, action_id)
        assert action is not None
        action.status = "approved"
        action.status = "executed"
        action.result = {"sent": True, "verified_in_thread": True,
                         "message_id": "m1", "channel": "whatsapp",
                         "spoken": "Sent it."}
        return action

    monkeypatch.setattr("app.services.runtime.decide_action", fake_decide)
    action = await park_graph_action(
        db_session, node_id="send-it", label="S", tool="life.send",
        arguments={"to": "Mom", "text": "hi"}, question="Q?")
    await db_session.commit()
    out = await approve_graph_action(db_session, action, actor="master")
    assert out["ok"] is True
    assert out["sent"] is True
    assert out["delivery_receipt"]["verified"] is True
    assert out["delivery_receipt"]["message_id"] == "m1"
    assert calls == ["approve"]
    assert action.result is not None
    assert action.result["delivery_receipt"]["tool"] == "life.send"


async def test_approve_rejects_tampered_arguments(db_session, monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError("tampered ticket must not execute")

    monkeypatch.setattr("app.services.runtime.decide_action", _boom)
    action = await park_graph_action(
        db_session, node_id="send-it", label="S", tool="life.send",
        arguments={"to": "Mom", "text": "hi"}, question="Q?")
    await db_session.commit()
    payload = dict(action.payload)
    payload["text"] = "swapped"
    action.payload = payload
    out = await approve_graph_action(db_session, action, actor="master")
    assert out["ok"] is False
    assert "no longer matches" in out["spoken"]


async def test_approve_rejects_wrong_kind(db_session):
    action = ApprovedAction(action_type="send_message", title="t", payload={},
                            status="pending", requested_by="x")
    db_session.add(action)
    await db_session.commit()
    out = await approve_graph_action(db_session, action, actor="master")
    assert out["ok"] is False


def test_delivery_receipt_maps_each_family():
    assert delivery_receipt("x", {"sent": True, "verified_in_thread": True})["verified"] is True
    assert delivery_receipt("x", {"opened": True})["verified"] is True
    assert delivery_receipt("x", {"accepted_by_client": True})["verified"] is True
    assert delivery_receipt("x", {})["verified"] is None
    assert delivery_receipt("x", {"sent": True})["retry_safe"] is False
    assert delivery_receipt("x", {})["retry_safe"] is True


# --------------------------------------------------------------------------- #
# Post-approval verdicts judge evidence; pre-approval verdicts still park
# --------------------------------------------------------------------------- #


def test_local_verdict_still_parks_unapproved_tier_d():
    verdict = local_verdict(_node(), _receipt(ok=False, error="tier_d_requires_approval"))
    assert verdict.next is VerdictNext.ASK_OWNER


def test_local_verdict_accepts_approved_evidenced_execution():
    verdict = local_verdict(_node(), _receipt(), approved=True)
    assert verdict.next is VerdictNext.ACCEPT


def test_decide_next_matrix_unchanged_without_approved():
    assert decide_next(state=NodeState.DONE, ok=True, score=0.9, has_evidence=True,
                       tier=TaskTier.D, tier_blocked=True, attempt=1) is VerdictNext.ASK_OWNER
    assert decide_next(state=NodeState.DONE, ok=True, score=0.9, has_evidence=True,
                       tier=TaskTier.D, tier_blocked=True, attempt=1,
                       approved=True) is VerdictNext.ACCEPT


async def test_supervise_judges_approved_execution_without_reparking():
    verdict = await supervise(_node(), _receipt(), approved=True)
    assert verdict.next is VerdictNext.ACCEPT


async def test_supervise_parks_unapproved_tier_d_confirmation():
    receipt = _receipt(ok=False, error="confirmation_required")
    receipt.evidence = []
    verdict = await supervise(_node(), receipt)
    assert verdict.next is VerdictNext.ASK_OWNER


# --------------------------------------------------------------------------- #
# Answer door: yes resumes the ticket, no denies it
# --------------------------------------------------------------------------- #


async def _waiting_job_with_ticket(db_session, *, node_id="send-it") -> tuple[str, str]:
    action = await park_graph_action(
        db_session, node_id=node_id, label="Send the update", tool="life.send",
        arguments={"to": "Mom", "text": "hi"}, question='Should I send "hi" to Mom?',
        actor="master", live_session_id="live-1", device_id="dev-1")
    await db_session.commit()
    node = _node(id=node_id, tool="life.send", arguments={"to": "Mom", "text": "hi"})
    receipt = WorkerReceipt(node_id=node_id, ok=False, worker="router",
                            spoken='Should I send "hi" to Mom?',
                            error="confirmation_required",
                            resumption={"node": node.model_dump(mode="json"),
                                        "op": "approved_action",
                                        "params": {"action_id": str(action.id)}})
    verdict = SupervisorVerdict(node_id=node_id, state=NodeState.BLOCKED, ok=None,
                                score=0.0, reasons=['Should I send "hi" to Mom?'],
                                next=VerdictNext.ASK_OWNER)
    job_id = uuid4()
    db_session.add(ResearchSession(
        id=job_id, question="Send hi to Mom.", owner="master", mode=_TAG,
        status="waiting", conclusion='Should I send "hi" to Mom?',
        budget={"live_session_id": "live-1", "device_id": "dev-1"},
        evidence={"result": {"graph": {
            "receipts": [receipt.model_dump(mode="json")],
            "verdicts": [verdict.model_dump(mode="json")],
            "evidence": {"plan": [node.model_dump(mode="json")]},
        }}}))
    await db_session.commit()
    return str(job_id), str(action.id)


async def test_answer_yes_resumes_approved_ticket(db_session, monkeypatch):
    async def fake_decide(session, action_id, *, actor, decision, reason=None,
                          device_id=None, reverify_token=None):
        action = await session.get(ApprovedAction, action_id)
        assert action is not None
        action.status = "approved"
        action.status = "executed"
        action.result = {"sent": True, "verified_in_thread": True,
                         "message_id": "m1", "spoken": "Sent it."}
        return action

    monkeypatch.setattr("app.services.runtime.decide_action", fake_decide)
    job_id, action_id = await _waiting_job_with_ticket(db_session)
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id, owner_transcript="yes, send it")
    assert out["ok"] is True
    assert "Sent it." in out["spoken"]
    async with SessionLocal() as db:
        action = await db.get(ApprovedAction, UUID(action_id))
        assert action is not None
        assert action.status == "executed"
        job = await db.get(ResearchSession, UUID(job_id))
        assert job is not None
        assert job.status == "answered"


async def test_answer_no_denies_ticket_and_fails_job(db_session, monkeypatch):
    async def fake_decide(session, action_id, *, actor, decision, reason=None,
                          device_id=None, reverify_token=None):
        action = await session.get(ApprovedAction, action_id)
        assert action is not None
        action.status = "denied"
        action.denied_reason = reason
        return action

    monkeypatch.setattr("app.services.runtime.decide_action", fake_decide)
    job_id, action_id = await _waiting_job_with_ticket(db_session)
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id, owner_transcript="no, don't")
    assert out["ok"] is True
    assert "left that step undone" in out["spoken"]
    async with SessionLocal() as db:
        action = await db.get(ApprovedAction, UUID(action_id))
        assert action is not None
        assert action.status == "denied"
        job = await db.get(ResearchSession, UUID(job_id))
        assert job is not None
        assert job.status == "failed"
