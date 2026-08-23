"""G1 tool dispatch: routes life_* / mission_control tools to services.

Model-agnostic — called from ev/tools.dispatch so Realtime, DeepSeek, REST,
and future agents share one execution contract.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.life import service as life
from app.life.situation import changes_since_text, snapshot, summarize
from app.utils.text import utcnow


async def handle_life_tool(
    session: AsyncSession,
    name: str,
    args: dict,
    *,
    actor: str = "master",
) -> dict:
    if name == "life_project_create":
        return await life.create_project(
            session, actor=actor,
            title=str(args.get("title") or ""),
            priority=str(args.get("priority") or "NORMAL"),
            description=str(args.get("description") or ""),
        )
    if name == "life_project_update":
        proj = await life.find_project(session, actor=actor, query=str(args.get("project") or ""))
        pid = str(proj.id) if proj else str(args.get("project") or "")
        return await life.update_project(
            session, actor=actor, project_id=pid,
            status=args.get("status"), priority=args.get("priority"),
            title=args.get("title"), description=args.get("description"),
        )
    if name == "life_project_query":
        projects = await life.list_projects(
            session, actor=actor,
            active_only=not bool(args.get("include_completed")),
        )
        q = str(args.get("project") or "").lower()
        prio = args.get("priority")
        if q:
            projects = [p for p in projects if q in p["title"].lower()]
        if prio:
            projects = [p for p in projects if p["priority"] == prio]
        goals = await life.list_goals(session, actor=actor)
        for p in projects:
            p["active_goals"] = [
                g for g in goals
                if g.get("project_id") == p["id"] and g["state"] == "ACTIVE"
            ]
        return {"ok": True, "projects": projects}

    if name == "life_goal_create":
        return await life.create_goal(
            session, actor=actor,
            title=str(args.get("title") or ""),
            project_ref=args.get("project"),
            priority=str(args.get("priority") or "NORMAL"),
            success_criteria=str(args.get("success_criteria") or ""),
        )

    if name == "life_goal_query":
        state = args.get("state")
        goals = await life.list_goals(session, actor=actor, state=state)
        gid = args.get("goal_id")
        if gid:
            got = await life.get_goal(session, actor=actor, goal_id=gid)
            goals = [got["goal"]] if got.get("ok") else []
        # Project filter: resolve name/title to canonical project_id (never UUID substring match)
        project_ref = str(args.get("project") or "").strip()
        if project_ref:
            try:
                from uuid import UUID as _UUID
                _UUID(project_ref)
                is_uuid = True
            except ValueError:
                is_uuid = False
            if is_uuid:
                goals = [g for g in goals if g.get("project_id") == project_ref]
            else:
                proj = await life.find_project(session, actor=actor, query=project_ref)
                if proj is not None:
                    goals = [g for g in goals if g.get("project_id") == str(proj.id)]
                else:
                    # No matching project => no goals in that project (avoid silent wrong-project leakage)
                    goals = []
        # Title substring filter for natural queries like "what goals do I have in Personal Fitness"
        # also handles legacy title_query param some Realtime calls send
        title_q = str(args.get("title") or args.get("title_query") or args.get("query") or "").strip().lower()
        if title_q:
            goals = [g for g in goals if title_q in g.get("title", "").lower()]
        return {"ok": True, "goals": goals}

    goal_id = str(args.get("goal_id") or "")
    title_query = str(args.get("title_query") or "")
    if not goal_id and title_query:
        goals = await life.list_goals(session, actor=actor)
        lowered = title_query.lower()
        for g in goals:
            if lowered in g["title"].lower():
                goal_id = g["id"]
                break

    if name == "life_goal_update":
        return await life.update_goal(
            session, actor=actor, goal_id=goal_id,
            state=args.get("state"),
            progress_note=args.get("progress_note"),
            next_action=args.get("next_action"),
            blocked_reason=args.get("blocked_reason"),
            priority=args.get("priority"),
            title=args.get("title"),
        )

    if name == "life_goal_add_step":
        if not goal_id:
            return {"ok": False, "error": "goal_not_found"}
        return await life.add_step(
            session, actor=actor, goal_id=goal_id,
            title=str(args.get("title") or ""),
        )

    if name == "mission_control":
        query = str(args.get("query") or "status")
        snap = await snapshot(session, actor=actor)
        since_raw = args.get("since")
        explicit_since = None
        if since_raw:
            try:
                from dateutil import parser as _p
                explicit_since = _p.parse(str(since_raw))
            except Exception:  # noqa: BLE001
                explicit_since = None
        # CHECKPOINT SEMANTICS (G1.1):
        # - status is a READ. It never advances the changes-seen cursor.
        # - query="changes" without an explicit `since` IS the checkpointed
        #   "what changed since I last checked" check: on success it advances
        #   the cursor. An explicit `since` is a pure historical query.
        since = explicit_since or (await life.last_checkpoint(session, actor=actor)) or (
            utcnow() - timedelta(hours=24)
        )
        changes = await life.changes_since(session, actor=actor, since=since)
        checkpointed_at = None
        if query == "changes" and explicit_since is None:
            checkpointed_at = await life.checkpoint(session, actor=actor)
        out = {
            "ok": True,
            "query": query,
            "summary": summarize(snap),
            "changes_summary": changes_since_text(changes, since),
            "snapshot": {
                "top_focus": snap.get("top_focus"),
                "blocked_goals": snap.get("blocked_goals"),
                "open_commitments": len(snap.get("open_commitments") or []),
                "overdue_commitments": len(snap.get("overdue_commitments") or []),
            },
            "recent_changes": changes[:10],
        }
        if query == "changes":
            out["since"] = since.isoformat()
        if checkpointed_at is not None:
            out["checkpointed_at"] = checkpointed_at.isoformat()
        return out

    if name == "life_relationship_set":
        from app.life import people

        result = await people.set_relationship(
            session,
            actor=actor,
            person_name=str(args.get("person") or ""),
            relation=str(args.get("relation") or ""),
        )
        if result.get("ok"):
            result["relationships"] = await people.list_relationships(session)
        return result

    # commitment create/update/query by id/description
    if name == "life_commitment_create":
        from app.ev.resolve import parse_owner_when
        due_raw = str(args.get("due_at") or "").strip()
        desc = str(args.get("description") or "")
        due = parse_owner_when(due_raw) if due_raw else None
        # Fallback: many Realtime utterances pack time into description
        # ("Workout session at 7 PM tomorrow") and leave due_at empty.
        if due is None and desc:
            due = parse_owner_when(desc)
        return await life.create_commitment(
            session, actor=actor,
            description=desc,
            due_at=due,
            project_ref=args.get("project"),
        )
    if name == "life_commitment_query":
        q = str(args.get("query") or args.get("description") or "").strip().lower()
        project_filter = str(args.get("project") or "").strip().lower()
        status_filter = str(args.get("status") or "").upper() or None
        include_completed = bool(args.get("include_completed"))
        commitments = await life.list_commitments(
            session, actor=actor, open_only=not include_completed and status_filter is None
        )
        if status_filter and status_filter in ("OPEN", "FULFILLED", "CANCELLED", "MISSED"):
            commitments = [c for c in commitments if c["status"] == status_filter]
        if project_filter:
            proj = await life.find_project(session, actor=actor, query=project_filter)
            if proj is not None:
                commitments = [c for c in commitments if c.get("project_id") == str(proj.id)]
            else:
                commitments = [c for c in commitments if project_filter in (c.get("project_id") or "").lower()]
        if q:
            commitments = [c for c in commitments if q in c["description"].lower()]
        return {"ok": True, "commitments": commitments, "count": len(commitments)}
    if name == "life_commitment_update":
        cid = str(args.get("commitment_id") or "")
        commitments = await life.list_commitments(session, actor=actor, open_only=True)
        if not cid:
            desc = str(args.get("description") or "").lower()
            for c in commitments:
                if desc and desc in c["description"].lower():
                    cid = c["id"]
                    break
        if not cid:
            return {"ok": False, "error": "commitment_not_found"}
        return await life.update_commitment(
            session, actor=actor, commitment_id=cid, status=str(args.get("status") or "FULFILLED")
        )

    return {"ok": False, "error": f"unknown_life_tool:{name}"}
