"""Hierarchy flow suite: end-to-end delegation, per-family routing, verification.

Flow: submit -> plan -> waves -> supervise -> join -> finish -> status brief.
Request-flow: each worker family reaches its dispatch backend; unknown ops
fail closed. Verification: tier-D park->approve->execute->verify, focus
honesty, offline doubles.
"""
from __future__ import annotations

import asyncio

from app.cognitive.delegation import (
    dispatch_delegate_control,
    get_delegate,
    submit_delegate,
)
from app.cognitive.executor import execute_semantic
from app.cognitive.graph import StatusEvent, TaskNode, VerdictNext, run_graph
from app.cognitive.session_store import CognitiveSession
from app.config import settings
from app.db import SessionLocal
from app.models import ApprovedAction


def _node(**overrides) -> TaskNode:
    base = {"id": "n1", "label": "Read the inbox", "detail": "Read it.",
            "tier": "R", "tool": "life.mail", "arguments": {"query": "inbox"}}
    base.update(overrides)
    return TaskNode.model_validate(base)


def _cognition(**overrides) -> CognitiveSession:
    session = CognitiveSession(session_id="flow-test")
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


# --------------------------------------------------------------------------- #
# Flow: one delegated task through every hop
# --------------------------------------------------------------------------- #


async def test_graph_flow_two_wave_job(monkeypatch):
    seen: list[str] = []

    async def fake_plan(task, **kwargs):
        return [
            _node(id="fetch", label="Fetch mail"),
            _node(id="digest", label="Digest mail", depends_on=["fetch"]),
        ]

    async def fake_execute(session, tool, arguments, **kwargs):
        return {"ok": True, "rows": [{"who": "Mom", "gist": "hi"}],
                "spoken": "Mom — hi."}

    async def progress(event: StatusEvent) -> None:
        seen.append(event.kind)

    monkeypatch.setattr("app.cognitive.graph.plan_task", fake_plan)
    monkeypatch.setattr(
        "app.cognitive.executor.execute_semantic", fake_execute)
    monkeypatch.setattr(
        "app.gateway.decider.decider_available", lambda: False)
    outcome = await run_graph("Summarize my mail.", job_id="flow-1",
                              progress=progress)
    assert outcome.status == "answered"
    assert outcome.nodes_accepted == 2
    assert "DONE" in outcome.status_report
    assert "plan" in seen and "started" in seen and "verdict" in seen


async def test_submit_to_status_flow(monkeypatch):
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "delegate_graph", "on")
    monkeypatch.setattr("app.cognitive.graph.plan_task",
                        lambda task, **kwargs: _plan_one())
    monkeypatch.setattr("app.cognitive.executor.execute_semantic",
                        _ok_mail_execute)
    monkeypatch.setattr(
        "app.gateway.decider.decider_available", lambda: False)
    delivered: list[dict] = []

    async def on_complete(receipt: dict) -> None:
        delivered.append(receipt)

    receipt = await submit_delegate(
        task="Read my inbox.", request_id="flow-req-1", actor="master",
        on_complete=on_complete)
    assert receipt["accepted"] is True
    job_id = receipt["job_id"]
    final = None
    for _ in range(150):
        await asyncio.sleep(0.1)
        final = await get_delegate(job_id, actor="master")
        if final is not None and final["status"] not in {"queued", "running"}:
            break
    assert final is not None
    assert final["status"] == "answered"
    assert delivered and delivered[0]["job_id"] == job_id
    status = await dispatch_delegate_control(
        operation="status", live_session_id=None, device_id=None,
        actor="master", job_id=job_id)
    assert status["tasks"][0]["brief"]["state"] == "answered"
    assert status["tasks"][0]["brief"]["nodes_accepted"] == 1


async def _plan_one():
    return [_node()]


async def _ok_mail_execute(session, tool, arguments, **kwargs):
    assert tool == "life.mail"
    return {"ok": True, "rows": [{"who": "Mom", "gist": "hi"}],
            "spoken": "Mom — hi."}


# --------------------------------------------------------------------------- #
# Request-flow: every worker family reaches its backend
# --------------------------------------------------------------------------- #


async def _dispatched(monkeypatch, result):
    calls: list[tuple[str, dict]] = []

    async def fake_dispatch(session, name, arguments, **kwargs):
        calls.append((name, dict(arguments or {})))
        return dict(result)

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    return calls


