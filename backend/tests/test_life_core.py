"""Evie OS G1 — Core State invariants.

Architectural laws under test:
- ONE STATE AUTHORITY: Project/Goal/Commitment rows are canonical.
- ONE DURABLE HISTORY: every transition emits a canonical `events` row.
- Persistence across sessions (restart-survival proxy).
- Mission Control reads canonical state; changes_since reconstructs history.
- Exactly-once UI contract: no-op when nothing playing (service-level guard).
"""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.life import service as life
from app.life.situation import snapshot, summarize
from app.models import Event
from app.utils.text import utcnow

ACTOR = "master"


async def _events(db_session: AsyncSession, etype: str) -> list[Event]:
    rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == etype).order_by(Event.occurred_at)
        )
    ).scalars().all()
    return list(rows)


@pytest.fixture
async def seeded_session(db_session: AsyncSession) -> AsyncSession:
    return db_session


# ---- Projects ----


async def test_project_create_emits_event_and_persists(db_session: AsyncSession):
    result = await life.create_project(
        db_session, actor=ACTOR, title="Evie", priority="HIGH"
    )
    assert result["ok"] is True
    assert result["project"]["priority"] == "HIGH"
    await db_session.commit()

    events = await _events(db_session, "project.created")
    assert len(events) == 1
    assert events[0].source == "life"
    assert events[0].content["title"] == "Evie"

    # persistence across db_session (fresh query = restart proxy)
    fresh = await life.list_projects(db_session, actor=ACTOR)
    assert [p["title"] for p in fresh] == ["Evie"]


async def test_project_priority_change_emits_dedicated_event(db_session: AsyncSession):
    created = await life.create_project(db_session, actor=ACTOR, title="Fitness")
    pid = created["project"]["id"]
    await db_session.commit()

    updated = await life.update_project(
        db_session, actor=ACTOR, project_id=pid, priority="CRITICAL"
    )
    assert updated["changes"]["priority"] == "CRITICAL"
    await db_session.commit()

    events = await _events(db_session, "project.priority_changed")
    assert any(e.content["project_id"] == pid for e in events)


async def test_project_complete_and_pause_events(db_session: AsyncSession):
    created = await life.create_project(db_session, actor=ACTOR, title="Temp")
    pid = created["project"]["id"]
    paused = await life.update_project(db_session, actor=ACTOR, project_id=pid, status="PAUSED")
    assert paused["project"]["status"] == "PAUSED"
    done = await life.update_project(db_session, actor=ACTOR, project_id=pid, status="COMPLETED")
    assert done["project"]["status"] == "COMPLETED"
    await db_session.commit()
    for etype in ("project.paused", "project.completed"):
        rows = await _events(db_session, etype)
        assert any(e.content["project_id"] == pid for e in rows), etype


# ---- Goals ----


async def test_goal_create_with_project_ref_autocreate(db_session: AsyncSession):
    # project does not exist yet: reference-by-title must auto-create it
    result = await life.create_goal(
        db_session, actor=ACTOR, title="Build Active Goals",
        project_ref="Evie", success_criteria="Tools live in manifest",
    )
    assert result["ok"] is True
    goal = result["goal"]
    assert goal["state"] == "ACTIVE"
    await db_session.commit()

    projects = await life.list_projects(db_session, actor=ACTOR)
    assert any(p["title"] == "Evie" for p in projects)

    events = await _events(db_session, "goal.created")
    assert any(
        e.content["title"] == "Build Active Goals" and e.content.get("project_id")
        for e in events
    )


async def test_goal_block_unblock_events(db_session: AsyncSession):
    g = await life.create_goal(db_session, actor=ACTOR, title="Ship it")
    gid = g["goal"]["id"]
    blocked = await life.update_goal(
        db_session, actor=ACTOR, goal_id=gid, state="BLOCKED", blocked_reason="waiting on API"
    )
    assert blocked["goal"]["state"] == "BLOCKED"
    unblocked = await life.update_goal(db_session, actor=ACTOR, goal_id=gid, state="ACTIVE")
    assert unblocked["goal"]["state"] == "ACTIVE"
    await db_session.commit()
    for etype in ("goal.blocked", "goal.unblocked"):
        rows = await _events(db_session, etype)
        assert any(r.content.get("goal_id") == gid for r in rows)


