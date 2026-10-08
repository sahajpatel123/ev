"""Offline regressions for the delegate graph (planner -> supervisors -> workers)."""
from __future__ import annotations

import asyncio
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
from app.cognitive.supervisor import (
    answers_to_verdict,
    decide_next,
    local_verdict,
    supervise,
)
from app.cognitive.worker import (
    WorkerCtx,
    _file_op_from_node,
    _worker_family,
    fast_path_eligible,
    run_node,
)
from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.db import SessionLocal
from app.gateway.decider import (
    DeciderProvider,
    DeciderResult,
    DeciderUnavailable,
    decider_available,
)
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


def test_local_verdict_rejects_read_tool_for_write_node():
    """A tier-W node answered by a read-only tool must not verify.

    Live catch: the planner chose life.messages for a WhatsApp send; the
    read receipt verified and the job said Done with nothing sent.
    """
    verdict = local_verdict(_node(tier="W", tool="life.messages"), _receipt())
    assert verdict.next is VerdictNext.ESCALATE
    assert verdict.ok is None
    assert "read-only" in verdict.reasons[0]


def test_local_verdict_still_accepts_write_tool_for_write_node():
    verdict = local_verdict(_node(tier="W", tool="life.send"), _receipt())
    assert verdict.next is VerdictNext.ACCEPT


def test_local_verdict_still_accepts_read_tool_for_read_node():
    verdict = local_verdict(_node(tier="R", tool="life.messages"), _receipt())
    assert verdict.next is VerdictNext.ACCEPT


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
    default_model = "typesafe/jev-1.13"

    def __init__(self, answers: dict, model: str | None = None):
        self._answers = answers
        self._model = model or "typesafe/jev-1.13-20260917"
        self.seen: list[tuple[dict, dict]] = []

    async def judge(self, state, questions):
        self.seen.append((state, questions))
        return DeciderResult(
            answers=self._answers, model=self._model, usage={"cost": 0.00001}
        )


def _typed_answers(probability=0.9, choice="done", choice_p=0.91):
    return {
        "done": {
            "type": "noul",
            "noul": probability,
        },
        "verdict": {
            "type": "choice",
            "choice": choice,
            "probabilities": {choice: choice_p},
            "confidence": 0.8,
        },
        "quality": {
            "type": "score",
            "score": 2.7,
            "legend": {"0": "broken", "3": "solid"},
            "probabilities": {"2": 0.21, "3": 0.75},
            "confidence": 0.8,
        },
    }


async def test_supervise_accepts_decider_verdict_with_evidence():
    decider = _FakeDecider(_typed_answers())
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.next is VerdictNext.ACCEPT
    assert verdict.judge == "decider"
    assert verdict.model == "typesafe/jev-1.13-20260917"
    assert verdict.score == 0.9


async def test_supervise_sends_typed_questions_over_node_state():
    decider = _FakeDecider(_typed_answers())
    await supervise(_node(), _receipt(), decider=decider)
    assert len(decider.seen) == 1
    state, questions = decider.seen[0]
    assert state["node"]["id"] == "n1"
    assert state["receipt"]["evidence"]
    assert questions["done"]["type"] == "noul"
    assert questions["verdict"]["type"] == "choice"
    assert set(questions["verdict"]["criteria"]) == {
        "done", "partial", "blocked", "failed", "unknown",
    }
    assert questions["quality"]["type"] == "score"


async def test_supervise_downgrades_evidence_free_ok():
    decider = _FakeDecider(_typed_answers(probability=0.95))
    verdict = await supervise(
        _node(), _receipt(evidence=[], artifacts=[]), decider=decider
    )
    assert verdict.ok is None
    assert verdict.next is VerdictNext.ESCALATE


_BAD_ANSWERS = [
    {"done": {"probability": 0.9}},  # missing verdict + quality
    _typed_answers(choice="nonsense"),  # not a node state
    _typed_answers(probability="high"),  # non-numeric noul
    _typed_answers(probability=1.5),  # outside 0-1
    ["not", "an", "object"],
]


@pytest.mark.parametrize("answers", _BAD_ANSWERS)
async def test_supervise_falls_back_on_bad_verdict(answers):
    decider = _FakeDecider(answers)
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.judge == "local"


async def test_supervise_falls_back_when_decider_raises():
    class _Raising:
        default_model = "x"

        async def judge(self, *args, **kwargs):
            raise RuntimeError("provider down")

    verdict = await supervise(_node(), _receipt(), decider=_Raising())
    assert verdict.judge == "local"


async def test_supervise_escalates_low_confidence_done():
    decider = _FakeDecider(_typed_answers(probability=0.3))
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.judge == "decider"
    assert verdict.ok is None
    assert verdict.next is VerdictNext.ESCALATE


async def test_supervise_retries_failed_choice():
    decider = _FakeDecider(_typed_answers(probability=0.1, choice="failed"))
    verdict = await supervise(_node(), _receipt(), decider=decider)
    assert verdict.ok is False
    assert verdict.next is VerdictNext.RETRY


def test_answers_to_verdict_rejects_malformed_numbers():
    with pytest.raises(ValueError):
        answers_to_verdict(_node(), _receipt(), _typed_answers(probability=2.0))
    with pytest.raises(ValueError):
        answers_to_verdict(_node(), _receipt(), None)


def test_answers_to_verdict_accepts_probability_alias():
    answers = _typed_answers()
    answers["done"] = {"type": "noul", "probability": 0.88}
    verdict = answers_to_verdict(_node(), _receipt(), answers)
    assert verdict.state is NodeState.DONE
    assert verdict.score == 0.88


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


async def test_static_worker_scopes_texts_label_to_imessage():
    """A texts label scopes the aisle via channel, never via query words."""

    seen: dict = {}

    async def _capture(session, name, args, **kwargs):
        seen.update(args)
        return {"ok": True, "spoken": "done", "messages": [{"text": "x"}]}

    node = _node(id="fetch", label="Fetch 3 latest texts",
                tool="life.messages", arguments={"limit": 3})
    receipt = await run_node(node, WorkerCtx(), job_id="j", execute_fn=_capture)
    assert receipt.ok is True
    assert seen.get("channel") == "imessage"
    assert "query" not in seen


