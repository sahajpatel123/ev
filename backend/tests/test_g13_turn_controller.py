"""G1.3 TurnController mandatory tests — owner failures, write/read, fresh session, Luna eval."""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.turn_controller import TurnController

ACTOR = "master"


@pytest.mark.asyncio
async def test_g13_exact_owner_failures(db_session: AsyncSession):
    # Uses live owner data (Personal Fitness) if present, otherwise creates it
    from sqlalchemy import text
    # Ensure clean checked
    await db_session.execute(text("DELETE FROM events WHERE event_type='mission_control.checked'"))
    await db_session.commit()

    tc = TurnController(db_session, actor=ACTOR)
    # Ensure Personal Fitness exists (owner data)
    from app.life.service import list_projects
    projects = await list_projects(db_session, actor=ACTOR)
    if not any(p["title"] == "Personal Fitness" for p in projects):
        from app.life.service import create_project
        await create_project(db_session, actor=ACTOR, title="Personal Fitness")
        await db_session.commit()
        from app.life.service import create_goal
        await create_goal(db_session, actor=ACTOR, title="Improve cardiovascular fitness", project_ref="Personal Fitness")
        await db_session.commit()

    cases = [
        ("What priority is Personal Fitness?", "PROJECT_GET"),
        ("What goals do I have in Personal Fitness?", "GOAL_LIST"),
        ("When is my workout commitment due?", "COMMITMENT_LIST"),
        ("Evie, status.", "STATUS"),
        ("What changed?", "WHAT_CHANGED"),
    ]
    for turn, exp_op in cases:
        res = await tc.handle_turn(turn)
        await db_session.rollback()
        assert res.ok, f"{turn} failed: {res.error}"
        assert res.operation == exp_op, f"{turn} got {res.operation} expected {exp_op}"
        assert res.owner_message, f"{turn} empty owner_message"
    # Second what changed should be empty
    res = await tc.handle_turn("What changed?")
    await db_session.commit()
    res2 = await tc.handle_turn("What changed?")
    await db_session.rollback()
    assert isinstance(res2.canonical_data, dict)
    assert len(res2.canonical_data.get("changes", [])) == 0
    # Clean
    await db_session.execute(text("DELETE FROM events WHERE event_type='mission_control.checked'"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_g13_write_read_via_turn_controller(db_session: AsyncSession):
    from sqlalchemy import select, text

    from app.models import Goal, Project
    # Clean — use Python filtering for cross-DB compatibility (SQLite vs Postgres)
    for proj in (await db_session.execute(select(Project).where(Project.title == "Luna Control Test"))).scalars().all():
        await db_session.execute(text("DELETE FROM goal_steps WHERE goal_id IN (SELECT id FROM goals WHERE project_id = :pid)"), {"pid": str(proj.id)})
        await db_session.execute(text("DELETE FROM goals WHERE project_id = :pid"), {"pid": str(proj.id)})
        await db_session.execute(text("DELETE FROM commitments WHERE project_id = :pid"), {"pid": str(proj.id)})
        await db_session.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": str(proj.id)})
    for g in (await db_session.execute(select(Goal).where(Goal.title.ilike("prove luna routing")))).scalars().all():
        await db_session.execute(text("DELETE FROM goal_steps WHERE goal_id = :gid"), {"gid": str(g.id)})
        await db_session.execute(text("DELETE FROM goals WHERE id = :gid"), {"gid": str(g.id)})
    await db_session.execute(text("DELETE FROM commitments WHERE description LIKE '%Luna%' OR description LIKE '%luna%'"))
    await db_session.execute(text("DELETE FROM events WHERE event_type='mission_control.checked'"))
    # JSON cleanup best-effort (may differ per DB)
    with contextlib.suppress(Exception):
        await db_session.execute(text("DELETE FROM events WHERE lower(content->>'title') LIKE '%luna%' OR lower(content->>'description') LIKE '%luna%'"))
    await db_session.commit()

    tc = TurnController(db_session, actor=ACTOR)
    steps = [
        ("Create a project called Luna Control Test.", "PROJECT_CREATE"),
        ("What priority is Luna Control Test?", "PROJECT_GET"),
        ("Add a goal to Luna Control Test: prove Luna routing.", "GOAL_CREATE"),
        ("What goals do I have in Luna Control Test?", "GOAL_LIST"),
        ("Create a commitment for tomorrow at 7 PM to test Luna.", "COMMITMENT_CREATE"),
        ("When is my Luna commitment due?", "COMMITMENT_LIST"),
    ]
    for turn, exp_op in steps:
        res = await tc.handle_turn(turn)
        await db_session.commit()
        assert res.ok, f"{turn} failed: {res.error}"
        assert res.operation == exp_op

    # Fresh session — new controller, no history
    tc2 = TurnController(db_session, actor=ACTOR)
    for turn in ["What priority is Luna Control Test?", "What goals do I have in Luna Control Test?", "When is my Luna commitment due?"]:
        res = await tc2.handle_turn(turn)
        await db_session.rollback()
        assert res.ok and res.owner_message

    # Clean — Python-filtered for SQLite compatibility
    for proj in (await db_session.execute(select(Project).where(Project.title == "Luna Control Test"))).scalars().all():
        await db_session.execute(text("DELETE FROM goal_steps WHERE goal_id IN (SELECT id FROM goals WHERE project_id = :pid)"), {"pid": str(proj.id)})
        await db_session.execute(text("DELETE FROM goals WHERE project_id = :pid"), {"pid": str(proj.id)})
        await db_session.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": str(proj.id)})
    for g in (await db_session.execute(select(Goal).where(Goal.title.ilike("prove luna routing")))).scalars().all():
        await db_session.execute(text("DELETE FROM goals WHERE id = :gid"), {"gid": str(g.id)})
    await db_session.execute(text("DELETE FROM commitments WHERE description LIKE '%Luna%' OR description LIKE '%luna%'"))
    with contextlib.suppress(Exception):
        await db_session.execute(text("DELETE FROM events WHERE lower(content->>'title') LIKE '%luna%' OR lower(content->>'description') LIKE '%luna%'"))
    await db_session.execute(text("DELETE FROM events WHERE event_type='mission_control.checked'"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_g13_luna_routing_eval(db_session: AsyncSession):
    tc = TurnController(db_session, actor=ACTOR)
    cases = [
        ("what projects do I have", "STATE_QUERY", "PROJECT_LIST"),
        ("what's the priority of fitness", "STATE_QUERY", "PROJECT_GET"),
        ("what are we trying to accomplish in fitness", "STATE_QUERY", "GOAL_LIST"),
        ("remember that I want to run a marathon", "STATE_MUTATION", "GOAL_CREATE"),
        ("add this as a goal", "STATE_MUTATION", "GOAL_CREATE"),
        ("what changed", "MISSION_CONTROL", "WHAT_CHANGED"),
        ("give me status", "MISSION_CONTROL", "STATUS"),
        ("research this properly", "DELEGATED_JOB", None),
        ("fix this project", "CLARIFICATION", None),
        ("how are you", "CONVERSATION", None),
        ("tell me a joke", "CONVERSATION", None),
        ("make that high priority", "CLARIFICATION", None),
    ]
    for turn, exp_route, exp_op in cases:
        res = await tc.handle_turn(turn)
        await db_session.rollback()
        assert res.route == exp_route, f"{turn} route {res.route} != {exp_route}"
        if exp_op:
            assert res.operation == exp_op, f"{turn} op {res.operation} != {exp_op}"


@pytest.mark.asyncio
async def test_g13_false_success_guard(db_session: AsyncSession):
    tc = TurnController(db_session, actor=ACTOR)
    # Non-existent project query should be ok=False or needs_clarification, never ok with false data
    res = await tc.handle_turn("What priority is NonexistentProjectXYZ?")
    await db_session.rollback()
    # Should be not_found, not ok with fake data
    assert not res.ok or res.error == "not_found" or "couldn't find" in (res.owner_message or "").lower()
    # Verify that a successful create has ok True and spoken
    res2 = await tc.handle_turn("Create a project called GuardTest123.")
    await db_session.commit()
    assert res2.ok and res2.owner_message
    # Clean
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM projects WHERE title='GuardTest123'"))
    await db_session.execute(text("DELETE FROM events WHERE content->>'title'='GuardTest123'"))
    await db_session.commit()
