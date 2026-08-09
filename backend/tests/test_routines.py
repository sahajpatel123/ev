"""Tests for routines & automations: scheduling, triggers, approval, duplicate
prevention, failure recovery, observability, disable, and undo."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Event
from app.routines.schedule import next_run_after, validate_cron
from app.routines.service import (
    approve_run,
    consider_event,
    create_routine,
    execute_run,
    list_runs,
    manual_run,
    retry_run,
    rollback_run,
    tick,
)
from app.schemas import RoutineCreate, RoutineRunDecisionRequest


# --------------------------------------------------------------------------- #
# Cron parsing
# --------------------------------------------------------------------------- #


def test_next_run_after_basic_fields() -> None:
    after = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    assert next_run_after("* * * * *", after) == datetime(2026, 8, 9, 10, 1, tzinfo=UTC)
    assert next_run_after("*/5 * * * *", after) == datetime(2026, 8, 9, 10, 5, tzinfo=UTC)
    assert next_run_after("30 9 * * 1-5", after) == datetime(2026, 8, 10, 9, 30, tzinfo=UTC)


def test_cron_validation_rejects_bad_expressions() -> None:
    validate_cron("*/10 * * * *")
    with pytest.raises(ValueError):
        validate_cron("61 * * * *")
    with pytest.raises(ValueError):
        validate_cron("*/0 * * * *")
    with pytest.raises(ValueError):
        validate_cron("* * * *")


# --------------------------------------------------------------------------- #
# Scheduled routines
# --------------------------------------------------------------------------- #


async def test_scheduled_routine_tick_executes_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="brief",
            kind="scheduled",
            schedule="* * * * *",
            backfill_max=0,
            action_type="hud_card",
            action_payload={"title": "morning brief"},
        ),
    )
    now = datetime(2026, 8, 9, 10, 0, 30, tzinfo=UTC)
    routine.next_run_at = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)

    outcome = await tick(db_session, now=now)
    await db_session.commit()

    assert outcome.created == 1
    assert outcome.failed == 0
    run = outcome.runs[0]
    assert run.status == "executed"
    assert run.action_id is not None
    assert run.kind == "scheduled"
    assert run.scheduled_for == datetime(2026, 8, 9, 10, 0, tzinfo=UTC)

    second = await tick(db_session, now=now)
    await db_session.commit()
    assert second.created == 0
    assert len(await list_runs(db_session)) == 1


async def test_scheduled_tick_respects_quiet_hours(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="nightly",
            kind="scheduled",
            schedule="0 12 * * *",
            quiet_hours_skip=True,
            action_type="hud_card",
        ),
    )
    routine.next_run_at = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

    outcome = await tick(db_session, now=datetime(2026, 8, 9, 12, 30, tzinfo=UTC))
    await db_session.commit()

    assert outcome.created == 0
    assert outcome.skipped == 1
    assert outcome.runs[0].status == "skipped"


async def test_one_tap_disable_prevents_future_runs(
    db_session: AsyncSession,
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="brief",
            kind="scheduled",
            schedule="* * * * *",
            action_type="hud_card",
        ),
    )
    routine.enabled = False
    routine.next_run_at = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)

    outcome = await tick(db_session, now=datetime(2026, 8, 9, 10, 1, tzinfo=UTC))
    await db_session.commit()

    assert outcome.created == 0
    assert await list_runs(db_session) == []


# --------------------------------------------------------------------------- #
# Trigger automations
# --------------------------------------------------------------------------- #


async def test_event_trigger_matches_and_deduplicates(
    db_session: AsyncSession,
) -> None:
    await create_routine(
        db_session,
        RoutineCreate(
            name="deadline-brief",
            kind="trigger",
            trigger={
                "event_type": "deadline",
                "conditions": [{"path": "hours_until", "op": "lte", "value": 24}],
            },
            action_type="hud_card",
            action_payload={"kind": "deadline_brief"},
        ),
    )
    event = Event(
        source="calendar",
        event_type="deadline",
        content={"hours_until": 20},
        sha256="a" * 64,
    )
    db_session.add(event)
    await db_session.flush()

    first = await consider_event(db_session, event=event)
    second = await consider_event(db_session, event=event)
    await db_session.commit()

    assert len(first) == 1
    assert first[0].status == "executed"
    assert first[0].trigger_event_id == event.id
    assert second == []
    runs = await list_runs(db_session)
    assert len(runs) == 1


async def test_trigger_conditions_can_exclude_events(
    db_session: AsyncSession,
) -> None:
    await create_routine(
        db_session,
        RoutineCreate(
            name="low-readiness",
            kind="trigger",
            trigger={
                "event_type": "readiness",
                "conditions": [{"path": "score", "op": "lt", "value": 40}],
            },
            action_type="hud_card",
        ),
    )
    event = Event(
        source="health",
        event_type="readiness",
        content={"score": 80},
        sha256="b" * 64,
    )
    db_session.add(event)
    await db_session.flush()

    matched = await consider_event(db_session, event=event)
    await db_session.commit()
    assert matched == []


# --------------------------------------------------------------------------- #
# Approval, failure recovery, undo
# --------------------------------------------------------------------------- #


async def test_sensitive_action_requires_approval_then_executes(
    db_session: AsyncSession,
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="send-followup",
            kind="trigger",
            trigger={"event_type": "followup_due"},
            action_type="send_message",
            action_payload={"channel": "whatsapp"},
        ),
    )
    run = await manual_run(db_session, routine.id, actor="owner")
    await db_session.commit()

    assert run.status == "awaiting_approval"
    assert run.action_id is not None

    run = await approve_run(db_session, run.id, actor="owner")
    await db_session.commit()
    assert run.status == "approved"

    run = await execute_run(
        db_session,
        run.id,
        actor="owner",
        data=RoutineRunDecisionRequest(result={"sent": True}),
    )
    await db_session.commit()
    assert run.status == "executed"
    assert run.result == {"sent": True}


async def test_failed_run_can_be_retried(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="backup",
            kind="scheduled",
            schedule="0 3 * * *",
            action_type="hud_card",
        ),
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("integration down")

    monkeypatch.setattr("app.services.runtime.route_action", broken)
    run = await manual_run(db_session, routine.id, actor="owner")
    await db_session.commit()
    assert run.status == "failed"
    assert "integration down" in (run.error or "")

    monkeypatch.undo()
    run = await retry_run(db_session, run.id, actor="owner")
    await db_session.commit()
    assert run.status == "executed"
    assert run.attempts == 2


async def test_undoable_executed_run_can_roll_back(
    db_session: AsyncSession,
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="reorder",
            kind="trigger",
            trigger={"event_type": "stock_low"},
            action_type="hud_card",
            action_payload={"item": "filament"},
            undoable=True,
        ),
    )
    run = await manual_run(db_session, routine.id, actor="owner")
    await db_session.commit()
    assert run.status == "executed"

    run = await rollback_run(db_session, run.id, actor="owner")
    await db_session.commit()
    assert run.status == "rolled_back"
    assert run.undo_status == "done"
    assert run.undo_payload is not None
    assert run.undo_payload["_undo"] is True

    with pytest.raises(ValueError):
        await rollback_run(db_session, run.id, actor="owner")


async def test_non_undoable_routine_rejects_rollback(
    db_session: AsyncSession,
) -> None:
    routine = await create_routine(
        db_session,
        RoutineCreate(
            name="brief",
            kind="trigger",
            trigger={"event_type": "morning"},
            action_type="hud_card",
            undoable=False,
        ),
    )
    run = await manual_run(db_session, routine.id, actor="owner")
    await db_session.commit()
    with pytest.raises(ValueError):
        await rollback_run(db_session, run.id, actor="owner")


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


async def test_api_create_validate_and_disable(client) -> None:
    resp = await client.post(
        "/v1/routines",
        json={
            "name": "invalid",
            "kind": "scheduled",
            "schedule": "99 * * * *",
            "action_type": "hud_card",
        },
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/v1/routines",
        json={
            "name": "morning-brief",
            "kind": "scheduled",
            "schedule": "0 8 * * 1-5",
            "action_type": "hud_card",
            "action_payload": {"title": "brief"},
        },
    )
    assert resp.status_code == 201, resp.text
    routine = resp.json()
    assert routine["enabled"] is True
    assert routine["next_run_at"] is not None

    resp = await client.post(f"/v1/routines/{routine['id']}/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = await client.get("/v1/routines")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [routine["id"]]


async def test_api_live_trigger_fires_automation(client, db_session: AsyncSession) -> None:
    resp = await client.post(
        "/v1/routines",
        json={
            "name": "low-readiness",
            "kind": "trigger",
            "trigger": {
                "event_type": "readiness",
                "conditions": [{"path": "readiness", "op": "lt", "value": 40}],
            },
            "action_type": "hud_card",
            "action_payload": {"card": "recovery"},
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "health-belt",
            "kind": "health",
            "events": [{"event_type": "readiness", "payload": {"readiness": 35}}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()) == 1

    runs = await list_runs(db_session)
    assert len(runs) == 1
    assert runs[0].status == "executed"
    assert runs[0].kind == "trigger"
    assert runs[0].trigger_snapshot["channel_kind"] == "health"

    resp = await client.get("/v1/routines/runs")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "executed"