async def test_static_worker_keeps_mixed_labels_unscoped():
    """Bare "messages", person labels, and explicit queries stay untouched."""

    seen: dict = {}

    async def _capture(session, name, args, **kwargs):
        seen.clear()
        seen.update(args)
        return {"ok": True, "spoken": "done", "messages": [{"text": "x"}]}

    for label, arguments in [
        ("Summarize recent messages", {"limit": 3}),
        ("Texts from Mansi", {"limit": 3}),
        ("Fetch 3 latest texts", {"query": "custom"}),
    ]:
        node = _node(id="fetch", label=label, tool="life.messages",
                      arguments=arguments)
        receipt = await run_node(node, WorkerCtx(), job_id="j",
                                 execute_fn=_capture)
        assert receipt.ok is True
        assert "channel" not in seen, label


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


async def test_mimo_worker_receives_prior_results():
    """depends_on receipts reach the worker prompt (live catch: they never did)."""

    seen: list = []

    class _Chat:
        async def chat_with_tools(self, messages, specs):
            seen.extend(messages)
            return ChatResult(text="Synthesized.")

    node = _node(id="summarize", tool=None, depends_on=["fetch-mail"])
    prior = [_receipt(node_id="fetch-mail", spoken="Recent mail: A. B.")]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), prior=prior)
    assert receipt.ok is True
    blob = " ".join(m.content for m in seen)
    assert "fetch-mail" in blob
    assert "Recent mail: A. B." in blob
    assert "only call tools for what is still missing" in blob


async def test_mimo_worker_empty_recall_does_not_hide_substance():
    """An honest-empty second call must not overwrite a substantive spoken."""

    script = [
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="life.messages",
                                                 arguments={"query": "recent"})]),
        ChatResult(text="", tool_calls=[ToolCall(id="c2", name="life.messages",
                                                 arguments={"query": "narrow"})]),
        ChatResult(text="Synthesis."),
    ]

    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            out = script[min(self.rounds, len(script) - 1)]
            self.rounds += 1
            return out

    async def _scripted(session, name, args, **kwargs):
        if args.get("query") == "recent":
            return {"ok": True, "spoken": "Latest: A. B.",
                    "messages": [{"text": "a"}]}
        return {"ok": True, "spoken": "I don't see new messages.",
                "messages": []}

    receipt = await run_node(_node(tool=None), WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_scripted)
    assert receipt.ok is True
    assert receipt.spoken == "Latest: A. B."


async def test_mimo_worker_failure_spoken_stays_visible():
    """A failed call still overwrites: failures must never be hidden."""

    script = [
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="life.messages",
                                                 arguments={"query": "recent"})]),
        ChatResult(text="", tool_calls=[ToolCall(id="c2", name="digital.discover",
                                                 arguments={})]),
        ChatResult(text="Synthesis."),
    ]

    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            out = script[min(self.rounds, len(script) - 1)]
            self.rounds += 1
            return out

    async def _scripted(session, name, args, **kwargs):
        if name == "life.messages":
            return {"ok": True, "spoken": "Latest: A. B.",
                    "messages": [{"text": "a"}]}
        return {"ok": False, "spoken": "boom", "error": "discover_failed"}

    receipt = await run_node(_node(tool=None), WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_scripted)
    assert receipt.ok is False
    assert receipt.spoken == "boom"
    assert receipt.error == "tool_failures"


def test_is_stub_flags_lone_openers_and_headers():
    from app.cognitive.worker import _is_stub

    prior = [
        _receipt(node_id="a", spoken="Mail: X."),
        _receipt(node_id="b", spoken="Texts: Y."),
    ]
    assert _is_stub("Emails\n", prior) is True
    assert _is_stub("Here's your combined summary:", prior) is True
    assert _is_stub("**Emails**\n\n**Texts**\n", prior) is True
    assert _is_stub("A — x.\nB — y.", prior) is False
    assert _is_stub("Anything at all.", []) is False
    assert _is_stub("One line.", [_receipt(node_id="a")]) is False


async def test_mimo_synthesis_stub_fails_with_text_kept():
    """A stub synthesis fails loudly; the stub stays quoted for the retry."""

    class _Chat:
        async def chat_with_tools(self, messages, specs):
            return ChatResult(text="Emails\n")

    async def _boom(*args, **kwargs):
        raise AssertionError("synthesis must not call tools")

    node = _node(id="summarize", tool=None, depends_on=["a", "b"])
    prior = [
        _receipt(node_id="a", spoken="Mail: X."),
        _receipt(node_id="b", spoken="Texts: Y."),
    ]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_boom, prior=prior)
    assert receipt.ok is False
    assert receipt.error == "empty_synthesis"
    assert receipt.spoken == "Emails"


async def test_mimo_prose_stub_without_tools_is_no_action():
    """Loop prose that stubs on a multi-dep node is honestly no action."""

    class _Chat:
        async def chat_with_tools(self, messages, specs):
            return ChatResult(text="Here's your combined summary:")

    node = _node(id="summarize", tool="frobnicate", depends_on=["a", "b"])
    prior = [
        _receipt(node_id="a", spoken="Mail: X."),
        _receipt(node_id="b", spoken="Texts: Y."),
    ]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_ok_execute,
                             prior=prior)
    assert receipt.ok is False
    assert receipt.error == "no_action_taken"
    assert "combined summary" in receipt.spoken


async def test_mimo_synthesis_falls_through_when_tools_demanded():
    """A synthesis probe that still wants tools keeps the loop (live catch).

    Dropping the calls kept only a lead-in ("Here's your combined
    summary:" with nothing after). The calls name a genuine gap.
    """

    script = [
        ChatResult(text="Here's your combined summary:",
                   tool_calls=[ToolCall(id="c1", name="memory.search",
                                        arguments={"query": "x"})]),
        ChatResult(text="", tool_calls=[ToolCall(id="c2", name="memory.search",
                                                 arguments={"query": "x"})]),
        ChatResult(text="Filled the gap."),
    ]

    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            out = script[min(self.rounds, len(script) - 1)]
            self.rounds += 1
            return out

    node = _node(id="summarize", tool=None, depends_on=["a"])
    prior = [_receipt(node_id="a", spoken="Mail: X.")]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_ok_execute,
                             prior=prior)
    assert receipt.ok is True
    assert len(receipt.rounds) == 1
    assert receipt.rounds[0]["tool"] == "memory.search"