async def test_goal_completed_sets_timestamp(db_session: AsyncSession):
    g = await life.create_goal(db_session, actor=ACTOR, title="Finish")
    gid = g["goal"]["id"]
    done = await life.update_goal(db_session, actor=ACTOR, goal_id=gid, state="COMPLETED")
    assert done["goal"]["completed_at"] is not None


# ---- Goal steps ----


async def test_add_step_and_complete(db_session: AsyncSession):
    g = await life.create_goal(db_session, actor=ACTOR, title="G")
    gid = g["goal"]["id"]
    s1 = await life.add_step(db_session, actor=ACTOR, goal_id=gid, title="Step one")
    s2 = await life.add_step(db_session, actor=ACTOR, goal_id=gid, title="Step two")
    assert s1["ok"] and s2["ok"]
    got = await life.get_goal(db_session, actor=ACTOR, goal_id=gid)
    titles = [s["title"] for s in got["goal"]["steps"]]
    assert titles == ["Step one", "Step two"]
    done = await life.complete_step(db_session, actor=ACTOR, step_id=s1["step"]["id"])
    assert done["ok"] is True


# ---- Commitments ----


async def test_commitment_lifecycle(db_session: AsyncSession):
    c = await life.create_commitment(
        db_session, actor=ACTOR,
        description="Complete the architecture review tomorrow",
    )
    assert c["commitment"]["status"] == "OPEN"
    cid = c["commitment"]["id"]
    fulfilled = await life.update_commitment(
        db_session, actor=ACTOR, commitment_id=cid, status="FULFILLED"
    )
    assert fulfilled["commitment"]["status"] == "FULFILLED"
    await db_session.commit()
    events = await _events(db_session, "commitment.fulfilled")
    assert any(e.content["commitment_id"] == cid for e in events)
    open_rows = await life.list_commitments(db_session, actor=ACTOR, open_only=True)
    assert all(c["id"] != cid for c in open_rows)


# ---- Situation / Mission Control ----


async def test_situation_snapshot_reads_canonical_state(db_session: AsyncSession):
    await life.create_project(db_session, actor=ACTOR, title="Evie", priority="HIGH")
    await life.create_goal(
        db_session, actor=ACTOR, title="Build Active Goals", project_ref="Evie"
    )
    await life.create_commitment(
        db_session, actor=ACTOR, description="Review architecture tomorrow"
    )
    await db_session.commit()

    snap = await snapshot(db_session, actor=ACTOR)
    assert snap["top_focus"]["title"] == "Evie"
    assert len(snap["active_goals"]) == 1
    assert goals_active_count(snap) == 1
    assert len(snap["open_commitments"]) == 1
    summary = summarize(snap)
    assert "Top priority" in summary or "Evie" in summary
    assert "Active goals" in summary or "commitment" in summary.lower()


def goals_active_count(snap: dict) -> int:
    return len(snap.get("active_goals") or [])


async def test_changes_since_reconstructs_history(db_session: AsyncSession):
    await life.create_project(db_session, actor=ACTOR, title="P1")
    await life.create_goal(db_session, actor=ACTOR, title="G1")
    await db_session.commit()
    changes = await life.changes_since(
        db_session, actor=ACTOR, since=utcnow() - timedelta(minutes=5)
    )
    kinds = sorted(c["type"] for c in changes)
    assert "project.created" in kinds and "goal.created" in kinds


# ---- Memory relationship law ----


async def test_memory_is_not_goal_authority(db_session: AsyncSession):
    """A memory(type=goal) must NOT satisfy a goal query — canonical truth
    lives only in the Goal table."""
    from app.models import Memory

    db_session.add(
        Memory(
            memory_type="goal",
            text="Owner wants to be fit.",
            importance=0.9,
            fingerprint="test-goal-memory",
        )
    )
    await db_session.flush()
    goals = await life.list_goals(db_session, actor=ACTOR)
    assert all(g["title"] != "Owner wants to be fit." for g in goals)