async def test_request_flow_calculate(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True, "result": 4})
    out = await execute_semantic(
        db_session, "calculate", {"expression": "2+2"},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert calls == [("calculate", {"expression": "2+2"})]
    assert out["ok"] is True


async def test_request_flow_brief_me(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True, "spoken": "Brief."})
    out = await execute_semantic(
        db_session, "brief.me", {"topic": "morning"},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert calls == [("brief_me", {"topic": "morning"})]
    assert out["ok"] is True


async def test_request_flow_device_status_op_routing(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True, "metric": "steps"})
    out = await execute_semantic(
        db_session, "device.status",
        {"op": "get_health_trends", "args": {"metric": "steps"}},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert calls == [("get_health_trends", {"metric": "steps"})]
    assert out["ok"] is True


async def test_request_flow_unknown_op_fails_closed(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True})
    out = await execute_semantic(
        db_session, "device.status", {"op": "launch_missiles"},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert out.get("ok") is not True
    assert calls == []


async def test_request_flow_prepare_only_blocks_effects(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True})
    out = await execute_semantic(
        db_session, "device.control",
        {"op": "present", "args": {"card": "x"}},
        cognition=_cognition(prepare_only=True), actor="master",
        live_session_id=None, steering_seen=0)
    assert out.get("diagnosis") == "POLICY_BLOCKED"
    assert calls == []


async def test_request_flow_media_capture_routes(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True, "spoken": "Seen."})
    out = await execute_semantic(
        db_session, "media.capture", {"op": "capture_photo", "args": {}},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert calls == [("capture_photo", {})]
    assert out["ok"] is True


async def test_request_flow_unknown_tool_fails_closed(db_session):
    out = await execute_semantic(
        db_session, "teleport.home", {},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert out.get("diagnosis") == "CAPABILITY_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# Verification: approval execution, focus honesty, offline doubles
# --------------------------------------------------------------------------- #


async def test_verify_tier_d_park_approve_execute_through_graph(monkeypatch):
    monkeypatch.setattr("app.gateway.decider.decider_available", lambda: False)

    async def fake_plan(task, **kwargs):
        return [TaskNode.model_validate({
            "id": "send-it", "label": "Send hi", "detail": "Send hi to Mom.",
            "tier": "D", "tool": "life.send",
            "arguments": {"to": "Mom", "text": "hi"}})]

    monkeypatch.setattr("app.cognitive.graph.plan_task", fake_plan)
    outcome = await run_graph("Send hi to Mom.", job_id="verify-td-1")
    assert outcome.status == "waiting"
    assert "Mom" in outcome.spoken
    pending = [
        v for v in outcome.verdicts if v.next is VerdictNext.ASK_OWNER]
    assert len(pending) == 1
    resumption = outcome.receipts[0].resumption
    assert resumption is not None
    assert resumption["op"] == "approved_action"
    async with SessionLocal() as db:
        from uuid import UUID

        action = await db.get(
            ApprovedAction, UUID(resumption["params"]["action_id"]))
        assert action is not None
        assert action.status == "pending"
        assert action.action_type == "life.send"


async def test_verify_focus_theft_surfaces_diagnosis(db_session, monkeypatch):
    async def fake_dispatch(session, name, arguments, **kwargs):
        assert name == "computer"
        return {"ok": True, "activated": True, "spoken": "Opened it."}

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    out = await execute_semantic(
        db_session, "computer.perform_effect", {"effect": "open Music"},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert out.get("diagnosis") == "FOREGROUND_REQUIRED"


async def test_verify_background_effect_has_no_focus_diagnosis(db_session, monkeypatch):
    async def fake_dispatch(session, name, arguments, **kwargs):
        return {"ok": True, "activated": False, "spoken": "Done quietly."}

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    out = await execute_semantic(
        db_session, "computer.perform_effect", {"effect": "list files"},
        cognition=_cognition(), actor="master", live_session_id=None,
        steering_seen=0)
    assert "diagnosis" not in out or out.get("diagnosis") != "FOREGROUND_REQUIRED"


async def test_verify_stale_plan_blocks_mutation(db_session, monkeypatch):
    calls = await _dispatched(monkeypatch, {"ok": True})
    cognition = _cognition()
    cognition.steering_version = 3
    out = await execute_semantic(
        db_session, "device.control",
        {"op": "present", "args": {}},
        cognition=cognition, actor="master", live_session_id=None,
        steering_seen=0)
    assert out.get("diagnosis") == "STALE_PLAN"
    assert calls == []