async def test_execute_plan_threads_prior_receipts_to_dependents(monkeypatch):
    import app.cognitive.worker as worker_mod
    from app.cognitive.graph import _execute_plan

    seen: dict = {}

    async def _spy(node, ctx, **kwargs):
        seen[node.id] = list(kwargs.get("prior") or [])
        return _receipt(node_id=node.id, spoken=f"spoke-{node.id}")

    monkeypatch.setattr(worker_mod, "run_node", _spy)
    nodes = validate_dag(
        [
            {"id": "a", "label": "A", "detail": "first", "tier": "R"},
            {"id": "b", "label": "B", "detail": "second", "tier": "R",
             "depends_on": ["a"]},
        ],
        max_nodes=8,
    )
    receipts, verdicts = await _execute_plan(
        nodes, ctx=WorkerCtx(), job_id="j", progress=None, decider=None
    )
    assert [r.node_id for r in receipts] == ["a", "b"]
    assert seen["a"] == []
    assert [r.node_id for r in seen["b"]] == ["a"]


def test_payload_evidence_carries_row_counts():
    """List results cite counts so the decider can verify substance."""

    from app.cognitive.worker import receipt_from_result

    hub_like = receipt_from_result(
        _node(),
        {"ok": True, "spoken": "Latest: A. B.",
         "messages": [{"text": "a"}, {"text": "b"}, {"text": "c"}],
         "source": "live_mac", "channel": "messages"},
        worker="static", duration_ms=1.0,
    )
    assert hub_like.has_evidence is True
    assert hub_like.evidence[0]["counts"] == {"messages": 3}
    adapter_like = receipt_from_result(
        _node(),
        {"ok": True, "spoken": "Latest: A.",
         "evidence": {"source": "messaging", "timestamp": "t"},
         "messages": [{"text": "a"}]},
        worker="static", duration_ms=1.0,
    )
    assert adapter_like.evidence[0]["source"] == "messaging"
    assert adapter_like.evidence[-1]["counts"] == {"messages": 1}


async def test_mimo_synthesis_mode_answers_without_tools():
    """Deps delivered: one prose call, no tool loop, evidence cites priors."""

    seen_specs: list = []
    seen_messages: list = []

    class _Chat:
        async def chat_with_tools(self, messages, specs):
            seen_specs.append(list(specs))
            seen_messages.extend(messages)
            return ChatResult(text="A — x.\nB — y.")

    async def _boom(*args, **kwargs):
        raise AssertionError("synthesis must not call tools")

    node = _node(id="summarize", tool=None, depends_on=["a", "b"])
    prior = [
        _receipt(node_id="a", spoken="Mail: X."),
        _receipt(node_id="b", spoken="Texts: Y."),
    ]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_boom, prior=prior)
    assert receipt.ok is True
    assert receipt.worker == "mimo"
    assert receipt.spoken == "A — x.\nB — y."
    assert receipt.rounds == []
    assert seen_specs == [[]]
    assert [e["node_id"] for e in receipt.evidence] == ["a", "b"]
    assert receipt.evidence[0]["spoken"] == "Mail: X."
    assert "first line must be the first item" in seen_messages[0].content
    assert "one short factual sentence" not in seen_messages[0].content


async def test_mimo_synthesis_skipped_when_prior_failed():
    """A failed dependency keeps the tool loop so the worker can fill the gap."""

    script = [
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="memory.search",
                                                 arguments={"query": "x"})]),
        ChatResult(text="Filled the gap."),
    ]

    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            out = script[min(self.rounds, len(script) - 1)]
            self.rounds += 1
            return out

    node = _node(id="summarize", tool=None, depends_on=["a", "b"])
    prior = [
        _receipt(node_id="a", spoken="Mail: X."),
        _receipt(node_id="b", ok=False, spoken="", error="boom"),
    ]
    receipt = await run_node(node, WorkerCtx(), job_id="j",
                             provider=_Chat(), execute_fn=_ok_execute,
                             prior=prior)
    assert receipt.ok is True
    assert receipt.worker == "mimo"
    assert len(receipt.rounds) == 1
    assert receipt.rounds[0]["tool"] == "memory.search"


async def test_retry_hint_quotes_previous_spoken(monkeypatch):
    """A stub retry shows the model its stub (live catch: bare lead-ins)."""

    import app.cognitive.worker as worker_mod
    from app.cognitive.graph import _execute_plan

    hints: dict = {}

    async def _spy(node, ctx, **kwargs):
        hints.setdefault(node.id, []).append(kwargs.get("hint"))
        if node.id == "b" and len(hints["b"]) == 1:
            return _receipt(node_id="b", ok=False, spoken="My stub answer.",
                            error="x")
        return _receipt(node_id=node.id, spoken=f"spoke-{node.id}")

    monkeypatch.setattr(worker_mod, "run_node", _spy)
    nodes = validate_dag(
        [
            {"id": "a", "label": "A", "detail": "first", "tier": "R"},
            {"id": "b", "label": "B", "detail": "second", "tier": "R"},
        ],
        max_nodes=8,
    )
    await _execute_plan(nodes, ctx=WorkerCtx(), job_id="j", progress=None,
                        decider=None)
    assert hints["a"] == [None]
    assert len(hints["b"]) == 2
    assert "My stub answer." in (hints["b"][1] or "")


def test_peek_mac_life_channel_override_scopes_aisle():
    """channel=imessage reads SMS even when the query is mixed-aisle."""

    from types import SimpleNamespace

    from app.memory.live_life import peek_mac_life

    calls: list = []

    def _imessage(*, tokens, limit):
        calls.append("imessage")
        return [{"text": "sms row"}]

    def _whatsapp(*, tokens, limit, person=None):
        calls.append("whatsapp")
        return [{"text": "wa row"}]

    daemon = SimpleNamespace(peek_imessage=_imessage, peek_whatsapp=_whatsapp)
    rows = peek_mac_life("any new messages", shelf="chats", daemon=daemon,
                         channel="imessage")
    assert calls == ["imessage"]
    assert rows == [{"text": "sms row"}]
    calls.clear()
    rows = peek_mac_life("any new messages", shelf="chats", daemon=daemon)
    assert sorted(calls) == ["imessage", "whatsapp"]
    assert len(rows) == 2


def _accept(node_id: str) -> SupervisorVerdict:
    return SupervisorVerdict(
        node_id=node_id, state=NodeState.DONE, ok=True, score=0.9,
        next=VerdictNext.ACCEPT,
    )