# ---- Person / relationship semantics ----


async def test_relationship_set_lifecycle(db_session: AsyncSession):
    from app.life import people

    first = await people.set_relationship(
        db_session, actor=ACTOR, person_name="Maya", relation="friend"
    )
    assert first["ok"] is True and first["relationship"]["relation"] == "friend"
    await db_session.commit()

    created = await _events(db_session, "relationship.created")
    assert any(e.content["person"] == "Maya" for e in created)

    # Idempotent re-assert is a no-op, never a duplicate edge.
    again = await people.set_relationship(
        db_session, actor=ACTOR, person_name="Maya", relation="friend"
    )
    assert again.get("unchanged") is True

    # Relation change closes the old edge and emits relationship.updated.
    changed = await people.set_relationship(
        db_session, actor=ACTOR, person_name="Maya", relation="colleague"
    )
    assert changed["ok"] is True
    assert changed["previous_relation"] == "friend"
    await db_session.commit()
    updated = await _events(db_session, "relationship.updated")
    assert any(
        e.content["person"] == "Maya"
        and e.content["previous_relation"] == "friend"
        for e in updated
    )

    active = await people.list_relationships(db_session)
    maya = [r for r in active if r["person"] == "Maya"]
    assert len(maya) == 1 and maya[0]["relation"] == "colleague"


async def test_relationship_rejects_unknown_vocabulary(db_session: AsyncSession):
    from app.life import people

    out = await people.set_relationship(
        db_session, actor=ACTOR, person_name="Zed", relation="nemesis"
    )
    assert out["ok"] is False and out["error"] == "unknown_relation"


# ---- Mission Control checkpoint ('since I last checked') ----


async def test_checkpoint_cursor_advances_changes_window(db_session: AsyncSession):
    await life.create_project(db_session, actor=ACTOR, title="Evie", priority="HIGH")
    await life.create_goal(db_session, actor=ACTOR, title="Build Active Goals")
    await db_session.commit()

    assert await life.last_checkpoint(db_session, actor=ACTOR) is None

    first = await life.changes_since(
        db_session, actor=ACTOR, since=utcnow() - timedelta(minutes=5)
    )
    kinds = {c["type"] for c in first}
    assert {"project.created", "goal.created"} <= kinds

    at = await life.checkpoint(db_session, actor=ACTOR)
    await db_session.commit()

    stored = await life.last_checkpoint(db_session, actor=ACTOR)
    assert stored is not None and stored >= at - timedelta(seconds=1)

    after = await life.changes_since(
        db_session, actor=ACTOR, since=stored - timedelta(milliseconds=1)
    )
    semantic = [c for c in after if c["type"] != "mission_control.checked"]
    assert semantic == []


# ---- Dispatch contract (model-agnostic execution path) ----


async def test_dispatch_goal_add_step_by_title_query(db_session: AsyncSession):
    from app.life.dispatch import handle_life_tool

    g = await life.create_goal(db_session, actor=ACTOR, title="Ship Active Goals")
    gid = g["goal"]["id"]
    out = await handle_life_tool(
        db_session,
        "life_goal_add_step",
        {"title_query": "Ship Active Goals", "title": "Wire dispatch"},
        actor=ACTOR,
    )
    assert out["ok"] is True
    await db_session.commit()

    got = await life.get_goal(db_session, actor=ACTOR, goal_id=gid)
    assert [s["title"] for s in got["goal"]["steps"]] == ["Wire dispatch"]
    created_steps = await _events(db_session, "goal_step.created")
    assert any(e.content["goal_id"] == gid for e in created_steps)


