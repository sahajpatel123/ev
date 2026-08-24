"""G1 commitment cancellation contract tests.

These tests deliberately exercise the owner-facing TurnGate/controller path,
not a direct SQL mutation.  Commitment creation remains covered by the
existing G1 tests and is not changed here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import luna_adapter
from app.ev.owner_turn import create_owner_turn
from app.ev.turn_controller import TurnController
from app.ev.turn_gate import create_realtime_response_payload, handle_owner_turn
from app.ev.turn_intent import TurnIntent
from app.life import service as life
from app.memory.turns import record_conversation_turn
from app.models import Event

ACTOR = "master"
TARGET = "G1 Final Commitment Proof"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "Cancel my G1 Final Commitment Proof.",
        "Delete my G1 Final Commitment Proof.",
        "Remove the G1 Final Commitment Proof commitment.",
        "Get rid of my G1 Final Commitment Proof commitment.",
        "Cancel the commitment proof.",
    ],
)
async def test_owner_cancel_phrases_are_deterministic_and_cancel_one_open_commitment(
    db_session: AsyncSession, phrase: str, monkeypatch: pytest.MonkeyPatch
):
    luna_calls: list[str] = []

    async def unexpected_luna(*args, **kwargs):
        luna_calls.append(str(args[0] if args else ""))
        raise AssertionError("explicit commitment cancellation must not call Luna")

    monkeypatch.setattr(luna_adapter.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(luna_adapter, "_call_luna", unexpected_luna)

    created = await life.create_commitment(db_session, actor=ACTOR, description=TARGET)
    await db_session.commit()

    intent = await luna_adapter.classify_intent(phrase)
    assert intent.route == "STATE_MUTATION"
    assert intent.operation == "COMMITMENT_CANCEL"
    assert intent.needs_clarification is False
    assert luna_calls == []

    result = await TurnController(db_session, actor=ACTOR).handle_turn(phrase)
    assert result.ok, result.error
    assert result.route == "STATE_MUTATION"
    assert result.operation == "COMMITMENT_CANCEL"
    await db_session.commit()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    target = next(row for row in rows if row["id"] == created["commitment"]["id"])
    assert target["status"] == "CANCELLED"
    assert luna_calls == []


@pytest.mark.asyncio
async def test_cancel_meta_questions_remain_conversation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    luna_calls: list[str] = []

    async def unexpected_luna(*args, **kwargs):
        luna_calls.append(str(args[0] if args else ""))
        raise AssertionError("meta cancellation questions must not call Luna")

    monkeypatch.setattr(luna_adapter.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(luna_adapter, "_call_luna", unexpected_luna)

    for phrase in ("Why would I cancel a commitment?", "What does cancelled mean?"):
        intent = await luna_adapter.classify_intent(phrase)
        assert intent.route == "CONVERSATION"
        assert intent.operation == "UNKNOWN"
    assert luna_calls == []


@pytest.mark.asyncio
async def test_cancel_exact_match_wins_and_ambiguous_substring_clarifies(
    db_session: AsyncSession,
):
    exact = await life.create_commitment(db_session, actor=ACTOR, description=TARGET)
    longer = await life.create_commitment(
        db_session, actor=ACTOR, description=f"{TARGET} extended"
    )
    await db_session.commit()

    exact_result = await TurnController(db_session, actor=ACTOR).handle_turn(
        "Cancel my G1 Final Commitment Proof."
    )
    assert exact_result.ok
    assert exact_result.operation == "COMMITMENT_CANCEL"
    await db_session.commit()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {row["id"]: row for row in rows}
    assert by_id[exact["commitment"]["id"]]["status"] == "CANCELLED"
    assert by_id[longer["commitment"]["id"]]["status"] == "OPEN"

    # A short reference with two plausible OPEN candidates must never pick one.
    a = await life.create_commitment(db_session, actor=ACTOR, description="Proof Alpha")
    b = await life.create_commitment(db_session, actor=ACTOR, description="Proof Beta")
    await db_session.commit()
    ambiguous = await TurnController(db_session, actor=ACTOR).handle_turn(
        "Cancel the proof commitment."
    )
    assert ambiguous.ok
    assert ambiguous.route == "CLARIFICATION"
    assert ambiguous.operation == "COMMITMENT_CANCEL"
    assert ambiguous.needs_clarification
    await db_session.rollback()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {row["id"]: row for row in rows}
    assert by_id[a["commitment"]["id"]]["status"] == "OPEN"
    assert by_id[b["commitment"]["id"]]["status"] == "OPEN"


@pytest.mark.asyncio
async def test_physical_pronoun_cancel_uses_same_session_commitment_context(
    db_session: AsyncSession,
):
    target = await life.create_commitment(db_session, actor=ACTOR, description=TARGET)
    unrelated = await life.create_commitment(
        db_session,
        actor=ACTOR,
        description="No, but did you set a reminder for G1 final commitment proof?",
    )
    await db_session.commit()

    await record_conversation_turn(
        db_session,
        text="Hey Eve, when is my G1 final commitment proof due?",
        role="owner",
        source="voice",
        conversation_id="f5cf845f-9c5c-441c-814b-d1d7992a5da8",
        live_session_id="physical-cancel-context",
        transcript_source="provider",
    )
    await db_session.commit()

    result = await TurnController(
        db_session, actor=ACTOR, session_id="physical-cancel-context"
    ).handle_turn("Okay, so can you delete it?")
    assert result.ok, result.error
    assert result.route == "STATE_MUTATION"
    assert result.operation == "COMMITMENT_CANCEL"
    await db_session.commit()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    by_id = {row["id"]: row for row in rows}
    assert by_id[target["commitment"]["id"]]["status"] == "CANCELLED"
    assert by_id[unrelated["commitment"]["id"]]["status"] == "OPEN"


@pytest.mark.asyncio
async def test_cancel_not_found_and_already_cancelled_are_truthful(
    db_session: AsyncSession,
):
    not_found = await TurnController(db_session, actor=ACTOR).handle_turn(
        "Delete my Missing Commitment Proof."
    )
    assert not not_found.ok
    assert not_found.route == "STATE_MUTATION"
    assert not_found.operation == "COMMITMENT_CANCEL"
    assert not_found.error == "not_found"

    created = await life.create_commitment(db_session, actor=ACTOR, description=TARGET)
    await life.update_commitment(
        db_session,
        actor=ACTOR,
        commitment_id=created["commitment"]["id"],
        status="CANCELLED",
    )
    await db_session.commit()

    already = await TurnController(db_session, actor=ACTOR).handle_turn("Cancel it again.")
    assert already.ok
    assert already.route == "STATE_MUTATION"
    assert already.operation == "COMMITMENT_CANCEL"
    assert "already cancelled" in (already.owner_message or "").lower()

    cancelled_events = (
        await db_session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == "commitment.cancelled")
        )
    ).scalar_one()
    assert cancelled_events == 1


@pytest.mark.asyncio
async def test_gate_replay_is_idempotent_and_cancel_leaves_open_snapshot_empty(
    db_session: AsyncSession,
):
    created = await life.create_commitment(db_session, actor=ACTOR, description=TARGET)
    await db_session.commit()

    owner_turn = create_owner_turn(
        live_session_id="g1-cancel-test-session",
        provider_item_id=None,
        owner_id=ACTOR,
        device_id=None,
        transcript="Cancel my G1 Final Commitment Proof.",
        transcript_source="test",
    )
    first = await handle_owner_turn(db_session, owner_turn)
    assert first.ok
    payload = create_realtime_response_payload(owner_turn, first)
    assert payload["type"] == "response.create"
    assert "Cancelled" in payload["response"]["instructions"]
    await db_session.commit()

    replay = await handle_owner_turn(db_session, owner_turn)
    assert replay.ok
    assert replay.operation == "COMMITMENT_CANCEL"
    assert replay.owner_message == first.owner_message
    await db_session.rollback()

    rows = await life.list_commitments(db_session, actor=ACTOR, open_only=False)
    target = next(row for row in rows if row["id"] == created["commitment"]["id"])
    assert target["status"] == "CANCELLED"
    open_rows = await life.list_commitments(db_session, actor=ACTOR, open_only=True)
    assert all(row["id"] != created["commitment"]["id"] for row in open_rows)

    snapshot = await life.situation_snapshot(db_session, actor=ACTOR)
    assert all(
        row["id"] != created["commitment"]["id"]
        for row in snapshot["open_commitments"]
    )

    read = await TurnController(db_session, actor=ACTOR).handle_turn(
        "When is my G1 Final Commitment Proof due?"
    )
    assert read.ok
    assert "cancel" in (read.owner_message or "").lower()
    assert all(
        row.get("status") != "OPEN" for row in (read.canonical_data or [])
    )

    cancelled_events = (
        await db_session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == "commitment.cancelled")
        )
    ).scalar_one()
    assert cancelled_events == 1


@pytest.mark.asyncio
async def test_turn_intent_contract_exposes_commitment_cancel():
    assert "COMMITMENT_CANCEL" in luna_adapter.EMIT_INTENT_TOOL["parameters"]["properties"]["operation"]["enum"]
    assert TurnIntent(route="STATE_MUTATION", operation="COMMITMENT_CANCEL").operation == "COMMITMENT_CANCEL"