def test_join_outcome_joins_multi_fetch_spoken():
    """Without a synthesis node, every accepted digest reaches the answer."""

    receipts = [
        _receipt(node_id="a", spoken="Mail: X.", worker="static"),
        _receipt(node_id="b", spoken="Texts: Y.", worker="static"),
    ]
    verdicts = [_accept("a"), _accept("b")]
    outcome = join_outcome(
        job_id="j", receipts=receipts, verdicts=verdicts, nodes_total=2,
    )
    assert outcome.status == "answered"
    assert "Mail: X." in outcome.spoken
    assert "Texts: Y." in outcome.spoken


def test_join_outcome_prefers_synthesis_spoken():
    """A synthesis node speaks for the job; fetches stay in evidence."""

    receipts = [
        _receipt(node_id="a", spoken="Mail: X.", worker="static"),
        _receipt(node_id="b", spoken="Texts: Y.", worker="static"),
        _receipt(
            node_id="s", spoken="Combined.", worker="mimo",
            evidence=[{"source": "prior_step", "node_id": "a"}],
        ),
    ]
    verdicts = [_accept("a"), _accept("b"), _accept("s")]
    outcome = join_outcome(
        job_id="j", receipts=receipts, verdicts=verdicts, nodes_total=3,
    )
    assert outcome.status == "answered"
    assert outcome.spoken == "Combined."


def test_join_outcome_supersedes_retry_spoken():
    """A retried node speaks once (its latest receipt)."""

    receipts = [
        _receipt(node_id="a", spoken="First try.", worker="static"),
        _receipt(node_id="a", spoken="Second try.", worker="static"),
    ]
    verdicts = [_accept("a")]
    outcome = join_outcome(
        job_id="j", receipts=receipts, verdicts=verdicts, nodes_total=1,
    )
    assert outcome.status == "answered"
    assert outcome.spoken == "Second try."


async def test_execute_plan_replan_skips_fast_path(monkeypatch):
    """A single-node replan still gets a decider verdict (live catch).

    Shift 2 planned one narrow node for a six-item ask and fast-path local
    accepted it, so the job "answered" short. Replans must face the model.
    """
    import app.cognitive.worker as worker_mod
    from app.cognitive.graph import _execute_plan

    seen: list = []

    class _Decider:
        default_model = "t"

        async def judge(self, state, questions):
            seen.append(state)
            return DeciderResult(answers=_typed_answers(), model="t")

    async def _spy(node, ctx, **kwargs):
        return _receipt(node_id=node.id, spoken=f"spoke-{node.id}")

    monkeypatch.setattr(worker_mod, "run_node", _spy)
    nodes = validate_dag(
        [{"id": "a", "label": "A", "detail": "only", "tier": "R"}],
        max_nodes=8,
    )
    _, verdicts = await _execute_plan(
        nodes, ctx=WorkerCtx(), job_id="j", progress=None, decider=_Decider(),
    )
    assert seen == []
    assert verdicts[0].judge == "local"
    _, verdicts = await _execute_plan(
        nodes, ctx=WorkerCtx(), job_id="j", progress=None, decider=_Decider(),
        allow_fast=False,
    )
    assert len(seen) == 1
    assert verdicts[0].judge == "decider"


async def test_execute_plan_threads_prior_shift_receipts(monkeypatch):
    import app.cognitive.worker as worker_mod
    from app.cognitive.graph import _execute_plan

    seen: dict = {}

    async def _spy(node, ctx, **kwargs):
        seen[node.id] = [r.node_id for r in (kwargs.get("prior") or [])]
        return _receipt(node_id=node.id, spoken=f"spoke-{node.id}")

    monkeypatch.setattr(worker_mod, "run_node", _spy)
    nodes = validate_dag(
        [
            {"id": "c", "label": "C", "detail": "shift two", "tier": "R"},
            {"id": "d", "label": "D", "detail": "shift two", "tier": "R"},
        ],
        max_nodes=8,
    )
    shift_one = [_receipt(node_id="a", spoken="spoke-a")]
    receipts, verdicts = await _execute_plan(
        nodes, ctx=WorkerCtx(), job_id="j", progress=None, decider=None,
        prior_shift=shift_one,
    )
    assert [r.node_id for r in receipts] == ["c", "d"]
    assert seen["c"] == ["a"]
    assert seen["d"] == ["a"]


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
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert decider_available() is False
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "decider_model", "typesafe/jev-1.13")
    assert decider_available() is True
    provider = DeciderProvider()
    assert provider.default_model == "typesafe/jev-1.13"
    assert provider.api_key == "test-key"
    assert provider.endpoint == "https://openrouter.ai/api/alpha/decisions"


def test_decider_provider_builds_decisions_envelope():
    provider = DeciderProvider(default_model="m")
    payload = provider.build_payload({"node": "n1"}, {"done": {"type": "noul"}})
    assert payload == {
        "model": "m",
        "state": {"node": "n1"},
        "questions": {"done": {"type": "noul"}},
    }


async def test_decider_judge_maps_envelope_to_result(monkeypatch):
    import httpx

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": _typed_answers(),
                "usage": {"cost": 0.00002, "input_tokens": 120},
            },
        )

    provider = DeciderProvider(
        api_key="test-key", transport=httpx.MockTransport(_handler)
    )

    async def _allow(self) -> None:
        return None

    monkeypatch.setattr(DeciderProvider, "_authorize", _allow)
    result = await provider.judge({"node": "n1"}, {"done": {"type": "noul"}})
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == "Bearer test-key"
    assert result.answers["verdict"]["choice"] == "done"
    assert result.model == "typesafe/jev-1.13-20260917"
    assert result.cost_usd == 0.00002


async def test_decider_post_rejects_bad_envelopes():
    import httpx

    async def _run(handler) -> object:
        provider = DeciderProvider(
            api_key="k", transport=httpx.MockTransport(handler)
        )
        try:
            return await provider._post({"model": "m"})
        except DeciderUnavailable as exc:
            return str(exc)

    assert "HTTP 400" in str(
        await _run(lambda req: httpx.Response(400, text="ordinary chat model ids rejected"))
    )
    assert "non-JSON" in str(
        await _run(lambda req: httpx.Response(200, text="<html>nope</html>"))
    )
    assert "no answers object" in str(
        await _run(lambda req: httpx.Response(200, json={"model": "m"}))
    )
    assert "Decisions API error" in str(
        await _run(
            lambda req: httpx.Response(
                200, json={"error": {"message": "model not found"}}
            )
        )
    )


