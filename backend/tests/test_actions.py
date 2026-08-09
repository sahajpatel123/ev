"""Tests for the formal action dispatcher: specs, validation, logging, rollback."""

from __future__ import annotations

from sqlalchemy import select

from app.models import AccessLog, ApprovedAction
from app.routines.service import manual_run, rollback_run
from app.schemas import RoutineCreate


async def test_action_specs_declare_execution_boundaries(client) -> None:
    resp = await client.get("/v1/runtime/action-specs")
    assert resp.status_code == 200, resp.text
    specs = resp.json()
    names = {s["name"] for s in specs}
    assert {
        "search_memory",
        "hud_card",
        "notification",
        "fleet_task",
        "web_search",
        "send_message",
        "execute_command",
    } <= names
    for spec in specs:
        assert spec["payload"]["type"] == "object"
        assert isinstance(spec["output"], dict)
        assert spec["permission"]
        assert spec["read_only"] in (True, False)
    hud = next(s for s in specs if s["name"] == "hud_card")
    assert hud["requires_approval"] is False
    assert hud["undoable"] is True
    notification = next(s for s in specs if s["name"] == "notification")
    assert notification["requires_approval"] is True
    assert notification["undoable"] is False


async def test_route_action_rejects_unknown_types_and_bad_payloads(client) -> None:
    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "arbitrary_side_effect", "auto_approve": True},
    )
    assert resp.status_code == 422, resp.text
    assert "Unknown action type" in resp.json()["detail"]

    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "send_message", "payload": {}},
    )
    assert resp.status_code == 422, resp.text
    assert "Invalid action payload" in resp.json()["detail"]
    assert "missing required argument 'channel'" in resp.json()["detail"]

    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "notification", "payload": {}},
    )
    assert resp.status_code == 422, resp.text
    assert "missing required argument 'text'" in resp.json()["detail"]


async def test_action_lifecycle_is_validated_logged_and_rolls_back(client) -> None:
    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "hud_card", "title": "brief", "payload": {"item": "filament"}, "auto_approve": True},
    )
    assert resp.status_code == 201, resp.text
    action = resp.json()
    assert action["status"] == "approved"
    action_id = action["id"]

    resp = await client.post(
        f"/v1/runtime/actions/{action_id}/execute",
        json={"result": {"card_rendered": True}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executed"

    resp = await client.post(
        f"/v1/runtime/actions/{action_id}/rollback",
        json={"reason": "dismissed"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rolled_back"
    assert body["rolled_back_at"] is not None
    assert body["rolled_back_reason"] == "dismissed"

    resp = await client.get("/v1/runtime/actions?status=rolled_back")
    assert resp.status_code == 200
    assert any(a["id"] == action_id for a in resp.json())

    resp = await client.post(
        f"/v1/runtime/actions/{action_id}/rollback",
        json={"reason": "again"},
    )
    assert resp.status_code == 409
    assert "already" in resp.json()["detail"] or "rolled back" in resp.json()["detail"]


async def test_non_undoable_action_rejects_rollback(client) -> None:
    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "notification", "payload": {"text": "hello"}},
    )
    assert resp.status_code == 201, resp.text
    action_id = resp.json()["id"]
    await client.post(f"/v1/runtime/actions/{action_id}/approve")
    resp = await client.post(
        f"/v1/runtime/actions/{action_id}/execute",
        json={"result": {"delivered": True}},
    )
    assert resp.status_code == 200
    resp = await client.post(f"/v1/runtime/actions/{action_id}/rollback")
    assert resp.status_code == 409, resp.text
    assert "not undoable" in resp.json()["detail"]


async def test_action_lifecycle_is_written_to_access_log(client, db_session) -> None:
    resp = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "hud_card", "auto_approve": True},
    )
    action_id = resp.json()["id"]
    await client.post(f"/v1/runtime/actions/{action_id}/execute")
    await client.post(f"/v1/runtime/actions/{action_id}/rollback")

    rows = (
        await db_session.execute(
            select(AccessLog)
            .where(AccessLog.resource_type == "action")
            .order_by(AccessLog.occurred_at.asc())
        )
    ).scalars().all()
    actions = [row.action for row in rows]
    assert "action.route" in actions
    assert "action.execute" in actions
    assert "action.rollback" in actions
    rollback_log = next(row for row in rows if row.action == "action.rollback")
    assert rollback_log.resource_ids == [action_id]
    assert rollback_log.details["action_type"] == "hud_card"


async def test_routine_rollback_marks_linked_action_rolled_back(
    client, db_session
) -> None:
    routine = await create_undoable_routine(db_session)
    run = await manual_run(db_session, routine.id, actor="owner")
    await db_session.commit()
    assert run.status == "executed"

    run = await rollback_run(db_session, run.id, actor="owner")
    await db_session.commit()
    assert run.status == "rolled_back"

    action = await db_session.get(ApprovedAction, run.action_id)
    assert action is not None
    assert action.status == "rolled_back"
    assert action.rolled_back_at is not None


async def create_undoable_routine(db_session):
    from app.routines.service import create_routine

    return await create_routine(
        db_session,
        RoutineCreate(
            name="undoable-brief",
            kind="trigger",
            trigger={"event_type": "morning"},
            action_type="hud_card",
            action_payload={"title": "brief"},
            undoable=True,
        ),
    )