async def test_dispatch_mission_control_changes_with_checkpoint(db_session: AsyncSession):
    from app.life.dispatch import handle_life_tool

    await life.create_project(db_session, actor=ACTOR, title="Fitness")
    await db_session.commit()

    # STATUS IS A READ: must never advance the changes-seen cursor.
    before = await life.last_checkpoint(db_session, actor=ACTOR)
    status_out = await handle_life_tool(
        db_session, "mission_control", {"query": "status"}, actor=ACTOR
    )
    assert status_out["ok"] is True
    after_status = await life.last_checkpoint(db_session, actor=ACTOR)
    assert before is None and after_status is None

    # The checkpointed What-Changed check returns semantic changes and
    # advances the cursor on success.
    first = await handle_life_tool(
        db_session, "mission_control", {"query": "changes"}, actor=ACTOR
    )
    assert first["ok"] is True
    assert any(c["type"] == "project.created" for c in first["recent_changes"])
    assert first.get("checkpointed_at")
    await db_session.commit()

    # Next 'what changed' sees only NEW semantic changes — and never the
    # bookkeeping event itself.
    second = await handle_life_tool(
        db_session, "mission_control", {"query": "changes"}, actor=ACTOR
    )
    fresh = [
        c for c in second["recent_changes"] if c["type"] != "mission_control.checked"
    ]
    assert fresh == []
    await db_session.commit()

    # A change after the checkpoint shows up on the next check.
    await life.create_goal(db_session, actor=ACTOR, title="Post-checkpoint goal")
    await db_session.commit()
    third = await handle_life_tool(
        db_session, "mission_control", {"query": "changes"}, actor=ACTOR
    )
    assert [c["type"] for c in third["recent_changes"]] == ["goal.created"]

    # Explicit `since` is a pure historical query: no cursor advance.
    cursor_before_explicit = await life.last_checkpoint(db_session, actor=ACTOR)
    historical = await handle_life_tool(
        db_session,
        "mission_control",
        {"query": "changes", "since": "2020-01-01T00:00:00+00:00"},
        actor=ACTOR,
    )
    assert any(c["type"] == "project.created" for c in historical["recent_changes"])
    assert historical.get("checkpointed_at") is None
    assert await life.last_checkpoint(db_session, actor=ACTOR) == cursor_before_explicit


async def test_what_changed_never_reports_bookkeeping_events(db_session: AsyncSession):
    """Bookkeeping events are canonical history but NEVER owner-facing changes."""
    from app.models import Event

    await life.checkpoint(db_session, actor=ACTOR)
    db_session.add(
        Event(
            source="life",
            event_type="mission_control.checked",
            content={"actor": ACTOR},
            sha256="bookkeeping-test",
        )
    )
    await db_session.commit()

    rows = await life.changes_since(
        db_session, actor=ACTOR, since=utcnow() - timedelta(minutes=5)
    )
    assert all(r["type"] != "mission_control.checked" for r in rows)

    snap = await snapshot(db_session, actor=ACTOR)
    assert all(c["type"] != "mission_control.checked" for c in snap["recent_changes"])


async def test_dispatch_relationship_set_via_tool(db_session: AsyncSession):
    from app.life.dispatch import handle_life_tool

    out = await handle_life_tool(
        db_session,
        "life_relationship_set",
        {"person": "Maya", "relation": "family"},
        actor=ACTOR,
    )
    assert out["ok"] is True
    assert out["spoken"]
    assert any(r["person"] == "Maya" for r in out["relationships"])
    await db_session.commit()


# ---- Capability manifest law ----


def test_capability_manifest_exposes_all_life_tools():
    """Hard architecture law: every life tool must be (a) a registered spec in
    TOOL_SPECS and (b) stamped by the derived capability overlay — availability
    is projected, never hand-written into prompts."""
    import app.life.capability as life_capability
    from app.ev.capability_registry import apply_capability_overlays
    from app.ev.policy import resolve_capability
    from app.ev.tool_select import LIVE_VOICE_TOOLS
    from app.life.capability import LIFE_TOOLS

    entries = []
    for name in sorted(LIFE_TOOLS):
        spec = resolve_capability(name)
        assert spec is not None, f"{name} missing from TOOL_SPECS"
        entries.append({"name": name, "availability": "available"})
        assert name in LIVE_VOICE_TOOLS, f"{name} not voice-eligible"

    stamped = apply_capability_overlays(
        entries, {"life_state": {"ready": True, "tools": sorted(LIFE_TOOLS)}}
    )
    by_name = {e["name"]: e for e in stamped}
    for name in LIFE_TOOLS:
        assert by_name[name]["capability"] == "life_state"
        assert by_name[name]["readiness"] == "ready"

    # The registered family must cover exactly the shipped G1 surface.
    assert frozenset(LIFE_TOOLS) == life_capability.LIFE_TOOLS


