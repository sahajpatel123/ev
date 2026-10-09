"""Action recovery: prose sends park, slots fill, EV.app waits resume, brain down is loud."""
from __future__ import annotations

from uuid import UUID, uuid4

from app.cognitive.delegation import (
    _TAG,
    _with_serving_path,
    dispatch_delegate_control,
)
from app.cognitive.graph import (
    NodeState,
    SupervisorVerdict,
    TaskNode,
    VerdictNext,
    WorkerReceipt,
)
from app.cognitive.supervisor import local_verdict, supervise
from app.cognitive.worker import WorkerCtx, _computer_worker, run_node
from app.db import SessionLocal
from app.models import ResearchSession


def _node(**overrides) -> TaskNode:
    base = {"id": "n1", "label": "Act", "detail": "Do it.", "tier": "W"}
    base.update(overrides)
    return TaskNode.model_validate(base)


def _waiting_job(db_session, *, node: TaskNode, resumption: dict,
                 spoken: str) -> str:
    receipt = WorkerReceipt(node_id=node.id, ok=False, worker="router",
                            spoken=spoken, error="confirmation_required",
                            resumption=resumption)
    verdict = SupervisorVerdict(node_id=node.id, state=NodeState.BLOCKED,
                                ok=None, score=0.0, reasons=[spoken],
                                next=VerdictNext.ASK_OWNER)
    job_id = uuid4()
    db_session.add(ResearchSession(
        id=job_id, question="Do it.", owner="master", mode=_TAG,
        status="waiting", conclusion=spoken,
        budget={"live_session_id": "live-1", "device_id": "dev-1"},
        evidence={"result": {"graph": {
            "receipts": [receipt.model_dump(mode="json")],
            "verdicts": [verdict.model_dump(mode="json")],
            "evidence": {"plan": [node.model_dump(mode="json")]},
        }}}))
    return str(job_id)


# --------------------------------------------------------------------------- #
# Prose-written tier-D sends park instead of dead-ending
# --------------------------------------------------------------------------- #


async def test_prose_send_detail_parks_with_exact_question(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return True

    async def fake_resolve(to, **kwargs):
        return {"status": "unique", "display": "Mom",
                "peer": {"phone": "+1555"}, "chat_ref": "c1"}

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(web, "resolve", fake_resolve)
    node = _node(tier="D", label="Message Mom",
                 detail="Send 'I'll be late' to Mom on WhatsApp.")
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "confirmation_required"
    assert "Mom" in receipt.spoken and "late" in receipt.spoken
    assert receipt.resumption is not None
    assert receipt.resumption["op"] == "approved_action"


async def test_inquiry_detail_stays_blocked():
    node = _node(tier="D", label="Check mail",
                 detail="Did Mom reply to my mail?")
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "tier_d_requires_approval"
    assert receipt.resumption is None


async def test_nonsend_detail_stays_blocked():
    node = _node(tier="D", label="Wipe cache", detail="Delete the render cache.")
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "tier_d_requires_approval"
    assert receipt.resumption is None


# --------------------------------------------------------------------------- #
# Slot-fill: missing who/what becomes a question, then a parked approval
# --------------------------------------------------------------------------- #


async def test_static_send_without_slots_attaches_slot_resumption(monkeypatch):
    async def fake_execute(session, tool, arguments, **kwargs):
        return {"ok": False, "error": "missing_recipient_or_body",
                "spoken": "Who should I message, and what should I say?"}

    from app.cognitive.worker import _static_worker

    receipt = await _static_worker(
        _node(tool="life.send", arguments={}), ctx=WorkerCtx(),
        execute_fn=fake_execute)
    assert receipt.error == "missing_recipient_or_body"
    assert receipt.resumption is not None
    assert receipt.resumption["op"] == "answer_slots"


def test_missing_slots_verdict_asks_owner():
    receipt = WorkerReceipt(node_id="n1", ok=False, worker="static",
                            spoken="Who should I message?",
                            error="missing_recipient_or_body")
    assert local_verdict(_node(), receipt).next is VerdictNext.ASK_OWNER


async def test_answer_slots_parks_filled_send_for_approval(db_session):
    node = _node(tool="life.send", arguments={})
    job_id = _waiting_job(
        db_session, node=node,
        resumption={"node": node.model_dump(mode="json"),
                    "op": "answer_slots", "params": {}},
        spoken="Who should I message, and what should I say?")
    await db_session.commit()
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id,
        owner_transcript="tell Mom I'll be late")
    assert out["ok"] is True
    assert "Mom" in out["spoken"]
    async with SessionLocal() as db:
        job = await db.get(ResearchSession, UUID(job_id))
        assert job is not None
        # Still waiting — now on the approval question, not the slots.
        assert job.status == "waiting"
        assert "Mom" in (job.conclusion or "")


