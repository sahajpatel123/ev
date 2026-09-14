"""Presence service hermetic tests (Goal Presence OS V1).

Offline SQLite via the autouse ``fresh_db`` fixture; every behavior here goes
through the real ``app.presence.service`` functions (no reimplementation).
Origin devices are left as ``None`` so no ``Device`` rows are needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceCondition
from app.presence import service as presence


async def test_create_contract_active_by_default(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Watch the build")
    assert row.state == "ACTIVE"
    assert row.interruption_policy == "NORMAL"
    assert row.autonomy_policy == "SAFE_DIGITAL"


async def test_create_contract_draft_when_not_activated(db_session: AsyncSession) -> None:
    row = await presence.create_contract(
        db_session, objective="Watch the build", activate=False
    )
    assert row.state == "DRAFT"


async def test_create_contract_derives_policies_from_text(db_session: AsyncSession) -> None:
    row = await presence.create_contract(
        db_session, objective="Don't bother me unless blocked — ping me only if stuck"
    )
    assert row.interruption_policy == "ONLY_IF_BLOCKED"

    read_only = await presence.create_contract(
        db_session, objective="Just look, don't change anything"
    )
    assert read_only.autonomy_policy == "READ_ONLY"


async def test_illegal_transition_raises(db_session: AsyncSession) -> None:
    row = await presence.create_contract(
        db_session, objective="Draft thing", activate=False
    )
    assert row.state == "DRAFT"
    with pytest.raises(ValueError):
        await presence.transition(db_session, row, "COMPLETED")


async def test_park_resume_roundtrip_bumps_version(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Park me")
    v0 = int(row.version or 0)
    await presence.transition(db_session, row, "PARKED")
    assert row.state == "PARKED"
    await presence.transition(db_session, row, "ACTIVE")
    assert row.state == "ACTIVE"
    assert int(row.version or 0) == v0 + 2


async def test_upsert_node_idempotent_preserves_terminal(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Graph thing")
    first = await presence.upsert_node(
        db_session, row, node_id="n1", kind="CORE_READ", target="CORE"
    )
    assert first["status"] == "PENDING"
    marked = await presence.mark_node(db_session, row, "n1", "SUCCEEDED")
    assert marked is not None and marked["status"] == "SUCCEEDED"
    second = await presence.upsert_node(
        db_session, row, node_id="n1", kind="CORE_READ", target="CORE"
    )
    assert second["status"] == "SUCCEEDED"


async def test_mark_node_terminal_never_flips(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Fence thing")
    await presence.upsert_node(
        db_session, row, node_id="n1", kind="CORE_READ", target="CORE"
    )
    await presence.mark_node(db_session, row, "n1", "SUCCEEDED")
    again = await presence.mark_node(db_session, row, "n1", "FAILED")
    assert again is not None
    assert again["status"] == "SUCCEEDED"


async def test_condition_time_past_true_future_false(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Timed thing")
    past = await presence.add_condition(
        db_session,
        row,
        cond_class="TIME",
        payload={"at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert await presence.evaluate_condition(db_session, past) is True

    future = await presence.add_condition(
        db_session,
        row,
        cond_class="TIME",
        payload={"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    assert await presence.evaluate_condition(db_session, future) is False


async def test_condition_custom_predicate_over_context(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Predicate thing")
    cond = await presence.add_condition(
        db_session,
        row,
        cond_class="CUSTOM_PREDICATE",
        payload={"predicate": {"path": "task.status", "op": "eq", "value": "done"}},
    )
    assert (
        await presence.evaluate_condition(
            db_session, cond, context={"task": {"status": "done"}}
        )
        is True
    )


async def test_condition_unknown_class_false(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Unknown thing")
    cond = PresenceCondition(
        contract_id=row.id, cond_class="NOPE_UNKNOWN", payload={}, state="PENDING"
    )
    db_session.add(cond)
    await db_session.flush()
    assert await presence.evaluate_condition(db_session, cond) is False


async def test_resolve_continue_with_one_active(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Active thing")
    out = await presence.resolve_short_command(db_session, "continue")
    assert out["command"] == "CONTINUE"
    assert out["resolved"] is True
    assert out["goal_id"] == str(row.id)


async def test_resolve_park_with_none_active_unresolves(db_session: AsyncSession) -> None:
    out = await presence.resolve_short_command(db_session, "park this")
    assert out["command"] == "PARK"
    assert out["resolved"] is False


def test_diagnose_failure_maps_device_offline() -> None:
    out = presence.diagnose_failure(error_code="TARGET_DEVICE_OFFLINE")
    assert out["boundary"] == "device_offline"
    assert out["recovery"] == "wait"


def test_diagnose_failure_unknown_asks_owner() -> None:
    out = presence.diagnose_failure(error_code="SOME_WEIRD_XYZ_123")
    assert out["boundary"] == "unknown"
    assert out["recovery"] == "ask_owner"


def test_attention_verdict_approval_is_urgent() -> None:
    assert (
        presence.attention_verdict(interruption="NORMAL", approval_required=True).value
        == "URGENT_PUSH"
    )


def test_attention_verdict_silent_until_complete_incomplete_is_silent() -> None:
    assert (
        presence.attention_verdict(
            interruption="SILENT_UNTIL_COMPLETE", completed=False
        ).value
        == "SILENT"
    )