def test_decider_result_cost_guards_non_numbers():
    assert DeciderResult(answers={}, usage={"cost": True}).cost_usd is None
    assert DeciderResult(answers={}, usage={}).cost_usd is None
    assert DeciderResult(answers={}, usage={"cost": "free"}).cost_usd is None


# --------------------------------------------------------------------------- #
# Worker specialists, confirm-resume, timeout attribution (worker hardening)
# --------------------------------------------------------------------------- #


def test_worker_family_routes_file_computer_static_mimo():
    assert _worker_family(_node(tool="files.act")) == "file"
    assert _worker_family(_node(tool="files.read")) == "file"
    assert _worker_family(_node(tool="computer.observe")) == "computer"
    assert _worker_family(_node(tool="computer.perform_effect")) == "computer"
    assert _worker_family(_node(tool="life.mail")) == "static"
    assert _worker_family(_node(tool="not-a-tool")) == "mimo"
    assert _worker_family(_node(tool=None)) == "mimo"


def test_file_op_from_node_explicit_and_effect():
    op, params = _file_op_from_node(
        _node(tool="files.read", arguments={"path": "/tmp/a.txt"})
    )
    assert op == "read"
    assert params["path"] == "/tmp/a.txt"
    op, params = _file_op_from_node(
        _node(tool="files.act", arguments={"op": "Write", "path": "/tmp/b.txt"})
    )
    assert op == "write"
    op, _ = _file_op_from_node(_node(tool="files.act", arguments={"op": "nuke"}))
    assert op is None
    op, args = _file_op_from_node(
        _node(tool="files.act", arguments={"text": "tidy the desktop"})
    )
    assert op is None
    assert args == {"text": "tidy the desktop"}


async def test_file_worker_executes_explicit_op(monkeypatch):
    import app.ev.file_sandbox as sandbox_mod

    calls: list = []

    def _fake_execute(op, params, origin, confirm):
        calls.append((op, params, origin, confirm))
        return {"ok": True, "spoken": "Read it."}

    monkeypatch.setattr(sandbox_mod, "execute_op", _fake_execute)
    receipt = await run_node(
        _node(tool="files.read", arguments={"path": "/tmp/a.txt"}),
        WorkerCtx(), job_id="j",
    )
    assert receipt.worker == "file"
    assert receipt.ok is True
    assert calls == [("read", {"path": "/tmp/a.txt"}, "graph", False)]


async def test_file_worker_gated_op_returns_confirmation_receipt(monkeypatch):
    import app.ev.file_sandbox as sandbox_mod

    def _gated(op, params, origin, confirm):
        return {"ok": False, "error": "confirmation_required",
                "spoken": "Confirm delete and I'll do it."}

    monkeypatch.setattr(sandbox_mod, "execute_op", _gated)
    node = _node(tool="files.act",
                 arguments={"op": "delete", "path": "/tmp/scratch.txt"})
    receipt = await run_node(node, WorkerCtx(), job_id="j")
    assert receipt.worker == "file"
    assert receipt.ok is False
    assert receipt.error == "confirmation_required"
    assert receipt.resumption is not None
    assert receipt.resumption["op"] == "delete"
    assert receipt.resumption["params"]["path"] == "/tmp/scratch.txt"
    assert receipt.resumption["node"]["id"] == node.id


async def test_file_effect_worker_maps_semantic_confirmation():
    async def _gated(session, name, args, **kwargs):
        assert name == "files.act"
        return {"ok": False, "needs_confirm": True,
                "spoken": "Confirm that file step and I'll do it."}

    receipt = await run_node(
        _node(tool="files.act", arguments={"text": "tidy the desktop"}),
        WorkerCtx(), job_id="j", execute_fn=_gated,
    )
    assert receipt.worker == "file"
    assert receipt.error == "confirmation_required"
    assert receipt.resumption is not None
    assert receipt.resumption["node"]["id"] == "n1"


async def test_computer_worker_grounds_then_acts():
    calls: list = []

    async def _fake_execute(session, name, args, **kwargs):
        calls.append((name, args))
        if name == "look.capture":
            return {"ok": True, "spoken": "Mail app is open."}
        return {"ok": True, "spoken": "Clicked send."}

    receipt = await run_node(
        _node(tool="computer.perform_effect",
              arguments={"goal": "Send the draft in Mail"}),
        WorkerCtx(), job_id="j", execute_fn=_fake_execute,
    )
    assert receipt.worker == "computer"
    assert receipt.ok is True
    assert [name for name, _ in calls] == [
        "look.capture", "computer.perform_effect",
    ]
    assert "Mail app is open" in calls[1][1]["effect"]
    assert [step["phase"] for step in receipt.rounds] == ["ground", "act"]
    assert all(step["ok"] for step in receipt.rounds)


async def test_computer_observe_skips_grounding():
    calls: list = []

    async def _fake_execute(session, name, args, **kwargs):
        calls.append(name)
        return {"ok": True, "spoken": "Desktop."}

    receipt = await run_node(
        _node(tool="computer.observe", arguments={"goal": "What is on screen?"}),
        WorkerCtx(), job_id="j", execute_fn=_fake_execute,
    )
    assert receipt.worker == "computer"
    assert calls == ["computer.observe"]
    assert [step["phase"] for step in receipt.rounds] == ["act"]


async def test_mimo_worker_text_answer_is_read_only_artifact():
    class _Chat:
        async def chat_with_tools(self, messages, specs):
            return ChatResult(text="1. Milk\n2. Eggs")

    receipt = await run_node(
        _node(tier="R", tool=None), WorkerCtx(), job_id="j",
        provider=_Chat(), execute_fn=_ok_execute,
    )
    assert receipt.worker == "mimo"
    assert receipt.ok is True
    assert receipt.error is None
    assert receipt.artifacts and receipt.artifacts[0]["kind"] == "text"
    assert "Milk" in receipt.artifacts[0]["text"]


async def test_mimo_worker_writes_without_action_still_fail():
    class _Chat:
        async def chat_with_tools(self, messages, specs):
            return ChatResult(text="I would delete it.")

    receipt = await run_node(
        _node(tier="W", tool=None), WorkerCtx(), job_id="j",
        provider=_Chat(), execute_fn=_ok_execute,
    )
    assert receipt.ok is False
    assert receipt.error == "no_action_taken"
    assert receipt.artifacts == []