async def test_answer_slots_junk_keeps_waiting(db_session):
    node = _node(tool="life.send", arguments={})
    job_id = _waiting_job(
        db_session, node=node,
        resumption={"node": node.model_dump(mode="json"),
                    "op": "answer_slots", "params": {}},
        spoken="Who should I message, and what should I say?")
    await db_session.commit()
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id, owner_transcript="hmm, not sure")
    assert out["ok"] is False
    assert "still need" in out["spoken"]
    async with SessionLocal() as db:
        job = await db.get(ResearchSession, UUID(job_id))
        assert job is not None
        assert job.status == "waiting"


async def test_answer_slots_denial_cancels(db_session):
    node = _node(tool="life.send", arguments={})
    job_id = _waiting_job(
        db_session, node=node,
        resumption={"node": node.model_dump(mode="json"),
                    "op": "answer_slots", "params": {}},
        spoken="Who should I message, and what should I say?")
    await db_session.commit()
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id, owner_transcript="no, never mind")
    assert out["ok"] is True
    assert "left that step undone" in out["spoken"]


# --------------------------------------------------------------------------- #
# EV.app gate: wait resumably instead of failing
# --------------------------------------------------------------------------- #


async def test_computer_worker_parks_ev_app_gate():
    async def fake_execute(session, tool, arguments, **kwargs):
        if tool == "look.capture":
            return {"ok": True, "spoken": "A desktop."}
        return {"ok": False, "error": "EV.app is not connected for Mac UI control",
                "spoken": "I need the EV app live."}

    node = _node(tool="computer.perform_effect",
                 arguments={"effect": "click Play in Music"})
    receipt = await _computer_worker(node, ctx=WorkerCtx(), execute_fn=fake_execute)
    assert receipt.error == "ev_app_not_connected"
    assert "say go ahead" in receipt.spoken
    assert receipt.resumption is not None
    assert receipt.resumption["op"] == "retry_node"


async def test_ev_app_gate_verdict_asks_owner():
    receipt = WorkerReceipt(node_id="n1", ok=False, worker="computer",
                            spoken="Open the EV app.", error="ev_app_not_connected")
    verdict = await supervise(_node(), receipt)
    assert verdict.next is VerdictNext.ASK_OWNER


async def test_answer_go_ahead_retries_node(db_session, monkeypatch):
    async def fake_run_node(node, ctx, **kwargs):
        return WorkerReceipt(
            node_id=node.id, ok=True, worker="computer",
            spoken="Playing now.",
            evidence=[{"tool": "computer.perform_effect", "verified": True}])

    monkeypatch.setattr("app.cognitive.worker.run_node", fake_run_node)
    monkeypatch.setattr(
        "app.gateway.decider.decider_available", lambda: False)
    node = _node(tool="computer.perform_effect",
                 arguments={"effect": "click Play in Music"})
    job_id = _waiting_job(
        db_session, node=node,
        resumption={"node": node.model_dump(mode="json"),
                    "op": "retry_node", "params": {}},
        spoken="Open it and say go ahead.")
    await db_session.commit()
    out = await dispatch_delegate_control(
        operation="answer", live_session_id="live-1", device_id="dev-1",
        actor="master", job_id=job_id, owner_transcript="go ahead")
    assert out["ok"] is True
    async with SessionLocal() as db:
        job = await db.get(ResearchSession, UUID(job_id))
        assert job is not None
        assert job.status == "answered"
        assert "Playing now." in (job.conclusion or "")


# --------------------------------------------------------------------------- #
# Submit-time brain flag: the fallback is visible at admission
# --------------------------------------------------------------------------- #


def test_serving_path_flags_brain_down(monkeypatch):
    from app.cognitive import telemetry

    monkeypatch.setattr("app.cognitive.graph.delegate_graph_active", lambda: True)
    monkeypatch.setattr(
        "app.gateway.roles.text_role_available", lambda: False)
    telemetry.reset_for_tests()
    out = _with_serving_path({"ok": True})
    assert out is not None
    assert out["graph_expected"] is True
    assert out["brain_available"] is False
    assert telemetry.snapshot()["delegate_admitted_brain_down"] == 1


def test_serving_path_quiet_when_brain_up(monkeypatch):
    monkeypatch.setattr("app.cognitive.graph.delegate_graph_active", lambda: True)
    monkeypatch.setattr(
        "app.gateway.roles.text_role_available", lambda: True)
    out = _with_serving_path({"ok": True})
    assert out is not None
    assert out["brain_available"] is True
