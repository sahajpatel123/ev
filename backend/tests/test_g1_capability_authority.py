"""G1 capability authority + commitment cancel execution tests.

Laws under test:
- Evie's capabilities derive from canonical registry + TurnController truth,
  never from GPT-Realtime tool visibility.
- Explicit owner cancellation language must resolve deterministically even
  when the owner paraphrases the stored name or chains verbs
  ("can you cancel or delete the X").
- Bare ability questions ("Can you cancel commitments?") answer from
  capability truth and never mutate.
- The response.create envelope carries canonical outcomes so Realtime can
  reword but never reinterpret them into invented inabilities.

All data is isolated fixture data; owner state is untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import luna_adapter
from app.ev.capability_registry import (
    capability_diagnostics,
    semantic_capabilities,
    semantic_capability_answer,
)
from app.ev.luna_adapter import _commitment_cancel_query
from app.ev.owner_turn import create_owner_turn
from app.ev.turn_controller import TurnController
from app.ev.turn_gate import create_realtime_response_payload, handle_owner_turn
from app.life import service as life
from app.memory.turns import record_conversation_turn
from app.models import Event

ACTOR = "master"


def _no_luna(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def unexpected_luna(*args, **kwargs):
        calls.append(str(args[0] if args else ""))
        raise AssertionError("this turn must be deterministic (no Luna)")

    monkeypatch.setattr(luna_adapter.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(luna_adapter, "_call_luna", unexpected_luna)
    return calls


@pytest.mark.asyncio
async def test_exact_owner_transcript_chained_verbs_cancels(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Owner transcript 20:33:46 IST: '...can you cancel or delete the X?'."""
    calls = _no_luna(monkeypatch)
    target = await life.create_commitment(
        db_session, actor=ACTOR, description="g1 final commitment proof"
    )
    noise = await life.create_commitment(
        db_session,
        actor=ACTOR,
        description="No, but did you set a reminder for G1 final commitment proof?",
    )
    await db_session.commit()

    phrase = "Okay, so now, can you cancel or delete the G1 final commitment proof?"
    assert _commitment_cancel_query(phrase).lower() == "g1 final commitment proof"

    result = await TurnController(db_session, actor=ACTOR).handle_turn(phrase)
    assert result.ok, result.error
    assert result.route == "STATE_MUTATION"
    assert result.operation == "COMMITMENT_CANCEL"
    assert "cancel" in (result.owner_message or "").lower()
    await db_session.commit()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {r["id"]: r for r in rows}
    assert by_id[target["commitment"]["id"]]["status"] == "CANCELLED"
    assert by_id[noise["commitment"]["id"]]["status"] == "OPEN"
    assert calls == []