async def test_mimo_worker_rounds_record_tool_calls():
    class _Chat:
        def __init__(self):
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            self.rounds += 1
            if self.rounds == 1:
                return ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="life.mail",
                                         arguments={"query": "inbox"})],
                )
            return ChatResult(text="Found it.")

    receipt = await run_node(
        _node(tool="not-a-tool"), WorkerCtx(), job_id="j",
        provider=_Chat(), execute_fn=_ok_execute,
    )
    assert receipt.worker == "mimo"
    assert receipt.ok is True
    assert receipt.rounds and receipt.rounds[0]["tool"] == "life.mail"
    assert receipt.rounds[0]["ok"] is True


async def test_run_node_timeout_attributes_worker_family():
    async def _hang(session, name, args, **kwargs):
        await asyncio.sleep(30)

    receipt = await run_node(
        _node(tool="computer.perform_effect", arguments={"goal": "x"},
              timeout_seconds=5.0),
        WorkerCtx(), job_id="j", execute_fn=_hang,
    )
    assert receipt.ok is False
    assert receipt.error == "node_timeout"
    assert receipt.worker == "computer"
    assert receipt.rounds[0]["phase"] == "worker_call"


async def test_mimo_worker_timeout_keeps_partial_rounds():
    class _ThenHang:
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
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    receipt = await run_node(
        _node(tool=None, timeout_seconds=5.0), WorkerCtx(), job_id="j",
        provider=_ThenHang(), execute_fn=_ok_execute,
    )
    assert receipt.ok is False
    assert receipt.error == "node_timeout"
    assert receipt.worker == "mimo"
    assert [entry["tool"] for entry in receipt.rounds] == ["memory.search"]


def test_local_verdict_maps_confirmation_to_ask_owner():
    from app.cognitive.supervisor import local_verdict

    receipt = _receipt(ok=False, error="confirmation_required",
                       spoken="Confirm delete and I'll do it.")
    verdict = local_verdict(_node(), receipt)
    assert verdict.next is VerdictNext.ASK_OWNER
    assert verdict.ok is None
    assert "Confirm delete" in verdict.reasons[0]


def test_local_verdict_maps_timeout_to_retry():
    from app.cognitive.supervisor import local_verdict

    verdict = local_verdict(_node(), _receipt(ok=False, error="node_timeout"))
    assert verdict.next is VerdictNext.RETRY
    assert verdict.ok is False


def test_join_outcome_persists_plan_for_resume():
    node = _node(tool="files.act")
    accepted = SupervisorVerdict(node_id="n1", state=NodeState.DONE, ok=True,
                                 score=0.9, next=VerdictNext.ACCEPT)
    out = join_outcome(job_id="j", receipts=[_receipt()], verdicts=[accepted],
                       nodes_total=1, nodes=[node])
    assert out.status == "answered"
    assert out.evidence["plan"][0]["id"] == "n1"
    assert out.evidence["plan"][0]["tool"] == "files.act"


async def test_run_graph_stores_plan_in_evidence():
    plan = _FakePlanProvider({"nodes": [
        {"id": "wipe", "label": "Wipe folder", "detail": "Delete everything.",
         "tier": "D"},
    ]})
    outcome = await run_graph("delete everything", job_id="job-plan",
                              plan_provider=plan)
    assert outcome.status == "waiting"
    assert outcome.evidence["plan"][0]["id"] == "wipe"


def test_parse_approval_yes_no_ambiguous():
    from app.cognitive.delegation import _parse_approval

    assert _parse_approval("yes, do it") is True
    assert _parse_approval("Go ahead") is True
    assert _parse_approval("no") is False
    assert _parse_approval("don't do it") is False
    assert _parse_approval("don't, actually yes") is False  # denial wins
    assert _parse_approval("hmm, what did you say?") is None
    assert _parse_approval("") is None
    assert _parse_approval(None) is None
    assert _parse_approval("yes " * 200) is None


def _waiting_row(job_id, node_raw, resumption):
    asked = SupervisorVerdict(
        node_id=node_raw["id"], state=NodeState.BLOCKED, ok=None, score=0.0,
        reasons=["Confirm delete and I'll do it."],
        next=VerdictNext.ASK_OWNER,
    )
    waiting_receipt = WorkerReceipt(
        node_id=node_raw["id"], ok=False,
        spoken="Confirm delete and I'll do it.", worker="file",
        error="confirmation_required", resumption=resumption,
    )
    return ResearchSession(
        id=job_id, owner="master", mode="rt_delegate", status="waiting",
        question="delete the scratch file", goal="delete the scratch file",
        conclusion="Confirm delete and I'll do it.",
        budget={"live_session_id": "live-1", "device_id": "dev-1",
                "status_events": []},
        evidence={"result": {"graph": {
            "receipts": [waiting_receipt.model_dump(mode="json")],
            "verdicts": [asked.model_dump(mode="json")],
            "evidence": {"plan": [node_raw]},
        }}},
    )


async def test_answer_door_approval_resumes_same_job(monkeypatch):
    import app.ev.file_sandbox as sandbox_mod
    from app.cognitive import delegation
    from app.cognitive import supervisor as supervisor_mod

    node_raw = _node(tool="files.act", arguments={"op": "delete"}).model_dump(
        mode="json"
    )
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(_waiting_row(job_id, node_raw, {
            "node": node_raw, "op": "delete",
            "params": {"op": "delete", "path": "/tmp/scratch.txt"},
        }))
        await db.commit()

    monkeypatch.setattr(
        sandbox_mod, "execute_op",
        lambda op, params, origin, confirm: (
            {"ok": True, "spoken": "Deleted it."}
            if confirm and origin == "graph-resume"
            else {"ok": False, "error": "confirmation_required"}
        ),
    )

    async def _accept(node, receipt, **kwargs):
        return SupervisorVerdict(node_id=node.id, state=NodeState.DONE,
                                 ok=True, score=0.9,
                                 next=VerdictNext.ACCEPT)

    monkeypatch.setattr(supervisor_mod, "supervise", _accept)
    result = await delegation._answer_waiting_job(
        actor="master", live_session_id="live-1", device_id="dev-1",
        job_id=str(job_id), owner_transcript="yes, do it",
    )
    assert result["ok"] is True
    assert result["job_id"] == str(job_id)
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.status == "answered"
        assert row.evidence["result"]["graph"]["resumed"] is True


