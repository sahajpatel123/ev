"""Offline regressions for the delegate graph (planner -> supervisors -> workers)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.cognitive import graph as graph_mod
from app.cognitive.graph import (
    GraphPlanError,
    GraphUnavailable,
    NodeState,
    StatusEvent,
    StatusThrottler,
    SupervisorVerdict,
    TaskNode,
    TaskTier,
    VerdictNext,
    WorkerReceipt,
    delegate_graph_active,
    execution_waves,
    join_outcome,
    plan_task,
    run_graph,
    validate_dag,
)
from app.cognitive.supervisor import decide_next, local_verdict, supervise
from app.cognitive.worker import WorkerCtx, fast_path_eligible, run_node
from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.db import SessionLocal
from app.models import ResearchSession


def _node(**overrides) -> TaskNode:
    base = {"id": "n1", "label": "Do a thing", "detail": "Do it well.", "tier": "R"}
    base.update(overrides)
    return TaskNode.model_validate(base)


def _receipt(**overrides) -> WorkerReceipt:
    base: dict = {"node_id": "n1", "ok": True, "worker": "static",
                  "evidence": [{"tool": "memory.search", "ok": True}]}
    base.update(overrides)
    return WorkerReceipt.model_validate(base)


# --------------------------------------------------------------------------- #
# Flag + DAG validation
# --------------------------------------------------------------------------- #


def test_graph_flag_defaults_off(monkeypatch):
    monkeypatch.setattr(settings, "delegate_graph", "off")
    assert delegate_graph_active() is False
    monkeypatch.setattr(settings, "delegate_graph", "on")
    assert delegate_graph_active() is True


def test_validate_dag_orders_dependencies():
    nodes = validate_dag(
        [
            {"id": "b", "label": "B", "detail": "second", "tier": "R", "depends_on": ["a"]},
            {"id": "a", "label": "A", "detail": "first", "tier": "R"},
        ],
        max_nodes=8,
    )
    assert [n.id for n in nodes] == ["a", "b"]


@pytest.mark.parametrize(
    "raw",
    [
        [{"id": "a", "label": "A", "detail": "x", "tier": "R"},
         {"id": "a", "label": "A2", "detail": "y", "tier": "R"}],
        [{"id": "a", "label": "A", "detail": "x", "tier": "R", "depends_on": ["ghost"]}],
        [{"id": "a", "label": "A", "detail": "x", "tier": "R", "depends_on": ["a"]}],
        [{"id": "a", "label": "A", "detail": "x", "tier": "R", "depends_on": ["b"]},
         {"id": "b", "label": "B", "detail": "y", "tier": "R", "depends_on": ["a"]}],
        [],
    ],
)
def test_validate_dag_rejects_bad_plans(raw):
    with pytest.raises(GraphPlanError):
        validate_dag(raw, max_nodes=8)


def test_validate_dag_enforces_cap():
    raw = [{"id": f"n{i}", "label": "L", "detail": "x", "tier": "R"} for i in range(9)]
    with pytest.raises(GraphPlanError):
        validate_dag(raw, max_nodes=8)


def test_execution_waves_group_independents():
    nodes = validate_dag(
        [
            {"id": "a", "label": "A", "detail": "x", "tier": "R"},
            {"id": "b", "label": "B", "detail": "x", "tier": "R"},
            {"id": "c", "label": "C", "detail": "x", "tier": "R", "depends_on": ["a", "b"]},
        ],
        max_nodes=8,
    )
    waves = execution_waves(nodes)
    assert [[n.id for n in wave] for wave in waves] == [["a", "b"], ["c"]]


# --------------------------------------------------------------------------- #
# Verdict mapping matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "state,ok,score,evidence,tier,blocked,attempt,expected",
    [
        (NodeState.DONE, True, 0.9, True, TaskTier.R, False, 1, VerdictNext.ACCEPT),
        (NodeState.DONE, True, 0.4, True, TaskTier.R, False, 1, VerdictNext.ESCALATE),
        (NodeState.DONE, True, 0.9, False, TaskTier.R, False, 1, VerdictNext.ESCALATE),
        (NodeState.FAILED, False, 0.8, True, TaskTier.R, False, 1, VerdictNext.RETRY),
        (NodeState.FAILED, False, 0.8, True, TaskTier.R, False, 2, VerdictNext.ESCALATE),
        (NodeState.PARTIAL, None, 0.5, True, TaskTier.R, False, 1, VerdictNext.RETRY),
        (NodeState.PARTIAL, None, 0.5, True, TaskTier.R, False, 2, VerdictNext.ESCALATE),
        (NodeState.BLOCKED, None, 0.3, True, TaskTier.R, False, 1, VerdictNext.ASK_OWNER),
        (NodeState.BLOCKED, None, 0.3, False, TaskTier.R, False, 1, VerdictNext.ASK_OWNER),
        (NodeState.UNKNOWN, None, 0.2, True, TaskTier.R, False, 1, VerdictNext.ESCALATE),
        (NodeState.DONE, True, 0.9, True, TaskTier.D, False, 1, VerdictNext.ASK_OWNER),
        (NodeState.DONE, True, 0.9, True, TaskTier.R, True, 1, VerdictNext.ASK_OWNER),
    ],
)
def test_decide_next_matrix(state, ok, score, evidence, tier, blocked, attempt, expected):
    assert decide_next(state=state, ok=ok, score=score, has_evidence=evidence,
                       tier=tier, tier_blocked=blocked, attempt=attempt) is expected


def test_local_verdict_never_accepts_without_evidence():
    verdict = local_verdict(_node(), _receipt(evidence=[], artifacts=[]))
    assert verdict.next is VerdictNext.ESCALATE
    assert verdict.ok is None


def test_local_verdict_accepts_evidenced_success():
    verdict = local_verdict(_node(), _receipt())
    assert verdict.next is VerdictNext.ACCEPT
    assert verdict.judge == "local"


def test_local_verdict_blocks_tier_d():
    verdict = local_verdict(_node(tier="D"), _receipt())
    assert verdict.next is VerdictNext.ASK_OWNER
    assert verdict.state is NodeState.BLOCKED


async def test_supervise_fast_path_skips_decider(monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError("fast path must not call the decider")

    verdict = await supervise(_node(), _receipt(), decider=_boom, fast_path=True)
    assert verdict.next is VerdictNext.ACCEPT
    assert verdict.judge == "local"


class _FakeDecider:
    default_model = "perplexity/pplx-decider-v1-27b"

    def __init__(self, text: str):
        self.text = text

    async def judge(self, messages, *, schema):
        return ChatResult(text=self.text)


async def test_supervise_accepts_decider_verdict_with_evidence():
    decider = _FakeDecider(json.dumps({
        "state": "done", "ok": True, "score": 0.9,
        "reasons": ["mail read"], "evidence_refs": ["n1:receipt"],
    }))
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.next is VerdictNext.ACCEPT
    assert verdict.judge == "decider"


async def test_supervise_downgrades_evidence_free_ok():
    decider = _FakeDecider(json.dumps({
        "state": "done", "ok": True, "score": 0.95,
        "reasons": ["trust me"], "evidence_refs": [],
    }))
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.ok is None
    assert verdict.next is VerdictNext.ESCALATE


@pytest.mark.parametrize("text", ["not json", json.dumps({"state": "done"})])
async def test_supervise_falls_back_on_bad_verdict(text):
    decider = _FakeDecider(text)
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.judge == "local"


async def test_supervise_falls_back_when_decider_raises():
    class _Raising:
        default_model = "x"

        async def judge(self, *args, **kwargs):
            raise RuntimeError("provider down")

    verdict = await supervise(_node(), _receipt(), decider=_Raising())
    assert verdict.judge == "local"


# --------------------------------------------------------------------------- #
# Throttle + join
# --------------------------------------------------------------------------- #


def test_throttler_passes_important_and_throttles_milestones():
    less_is_more = StatusThrottler(min_interval_seconds=60.0)
    job = "job-1"
    first = StatusEvent(job_id=job, kind="milestone", text="one")
    assert less_is_more.speakable(first, now=100.0) is True
    assert less_is_more.speakable(
        StatusEvent(job_id=job, kind="milestone", text="two"), now=110.0
    ) is False
    assert less_is_more.speakable(
        StatusEvent(job_id=job, kind="milestone", text="late", important=True), now=111.0
    ) is True
    assert less_is_more.speakable(
        StatusEvent(job_id=job, kind="verdict", text="v"), now=200.0
    ) is False


def test_join_outcome_vocabulary():
    accepted = SupervisorVerdict(node_id="n1", state=NodeState.DONE, ok=True,
                                 score=0.9, next=VerdictNext.ACCEPT)
    out = join_outcome(job_id="j", receipts=[_receipt(spoken="Did it.")],
                       verdicts=[accepted], nodes_total=1)
    assert out.status == "answered"
    assert out.spoken == "Did it."
    asked = SupervisorVerdict(node_id="n1", state=NodeState.BLOCKED, ok=None,
                              score=0.0, reasons=["Approve delete?"],
                              next=VerdictNext.ASK_OWNER)
    out = join_outcome(job_id="j", receipts=[_receipt(ok=False)], verdicts=[asked],
                       nodes_total=1)
    assert out.status == "waiting"
    assert "Approve delete?" in out.spoken
    failed = SupervisorVerdict(node_id="n2", state=NodeState.FAILED, ok=False,
                               score=0.8, next=VerdictNext.ESCALATE)
    out = join_outcome(job_id="j", receipts=[_receipt(), _receipt(node_id="n2", ok=False)],
                       verdicts=[accepted, failed], nodes_total=2)
    assert out.status == "failed"
    assert "n1" in out.spoken and "n2" in out.spoken


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #


class _FakePlanProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    async def chat_structured(self, messages, *, schema, schema_name):
        return ChatResult(text=json.dumps(self.payload))


async def test_plan_task_validates_provider_plan():
    provider = _FakePlanProvider({"nodes": [
        {"id": "read-mail", "label": "Read mail", "detail": "Read the inbox.",
         "tier": "R", "tool": "life.mail", "arguments": {"query": "inbox"}},
    ]})
    nodes = await plan_task("read my inbox", provider=provider)
    assert [n.id for n in nodes] == ["read-mail"]
    assert nodes[0].tool == "life.mail"


async def test_plan_task_rejects_empty_and_unstructured():
    with pytest.raises(GraphPlanError):
        await plan_task("   ", provider=_FakePlanProvider({"nodes": []}))
    with pytest.raises(GraphUnavailable):
        await plan_task("do it", provider=object())


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #


async def _ok_execute(session, name, args, **kwargs):
    return {"ok": True, "spoken": f"{name} done", "evidence": [{"tool": name}]}


async def test_run_node_routes_tier_d_without_executing():
    async def _boom(*args, **kwargs):
        raise AssertionError("tier D must not execute")

    receipt = await run_node(_node(tier="D"), WorkerCtx(), job_id="j", execute_fn=_boom)
    assert receipt.ok is False
    assert receipt.error == "tier_d_requires_approval"


async def test_run_node_static_executes_hinted_tool():
    node = _node(tool="life.mail", arguments={"query": "inbox"})
    assert fast_path_eligible(node) is True
    receipt = await run_node(node, WorkerCtx(), job_id="j", execute_fn=_ok_execute)
    assert receipt.ok is True
    assert receipt.worker == "static"
    assert receipt.has_evidence is True


def test_receipt_keeps_explicit_evidence_and_payload_substance():
    from app.cognitive.worker import receipt_from_result

    full = receipt_from_result(_node(), {"ok": True, "evidence": [{"tool": "t"}]},
                               worker="static", duration_ms=1.0)
    assert full.has_evidence is True
    substance = receipt_from_result(
        _node(), {"ok": True, "spoken": "done", "timer_id": "t-1"},
        worker="static", duration_ms=1.0)
    assert substance.has_evidence is True
    assert substance.evidence[0]["payload_keys"] == ["timer_id"]


def test_receipt_rejects_bare_assertion_as_evidence():
    from app.cognitive.worker import receipt_from_result

    bare = receipt_from_result(_node(), {"ok": True, "spoken": "trust me"},
                               worker="static", duration_ms=1.0)
    assert bare.ok is True
    assert bare.has_evidence is False


async def test_run_node_static_bare_ok_carries_no_evidence():
    async def _bare(session, name, args, **kwargs):
        return {"ok": True, "spoken": "trust me"}

    receipt = await run_node(_node(tool="life.mail"), WorkerCtx(), job_id="j",
                             execute_fn=_bare)
    assert receipt.ok is True
    assert receipt.has_evidence is False


async def test_run_node_unknown_tool_falls_to_mimo_worker():
    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            self.rounds += 1
            if self.rounds == 1:
                return ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="memory.search",
                                         arguments={"query": "x"})],
                )
            return ChatResult(text="Found it.")

    receipt = await run_node(_node(tool="not-a-tool"), WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_ok_execute)
    assert receipt.ok is True
    assert receipt.worker == "mimo"


async def test_run_node_restores_shared_session(tmp_path, monkeypatch):
    from app.cognitive import session_store

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    session_store.reset_for_tests()
    live = session_store.current()
    live.semantic_objective = "live-turn-objective"
    session_store.save(live)
    before = session_store._path().read_bytes()

    async def _dirty_execute(session, name, args, **kwargs):
        worker_session = session_store.CognitiveSession(session_id="worker-pollution")
        session_store.save(worker_session)
        return {"ok": True, "spoken": "done", "evidence": [{"tool": name}]}

    receipt = await run_node(_node(tool="life.mail"), WorkerCtx(), job_id="j",
                             execute_fn=_dirty_execute)
    assert receipt.ok is True
    assert session_store._path().read_bytes() == before
    assert session_store.current().semantic_objective == "live-turn-objective"
    session_store.reset_for_tests()


# --------------------------------------------------------------------------- #
# Graph runs + delegation wiring
# --------------------------------------------------------------------------- #


async def test_run_graph_fast_path_answers():
    plan = _FakePlanProvider({"nodes": [
        {"id": "read-mail", "label": "Read mail", "detail": "Read the inbox.",
         "tier": "R", "tool": "life.mail", "arguments": {"query": "inbox"}},
    ]})
    events: list = []

    async def _progress(event):
        events.append(event)

    import app.cognitive.worker as worker_mod

    real_run = worker_mod.run_node

    async def _patched(node, ctx, **kwargs):
        kwargs.setdefault("execute_fn", _ok_execute)
        return await real_run(node, ctx, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_mod, "run_node", _patched)
    try:
        outcome = await run_graph("read my inbox", job_id="job-1",
                                  plan_provider=plan, progress=_progress)
    finally:
        monkeypatch.undo()
    assert outcome.status == "answered"
    assert outcome.nodes_accepted == 1
    assert any(e.kind == "plan" for e in events)


async def test_run_graph_fast_path_retries_once_then_accepts():
    plan = _FakePlanProvider({"nodes": [
        {"id": "flaky", "label": "Flaky", "detail": "Flaky read.",
         "tier": "R", "tool": "life.mail", "arguments": {"query": "inbox"}},
    ]})
    calls: list = []

    async def _flaky(node, ctx, **kwargs):
        calls.append(node.id)
        if len(calls) == 1:
            return _receipt(ok=False, error="timeout")
        return _receipt()

    import app.cognitive.worker as worker_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_mod, "run_node", _flaky)
    try:
        outcome = await run_graph("flaky", job_id="job-r", plan_provider=plan)
    finally:
        monkeypatch.undo()
    assert outcome.status == "answered"
    assert calls == ["flaky", "flaky"]


async def test_run_graph_tier_d_waits_for_owner():
    plan = _FakePlanProvider({"nodes": [
        {"id": "wipe", "label": "Wipe folder", "detail": "Delete everything.",
         "tier": "D"},
    ]})
    outcome = await run_graph("delete everything", job_id="job-2", plan_provider=plan)
    assert outcome.status == "waiting"
    assert "approval" in outcome.spoken


async def test_maybe_run_graph_job_off_returns_none(monkeypatch):
    from app.cognitive import delegation

    monkeypatch.setattr(settings, "delegate_graph", "off")
    assert await delegation._maybe_run_graph_job(
        uuid4(), "do it", actor="master", binding={}
    ) is None


async def test_maybe_run_graph_job_without_key_falls_back_to_legacy(monkeypatch):
    from app.cognitive import delegation

    monkeypatch.setattr(settings, "delegate_graph", "on")
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert await delegation._maybe_run_graph_job(
        uuid4(), "do it", actor="master", binding={}
    ) is None


async def test_maybe_run_graph_job_persists_progress(monkeypatch):
    from app.cognitive import delegation

    async def _fake_run_graph(task, **kwargs):
        progress = kwargs["progress"]
        await progress(StatusEvent(job_id=kwargs["job_id"], kind="plan",
                                   text="Planned 1 step", important=True))
        from app.cognitive.graph import GraphOutcome

        return GraphOutcome(status="answered", spoken="Did it.", nodes_total=1,
                            nodes_accepted=1)

    monkeypatch.setattr(settings, "delegate_graph", "on")
    monkeypatch.setattr(graph_mod, "run_graph", _fake_run_graph)
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(ResearchSession(id=job_id, owner="master", mode="rt_delegate",
                               status="running", question="do it", goal="do it",
                               budget={"live_session_id": "s", "device_id": "d"}))
        await db.commit()
    receipt = await delegation._maybe_run_graph_job(
        job_id, "do it", actor="master",
        binding={"live_session_id": "s", "device_id": "d"},
    )
    assert receipt is not None
    assert receipt["status"] == "answered"
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.budget["status_events"][0]["kind"] == "plan"


# --------------------------------------------------------------------------- #
# Capability card + instructions
# --------------------------------------------------------------------------- #


def test_delegate_capability_card_empty_without_manifest():
    from app.ev.protocols import delegate_capability_card

    assert delegate_capability_card(None) == ""
    assert delegate_capability_card({}) == ""


def test_delegate_capability_card_marks_ready_and_setup():
    from app.ev.protocols import delegate_capability_card

    manifest = {
        "live_tool_projection": [
            {"name": "search_memory", "availability": "available"},
            {"name": "list_messages", "availability": "unavailable"},
        ]
    }
    card = delegate_capability_card(manifest)
    assert "Memory (ready now)" in card
    assert "messages (life helper)" in card
    assert "delegate_task" in card


def test_delegate_instructions_gain_live_reach_only_with_manifest():
    from app.voice.live.gemini_live import realtime_delegate_instructions

    bare = realtime_delegate_instructions()
    assert "LIVE REACH" not in bare
    assert "CAPABILITY CARD" in bare
    manifest = {"live_tool_projection": [{"name": "search_memory"}]}
    live = realtime_delegate_instructions(manifest)
    assert "LIVE REACH" in live
    assert "Memory (ready now)" in live


def test_decider_provider_shares_openrouter_key(monkeypatch):
    from app.gateway.decider import DeciderProvider, decider_available

    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert decider_available() is False
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "decider_model", "perplexity/pplx-decider-v1-27b")
    assert decider_available() is True
    provider = DeciderProvider()
    assert provider.default_model == "perplexity/pplx-decider-v1-27b"
    assert provider.api_key == "test-key"
