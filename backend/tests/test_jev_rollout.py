"""JEV rollout phase tests (offline, key-free).

- Phase 2: non-coding call sites route through the role resolver.
- Phase 3: the voice turn's text-reasoning hint follows the decision role while
  the mouth stays gpt-realtime-2.1-mini and ASR/TTS are untouched.
- Phase 4/5: a JEV decision call is audited but never writes memories, events,
  or actions by itself.
- Phase 6: the offline JEV eval gate passes without a key or network.

The kernel decision turn and the provider transport are covered in
``test_gateway_streaming.py``.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.contracts import ChatResult


async def test_phase2_spark_task_routes_through_role(monkeypatch) -> None:
    from app.ev import spark_task

    calls: dict = {}

    async def fake_structured(messages, *, schema, schema_name, model=None, reasoning_effort=None):
        calls["schema_name"] = schema_name
        calls["reasoning_effort"] = reasoning_effort
        return ChatResult(text='{"family":"message","tool":"send_message","confidence":0.9}')

    monkeypatch.setattr("app.gateway.roles.chat_structured_via_role", fake_structured)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)

    await spark_task._spark_decide("text mom I'm late", family_hint="message")
    assert calls.get("schema_name") == "life_task"
    assert calls.get("reasoning_effort") == "low"


async def test_phase2_desk_inventory_routes_through_role(monkeypatch) -> None:
    from app.ev import desk_meaning

    async def fake_structured(messages, *, schema, schema_name, model=None, reasoning_effort=None):
        assert schema_name == "desk_payload"
        return ChatResult(text='{"items": ["milk"], "empty": false}')

    monkeypatch.setattr("app.gateway.roles.chat_structured_via_role", fake_structured)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)

    items = await desk_meaning.spark_inventory("buy milk", label="shopping")
    assert items == ["milk"]


async def test_phase2_phone_tool_routes_through_role(monkeypatch) -> None:
    from app.ev import spark_phone

    calls: dict = {}

    async def fake_structured(messages, *, schema, schema_name, model=None, reasoning_effort=None):
        calls["schema_name"] = schema_name
        return ChatResult(text='{"tool": "evie_turn", "args": {"owner_turn": "hi"}}')

    monkeypatch.setattr("app.gateway.roles.chat_structured_via_role", fake_structured)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.text_brain_active", lambda: True)
    monkeypatch.setattr(spark_phone, "should_ask_spark", lambda _text: True)

    await spark_phone.spark_phone_tool("what's on my screen")
    assert calls.get("schema_name") == "phone_mac_tool"


async def test_phase2_file_intelligence_routes_through_role(monkeypatch) -> None:
    from app.ev import laptop_files

    async def fake_structured(messages, *, schema, schema_name, model=None, reasoning_effort=None):
        assert schema_name == "file_content"
        return ChatResult(text='{"content": "hello from the decision role"}')

    monkeypatch.setattr("app.gateway.roles.chat_structured_via_role", fake_structured)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)

    content, source = await laptop_files._intelligent_rewrite("", "write a note", create=True)
    assert content == "hello from the decision role"
    assert source == "jev"


async def test_phase2_brain_file_plan_uses_role_text(monkeypatch) -> None:
    from app.ev import brain_file_runner

    async def fake_chat(messages, *, model=None, temperature=0.7, reasoning_effort=None):
        return ChatResult(
            text='{"ops": [{"op": "search", "args": {"query": "notes"}}]}',
            usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.gateway.roles.chat_via_role", fake_chat)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.text_role_model", lambda: "typesafe/jev-1.13")
    monkeypatch.setattr("app.gateway.roles.note_text_call", lambda usage=None: None)

    plan, source, degraded = await brain_file_runner.plan_with_brain("find my notes")
    assert plan["ops"][0]["op"] == "search"
    assert source == "typesafe/jev-1.13"
    assert degraded is False


def test_phase3_voice_turn_model_follows_decision_role(monkeypatch) -> None:
    from app.voice.pipeline import _voice_turn_model

    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_model", "typesafe/jev-1.13")
    assert _voice_turn_model() == "typesafe/jev-1.13"

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    assert _voice_turn_model() == settings.muse_spark_model or _voice_turn_model() == "muse-spark-1.3-contributor"

    from app.gateway.roles import resolve_voice_mouth

    mouth = resolve_voice_mouth()
    assert mouth.provider == "openai-realtime"
    assert mouth.model == settings.openai_realtime_model


async def test_phase45_jev_decision_writes_no_memory_or_action(
    db_session, monkeypatch
) -> None:
    from sqlalchemy import select

    from app.contracts import RequestEnvelope
    from app.gateway.openrouter_jev import (
        JevAnswer,
        JevDecisionResult,
        JevQuestion,
        normalize_questions,
    )
    from app.gateway.service import ModelGateway
    from app.models import Event, Memory, ModelCallLog
    from app.services.model_call import log_model_call

    class StubDecisionProvider:
        name = "openrouter"

        def decision_payload(self, state, questions, *, model=None):
            return {
                "model": "typesafe/jev-1.13",
                "state": state,
                "questions": normalize_questions(questions),
            }

        async def decide(self, state, questions, *, model=None):
            return JevDecisionResult(
                model="typesafe/jev-1.13",
                answers={"route": JevAnswer(type="choice", choice="read_only")},
                usage={"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.0},
                response_id="stub-decision-1",
                latency_ms=1.0,
            )

    call = await ModelGateway(StubDecisionProvider()).decide(
        {"request": "read the visible settings only"},
        {
            "route": JevQuestion(
                type="choice",
                instructions="Pick the safe route.",
                criteria={"read_only": "Only read data.", "refuse": "Refuse."},
            )
        },
        envelope=RequestEnvelope(request_id="phase45", strategy={}),
    )
    assert call.status == "ok"
    assert call.decision_answers is not None
    assert call.decision_answers["route"].choice == "read_only"

    await log_model_call(db_session, call=call, actor="test")
    assert (await db_session.execute(select(Event))).scalars().all() == []
    assert (await db_session.execute(select(Memory))).scalars().all() == []
    rows = (await db_session.execute(select(ModelCallLog))).scalars().all()
    assert len(rows) == 1


async def test_phase5_record_outcome_requires_an_existing_decision(db_session) -> None:
    from uuid import uuid4

    from app.ev.decisions import record_outcome

    with pytest.raises(KeyError):
        await record_outcome(
            db_session,
            uuid4(),
            expected_outcome="expected",
            actual_outcome="actual",
            lesson=None,
        )


async def test_phase6_jev_eval_gate_passes() -> None:
    from app.scripts.eval_gates import run_jev_gate

    result = await run_jev_gate()
    failed = [
        {"name": check.name, "detail": check.detail}
        for check in result.checks
        if not check.passed
    ]
    assert result.passed, failed


def _decision_call(answers: dict) -> object:
    """Build a GatewayCall carrying validated typed answers for the fakes."""

    from app.contracts import ChatResult, RequestEnvelope
    from app.gateway.openrouter_jev import JevAnswer
    from app.gateway.service import GatewayCall

    parsed = {}
    for question_id, value in answers.items():
        if isinstance(value, str):
            parsed[question_id] = JevAnswer(type="choice", choice=value)
        else:
            parsed[question_id] = JevAnswer(type="noul", noul=float(value))
    return GatewayCall(
        provider="openrouter",
        request_id="jev-test-decision",
        envelope=RequestEnvelope(request_id="jev-test-decision", strategy={}),
        result=ChatResult(text="", usage={}, model="typesafe/jev-1.13"),
        status="ok",
        decision_answers=parsed,
    )


def _forbid_spark(monkeypatch) -> None:
    def _raise() -> None:
        raise AssertionError("Spark must not be used outside the code lane")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", _raise)


def _enable_jev_kernel(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "jev_kernel")
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)


async def test_jev_life_task_routing_uses_choices(monkeypatch) -> None:
    from app.ev import spark_task

    captured: dict = {}

    async def fake_decide(state, questions, *, session=None, actor="system"):
        captured["questions"] = set(questions)
        captured["state"] = state
        return _decision_call(
            {"family": "messages", "manner": "particular", "focus": "when", "latest": 1.0}
        )

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    monkeypatch.setattr(spark_task, "last_life_job", lambda: None)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    decision = await spark_task._spark_decide(
        "when did mom's text arrive", family_hint="messages"
    )
    assert decision is not None
    assert (
        decision.family,
        decision.manner,
        decision.focus,
        decision.latest,
        decision.source,
    ) == ("messages", "particular", "when", True, "jev")
    assert captured["questions"] == {"family", "manner", "focus", "latest"}


async def test_jev_turn_act_uses_choice(monkeypatch) -> None:
    from app.ev import spark_act

    async def fake_decide(state, questions, *, session=None, actor="system"):
        assert set(questions) == {"act"}
        return _decision_call({"act": "life"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    assert await spark_act._spark_decide("text mom I'm late") == "life"


async def test_jev_camera_action_uses_choice(monkeypatch) -> None:
    from app.ev import spark_look

    async def fake_decide(state, questions, *, session=None, actor="system"):
        return _decision_call({"action": "look"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)
    assert await spark_look._spark_decide("look at this") == "look"

    async def chat_decide(state, questions, *, session=None, actor="system"):
        return _decision_call({"action": "chat"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", chat_decide)
    assert await spark_look._spark_decide("what's the weather") is None


async def test_jev_phone_tool_uses_choice_with_deterministic_args(monkeypatch) -> None:
    from app.ev import spark_phone

    async def fake_decide(state, questions, *, session=None, actor="system"):
        assert "tool" in questions
        return _decision_call({"tool": "search_memory"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    monkeypatch.setattr("app.gateway.roles.text_brain_active", lambda: True)
    monkeypatch.setattr(spark_phone, "should_ask_spark", lambda _text: True)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    tool, args = await spark_phone.spark_phone_tool("what did I say about the trip")
    assert tool == "search_memory"
    assert args["query"] == "what did I say about the trip"


async def test_jev_desk_act_uses_choice_with_deterministic_items(monkeypatch) -> None:
    from app.ev import desk_meaning

    async def fake_decide(state, questions, *, session=None, actor="system"):
        assert "act" in questions
        return _decision_call({"act": "write_list"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    monkeypatch.setattr(desk_meaning, "extract_inventory", lambda _text: ["milk", "eggs"])
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    goal = await desk_meaning._jev_act("add milk and eggs to the shopping list", last_path=None)
    assert goal is not None
    assert goal["channel"] == "file"
    assert "milk" in str(goal["goal"])


async def test_jev_turn_intent_uses_route_and_operation_choices(monkeypatch) -> None:
    from app.ev import luna_adapter

    async def fake_decide(state, questions, *, session=None, actor="system"):
        assert set(questions) == {"route", "operation"}
        return _decision_call({"route": "STATE_QUERY", "operation": "GOAL_GET"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    intent = await luna_adapter._call_spark_intent("what's my goal", None)
    assert intent.route == "STATE_QUERY"
    assert intent.operation == "GOAL_GET"


async def test_jev_file_plan_picks_a_deterministic_candidate(monkeypatch) -> None:
    from app.ev import brain_file_runner

    async def pick_search(state, questions, *, session=None, actor="system"):
        assert state["candidates"]
        return _decision_call({"op": "search"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", pick_search)
    _enable_jev_kernel(monkeypatch)
    _forbid_spark(monkeypatch)

    plan, source, degraded = await brain_file_runner.plan_with_brain("find my notes")
    assert plan["ops"][0]["op"] == "search"
    assert source == "jev"
    assert degraded is False

    async def pick_none(state, questions, *, session=None, actor="system"):
        return _decision_call({"op": "none"})

    monkeypatch.setattr("app.gateway.roles.decide_via_role", pick_none)
    plan, source, degraded = await brain_file_runner.plan_with_brain("find my notes")
    assert plan["ops"] == []
    assert source == "deterministic"
    assert degraded is True