async def test_answer_door_denial_leaves_step_undone():
    from app.cognitive import delegation

    node_raw = _node(tool="files.act").model_dump(mode="json")
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(_waiting_row(job_id, node_raw, {"node": node_raw}))
        await db.commit()
    result = await delegation._answer_waiting_job(
        actor="master", live_session_id="live-1", device_id="dev-1",
        job_id=str(job_id), owner_transcript="no, don't",
    )
    assert result["ok"] is True
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.status == "failed"
        assert row.evidence["result"]["owner_answer"] == "declined"


async def test_answer_door_ambiguous_stays_waiting():
    from app.cognitive import delegation

    node_raw = _node(tool="files.act").model_dump(mode="json")
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(_waiting_row(job_id, node_raw, {"node": node_raw}))
        await db.commit()
    result = await delegation._answer_waiting_job(
        actor="master", live_session_id="live-1", device_id="dev-1",
        job_id=str(job_id), owner_transcript="hmm, what did you say?",
    )
    assert result["ok"] is False
    assert result["job_id"] == str(job_id)
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.status == "waiting"


# --------------------------------------------------------------------------- #
# Dynamic budgets + honest worker evidence
# --------------------------------------------------------------------------- #


async def test_plan_task_uses_dedicated_plan_budget(monkeypatch):
    """Planning rides its own budget: the 25s chat ceiling stalled live jobs."""

    import asyncio

    class _Slow:
        async def chat_structured(self, messages, *, schema, schema_name):
            await asyncio.sleep(0.4)
            return ChatResult(text='{"nodes": []}')

    monkeypatch.setattr(settings, "graph_plan_timeout_seconds", 0.05)
    with pytest.raises(GraphUnavailable):
        await plan_task("plan something", provider=_Slow())


async def test_mimo_worker_round_cap_is_dynamic(monkeypatch):
    """The worker loop is bounded by the configured cap, not a constant."""

    class _Chat:
        reasoning_effort = None

        def __init__(self) -> None:
            self.rounds = 0

        async def chat_with_tools(self, messages, specs):
            self.rounds += 1
            return ChatResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id=f"c{self.rounds}",
                        name="memory.search",
                        arguments={"query": f"q{self.rounds}"},
                    )
                ],
            )

    provider = _Chat()
    calls: list[str] = []

    async def _execute(session, name, args, **kwargs):
        calls.append(name)
        return {"ok": True, "spoken": "done", "path": f"/tmp/{len(calls)}.txt"}

    monkeypatch.setattr(settings, "graph_worker_max_rounds", 2)
    monkeypatch.setattr(settings, "graph_worker_reserve_seconds", 0.0)
    receipt = await run_node(
        _node(tool="not-a-tool"),
        WorkerCtx(),
        job_id="j",
        provider=provider,
        execute_fn=_execute,
    )
    assert provider.rounds == 2
    assert receipt.worker == "mimo"
    assert receipt.has_evidence is True


def test_node_budget_uses_global_timeout_when_unset(monkeypatch):
    """A planner node without an explicit timeout rides the global setting."""

    from app.cognitive.worker import _worker_budget

    monkeypatch.setattr(settings, "graph_node_timeout_seconds", 33.0)
    node = _node()
    assert node.timeout_seconds is None
    _cap, _reserve, limit = _worker_budget(node)
    assert limit == 33.0


async def test_mimo_worker_retry_reasons_harder(monkeypatch):
    """Attempt two spends the retry effort; attempt one stays fast."""

    seen: list[str | None] = []

    class _Chat:
        reasoning_effort = None

        async def chat_with_tools(self, messages, specs):
            seen.append(self.reasoning_effort)
            return ChatResult(text="nothing to do")

    monkeypatch.setattr(settings, "graph_retry_reasoning_effort", "medium")
    await run_node(
        _node(tool="not-a-tool"),
        WorkerCtx(),
        job_id="j",
        attempt=2,
        provider=_Chat(),
        execute_fn=_ok_execute,
    )
    assert seen == ["medium"]


async def test_mimo_worker_bare_ok_is_not_evidence():
    """A bare ok/spoken tool result proves nothing and must not be accepted."""

    class _Chat:
        reasoning_effort = None

        async def chat_with_tools(self, messages, specs):
            return ChatResult(
                text="",
                tool_calls=[
                    ToolCall(id="c1", name="memory.search", arguments={"query": "x"})
                ],
            )

    async def _bare(session, name, args, **kwargs):
        return {"ok": True, "spoken": "Done."}

    receipt = await run_node(
        _node(tool="not-a-tool"),
        WorkerCtx(),
        job_id="j",
        provider=_Chat(),
        execute_fn=_bare,
    )
    assert receipt.ok is True
    assert receipt.evidence == []
    assert receipt.has_evidence is False


async def test_maybe_run_graph_job_notes_planner_fallback(monkeypatch):
    """A planner outage is recorded on the job instead of vanishing."""

    from app.cognitive import delegation

    async def _boom(*args, **kwargs):
        raise GraphUnavailable("planner timed out")

    monkeypatch.setattr(settings, "delegate_graph", "on")
    monkeypatch.setattr(graph_mod, "run_graph", _boom)
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(
            ResearchSession(
                id=job_id,
                owner="master",
                mode="rt_delegate",
                status="running",
                question="do it",
                goal="do it",
                budget={},
            )
        )
        await db.commit()
    assert (
        await delegation._maybe_run_graph_job(
            job_id, "do it", actor="master", binding={}
        )
        is None
    )
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.budget["graph_fallback"]["reason"] == (
            "GraphUnavailable: planner timed out"
        )


# --------------------------------------------------------------------------- #
# Multi-shift delegation: manager report -> re-plan the remainder
# --------------------------------------------------------------------------- #


class _SequencePlanProvider:
    """Returns a different plan per call and records the planner messages."""

    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls = 0
        self.messages: list[list[str]] = []

    async def chat_structured(self, messages, *, schema, schema_name):
        self.calls += 1
        self.messages.append([str(m.content) for m in messages])
        index = min(self.calls - 1, len(self.payloads) - 1)
        return ChatResult(text=json.dumps(self.payloads[index]))