# ---- G1.1 idempotency sanity ----


async def test_idempotent_transitions_emit_no_duplicate_events(db_session: AsyncSession):
    from sqlalchemy import func

    g = await life.create_goal(db_session, actor=ACTOR, title="Idempotent Goal")
    gid = g["goal"]["id"]
    s = await life.add_step(db_session, actor=ACTOR, goal_id=gid, title="Only step")
    sid = s["step"]["id"]
    c = await life.create_commitment(db_session, actor=ACTOR, description="call back")
    cid = c["commitment"]["id"]
    p = await life.create_project(db_session, actor=ACTOR, title="Steady")
    pid = p["project"]["id"]

    # Complete goal twice.
    assert (await life.update_goal(db_session, actor=ACTOR, goal_id=gid, state="COMPLETED"))["ok"]
    again_goal = await life.update_goal(db_session, actor=ACTOR, goal_id=gid, state="COMPLETED")
    assert again_goal.get("unchanged") is True
    # Complete step twice.
    assert (await life.complete_step(db_session, actor=ACTOR, step_id=sid))["ok"]
    again_step = await life.complete_step(db_session, actor=ACTOR, step_id=sid)
    assert again_step.get("unchanged") is True
    # Fulfill commitment twice.
    assert (
        await life.update_commitment(db_session, actor=ACTOR, commitment_id=cid, status="FULFILLED")
    )["ok"]
    again_c = await life.update_commitment(
        db_session, actor=ACTOR, commitment_id=cid, status="FULFILLED"
    )
    assert again_c.get("unchanged") is True
    # Pause project twice.
    assert (await life.update_project(db_session, actor=ACTOR, project_id=pid, status="PAUSED"))["ok"]
    again_p = await life.update_project(db_session, actor=ACTOR, project_id=pid, status="PAUSED")
    assert again_p.get("unchanged") is True
    await db_session.commit()

    for etype in (
        "goal.completed",
        "goal_step.completed",
        "commitment.fulfilled",
        "project.paused",
    ):
        n = (
            await db_session.execute(
                select(func.count()).select_from(Event).where(Event.event_type == etype)
            )
        ).scalar_one()
        assert n == 1, f"{etype} emitted {n} times"


# ---- G1.1 timezone contract ----


def test_parse_owner_when_uses_owner_timezone(monkeypatch):
    """Wall-clock words interpret in settings.timezone → canonical UTC."""
    from datetime import datetime

    from app.config import settings
    from app.ev.resolve import parse_owner_when

    now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "timezone", "Asia/Kolkata")

    # 7am owner-local tomorrow (IST) = 2026-08-17T18:30:00Z... no:
    # Aug 18 07:00 IST == Aug 18 01:30 UTC.
    out = parse_owner_when("tomorrow 7am", now=now)
    expected_utc = datetime(2026, 8, 18, 1, 30, tzinfo=UTC)
    assert out is not None
    assert out.astimezone(UTC) == expected_utc


def test_parse_owner_when_date_only_gets_documented_default(monkeypatch):
    """Date-only 'tomorrow' = end of that owner-local day. Never None, never server-tz."""
    from datetime import datetime

    from app.config import settings
    from app.ev.resolve import parse_owner_when

    now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "timezone", "Asia/Kolkata")
    out = parse_owner_when("Complete workout tomorrow", now=now)
    assert out is not None
    # End of Aug 18 owner-local = 2026-08-18 23:59:59 IST = 18:29:59 UTC.
    assert (out.astimezone(UTC).hour, out.astimezone(UTC).minute) == (18, 29)