@pytest.mark.asyncio
async def test_owner_paraphrase_without_middle_word_still_resolves(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Owner transcript 20:32:25 IST omits 'final': 'cancel my G1 commitment proof?'.

    Production had three noisy OPEN rows whose long descriptions contain the
    same words; only the canonical row is essentially the reference itself.
    """
    calls = _no_luna(monkeypatch)
    target = await life.create_commitment(
        db_session, actor=ACTOR, description="g1 final commitment proof"
    )
    lookalike_question = await life.create_commitment(
        db_session,
        actor=ACTOR,
        description="No, but did you set a reminder for G1 final commitment proof?",
    )
    lookalike_story = await life.create_commitment(
        db_session,
        actor=ACTOR,
        description=(
            "Okay, then I want you to check for the system reminders which "
            "you have set as G1 final commitment proof"
        ),
    )
    await db_session.commit()

    phrase = "Would you do cancel my G1 commitment proof?"
    intent = await luna_adapter.classify_intent(phrase)
    assert intent.route == "STATE_MUTATION"
    assert intent.operation == "COMMITMENT_CANCEL"

    result = await TurnController(db_session, actor=ACTOR).handle_turn(phrase)
    assert result.ok, result.error
    assert result.operation == "COMMITMENT_CANCEL"
    await db_session.commit()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {r["id"]: r for r in rows}
    assert by_id[target["commitment"]["id"]]["status"] == "CANCELLED"
    assert by_id[lookalike_question["commitment"]["id"]]["status"] == "OPEN"
    assert by_id[lookalike_story["commitment"]["id"]]["status"] == "OPEN"
    assert calls == []


@pytest.mark.asyncio
async def test_token_overlap_with_two_equal_candidates_clarifies(
    db_session: AsyncSession,
):
    a = await life.create_commitment(
        db_session, actor=ACTOR, description="alpha final commitment proof"
    )
    b = await life.create_commitment(
        db_session, actor=ACTOR, description="beta final commitment proof"
    )
    await db_session.commit()
    result = await TurnController(db_session, actor=ACTOR).handle_turn(
        "Cancel my alpha commitment proof."
    )
    # alpha's row covers the reference uniquely at >=0.6 coverage -> wins.
    assert result.ok
    await db_session.commit()
    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {r["id"]: r for r in rows}
    assert by_id[a["commitment"]["id"]]["status"] == "CANCELLED"
    assert by_id[b["commitment"]["id"]]["status"] == "OPEN"


@pytest.mark.asyncio
async def test_pronoun_cancel_after_read_still_deterministic(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    calls = _no_luna(monkeypatch)
    target = await life.create_commitment(
        db_session, actor=ACTOR, description="G1 Capability Cancel Internal 2"
    )
    await db_session.commit()
    await record_conversation_turn(
        db_session,
        text="When is my G1 Capability Cancel Internal 2 due?",
        role="owner",
        source="voice",
        conversation_id="cap-auth-test",
        live_session_id="cap-auth-session",
        transcript_source="provider",
    )
    await db_session.commit()
    result = await TurnController(
        db_session, actor=ACTOR, session_id="cap-auth-session"
    ).handle_turn("Okay, delete it.")
    assert result.ok, result.error
    assert result.route == "STATE_MUTATION"
    assert result.operation == "COMMITMENT_CANCEL"
    await db_session.commit()
    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=True)
    assert all(r["id"] != target["commitment"]["id"] for r in rows)
    assert calls == []


@pytest.mark.asyncio
async def test_ability_questions_answer_from_capability_truth(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    calls = _no_luna(monkeypatch)
    created = await life.create_commitment(
        db_session, actor=ACTOR, description="must not be touched"
    )
    await db_session.commit()

    for phrase in ("Can you cancel commitments?", "What can you do with commitments?"):
        intent = await luna_adapter.classify_intent(phrase)
        assert intent.route == "STATE_QUERY", phrase
        assert intent.operation == "CAPABILITY_QUERY", phrase
        result = await TurnController(db_session, actor=ACTOR).handle_turn(phrase)
        assert result.ok
        spoken = (result.owner_message or "").lower()
        assert "cancel commitments" in spoken, phrase

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=True)
    assert any(r["id"] == created["commitment"]["id"] for r in rows)
    assert calls == []


def test_capability_registry_is_authoritative_and_derived():
    caps = semantic_capabilities()
    assert caps["COMMITMENT_CANCEL"]["registered"] is True
    assert caps["COMMITMENT_CANCEL"]["realtime_direct_tool"] is False
    assert caps["COMMITMENT_CANCEL"]["execution_owner"] == "TurnGate/Core"
    diag = capability_diagnostics()["commitment_cancel"]
    assert diag == {
        "registered": True,
        "controller_bound": True,
        "policy_allowed": True,
        "current_runtime_available": True,
        "realtime_direct_tool": False,
        "execution_owner": "TurnGate/Core",
    }


@pytest.mark.asyncio
async def test_negative_meta_questions_remain_conversation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    calls = _no_luna(monkeypatch)
    for phrase in (
        "Why do people cancel commitments?",
        "What does cancelling a commitment mean?",
        "Could cancelling commitments be a bad habit?",
    ):
        intent = await luna_adapter.classify_intent(phrase)
        assert intent.route == "CONVERSATION", phrase
        result = await TurnController(db_session, actor=ACTOR).handle_turn(phrase)
        assert result.route == "CONVERSATION", phrase
    events = (
        await db_session.execute(
            select(func.count()).select_from(Event).where(Event.event_type == "commitment.cancelled")
        )
    ).scalar_one()
    assert events == 0
    assert calls == []


@pytest.mark.asyncio
async def test_gate_failure_envelope_carries_canonical_reason_and_forbids_inability_claims(
    db_session: AsyncSession,
):
    owner_turn = create_owner_turn(
        live_session_id="cap-auth-fail-session",
        provider_item_id=None,
        owner_id=ACTOR,
        device_id=None,
        transcript="Cancel my Missing Proof commitment.",
        transcript_source="test",
    )
    result = await handle_owner_turn(db_session, owner_turn)
    assert not result.ok
    assert result.error == "not_found"
    payload = create_realtime_response_payload(owner_turn, result)
    instructions = payload["response"]["instructions"]
    assert "couldn't find" in instructions.lower()
    assert "never claim evie lacks the ability" in instructions.lower()
    assert "not_found" not in instructions  # raw error codes must not leak raw


@pytest.mark.asyncio
async def test_gate_success_envelope_states_action_already_happened(
    db_session: AsyncSession,
):
    await life.create_commitment(db_session, actor=ACTOR, description="Envelope Target")
    await db_session.commit()
    owner_turn = create_owner_turn(
        live_session_id="cap-auth-ok-session",
        provider_item_id=None,
        owner_id=ACTOR,
        device_id=None,
        transcript="Cancel my Envelope Target commitment.",
        transcript_source="test",
    )
    result = await handle_owner_turn(db_session, owner_turn)
    assert result.ok
    payload = create_realtime_response_payload(owner_turn, result)
    instructions = payload["response"]["instructions"]
    assert "Cancelled your commitment" in instructions
    assert "already succeeded in evie's backend" in instructions.lower()


@pytest.mark.asyncio
async def test_full_gate_internal_cancel_end_to_end(db_session: AsyncSession):
    """Part 14: full authoritative path with post-write verification."""
    await life.create_commitment(
        db_session, actor=ACTOR, description="G1 Capability Cancel Internal"
    )
    await db_session.commit()

    owner_turn = create_owner_turn(
        live_session_id="cap-auth-e2e",
        provider_item_id=None,
        owner_id=ACTOR,
        device_id=None,
        transcript="Cancel my G1 Capability Cancel Internal commitment.",
        transcript_source="test",
    )
    first = await handle_owner_turn(db_session, owner_turn)
    assert first.ok, first.error
    assert first.route == "STATE_MUTATION"
    assert first.operation == "COMMITMENT_CANCEL"
    assert first.entity_refs and first.entity_refs[0]["entity_type"] == "commitment"
    assert first.entity_refs[0]["result"] == "CANCELLED"
    await db_session.commit()

    open_rows = await life.list_commitments(db_session, actor=ACTOR, open_only=True)
    assert all(
        r["description"] != "G1 Capability Cancel Internal" for r in open_rows
    )
    cancelled_events = (
        await db_session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "commitment.cancelled",
                Event.content["description"].as_string() == "G1 Capability Cancel Internal",
            )
        )
    ).scalar_one()
    assert cancelled_events == 1


@pytest.mark.asyncio
async def test_semantic_capability_answer_truth():
    ans = semantic_capability_answer("commitments")
    assert ans["authority"] == "capability_registry+TurnController"
    assert "COMMITMENT_CANCEL" in ans["supported"]
    assert semantic_capability_answer("projects")["spoken"].startswith("Yes")