def test_shift_report_names_done_and_not_done():
    from app.cognitive.graph import shift_report

    node = _node(id="write-note", label="Write the note", tier="W")
    receipt = _receipt(node_id="write-note", spoken="Wrote it.", evidence=[])
    accepted = SupervisorVerdict(node_id="write-note", state=NodeState.DONE,
                                 ok=True, score=0.9, next=VerdictNext.ACCEPT)
    failed_node = _node(id="send-note", label="Send the note", tier="W")
    failed_receipt = _receipt(node_id="send-note", ok=False, error="not_found")
    failed = SupervisorVerdict(
        node_id="send-note", state=NodeState.FAILED, ok=False, score=0.7,
        reasons=["No such recipient"], next=VerdictNext.ESCALATE,
    )
    report = shift_report([receipt, failed_receipt], [accepted, failed],
                          [node, failed_node])
    assert "DONE: Write the note" in report
    assert "NOT_DONE: Send the note" in report
    assert "error=not_found" in report
    assert "No such recipient" in report


async def test_mimo_worker_retry_carries_supervisor_reasons():
    """A retry's prompt contains the supervisor's named failure and forbids the repeat."""

    seen: list[list[str]] = []

    class _Chat:
        reasoning_effort = None

        async def chat_with_tools(self, messages, specs):
            seen.append([str(m.content) for m in messages])
            return ChatResult(text="tried differently")

    receipt = await run_node(
        _node(tool="not-a-tool"),
        WorkerCtx(),
        job_id="j",
        attempt=2,
        hint="no such recipient",
        provider=_Chat(),
        execute_fn=_ok_execute,
    )
    assert receipt.error == "no_action_taken"
    assert any("no such recipient" in content for content in seen[0])
    assert any("Previous attempt failed" in content for content in seen[0])


async def test_run_graph_replans_the_remainder(monkeypatch):
    """A shift that leaves work open triggers a follow-up plan for it."""

    plans = _SequencePlanProvider([
        {"nodes": [{"id": "first", "label": "First step",
                    "detail": "Do the first half.", "tier": "W"}]},
        {"nodes": [{"id": "second", "label": "Second step",
                    "detail": "Finish the rest.", "tier": "W"}]},
    ])
    ran: list[str] = []
    hints: list[str | None] = []

    async def _fake_node(node, ctx, **kwargs):
        ran.append(node.id)
        hints.append(kwargs.get("hint"))
        if node.id == "first":
            return _receipt(node_id=node.id, ok=False, error="worker_failed")
        return _receipt(node_id=node.id, spoken="Finished the rest.")

    import app.cognitive.worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_node", _fake_node)
    monkeypatch.setattr(settings, "graph_max_shifts", 2)
    outcome = await run_graph("do both halves", job_id="job-shift",
                              plan_provider=plans)
    assert outcome.status == "answered"
    assert plans.calls == 2
    assert outcome.shifts == 2
    assert "WORK ALREADY ATTEMPTED" not in plans.messages[0][1]
    assert "WORK ALREADY ATTEMPTED" in plans.messages[1][1]
    assert "First step" in plans.messages[1][1]
    assert "NOT_DONE" in plans.messages[1][1]
    assert outcome.status_report
    assert outcome.evidence["shifts"] == 2
    assert ran.count("second") == 1
    assert any(hint and "worker_failed" in hint for hint in hints)


async def test_run_graph_does_not_replan_answered_work(monkeypatch):
    plans = _SequencePlanProvider([
        {"nodes": [{"id": "only", "label": "Only step",
                    "detail": "Do it.", "tier": "W"}]},
    ])

    async def _ok(node, ctx, **kwargs):
        return _receipt(node_id=node.id, spoken="Done it.")

    import app.cognitive.worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_node", _ok)
    outcome = await run_graph("do it", job_id="job-one", plan_provider=plans)
    assert outcome.status == "answered"
    assert plans.calls == 1
    assert outcome.shifts == 1


async def test_run_graph_stops_at_the_shift_cap(monkeypatch):
    plans = _SequencePlanProvider([
        {"nodes": [{"id": "stuck", "label": "Stuck step",
                    "detail": "Never works.", "tier": "W"}]},
    ])

    async def _stuck(node, ctx, **kwargs):
        return _receipt(node_id=node.id, ok=False, error="timeout")

    import app.cognitive.worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_node", _stuck)
    monkeypatch.setattr(settings, "graph_max_shifts", 2)
    outcome = await run_graph("do the impossible", job_id="job-cap",
                              plan_provider=plans)
    assert plans.calls == 2
    assert outcome.status == "failed"
    assert outcome.shifts == 2
    assert outcome.remaining
    assert "NOT_DONE" in outcome.status_report
    assert "Stuck step" in outcome.spoken


async def test_run_graph_tier_d_never_replans(monkeypatch):
    """Tier-D stops at the owner question; a re-plan must not route around it."""

    plans = _SequencePlanProvider([
        {"nodes": [{"id": "wipe", "label": "Wipe the folder",
                    "detail": "Delete everything.", "tier": "D"}]},
    ])
    outcome = await run_graph("delete everything", job_id="job-d",
                              plan_provider=plans)
    assert outcome.status == "waiting"
    assert plans.calls == 1
    assert outcome.shifts == 1


async def test_run_graph_followup_plan_failure_never_reruns_legacy(monkeypatch):
    """A follow-up planner outage after work ran ends honestly, no rerun."""

    class _PlanThenGarbage:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_structured(self, messages, *, schema, schema_name):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(text=json.dumps({"nodes": [
                    {"id": "stuck", "label": "Stuck step",
                     "detail": "Never works.", "tier": "W"},
                ]}))
            return ChatResult(text="planner melted")

    async def _stuck(node, ctx, **kwargs):
        return _receipt(node_id=node.id, ok=False, error="timeout")

    import app.cognitive.worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_node", _stuck)
    monkeypatch.setattr(settings, "graph_max_shifts", 2)
    plans = _PlanThenGarbage()
    outcome = await run_graph("do the impossible", job_id="job-followup",
                              plan_provider=plans)
    assert plans.calls == 2
    assert outcome.status == "failed"
    assert outcome.shifts == 1
    assert outcome.remaining


async def test_run_graph_first_plan_failure_still_falls_back(monkeypatch):
    """A first plan outage is pre-execution: it may still bubble to legacy."""

    class _BrokenPlanner:
        async def chat_structured(self, messages, *, schema, schema_name):
            return ChatResult(text="not json at all")

    with pytest.raises(GraphPlanError):
        await run_graph("do it", job_id="job-broken", plan_provider=_BrokenPlanner())
